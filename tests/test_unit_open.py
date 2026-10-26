"""Each unit exposes at most one stored source link; unlinked units have no Open."""
import tempfile
import unittest
from pathlib import Path

from infohub.organize import units as U
from infohub.store import Store


class UnitOpen(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "hub.db")
        self.parent, self.left, self.right, self.leaf, self.other = [
            U.upsert(self.store, None, kind="topic", title=name)
            for name in ("Parent", "Left", "Right", "Leaf", "Other")]
        for parent, child in [(self.parent, self.left), (self.parent, self.right),
                              (self.left, self.leaf), (self.right, self.leaf)]:
            U.link(self.store, parent, child)
        self.link = "https://applink.feishu.cn/client/chat/open?openChatId=oc_test&position=12"
        self.mids = self.store.add_messages([
            {"source": "lark", "account": "work", "item_key": "direct", "channel_name": "Research",
             "sent_at": 123, "text": "Source message", "link": self.link},
            {"source": "lark", "account": "work", "item_key": "descendant", "channel_name": "Planning",
             "sent_at": 124, "text": "Child message", "link": self.link + "0"},
            {"source": "chat", "item_key": "unlinked", "sent_at": 125, "text": "No link"}])
        U.attach(self.store, self.parent, self.mids[:1])
        U.attach(self.store, self.leaf, self.mids[1:2])
        U.attach(self.store, self.other, self.mids[2:])
        self.store.db.commit()

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_direct_message_is_preferred_and_detail_keeps_original_messages(self):
        unit = U.get(self.store, self.parent)
        self.assertEqual(unit["open_link"], self.link)
        self.assertEqual(unit["open_channel"], "Research")
        self.assertEqual([m["id"] for m in unit["messages"]], self.mids[:1])

    def test_fallback_to_descendants_without_sibling_leaks(self):
        targets = U.source_targets(self.store, [self.left, self.right, self.other])
        self.assertEqual(targets[self.left]["open_link"], self.link + "0")
        self.assertEqual(targets[self.left], targets[self.right])
        self.assertNotIn(self.other, targets)

    def test_unlinked_and_unsafe_links_do_not_get_an_open_target(self):
        self.assertIsNone(U.get(self.store, self.other)["open_link"])
        self.store.q("UPDATE messages SET link='javascript:alert(1)' WHERE id=?", (self.mids[2],))
        self.assertIsNone(U.get(self.store, self.other)["open_link"])
        self.assertEqual(U.source_targets(self.store, []), {})
        self.assertEqual(U.source_targets(self.store, [99999]), {})

    def test_latest_linked_message_wins_with_stable_ties_and_accounts(self):
        more = self.store.add_messages([
            {"source": "lark", "account": "other", "item_key": "direct", "sent_at": 200,
             "link": "https://applink.larksuite.com/client/chat/open?openChatId=oc_other&position=15"},
            {"source": "lark", "account": "work", "item_key": "newer", "sent_at": 200,
             "link": "feishu://test"}])
        U.attach(self.store, self.parent, more)
        self.assertEqual(U.get(self.store, self.parent)["open_link"], "feishu://test")
        self.store.q("UPDATE messages SET link=NULL WHERE id=?", (more[1],))
        self.assertIn("applink.larksuite.com", U.get(self.store, self.parent)["open_link"])

    def test_legacy_cycles_terminate(self):
        self.store.q("INSERT INTO unit_links(parent_id,child_id,relation) VALUES(?,?,?)",
                     (self.leaf, self.parent, "contains"))
        self.assertEqual(U.source_targets(self.store, [self.parent])[self.parent]["open_link"], self.link)

    def test_list_cards_include_link_or_null(self):
        rows = {r["id"]: r for r in U.query(self.store)}
        self.assertEqual(rows[self.parent]["open_link"], self.link)
        self.assertEqual(rows[self.left]["open_link"], self.link + "0")
        self.assertIsNone(rows[self.other]["open_link"])


if __name__ == "__main__":
    unittest.main()
