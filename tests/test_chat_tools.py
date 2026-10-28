"""Graph and evidence edits through the chat assistant, with synthetic data only."""
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from infohub.apps import chat
from infohub.config import DEFAULTS
from infohub.organize import units as U
from infohub.store import Store


class ChatTools(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "hub.db")
        self.cfg = copy.deepcopy(DEFAULTS)
        self.changes = []
        self.world = U.upsert(self.store, None, kind="topic", title="World models")
        self.control = U.upsert(self.store, None, kind="topic", title="Control")
        self.reward = U.upsert(self.store, None, kind="topic", title="Reward design")
        self.store.db.commit()

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def tool(self, name, **args):
        return chat.run_tool(self.cfg, self.store, name, args, self.changes)

    def test_connections_preserve_parents_and_remove_only_requested_edge(self):
        for parent in (self.world, self.control):
            self.assertTrue(self.tool("link_units", parent_id=parent, child_id=self.reward)["changed"])
        self.assertFalse(self.tool("link_units", parent_id=self.world, child_id=self.reward)["changed"])
        self.assertEqual(len(self.changes), 2)
        detail = self.tool("get_unit", id=self.reward)
        self.assertEqual({p["id"] for p in detail["parents"]}, {self.world, self.control})
        children = self.tool("get_unit", id=self.world)["children"]
        self.assertEqual([(u["id"], u["relation"]) for u in children], [(self.reward, "part_of")])
        self.assertIn("error", self.tool("link_units", parent_id=self.reward, child_id=self.world))
        self.assertIn("error", self.tool("link_units", parent_id=self.world, child_id=self.world))
        self.assertIn("error", self.tool("link_units", parent_id=99999, child_id=self.reward))
        self.assertEqual(len(self.changes), 2)
        self.tool("link_units", parent_id=self.world, child_id=self.reward, relation="related_to")
        self.tool("unlink_units", parent_id=self.world, child_id=self.reward)
        self.assertFalse(self.tool("unlink_units", parent_id=self.world, child_id=self.reward)["changed"])
        edges = self.tool("get_graph", query="Reward", limit=10)["edges"]
        self.assertEqual({(e["source"], e["target"], e["relation"]) for e in edges},
                         {(self.world, self.reward, "related_to"), (self.control, self.reward, "part_of")})
        self.assertEqual(U.get(self.store, self.reward)["title"], "Reward design")

    def test_cycle_check_in_large_graph(self):
        ids = [self.world] + [U.upsert(self.store, None, kind="topic", title=f"Step {i}") for i in range(205)]
        for parent, child in zip(ids, ids[1:]):
            U.link(self.store, parent, child)
        self.store.db.commit()
        self.assertIn("error", self.tool("link_units", parent_id=ids[-1], child_id=ids[0]))
        self.assertFalse(self.changes)

    def test_bulk_links_are_atomic_and_preserve_existing_edges(self):
        self.tool("link_units", parent_id=self.control, child_id=self.reward)
        out = self.tool("link_units_bulk", parent_id=self.world, child_ids=[self.reward, self.control])
        self.assertEqual(out["changed"], 2)
        self.assertEqual(set(out["linked"]), {self.reward, self.control})
        self.assertEqual({p["id"] for p in U.get(self.store, self.reward)["parents"]}, {self.world, self.control})
        self.assertEqual({p["id"] for p in U.get(self.store, self.control)["parents"]}, {self.world})
        self.assertEqual(self.tool("link_units_bulk", parent_id=self.reward, child_ids=[self.world, self.control]).get("error") is not None, True)
        self.assertEqual({p["id"] for p in U.get(self.store, self.control)["parents"]}, {self.world})

    def test_create_and_edit_dates_kind_scope_and_evidence(self):
        mid = self.store.add_messages([{"source": "demo", "item_key": "1", "channel_name": "Research Lab",
                                       "sender_name": "Alex", "sent_at": 100, "kind": "text",
                                       "text": "Review reward design at the next meeting", "is_group": True}])[0]
        uid = self.tool("create_unit", kind="topic", title="Review rewards", tags=["topic:rewards"],
                        parent_ids=[self.world, self.control], message_ids=[mid])["created"]
        self.assertEqual(len(U.get(self.store, uid)["parents"]), 2)
        self.assertEqual(self.tool("get_messages", message_ids=[mid])[0]["text"],
                         "Review reward design at the next meeting")
        self.tool("update_unit", id=uid, kind="event", summary="Discuss evaluation", priority="P1",
                  due="2026-11-01T10:00:00+08:00", start="2026-11-01T09:00:00+08:00",
                  end="2026-11-01T10:00:00+08:00", for_me=True)
        unit = U.get(self.store, uid)
        self.assertEqual((unit["kind"], unit["summary"], unit["priority"], unit["for_me"]),
                         ("event", "Discuss evaluation", "P1", 1))
        self.assertGreater(unit["end_at"], unit["start_at"])
        self.assertEqual([t for t in unit["tags"] if t.startswith("type:")], ["type:event"])
        self.tool("update_unit", id=uid, due=None, start=None, end=None, for_me=False,
                  add_tags=["TYPE:TODO"], remove_tags=["type:todo"], remove_message_ids=[mid])
        unit = U.get(self.store, uid)
        self.assertEqual((unit["kind"], unit["due_at"], unit["start_at"], unit["end_at"], unit["for_me"]),
                         ("todo", None, None, None, 0))
        self.assertIn("type:todo", unit["tags"])
        self.assertEqual(unit["messages"], [])
        self.tool("update_unit", id=uid, message_ids=[mid])
        self.assertEqual([m["id"] for m in U.get(self.store, uid)["messages"]], [mid])
        self.assertEqual([u["id"] for u in self.tool("list_units", query="Review rewards")], [uid])

    def test_tag_consolidation_preserves_units_and_edges(self):
        U.add_tags(self.store, self.world, ["topic:reward", "topic:rewards"])
        U.add_tags(self.store, self.reward, ["topic:reward"])
        U.link(self.store, self.world, self.reward)
        self.store.db.commit()
        tags = {t["tag"]: t["n"] for t in self.tool("list_tags", prefix="topic:")}
        self.assertEqual(tags, {"topic:reward": 2, "topic:rewards": 1})
        self.assertEqual(self.tool("rename_tag", from_tag="topic:reward", to_tag="topic:rewards")["units"], 2)
        self.assertEqual(self.tool("list_tags", prefix="topic:"), [{"tag": "topic:rewards", "n": 2}])
        self.assertEqual(len(self.tool("get_graph")["edges"]), 1)
        self.assertEqual(self.tool("rename_tag", from_tag="topic:reward", to_tag="topic:rewards")["units"], 0)
        self.assertEqual(len(self.changes), 1)
        for old, new in [("type:topic", "type:todo"), ("source:demo", "source:other"),
                         ("status:open", "status:done"), ("topic:rewards", "person:Alex")]:
            self.assertIn("error", self.tool("rename_tag", from_tag=old, to_tag=new))
        self.assertEqual(U.get(self.store, self.world)["kind"], "topic")

    def test_failed_edit_rolls_back_and_does_not_report_changes(self):
        with patch.object(U, "attach", side_effect=RuntimeError("injected failure")):
            with self.assertRaises(RuntimeError):
                self.tool("update_unit", id=self.reward, title="Changed", add_tags=["topic:changed"])
            with self.assertRaises(RuntimeError):
                self.tool("create_unit", kind="todo", title="Should roll back", parent_ids=[self.world])
        self.assertEqual(U.get(self.store, self.reward)["title"], "Reward design")
        self.assertEqual(U.get(self.store, self.reward)["tags"], [])
        self.assertEqual(len(U.query(self.store)), 3)
        self.assertEqual(self.store.q("SELECT count(*) FROM unit_links").fetchone()[0], 0)
        self.assertEqual(self.changes, [])
        for args in [{"title": " "}, {"due": "sometime"}, {"message_ids": [99999]}, {"kind": "invalid"}]:
            self.assertIn("error", self.tool("update_unit", id=self.reward, **args))
        self.assertIn("error", self.tool("create_unit", kind="topic", title="Invalid", parent_ids=[99999]))
        self.assertFalse(self.changes)
        self.assertEqual(len(U.query(self.store)), 3)

    def test_stream_and_nonstream_execute_graph_tools(self):
        args = json.dumps({"parent_id": self.world, "child_id": self.reward})
        self.cfg["ai"].update(provider="openai_compatible", daily_budget_usd=1)
        seen = []
        def post(ai, messages, tools=True):
            seen.append(copy.deepcopy(messages))
            if len(seen) == 1:
                return {"choices": [{"message": {"tool_calls": [{"id": "link", "function": {
                    "name": "link_units", "arguments": args}}]}}]}
            return {"choices": [{"message": {"content": "Topics linked."}}]}
        with patch.object(chat, "api_key", return_value="test"), patch.object(chat, "_post", side_effect=post):
            out = chat.chat(self.cfg, self.store, [{"role": "user", "content": "Connect the topics"}])
        self.assertEqual(out["changes"][0]["action"], "linked")
        self.assertTrue(json.loads(seen[-1][-1]["content"])["changed"])
        def stream(ai, messages, tools=True):
            if messages[-1]["role"] == "user":
                yield {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "unlink", "function": {
                    "name": "unlink_units", "arguments": args[:20]}}]}}]}
                yield {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": args[20:]}}]}}]}
            else:
                self.assertTrue(json.loads(messages[-1]["content"])["changed"])
                yield {"choices": [{"delta": {"content": "Connection removed."}}]}
        with patch.object(chat, "api_key", return_value="test"), patch.object(chat, "_post_stream", side_effect=stream):
            events = list(chat.chat_stream(self.cfg, self.store, threading.Lock(),
                                          [{"role": "user", "content": "Remove that connection"}]))
        self.assertEqual(events[-1]["changes"][0]["action"], "unlinked")
        self.assertTrue(any(e["type"] == "status" and "unlinking" in e["text"] for e in events))
        self.assertEqual(self.tool("get_graph")["edges"], [])
        self.assertTrue(json.loads(chat._tool_content({"text": "long " * 10000}))["truncated"])

    def test_legacy_dsml_calls_are_structured_and_not_user_visible(self):
        raw = ('<|DSML| | calls>\n'
               '<|DSML| | invoke name="search">\n'
               '<|DSML| | parameter name="query" string="true">DiffTaichi'
               '<|DSML| | parameter>\n<|DSML| | invoke>\n<|DSML| | calls>')
        content, calls = chat._legacy_tool_calls(raw)
        self.assertEqual(content, "")
        self.assertEqual(calls[0]["function"]["name"], "search")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"query": "DiffTaichi"})


if __name__ == "__main__":
    unittest.main()
