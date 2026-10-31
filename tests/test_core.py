"""Unit tests with synthetic data only (no real messages, no network): python -m unittest discover -s tests -v"""
import copy
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sqlite3
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from infohub.apps import brief, reminders
from infohub.apps.server import channel_messages, search, search_all, summary, today
from infohub.config import DEFAULTS
from infohub.dates import find_datetime
from infohub.organize import agent_bridge, ai_organizer, polish, prompt, providers, rule_organizer
from infohub.organize import units as U
from infohub.store import Store
from infohub.sync import policy
from infohub.sync.preprocess import assign_segments, close_segments, flags, resplit
from infohub.sync.triage import channel_policy, classify, mentions_me

TZ = timezone(timedelta(hours=8))
SCFG = {"default_policy": "store", "watch": {"channels": ["g_watch"], "people": ["boss"]},
        "digest_only": ["g_big"], "ignore": {"channels": ["g_spam"], "name_patterns": ["*Ads*"], "official_accounts": True}}
RULES = [{"name": "deadline", "when": {"keywords": ["DDL", "截止"]}, "priority": "P1", "extract": ["task"]}]


def cfg():
    c = copy.deepcopy(DEFAULTS)
    c["rules"] = RULES
    c["organize"]["tag_rules"] = [{"when": {"channels": ["g_course"]}, "tags": ["course:ai-eng"]}]
    # source config + per-source policy defaults come from the plugins (as config.load does)
    from infohub.sources import discover
    for name, mod in discover().items():
        c["sources"][name] = copy.deepcopy(getattr(mod, "DEFAULT_CONFIG", {}))
        if getattr(mod, "POLICY_DEFAULTS", None):
            c["policy_defaults"][name] = copy.deepcopy(mod.POLICY_DEFAULTS)
    c["sources"]["demo"] = {"default_policy": "store"}
    c["policy_defaults"]["demo"] = [
        {"when": {"is_group": False}, "policy": "store"},
        {"when": {"muted": True}, "policy": "ignore"},
        {"when": {"active_within_days": 7}, "policy": "watch"},
        {"policy": "ignore"},
    ]
    return c


def msg(**kw):
    base = {"source": "chatapp", "account": "", "item_key": str(time.time_ns()), "channel": "g1", "channel_name": "Lab",
            "is_group": True, "sender": "u1", "sender_name": "A", "sent_at": int(time.time()), "kind": "text",
            "text": "hello there", "mentions_me": False, "from_me": False, "policy": "store"}
    base.update(kw)
    base["flags"] = flags(base["text"], base["kind"], base["sent_at"], 8)
    return classify(base, RULES, SCFG, {})


class Dates(unittest.TestCase):
    REF = datetime(2026, 10, 23, 10, 0, tzinfo=TZ)  # a Friday

    def check(self, text, expect, has_time=None):
        d, t = find_datetime(text, self.REF)
        self.assertEqual(d.strftime("%m-%d %H:%M") if d else None, expect, text)
        if has_time is not None:
            self.assertEqual(t, has_time)

    def test_cases(self):
        self.check("明天下午3点开组会", "10-24 15:00", True)
        self.check("作业周日截止", "10-25 23:59", False)
        self.check("下周一 14:30 答辩", "10-26 14:30", True)
        self.check("请于10月30日前提交", "10-30 23:59")
        self.check("DDL 2026-11-02 23:59", "11-02 23:59")
        self.check("30号交报告", "10-30 23:59")
        self.check("meeting tomorrow 3pm", "10-24 15:00")
        self.check("due next Friday", "10-30 23:59")
        self.check("Oct 31 deadline", "10-31 23:59")
        self.check("今晚8点半", "10-23 20:30")
        self.check("version 3.2 released", None)
        self.check("比分 3:2", None)


