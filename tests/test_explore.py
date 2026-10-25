"""Shared Explore filtering, optional graph context and message totals per child branch."""
import copy
import http.client
import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from infohub.apps import server
from infohub.config import DEFAULTS
from infohub.organize import units as U
from infohub.store import Store


class Explore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "hub.db")
        self.now = 1900000000
        self.mids = self.store.add_messages([{"source": "demo", "item_key": str(i), "kind": "text",
                                              "text": f"Synthetic evidence {i}", "sent_at": self.now}
                                             for i in range(4)])
        self.parent = self.unit("Daily digest", kind="digest")
        self.topic = self.unit("World model evaluation", summary="Reward alignment", for_me=True)
        self.left = self.unit("Training plan")
        self.right = self.unit("Evaluation notes")
        self.shared = self.unit("Shared evidence")
        self.standalone = self.unit("Direct evidence")
        for parent, child in [(self.parent, self.topic), (self.topic, self.left), (self.topic, self.right),
                              (self.left, self.shared), (self.right, self.shared)]:
            U.link(self.store, parent, child)
        U.attach(self.store, self.topic, self.mids[:1])
        U.attach(self.store, self.left, self.mids[1:3])
        U.attach(self.store, self.right, self.mids[2:3])
        U.attach(self.store, self.shared, [self.mids[0], self.mids[3]])
        U.attach(self.store, self.standalone, self.mids)
        U.add_tags(self.store, self.topic, ["type:topic", "project:alpha", "person:Alex"])
        self.store.db.commit()

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def unit(self, title, kind="topic", **kwargs):
        uid = U.upsert(self.store, None, kind=kind, title=title, activity_at=self.now - 100, **kwargs)
        self.store.db.commit()
        return uid

    def test_branch_totals_count_shared_descendants_and_repeated_message_attribution(self):
        # Same child under two branches contributes through both branches.
        # The root's direct message is also attached to the shared child.
        stats = U.rollup_stats(self.store)
        self.assertEqual(stats[self.shared]["n_messages"], 2)
        self.assertEqual(stats[self.left]["n_messages"], 4)
        self.assertEqual(stats[self.right]["n_messages"], 3)
        self.assertEqual(stats[self.topic], {"n_direct_messages": 1, "n_messages": 8,
                                            "n_children": 2, "n_descendants": 4})
        self.assertEqual(stats[self.parent]["n_messages"], 8)
        self.assertEqual(stats[self.parent]["n_descendants"], 5)
        # Parallel relation labels between the same parent and child are one child placement.
        U.link(self.store, self.topic, self.left, relation="related_to")
        self.store.db.commit()
        unit = U.get(self.store, self.topic, depth=2)
        self.assertEqual(unit["n_messages"], 8)
        self.assertEqual(unit["n_children"], 2)
        self.assertEqual(len(unit["children"]), 2)
        self.assertEqual(len(unit["messages"]), 1)
        self.assertEqual([u["n_messages"] for u in U.query(self.store, q="World")], [8])
        node = next(n for n in server.graph(self.store)["nodes"] if n["id"] == self.topic)
        self.assertEqual(node["n_messages"], 8)
        self.assertEqual(node["n_direct_messages"], 1)

    def test_aggregate_sorts_happen_before_limit(self):
        self.assertEqual(U.get(self.store, self.standalone)["n_direct_messages"], 4)
        # A topic with only one direct message must beat a topic with four direct messages.
        for order in ("messages", "children", "descendants"):
            rows = U.query(self.store, kind="topic", order=order, limit=1)
            self.assertEqual([u["id"] for u in rows], [self.topic])
            graph = server.graph(self.store, kind="topic", order=order, limit=1)
            self.assertEqual([n["id"] for n in graph["nodes"]], [self.topic])

    def test_strict_topic_graph_excludes_digest_until_context_is_enabled(self):
        strict = server.graph(self.store, kind="topic")
        self.assertNotIn(self.parent, {n["id"] for n in strict["nodes"]})
        self.assertTrue(all(n["kind"] == "topic" and n["seed"] for n in strict["nodes"]))
        self.assertEqual(strict["context_count"], 0)
        expanded = server.graph(self.store, kind="topic", include_context=True)
        digest = next(n for n in expanded["nodes"] if n["id"] == self.parent)
        self.assertFalse(digest["seed"])
        self.assertEqual(expanded["matched_count"], strict["matched_count"])
        self.assertEqual(expanded["context_count"], 1)
        ids = {n["id"] for n in expanded["nodes"]}
        self.assertTrue(all(e["source"] in ids and e["target"] in ids for e in expanded["edges"]))

    def test_combined_keywords_kinds_tags_time_scope_and_status_are_shared(self):
        resource = self.unit("World reference", kind="resource", summary="Reward comparison", status="done", for_me=True)
        U.add_tags(self.store, resource, ["project:alpha", "person:Sam"])
        old = self.unit("World old", summary="Reward", for_me=True)
        U.update(self.store, old, status="dropped")
        self.store.q("UPDATE units SET last_activity_at=? WHERE id=?", (self.now - 2 * 86400, old))
        self.store.db.commit()
        qs = {"q": "world reward", "kind": "topic,resource", "tags": "project:alpha,person:Alex,person:Sam",
              "days": "1", "mine": "1", "status": "all", "order": "messages", "limit": "400"}
        options = server.unit_query_params(qs, now=self.now)
        units = U.query(self.store, **options)
        graph = server.graph(self.store, **options)
        self.assertEqual([u["id"] for u in units], [self.topic, resource])
        self.assertEqual([n["id"] for n in graph["nodes"]], [u["id"] for u in units])
        options["status"] = "done"
        self.assertEqual([u["id"] for u in U.query(self.store, **options)], [resource])
        options["status"] = "all"
        options["roots_only"] = True
        self.assertEqual([n["id"] for n in server.graph(self.store, **options)["nodes"]], [self.topic, resource])

    def test_top_level_matches_respect_source_kind_and_for_me(self):
        # Digest containers and ancestors outside the selected scope must not hide topics.
        for uid in (self.topic, self.left, self.right, self.shared, self.standalone):
            U.add_tags(self.store, uid, ["source:slack"])
        U.add_tags(self.store, self.parent, ["source:lark:example"])
        self.store.q("UPDATE units SET for_me=1 WHERE id IN (?,?)", (self.left, self.shared))
        self.store.db.commit()
        options = dict(tags=["source:slack"], kind="topic", roots_only=True, order="messages")
        self.assertEqual([u["id"] for u in U.query(self.store, **options)], [self.topic, self.standalone])
        options["mine"] = True
        self.assertEqual([u["id"] for u in U.query(self.store, **options)], [self.topic])
        self.assertEqual([n["id"] for n in server.graph(self.store, **options)["nodes"]], [self.topic])
        # The same source/scope combination previously returned no units at all.
        options["kind"] = None
        self.assertEqual([u["id"] for u in U.query(self.store, **options)], [self.topic])

    def test_source_filter_matches_untagged_parent_with_keyword_and_activity(self):
        U.update(self.store, self.topic, title="大话题：共享证据")
        U.add_tags(self.store, self.shared, ["source:slack"])
        other = self.unit("大话题：其他来源")
        U.add_tags(self.store, other, ["source:lark:example"])
        self.store.db.commit()
        options = server.unit_query_params({"tags": "source:slack", "q": "大话题", "days": "30",
                                            "order": "descendants", "roots": "0", "mine": "0"}, now=self.now)
        rows = U.query(self.store, **options)
        self.assertEqual([u["id"] for u in rows], [self.topic])
        self.assertNotIn("source:slack", rows[0]["tags"])
        self.assertEqual(rows[0]["n_messages"], 8)
        graph = server.graph(self.store, **options)
        self.assertEqual([n["id"] for n in graph["nodes"]], [self.topic])
        self.assertTrue(graph["nodes"][0]["seed"])
        self.assertEqual(graph["context_count"], 0)
        options["roots_only"] = True
        options["mine"] = True
        self.assertEqual([u["id"] for u in U.query(self.store, **options)], [self.topic])
        # Derived source membership changes immediately without copying tags to parents.
        U.remove_tags(self.store, self.shared, ["source:slack"])
        self.store.db.commit()
        self.assertEqual(U.query(self.store, **options), [])

    def test_source_filter_uses_evidence_and_all_parent_branches_without_sibling_leaks(self):
        other_parent = self.unit("Another containing topic")
        U.link(self.store, other_parent, self.shared)
        messages = self.store.add_messages([{"source": "lark", "account": "example", "item_key": "source-evidence",
                                             "kind": "text", "text": "Synthetic source evidence", "sent_at": self.now}])
        U.attach(self.store, self.shared, messages)
        U.add_tags(self.store, self.standalone, ["source:slack"])
        self.store.db.commit()
        actual = {u["id"] for u in U.query(self.store, tags=["source:lark:example"])}
        self.assertEqual(actual, {self.parent, self.topic, self.left, self.right, self.shared, other_parent})
        # A parent's union of sources must not make an unrelated sibling match.
        self.assertNotIn(self.standalone, actual)
        self.assertEqual(U.query(self.store, tags=["source:lark:other"]), [])
        both = {u["id"] for u in U.query(self.store, tags=["source:slack", "source:lark:example"])}
        self.assertEqual(both, actual | {self.standalone})
        # Other namespaces and For me retain their own-unit semantics.
        self.assertEqual([u["id"] for u in U.query(self.store, tags=["source:lark:example", "project:alpha"])],
                         [self.topic])
        self.assertEqual([u["id"] for u in U.query(self.store, tags=["source:lark:example"], mine=True)],
                         [self.topic])
        self.assertEqual(U.get(self.store, other_parent)["tags"], [])

    def test_top_level_matches_cross_nonmatching_bridges_and_precede_limit(self):
        bridge = self.unit("Hidden bridge", kind="resource")
        leaf = self.unit("Newest nested topic")
        U.link(self.store, self.topic, bridge)
        U.link(self.store, bridge, leaf)
        for uid in (self.topic, leaf):
            U.add_tags(self.store, uid, ["project:roots"])
        self.store.q("UPDATE units SET last_activity_at=? WHERE id=?", (self.now + 1, leaf))
        self.store.db.commit()
        options = dict(kind="topic", tags=["project:roots"], roots_only=True, limit=1)
        # Applying the limit first would keep only the newer leaf, or lose the result.
        for order in ("activity", "title", "messages", "children", "descendants"):
            self.assertEqual([u["id"] for u in U.query(self.store, order=order, **options)], [self.topic])
        # Excluding the ancestor with a keyword makes the nested match top-level itself.
        self.assertEqual([u["id"] for u in U.query(self.store, q="Newest", **options)], [leaf])

    def test_keywords_are_literal_and_support_short_chinese_and_and_matching(self):
        chinese = self.unit("模型结果")
        percent = self.unit("100% signal")
        underscore = self.unit("marker_value")
        self.assertEqual([u["id"] for u in U.query(self.store, q="模型")], [chinese])
        self.assertEqual([u["id"] for u in U.query(self.store, q="%")], [percent])
        self.assertEqual([u["id"] for u in U.query(self.store, q="_")], [underscore])
        self.assertEqual([u["id"] for u in U.query(self.store, q="world reward")], [self.topic])
        self.assertEqual(U.query(self.store, q="world absent"), [])
        self.assertEqual(U.query(self.store, q="' OR 1=1"), [])

    def test_deep_rollup_is_iterative_and_legacy_cycle_does_not_loop(self):
        ids = [U.upsert(self.store, None, kind="note", title=f"Level {i}") for i in range(1100)]
        self.store.db.executemany("INSERT INTO unit_links(parent_id,child_id) VALUES(?,?)", zip(ids, ids[1:]))
        U.attach(self.store, ids[-1], self.mids[:1])
        self.store.db.commit()
        stats = U.rollup_stats(self.store)
        self.assertEqual(stats[ids[0]]["n_messages"], 1)
        self.assertEqual(stats[ids[0]]["n_descendants"], 1099)
        self.assertEqual(U.query(self.store, order="descendants", limit=1)[0]["id"], ids[0])
        self.store.q("INSERT INTO unit_links(parent_id,child_id) VALUES(?,?)", (ids[-1], ids[0]))
        self.store.db.commit()
        self.assertEqual(U.rollup_stats(self.store)[ids[0]]["n_messages"], 1)
        U.add_tags(self.store, ids[-1], ["source:demo"])
        self.store.db.commit()
        self.assertEqual(U.query(self.store, tags=["source:demo"], kind="note", order="descendants", limit=1)[0]["id"], ids[0])

    def test_http_list_and_graph_share_exact_matches_order_and_limit(self):
        cfg = copy.deepcopy(DEFAULTS)
        cfg["hub"]["database"] = str(self.root / "hub.db")
        cfg["hub"]["auth"]["users_file"] = str(self.root / "users.local.json")
        cfg["hub"]["backup"].update(enabled=False, directory=str(self.root / "backups"))
        cfg["hub"]["dashboard"]["port"] = 0
        captured, ready = [], threading.Event()
        factory = server.ThreadingHTTPServer
        def capture(address, handler):
            httpd = factory(address, handler)
            captured.append(httpd); ready.set()
            return httpd
        with patch.object(server, "ThreadingHTTPServer", side_effect=capture):
            thread = threading.Thread(target=server.serve, args=(cfg, self.store, lambda: {}), daemon=True)
            thread.start()
            self.assertTrue(ready.wait(3))
            httpd = captured[0]
            def get(path):
                connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
                connection.request("GET", path, headers={"Host": "127.0.0.1:0"})
                response = connection.getresponse()
                data = json.loads(response.read()); connection.close()
                self.assertEqual(response.status, 200)
                return data
            try:
                for qs in [{"kind": "topic", "order": "messages", "limit": 2},
                           {"kind": "topic,digest", "roots": 1, "order": "children"},
                           {"q": "world reward", "kind": "topic", "mine": 1},
                           {"tags": "project:alpha", "order": "descendants"},
                           {"tags": "project:alpha", "kind": "topic", "mine": 1, "roots": 1},
                           {"tags": "source:demo", "q": "world", "days": 30, "order": "descendants"},
                           {"status": "done", "order": "title"}]:
                    query = urlencode(qs)
                    units = get("/api/units?" + query)
                    graph = get("/api/graph?" + query)
                    self.assertEqual([u["id"] for u in units], [n["id"] for n in graph["nodes"]])
                    self.assertEqual(graph["context_count"], 0)
                    if qs.get("tags") == "source:demo":
                        self.assertEqual([u["id"] for u in units], [self.topic])
                context = get("/api/graph?kind=topic&context=1")
                self.assertEqual(context["context_count"], 1)
            finally:
                httpd.shutdown(); thread.join(timeout=5)
                self.assertFalse(thread.is_alive())

    @unittest.skipUnless(shutil.which("node"), "Node is needed to validate async navigation")
    def test_frontend_navigation_keeps_latest_request_and_scroll(self):
        source = (Path(__file__).resolve().parents[1] / "infohub/apps/web/index.html").read_text(encoding="utf-8")
        scrolling = "function scrollState()" + source.split("function scrollState()", 1)[1].split("// Make a multi-column", 1)[0]
        rendering = "let renderVersion = 0;" + source.split("let renderVersion = 0;", 1)[1].split('$("poll").onclick', 1)[0]
        searching = '$("q").onkeydown = async e => {' + source.split('$("q").onkeydown = async e => {', 1)[1].split("// optional ?tab=", 1)[0]
        setup = r"""const assert = require('node:assert/strict');
let TAB='explore', VIEWS=[], lanes=[{scrollTop:100}];
const main={dataset:{tab:'explore'},node:null,replaceChildren(node){this.node=node;lanes=[{scrollTop:0}];}};
const q={value:''}, status={textContent:''};
const $=id=>({main,q,status})[id];
const document={querySelectorAll:()=>lanes};
const window={scrollY:0,scrollTo(x,y){this.scrollY=y;}};
const requestAnimationFrame=fn=>fn();
const drawTabs=()=>{}, drawScope=()=>{}, enhanceGrids=()=>{};
const el=(tag,attrs,...kids)=>({tag,attrs,kids});
const lane=(title,...rest)=>({title,rest});
const pendingViews=[],pendingSearches=[];
function deferred(){let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};}
const TABS={explore(){const d=deferred();pendingViews.push(d);return d.promise;},today(){return TABS.explore();}};
const api=()=>{const d=deferred();pendingSearches.push(d);return d.promise;};
const emptySearch={people:[],channels:[],tags:[],units:[],messages:[]};
"""
        checks = r"""
(async()=>{
 const old=render(), latest=render();
 lanes[0].scrollTop=240;window.scrollY=80;
 pendingViews[1].resolve({id:'latest'});await latest;
 assert.equal(main.node.id,'latest');assert.equal(lanes[0].scrollTop,240);assert.equal(window.scrollY,80);
 pendingViews[0].resolve({id:'obsolete'});await old;
 assert.equal(main.node.id,'latest','an older filter response must not replace the latest result');
 // Global search and tab/filter refreshes share the same request ordering.
 const pendingRender=render();q.value='evidence';
 const search=q.onkeydown({key:'Enter',target:q});
 pendingSearches[0].resolve(emptySearch);await search;
 const searchNode=main.node;
 pendingViews[2].resolve({id:'old tab'});await pendingRender;
 assert.equal(main.node,searchNode,'a pending filter response must not overwrite a newer global search');
 assert.equal(main.dataset.tab,'search','search results must not borrow Explore lane scroll positions');
 q.value='older search';const olderSearch=q.onkeydown({key:'Enter',target:q});
 const newerRender=render();pendingViews[3].resolve({id:'newer filter'});await newerRender;
 pendingSearches[1].resolve(emptySearch);await olderSearch;
 assert.equal(main.node.id,'newer filter','an older search response must not overwrite newer filter results');
 TAB='today';lanes[0].scrollTop=390;
 const navigation=render();pendingViews[4].resolve({id:'today'});await navigation;
 assert.equal(lanes[0].scrollTop,0,'scroll positions must not carry across unrelated tabs');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(["node", "-e", setup + scrolling + rendering + searching + checks],
                                capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to validate canvas size math")
    def test_node_area_follows_total_message_count(self):
        source = (Path(__file__).resolve().parents[1] / "infohub/apps/web/index.html").read_text(encoding="utf-8")
        function = re.search(r"function messageRadius\([^)]*\) \{.*?\n\}", source, re.S).group(0)
        script = function + "\n" + r'''const assert = require('node:assert/strict');
assert.equal(messageRadius(0, 8), 5);
assert.ok(messageRadius(8, 8) > messageRadius(4, 8));
assert.ok(messageRadius(4, 8) > messageRadius(2, 8));
assert.ok(Math.abs((messageRadius(8, 8)**2 - 25) / (messageRadius(4, 8)**2 - 25) - 2) < 1e-10);
assert.equal(messageRadius(8, 8), 26);
'''
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