class Layer1(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "hub.db")

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_policy_and_priority(self):
        self.assertEqual(channel_policy("chatapp", "g_watch", "x", SCFG, {}), "watch")
        self.assertEqual(channel_policy("chatapp", "g_big", "x", SCFG, {}), "digest")
        self.assertEqual(channel_policy("chatapp", "g2", "Spam Ads Group", SCFG, {}), "ignore")
        self.assertEqual(channel_policy("chatapp", "gh_1", "News", SCFG, {}), "ignore")
        self.assertEqual(channel_policy("chatapp", "g_watch", "x", SCFG, {"mute": {("chatapp", "g_watch")}}), "ignore")
        self.assertEqual(msg()["priority"], "P3")
        self.assertEqual(msg(text="周五 DDL")["priority"], "P1")
        self.assertEqual(msg(policy="digest", text="DDL")["priority"], "P2")
        self.assertEqual(msg(sender="boss")["priority"], "P1")
        self.assertTrue(mentions_me("@Alice 看下", ["Alice"]))
        self.assertTrue(mentions_me("@所有人 交作业", []))      # @everyone counts
        self.assertTrue(mentions_me("@all please submit", []))
        self.assertFalse(mentions_me("大家好", []))

    def test_dedupe_segments_and_accounts(self):
        t0 = int(time.time()) - 7200
        a = [msg(item_key="k1", sent_at=t0), msg(item_key="k2", sent_at=t0 + 60), msg(item_key="k3", sent_at=t0 + 3600)]
        ids = self.store.add_messages(a)
        self.assertEqual(len(ids), 3)
        self.assertEqual(self.store.add_messages(a), [])                       # re-poll: no duplicates
        same_key_other_org = msg(source="lark", account="org2", item_key="k1", sent_at=t0)
        self.assertEqual(len(self.store.add_messages([same_key_other_org])), 1)  # accounts are separate namespaces
        self.assertEqual(assign_segments(self.store, ids, 30), 2)              # 1h pause -> two segments
        self.assertEqual(close_segments(self.store, 30), 2)

    def test_segments_are_capped_and_resplit(self):
        t0 = int(time.time()) - 7200
        ids = self.store.add_messages([msg(item_key=f"c{i}", sent_at=t0 + i) for i in range(25)])
        self.assertEqual(assign_segments(self.store, ids, 30, max_messages=10), 3)     # 25 msgs -> 3 segments
        sizes = [r[0] for r in self.store.q("SELECT n FROM segments ORDER BY id")]
        self.assertEqual(sizes, [10, 10, 5])
        # an oversized segment from an older version is split, and every message keeps a segment
        self.store.q("UPDATE segments SET n=25 WHERE id=(SELECT min(id) FROM segments)")
        self.store.q("UPDATE messages SET segment_id=(SELECT min(id) FROM segments)")
        self.store.q("DELETE FROM segments WHERE id > (SELECT min(id) FROM segments)")
        self.store.db.commit()
        out = resplit(self.store, 10)
        self.assertEqual((out["split"], out["new_segments"]), (1, 2))
        self.assertEqual(sorted(r[0] for r in self.store.q("SELECT n FROM segments")), [5, 10, 10])
        self.assertEqual(self.store.q("SELECT count(*) FROM messages WHERE segment_id IS NULL").fetchone()[0], 0)
        self.assertEqual(self.store.q("SELECT count(DISTINCT segment_id) FROM messages").fetchone()[0], 3)

    def test_v1_database_migrates_with_backup(self):
        p = Path(self.tmp.name) / "old.db"
        con = sqlite3.connect(p)
        con.executescript("CREATE TABLE items(id INTEGER); CREATE TABLE overrides(kind,source,value,label,created_at);"
                          "INSERT INTO overrides VALUES('mute','chatapp','g9','',1);")
        con.commit(); con.close()
        s = Store(p)
        self.assertIn(("chatapp", "g9"), s.overrides()["mute"])
        self.assertTrue((Path(self.tmp.name) / "old.v1.bak.db").exists())
        s.db.close()


class Layer2(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "hub.db")
        self.cfg = cfg()

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_graph_tags_and_cycles(self):
        a = U.upsert(self.store, "a", kind="topic", title="A")
        b = U.upsert(self.store, "b", kind="todo", title="B")
        c = U.upsert(self.store, "c", kind="todo", title="C")
        self.assertTrue(U.link(self.store, a, b))
        self.assertTrue(U.link(self.store, b, c))
        self.assertFalse(U.link(self.store, c, a))      # would create a cycle
        self.assertFalse(U.link(self.store, a, a))
        d = U.upsert(self.store, "d", kind="topic", title="D")
        self.assertTrue(U.link(self.store, d, c))       # several parents allowed
        U.add_tags(self.store, c, ["Course: AI Eng", "type:homework", "plain"])
        self.assertEqual(sorted(U.get(self.store, c)["tags"]), ["course:ai-eng", "topic:plain", "type:homework"])
        self.assertEqual(len(U.get(self.store, a, depth=3)["children"][0]["children"]), 1)
        self.assertEqual([u["id"] for u in U.query(self.store, ["type:homework", "course:ai-eng"])], [c])
        self.assertEqual({u["id"] for u in U.query(self.store, roots_only=True)}, {a, d})
        # tag filter: OR within a namespace, AND across namespaces
        U.add_tags(self.store, b, ["person:Alice"])
        U.add_tags(self.store, c, ["person:Bob"])
        self.assertEqual({u["id"] for u in U.query(self.store, ["person:Alice", "person:Bob"])}, {b, c})   # OR
        self.assertEqual([u["id"] for u in U.query(self.store, ["person:Bob", "type:homework"])], [c])     # AND
        self.assertEqual(U.query(self.store, ["person:Alice", "type:homework"]), [])                        # b isn't homework
        from infohub.apps.server import graph
        g = graph(self.store)
        self.assertEqual({n["id"] for n in g["nodes"]}, {a, b, c, d})
        self.assertEqual({(e["source"], e["target"]) for e in g["edges"]}, {(a, b), (b, c), (d, c)})
        # filtering by a tag keeps the matching node and pulls its linked neighbours in as edges
        gh = graph(self.store, ["type:homework"], include_context=True)
        ids = {n["id"] for n in gh["nodes"]}
        self.assertIn(c, ids)                                  # the homework unit (seed)
        self.assertTrue({b, d} & ids)                          # neighbours pulled in so edges connect
        self.assertTrue(any(n["seed"] and n["id"] == c for n in gh["nodes"]))
        self.assertTrue(all(e["source"] in ids and e["target"] in ids for e in gh["edges"]))

    def test_rule_organizer(self):
        t = int(time.time())
        ms = [msg(item_key="1", text="大家记得明天下午3点开组会", sent_at=t - 7200),
              msg(item_key="2", text="作业周日截止，请按时提交", channel="g_course", channel_name="AI课", sent_at=t - 7100),
              msg(item_key="3", text="@所有人 【通知】下周一实验室安全检查，请整理工位", sent_at=t - 7000),
              msg(item_key="4", text="哈哈哈", sent_at=t - 6900),
              msg(item_key="5", text="DDL 是明天吗", from_me=True, sent_at=t - 6800)]
        ids = self.store.add_messages(ms)
        assign_segments(self.store, ids, 30)
        close_segments(self.store, 30)
        out = rule_organizer.run(self.cfg, self.store)
        kinds = {r[0]: r[1] for r in self.store.q("SELECT kind, count(*) FROM units GROUP BY kind")}
        self.assertEqual(kinds.get("event"), 1)

        self.assertEqual(kinds.get("todo"), 2)           # "记得…" and "作业…截止"
        self.assertEqual(kinds.get("announcement"), 1)
        self.assertEqual(kinds.get("digest"), 2)         # one container per chat-day
        hw = U.query(self.store, ["course:ai-eng"], kind="todo")
        self.assertEqual(len(hw), 1)
        self.assertIn("type:deadline", hw[0]["tags"])
        self.assertEqual(out["queued_for_ai"], 0)        # AI disabled -> nothing queued
        self.assertEqual(rule_organizer.run(self.cfg, self.store)["units"], 0)   # idempotent

    def _segment_job(self):
        t = int(time.time()) - 7200
        ids = self.store.add_messages([msg(item_key=str(i), policy="watch", sent_at=t + i * 60,
                                           text=f"GPU node {i} 预约讨论") for i in range(3)])
        assign_segments(self.store, ids, 30)
        close_segments(self.store, 30)
        self.cfg["ai"]["enabled"] = True
        rule_organizer.run(self.cfg, self.store)
        job = self.store.q("SELECT * FROM jobs").fetchone()
        self.assertIsNotNone(job)
        return job, ids

    def _result(self, ids):
        return {"units": [
            {"ref": "t1", "existing_id": None, "kind": "topic", "title": "Reserve GPU node for Friday run", "summary": "s",
             "tags": ["topic:gpu-scheduling", "project:predactor"], "people": ["A"], "due": None, "start": None,
             "status": "open", "parent": None, "message_ids": ids},
            {"ref": "t2", "existing_id": None, "kind": "todo", "title": "Book node 3", "summary": "",
             "tags": [], "people": [], "due": "2026-10-24T18:00:00+08:00", "start": None, "status": "open",
             "parent": "t1", "message_ids": [ids[-1], 999999]}], "close_units": []}

    def test_anthropic_provider_with_fake_client(self):
        job, ids = self._segment_job()
        result = self._result(ids)

        class FakeMessages:
            def create(self, **kw):
                self.kw = kw
                return types.SimpleNamespace(stop_reason="end_turn", content=[types.SimpleNamespace(type="text", text=json.dumps(result))],
                                             usage=types.SimpleNamespace(input_tokens=1000, output_tokens=200,
                                                                         cache_read_input_tokens=0, cache_creation_input_tokens=0))
        fake = types.SimpleNamespace(messages=FakeMessages(), beta=types.SimpleNamespace(messages=FakeMessages()))
        self.cfg["ai"].update(provider="anthropic", model="claude-haiku-5-5", price_usd_per_mtok={"input": 0.1, "output": 0.5})
        out = ai_organizer.run(self.cfg, self.store, provider=providers.Anthropic(self.cfg["ai"], client=fake))
        self.assertEqual(out["created"], 2)
        sent = fake.messages.kw
        self.assertEqual(sent["output_config"]["format"]["schema"], prompt.SCHEMA)
        self.assertEqual(sent["system"][0]["cache_control"], {"type": "ephemeral"})
        todo = U.query(self.store, kind="todo")[0]
        full = U.get(self.store, todo["id"])
        self.assertEqual(full["parents"][0]["title"], "Reserve GPU node for Friday run")
        self.assertEqual([m["id"] for m in full["messages"]], [ids[-1]])   # foreign message id dropped
        self.assertIn("project:predactor", U.get(self.store, full["parents"][0]["id"])["tags"])
        self.assertEqual(self.store.q("SELECT status FROM jobs").fetchone()[0], "done")
        self.assertGreater(ai_organizer.spent_today(self.store), 0)

    def test_openai_compatible_provider_against_local_server(self):
        job, ids = self._segment_job()
        result, seen, codes = self._result(ids), [], [500, 200, 401]

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append((self.path, self.headers.get("Authorization"), body))
                code = codes.pop(0)
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if code == 200:
                    self.wfile.write(json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}],
                                                 "usage": {"prompt_tokens": 2000, "completion_tokens": 300,
                                                           "prompt_cache_hit_tokens": 1500}}).encode())

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        os.environ["INFOHUB_TEST_KEY"] = "test-key"
        self.cfg["ai"].update(provider="openai_compatible", base_url=f"http://127.0.0.1:{srv.server_port}", model="deepseek-flash",
                              api_key_env="INFOHUB_TEST_KEY", price_usd_per_mtok={"input": 1.0, "output": 2.0, "cache_read": 0.1})
        try:
            out1 = ai_organizer.run(self.cfg, self.store)                      # HTTP 500 -> retry later
            self.assertEqual(out1["retry"], 1)
            row = self.store.q("SELECT status, attempts, not_before FROM jobs").fetchone()
            self.assertEqual((row["status"], row["attempts"]), ("pending", 1))
            self.assertGreater(row["not_before"], time.time())
            self.store.q("UPDATE jobs SET not_before=0"); self.store.db.commit()
            out2 = ai_organizer.run(self.cfg, self.store)                      # HTTP 200 -> applied
            self.assertEqual(out2["created"], 2)
            path, auth, body = seen[1]
            self.assertEqual((path, auth, body["model"]), ("/chat/completions", "Bearer test-key", "deepseek-flash"))
            self.assertEqual(body["response_format"], {"type": "json_object"})
            self.assertIn("JSON Schema", body["messages"][0]["content"])
            self.assertAlmostEqual(out2["cost"], (500 * 1.0 + 300 * 2.0 + 1500 * 0.1) / 1e6, places=9)
            # a fatal error (401) stops the run and leaves new jobs pending
            ids2 = self.store.add_messages([msg(item_key=f"n{i}", policy="watch", sent_at=int(time.time()) - 5000 + i,
                                                text="另一个讨论 next step") for i in range(2)])
            assign_segments(self.store, ids2, 30); close_segments(self.store, 30); rule_organizer.run(self.cfg, self.store)
            out3 = ai_organizer.run(self.cfg, self.store)
            self.assertIn("HTTP 401", out3["error"])
            self.assertEqual(self.store.q("SELECT count(*) FROM jobs WHERE status='pending'").fetchone()[0], 1)
        finally:
            srv.shutdown()
            srv.server_close()
            del os.environ["INFOHUB_TEST_KEY"]

    def test_missing_key_and_budget_leave_jobs_pending(self):
        job, _ = self._segment_job()
        self.cfg["ai"].update(provider="openai_compatible", api_key_env="INFOHUB_KEY_THAT_IS_NOT_SET")
        self.assertIn("is not set", ai_organizer.run(self.cfg, self.store)["error"])
        self.cfg["ai"]["daily_budget_usd"] = 0
        self.assertEqual(ai_organizer.run(self.cfg, self.store)["skipped"], "daily budget reached")
        self.assertEqual(self.store.q("SELECT status FROM jobs").fetchone()[0], "pending")

    def test_agent_lease_submit_and_expiry(self):
        job, ids = self._segment_job()
        leased = agent_bridge.lease(self.cfg, self.store, 10)
        self.assertEqual((len(leased["jobs"]), leased["schema"]), (1, prompt.SCHEMA))
        self.assertEqual(len(leased["jobs"][0]["input"]["messages"]), 3)
        self.assertEqual(agent_bridge.lease(self.cfg, self.store, 10)["jobs"], [])      # already leased
        self.store.q("UPDATE jobs SET not_before=?", (int(time.time()) - 1,)); self.store.db.commit()
        self.assertEqual(len(agent_bridge.lease(self.cfg, self.store, 10)["jobs"]), 1)  # expired lease reclaimed
        payload = json.dumps({"results": [{"job_id": job["id"], "result": self._result(ids)}, {"job_id": 424242, "result": {}}]})
        out = agent_bridge.submit(self.cfg, self.store, payload)
        self.assertEqual((out["applied"], out["rejected"], out["created"]), (1, 1, 2))
        self.assertEqual(agent_bridge.submit(self.cfg, self.store, payload)["applied"], 0)   # finished jobs ignored
        self.assertEqual(U.query(self.store, kind="topic")[0]["origin"], "agent")

    def test_agent_file_export_import(self):
        job, ids = self._segment_job()
        jobs_file = Path(self.tmp.name) / "jobs.jsonl"
        self.assertEqual(agent_bridge.export(self.cfg, self.store, jobs_file)["exported"], 1)
        line = json.loads(jobs_file.read_text(encoding="utf-8"))
        res_file = Path(self.tmp.name) / "results.jsonl"
        res_file.write_text(json.dumps({"job_id": line["job_id"], "result": self._result(ids)}) + "\nnot json\n", encoding="utf-8")
        out = agent_bridge.import_results(self.cfg, self.store, res_file)
        self.assertEqual((out["applied"], out["rejected"], out["created"]), (1, 1, 2))


class PolicyDefaults(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "hub.db")
        self.cfg = cfg()
        self.now = int(time.time())

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def chan(self, **kw):
        d = {"source": "demo", "account": "", "channel": f"c{time.time_ns()}", "channel_name": "G",
             "is_group": True, "policy": "store", "muted": 0, "chat_mode": "group", "last_at": self.now}
        d.update(kw)
        self.store.upsert_channel(d["source"], d["account"], d["channel"], d["channel_name"], d["is_group"],
                                  d["policy"], None, d["last_at"], muted=d["muted"], chat_mode=d["chat_mode"])
        self.store.db.commit()
        return d


    def test_default_engine(self):
        p = lambda **f: policy.default_policy("demo", {"name": "G", **f}, self.cfg, self.now)
        self.assertEqual(p(is_group=False, muted=None, last_at=self.now), "store")
        self.assertEqual(p(is_group=True, muted=True, last_at=self.now), "ignore")
        self.assertEqual(p(is_group=True, muted=False, last_at=self.now), "watch")
        self.assertEqual(p(is_group=True, muted=False, last_at=self.now - 30 * 86400), "ignore")
        self.assertEqual(p(is_group=True, muted=None, last_at=self.now), "watch")

    def test_lark_rules(self):
        p = lambda **f: policy.default_policy("lark", {"name": "G", "last_at": self.now, **f}, self.cfg, self.now)
        self.assertEqual(p(is_group=False, muted=True, chat_mode="p2p"), "watch")         # direct chat wins
        self.assertEqual(p(is_group=True, muted=True, chat_mode="topic"), "watch")        # topic chat wins
        self.assertEqual(p(is_group=True, muted=True, chat_mode="group"), "ignore")
        self.assertEqual(p(is_group=True, muted=False, chat_mode="group"), "watch")
        self.assertEqual(policy.default_policy("lark", {"name": "AI课程群", "is_group": True, "muted": True,
                                                        "chat_mode": "group", "last_at": self.now}, self.cfg, self.now), "watch")


    def test_plan_applies_and_respects_manual_choices(self):
        live = self.chan(channel="live", channel_name="Live", muted=0, last_at=self.now)
        stale = self.chan(channel="old", channel_name="Old", muted=0, last_at=self.now - 60 * 86400)
        pinned = self.chan(channel="pin", channel_name="Pinned", muted=1, policy="watch")
        self.store.add_override("watch", "demo", pinned["channel"])      # set by hand in the dashboard
        preview = policy.apply(self.store, self.cfg, dry_run=True)
        moves = {c["channel_name"]: (c["from"], c["to"]) for c in policy.plan(self.store, self.cfg)}
        self.assertEqual(moves, {"Live": ("store", "watch"), "Old": ("store", "ignore")})
        self.assertFalse(preview["applied"])
        self.assertEqual(self.store.q("SELECT policy FROM channels WHERE channel=?", (live["channel"],)).fetchone()[0], "store")
        out = policy.apply(self.store, self.cfg, dry_run=False)
        self.assertEqual((out["changes"], out["applied"]), (2, True))
        got = dict(self.store.q("SELECT channel_name, policy FROM channels").fetchall())
        self.assertEqual(got, {"Live": "watch", "Old": "ignore", "Pinned": "watch"})
        self.assertEqual(policy.apply(self.store, self.cfg, dry_run=False)["changes"], 0)   # idempotent


class Relevance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "hub.db")
        self.cfg = cfg()

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_for_me_signals(self):
        now = int(time.time())
        # four messages: @mention-you, @everyone announcement, a plain group line, a one-to-one chat
        mine = msg(item_key="m1", text="@我 请把稿子发我", mentions_me=True, sent_at=now)
        alls = msg(item_key="m2", text="@所有人 周日前交作业", mentions_me=True, sent_at=now)  # collector detects @all
        other = msg(item_key="m3", text="大家周末愉快", sent_at=now)
        direct = msg(item_key="m4", text="那个文件你发我一下", is_group=False, sent_at=now)
        ids = self.store.add_messages([mine, alls, other, direct])
        u_me = U.upsert(self.store, "u1", kind="note", title="mention"); U.attach(self.store, u_me, [ids[0]])
        u_all = U.upsert(self.store, "u2", kind="announcement", title="all"); U.attach(self.store, u_all, [ids[1]])
        u_other = U.upsert(self.store, "u3", kind="topic", title="chatter"); U.attach(self.store, u_other, [ids[2]])
        u_direct = U.upsert(self.store, "u4", kind="todo", title="direct"); U.attach(self.store, u_direct, [ids[3]])
        u_hw = U.upsert(self.store, "u5", kind="todo", title="homework"); U.add_tags(self.store, u_hw, ["type:homework"])
        self.store.db.commit()
        self.assertEqual(U.recompute_for_me(self.store), 4)        # mention, @all, direct, homework — not the plain topic
        forme = {u["id"] for u in U.query(self.store, mine=True)}
        self.assertEqual(forme, {u_me, u_all, u_direct, u_hw})
        self.assertNotIn(u_other, forme)
        return

    def test_polish_rewrites_titles(self):
        now = int(time.time())
        ids = self.store.add_messages([msg(item_key="p1", text="当然可以的，这一批是今天晚上截止哦[Grin]", sent_at=now)])
        assign_segments(self.store, ids, 30)
        rule_organizer.run(self.cfg, self.store)
        todo = U.query(self.store, kind="todo")[0]
        self.assertIn("当然可以的", todo["title"])            # raw chat line before polishing
        self.cfg["ai"].update(enabled=True, price_usd_per_mtok={"input": 1.0, "output": 2.0})

        class Fake:
            def complete(self, payload, system=None, schema=None):
                self.payload, self.system = payload, system
                items = [{"id": i["id"], "title": "这一批今晚截止", "summary": "今晚是提交期限。"} for i in payload["items"]]
                return providers.Reply(text=json.dumps({"items": items}), stop="end",
                                       usage={"input": 300, "output": 60, "cache_read": 0, "cache_write": 0})
        fake = Fake()
        out = polish.run(self.cfg, self.store, provider=fake)
        self.assertEqual(out["polished"], 1)
        again = U.get(self.store, todo["id"])
        self.assertEqual(again["title"], "这一批今晚截止")
        self.assertEqual(again["summary"], "今晚是提交期限。")
        self.assertEqual(polish.run(self.cfg, self.store, provider=fake)["polished"], 0)   # not redone
        self.assertIn("at most 20 characters", fake.system)

    def test_polish_respects_policies_and_failure_is_harmless(self):
        now = int(time.time())
        ids = self.store.add_messages([msg(item_key="q1", text="记得明天交作业", policy="digest", sent_at=now)])
        assign_segments(self.store, ids, 30)
        rule_organizer.run(self.cfg, self.store)
        self.cfg["ai"].update(enabled=True, polish_policies=["watch"])          # digest chat is out of scope
        self.assertEqual(polish.run(self.cfg, self.store)["polished"], 0)
        self.cfg["ai"]["polish_policies"] = ["watch", "digest"]

        class Down:
            def complete(self, payload, system=None, schema=None):
                raise providers.ProviderError("connection", retryable=True)
        before = U.query(self.store, kind="todo")[0]["title"]
        self.assertIn("error", polish.run(self.cfg, self.store, provider=Down()))
        self.assertEqual(U.query(self.store, kind="todo")[0]["title"], before)   # untouched


class Chat(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "hub.db")
        self.cfg = cfg()
        self.cfg["ai"].update(enabled=True, provider="openai_compatible", base_url="http://x", api_key_env="K",
                              price_usd_per_mtok={"input": 1.0, "output": 2.0})
        os.environ["K"] = "test"

    def tearDown(self):
        os.environ.pop("K", None)
        self.store.db.close()
        self.tmp.cleanup()

    def test_tool_loop_creates_a_unit(self):
        from infohub.apps import chat as chatmod
        # scripted API: first turn asks to create a unit; second turn replies with a summary
        turns = [
            {"choices": [{"message": {"content": "", "tool_calls": [
                {"id": "t1", "function": {"name": "create_unit",
                 "arguments": json.dumps({"kind": "todo", "title": "Buy milk", "tags": ["project:home"]})}}]}}],
             "usage": {"prompt_tokens": 100, "completion_tokens": 20}},
            {"choices": [{"message": {"content": "Created the todo 'Buy milk'."}}],
             "usage": {"prompt_tokens": 120, "completion_tokens": 10}},
        ]
        chatmod._post = lambda ai, messages, tools=True: turns.pop(0)
        out = chatmod.chat(self.cfg, self.store, [{"role": "user", "content": "add a todo to buy milk"}])
        self.assertEqual(out["changes"][0]["action"], "created")
        self.assertIn("Buy milk", out["reply"])
        u = U.query(self.store, kind="todo")[0]
        self.assertEqual(u["title"], "Buy milk")
        self.assertIn("project:home", u["tags"])
        self.assertGreater(out["cost"], 0)

    def test_disabled_without_key(self):
        from infohub.apps import chat as chatmod
        os.environ.pop("K", None)
        self.assertIn("environment variable", chatmod.chat(self.cfg, self.store, [{"role": "user", "content": "hi"}])["error"])


class Settings(unittest.TestCase):
    def test_local_api_settings_and_key_precedence(self):
        import infohub.config as cm
        from infohub.organize.providers import api_key
        with tempfile.TemporaryDirectory() as tmp:
            orig = cm.SETTINGS_FILE
            cm.SETTINGS_FILE = Path(tmp) / "settings.local.json"
            try:
                saved = cm.write_settings({"ai.base_url": "https://example.test/v1/", "ai.model": "test-model",
                                           "ai.api_key": "test-private-key"})
                self.assertEqual(saved["ai.base_url"], "https://example.test/v1")
                self.assertEqual(api_key(cm._nest(saved)["ai"]), "test-private-key")
                cm.write_settings({"ai.daily_budget_usd": 10})
                self.assertEqual(cm.read_settings()["ai.api_key"], "test-private-key")
                for url in ("file:///tmp/key", "https://user:password@example.test", "https://example.test?key=secret"):
                    with self.assertRaises(ValueError):
                        cm.write_settings({"ai.base_url": url})
            finally:
                cm.SETTINGS_FILE = orig

    def test_overlay_and_whitelist(self):
        import infohub.config as cm
        with tempfile.TemporaryDirectory() as tmp:
            orig = cm.SETTINGS_FILE
            cm.SETTINGS_FILE = Path(tmp) / "settings.local.json"
            try:
                cm.write_settings({"ai.enabled": True, "me.names": ["测试用户"]})
                self.assertEqual(cm.read_settings()["me.names"], ["测试用户"])
                merged = cm._merge(copy.deepcopy(cm.DEFAULTS), cm._nest(cm.read_settings()))
                self.assertTrue(merged["ai"]["enabled"])
                self.assertEqual(merged["me"]["names"], ["测试用户"])
                self.assertEqual(merged["ai"]["model"], cm.DEFAULTS["ai"]["model"])    # untouched keys survive
                with self.assertRaises(ValueError):
                    cm.write_settings({"hub.database": "C:/evil"})            # not editable
            finally:
                cm.SETTINGS_FILE = orig

    def test_me_names_accepts_a_list_or_a_delimited_string(self):
        import infohub.config as cm
        with tempfile.TemporaryDirectory() as tmp:
            orig = cm.SETTINGS_FILE
            cm.SETTINGS_FILE = Path(tmp) / "settings.local.json"
            try:
                cm.write_settings({"me.names": "Alice, Bob，alice"})
                self.assertEqual(cm.read_settings()["me.names"], ["Alice", "Bob"])
                cm.write_settings({"me.names": [" 张三 ", "", "张三", "San"]})
                self.assertEqual(cm.read_settings()["me.names"], ["张三", "San"])
                self.assertEqual(cm._merge(copy.deepcopy(cm.DEFAULTS),
                                           cm._nest(cm.read_settings()))["me"]["names"], ["张三", "San"])
                with self.assertRaises(ValueError):
                    cm.write_settings({"me.names": {"nope": 1}})
            finally:
                cm.SETTINGS_FILE = orig


class MyNames(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "hub.db")

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_apply_names_updates_existing_messages_and_units(self):
        now = int(time.time())
        ids = self.store.add_messages([
            msg(item_key="m1", text="please review @Alice", sent_at=now),
            msg(item_key="m2", text="ping @Bob", sent_at=now),
            msg(item_key="m3", text="ping @Carol", sent_at=now),
        ])
        units = {}
        for name, mid in zip(("Alice", "Bob", "Carol"), ids):
            units[name] = U.upsert(self.store, name, kind="note", title=name)
            U.attach(self.store, units[name], [mid])
        self.store.db.commit()

        U.apply_me_names(self.store, ["Alice", "Bob"])
        self.assertEqual({n: U.get(self.store, uid)["for_me"] for n, uid in units.items()},
                         {"Alice": 1, "Bob": 1, "Carol": 0})

        # Removing a name clears its old mention flag and its For me membership.
        U.apply_me_names(self.store, ["Bob"])
        self.assertEqual(self.store.q("SELECT mentions_me FROM messages WHERE id=?", (ids[0],)).fetchone()[0], 0)
        self.assertEqual(U.get(self.store, units["Alice"])["for_me"], 0)
        self.assertEqual(U.get(self.store, units["Bob"])["for_me"], 1)

        # @everyone still matches with no personal names configured.
        self.store.add_messages([msg(item_key="m4", text="@everyone standup", sent_at=now)])
        self.store.db.commit()
        U.apply_me_names(self.store, [])
        self.assertEqual(self.store.q("SELECT mentions_me FROM messages WHERE item_key='m4'").fetchone()[0], 1)

    def test_names_are_matched_literally(self):
        now = int(time.time())
        ids = self.store.add_messages([
            msg(item_key="lit1", text="progress @A_B here", sent_at=now),
            msg(item_key="lit2", text="progress @AXB here", sent_at=now),
        ])
        self.store.db.commit()
        U.apply_me_names(self.store, ["A_B"])
        flags = [self.store.q("SELECT mentions_me FROM messages WHERE id=?", (mid,)).fetchone()[0] for mid in ids]
        self.assertEqual(flags, [1, 0])



class Layer3(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "hub.db")
        self.cfg = cfg()

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_today_brief_search_reminders(self):
        now = int(time.time())
        self.store.add_messages([msg(item_key="x", text="会议改到明天 DDL 提醒", sent_at=now - 60)])
        todo = U.upsert(self.store, "t", kind="todo", title="Submit homework 2", due_at=now + 3600)
        U.add_tags(self.store, todo, ["type:todo", "course:ai-eng"])
        ev = U.upsert(self.store, "e", kind="event", title="Group meeting", start_at=now + 1800)
        self.store.db.commit()
        d = today(self.store)
        self.assertEqual([u["id"] for u in d["needs"]], [todo])          # to-do lane is units only
        self.assertEqual([u["id"] for u in d["events"]], [ev])
        # a flagged message nobody organized yet stays visible, and can be promoted
        self.assertEqual([m["text"] for m in d["unsorted"]], ["会议改到明天 DDL 提醒"])
        U.attach(self.store, todo, [d["unsorted"][0]["id"]])
        self.store.db.commit()
        self.assertEqual(today(self.store)["unsorted"], [])              # once it has a unit, it drops out
        self.assertEqual(len(search(self.store, "会议")), 1)
        # general search also finds people, chats and tags
        self.store.upsert_channel("lark", "org1", "oc_lab", "Robotics Lab", 1, "watch")
        self.store.add_messages([msg(channel="oc_lab", channel_name="Robotics Lab", source="lark", account="org1",
                                     sender="u9", sender_name="Alice Chen", text="hello team", sent_at=now)])
        r = search_all(self.store, "Robotics")
        self.assertEqual([c["channel_name"] for c in r["channels"]], ["Robotics Lab"])
        self.assertEqual([p["sender_name"] for p in search_all(self.store, "Alice")["people"]], ["Alice Chen"])
        self.assertTrue(any(t["tag"] == "course:ai-eng" for t in search_all(self.store, "ai-eng")["tags"]))
        md = brief.brief(self.cfg, self.store, 7, ["course:ai-eng"])
        self.assertIn("Submit homework 2", md)
        self.assertNotIn("Group meeting", md)
        sent = []
        self.assertEqual(reminders.run(self.cfg, self.store, notify=lambda a, b: sent.append(b) or True)["reminded"], 2)
        self.assertEqual(reminders.run(self.cfg, self.store, notify=lambda a, b: True)["reminded"], 0)   # once only
        self.assertEqual(summary(self.store, 24)["counts"][0]["messages"], 1)        # Ignore a unit (status dropped) leaves every list; every unit carries an activity time
        self.assertIsNotNone(U.query(self.store, kind="todo")[0]["last_activity_at"])
        U.update(self.store, todo, status="dropped"); self.store.db.commit()
        self.assertEqual([u["id"] for u in today(self.store)["needs"]], [])
        self.assertEqual(U.query(self.store, kind="todo"), [])
        self.assertEqual(len(U.query(self.store, kind="todo", status="dropped")), 1)


if __name__ == "__main__":
    unittest.main()
