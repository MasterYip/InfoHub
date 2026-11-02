import copy
import json
import http.client
import threading
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from infohub import config
from infohub.organize import agent_tasks as tasks, rule_organizer, units
from infohub.store import Store


class FakeCodex:
    def __init__(self, events=None, error=None):
        self.calls = []
        self.events = list(events or [
            {"method": "item/completed", "params": {"threadId": "thread-1", "item": {"type": "agentMessage", "text": "Finished"}}},
            {"method": "turn/completed", "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}}}])
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def call(self, method, params):
        self.calls.append((method, params))
        if self.error and method == "turn/start":
            raise self.error
        if method in ("thread/start", "thread/resume"):
            return {"thread": {"id": "thread-1"}}
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        return {}

    def next_event(self, timeout):
        item = self.events.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class AgentTasks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["hub"]["database"] = str(self.root / "hub.db")
        self.cfg["agent_tasks"].update(enabled=True, enabled_since=1000)
        self.store = Store(self.cfg["hub"]["database"])
        self.key = 0
        self.cfg["ai"]["enabled"] = True
        def prepare_reply(payload, **kwargs):
            result = {"unit_ids": []} if "candidates" in payload else {"brief": "Use the agreed task context."}
            return SimpleNamespace(text=json.dumps(result), usage={}, stop="end")
        self.context_provider = SimpleNamespace(complete=prepare_reply)
        self.provider_patch = patch("infohub.organize.task_context.providers.make", return_value=self.context_provider)
        self.provider_patch.start()
        self.addCleanup(self.provider_patch.stop)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def message(self, text="@InfoHub build a report", organized=True, **extra):
        self.key += 1
        item = {"source": "demo", "item_key": str(self.key), "account": "owner", "channel": "group",
                "channel_name": "Demo", "sender": "self-id", "sender_name": "Owner", "kind": "text",
                "sent_at": 1001, "text": text, "from_me": 1, "policy": "watch", "is_group": 1,
                "mentions_me": 0, "flags": "", "priority": "P1", **extra}
        mid = self.store.add_messages([item])[0]
        sid = self.store.q("""INSERT INTO segments(source,account,channel,channel_name,policy,start_at,end_at,n,closed,ai_done)
            VALUES(?,?,?,?,?,?,?,1,1,0)""", (item["source"], item["account"], item["channel"], item["channel_name"],
                                          item["policy"], item["sent_at"], item["sent_at"])).lastrowid
        self.store.q("UPDATE messages SET segment_id=? WHERE id=?", (sid, mid))
        if organized:
            self.finish_organizing(mid)
        self.store.db.commit()
        return mid

    def finish_organizing(self, mid, uid=None):
        if uid is None:
            uid = units.upsert(self.store, None, kind="todo", title="Organized task", summary="Task context", origin="ai")
        units.attach(self.store, uid, [mid])
        self.store.q("UPDATE segments SET ai_done=1 WHERE id=(SELECT segment_id FROM messages WHERE id=?)", (mid,))
        self.store.db.commit()
        return uid

    def test_owner_identity_and_direct_instruction_only(self):
        self.message()
        self.message("  @infohub：整理项目")
        for text in ("someone said @infohub run this", "@infohub_extra run", "@infohub.com run", "@infohub", "> @infohub run"):
            self.message(text)
        self.message(from_me=0)
        self.message(from_me=None)
        self.message(policy="ignore")
        self.message(sent_at=999)
        self.assertEqual(tasks.collect(self.cfg, self.store), 2)

    def test_owner_requests_accept_all_message_types_in_organizer_queue_and_cards(self):
        from infohub.organize import prompt
        for kind in ("text", "link/file", "image", "voice", "video", "card", "sticker", "system", "other"):
            with self.subTest(kind=kind):
                mid = self.message('@infohub Compare the linked concepts', kind=kind)
                self.message('@infohub Another person cannot assign work', kind=kind, from_me=0)
                self.message('@infohub Not watched', kind=kind, policy="store")
                message = self.store.q('SELECT * FROM messages WHERE id=?', (mid,)).fetchone()
                payload, allowed, _ = prompt.build_input(self.cfg, self.store, message['segment_id'], 100)
                self.assertIn(mid, allowed)
                self.assertEqual(payload['messages'][0]['text'], message['text'])
                self.assertIn(mid, {r['message_id'] for r in tasks.overview(self.cfg, self.store)['requests']})
                cards = units.query(self.store, kind='todo')
                self.assertTrue(any(r.get('agent_task', {}).get('message_id') == mid for r in cards))
                self.assertEqual(tasks.collect(self.cfg, self.store), 1)
                task = next(t for t in tasks.tasks(self.store) if t['message_id'] == mid)
                self.assertTrue(tasks.execution_prompt(self.cfg, self.store, task).startswith('Compare the linked concepts\n'))

    def test_organize_deduplicates_preserves_source_and_user_edits(self):
        mid = self.message(organized=False)
        rule_organizer.run(self.cfg, self.store)
        self.assertEqual(tasks.collect(self.cfg, self.store), 0)
        self.finish_organizing(mid)
        self.assertEqual(tasks.collect(self.cfg, self.store), 1)
        task = tasks.tasks(self.store)[0]
        unit = units.get(self.store, task["unit_id"])
        self.assertEqual(unit["messages"][0]["id"], mid)
        self.assertEqual(unit["kind"], "todo")
        self.assertTrue(unit["for_me"])
        units.update(self.store, unit["id"], title="Edited title", status="done")
        self.store.db.commit()
        self.store.set_cursor("organize:rule:msg", 0)
        rule_organizer.run(self.cfg, self.store)
        self.assertEqual(tasks.collect(self.cfg, self.store), 0)
        self.assertEqual(units.get(self.store, unit["id"])["title"], "Edited title")
        self.assertFalse(tasks.run_next(self.cfg, self.store))

    def test_disabled_and_uninitialized_do_not_backfill(self):
        self.message()
        self.cfg["agent_tasks"]["enabled"] = False
        self.assertEqual(tasks.collect(self.cfg, self.store), 0)
        self.cfg["agent_tasks"].update(enabled=True, enabled_since=0)
        self.assertEqual(tasks.collect(self.cfg, self.store), 0)

    def test_completion_and_long_silent_period(self):
        self.cfg["agent_tasks"]["sandbox"] = "danger-full-access"
        self.message()
        tasks.collect(self.cfg, self.store)
        client = FakeCodex()
        client.events.insert(0, TimeoutError())
        self.assertTrue(tasks.run_next(self.cfg, self.store, lambda _: client))
        task = tasks.tasks(self.store)[0]
        self.assertEqual((task["status"], task["result"]), ("completed", "Finished"))
        self.assertEqual(task["thread_id"], "thread-1")
        self.assertEqual(units.get(self.store, task["unit_id"])["status"], "done")
        self.assertEqual(client.calls[0][1]["approvalPolicy"], "on-request")
        self.assertEqual(client.calls[0][1]["sandbox"], "danger-full-access")
        self.assertTrue(Path(task["workspace"]).is_dir())
        self.assertFalse(tasks.run_next(self.cfg, self.store, lambda _: client))

    def test_approval_stops_without_authorizing(self):
        self.message()
        tasks.collect(self.cfg, self.store)
        client = FakeCodex(events=[{"id": 99, "method": "item/commandExecution/requestApproval", "params": {"threadId": "thread-1"}}])
        tasks.run_next(self.cfg, self.store, lambda _: client)
        task = tasks.tasks(self.store)[0]
        self.assertEqual(task["status"], "needs_attention")
        self.assertEqual(units.get(self.store, task["unit_id"])["status"], "open")
        self.assertEqual(client.calls[-1][0], "turn/interrupt")

    def test_uncertain_dispatch_is_not_replayed(self):
        self.message()
        tasks.collect(self.cfg, self.store)
        client = FakeCodex(error=TimeoutError("Connection lost"))
        tasks.run_next(self.cfg, self.store, lambda _: client)
        task = tasks.tasks(self.store)[0]
        self.assertEqual(task["status"], "needs_attention")
        self.assertEqual(task["thread_id"], "thread-1")
        self.assertFalse(tasks.run_next(self.cfg, self.store, lambda _: client))

    def test_settings_activation_cutoff_and_workspace_validation(self):
        with patch.object(config, "SETTINGS_FILE", self.root / "settings.json"):
            with patch("time.time", return_value=2000):
                saved = config.write_settings({"agent_tasks.enabled": True})
            self.assertEqual(saved["agent_tasks.enabled_since"], 2000)
            with patch("time.time", return_value=3000):
                saved = config.write_settings({"agent_tasks.enabled": True})
            self.assertEqual(saved["agent_tasks.enabled_since"], 2000)
            with self.assertRaises(ValueError):
                config.write_settings({"agent_tasks.enabled": "true"})
            with self.assertRaises(ValueError):
                config.write_settings({"agent_tasks.workspace": "relative/path"})
            with self.assertRaises(ValueError):
                config.write_settings({"agent_tasks.sandbox": "unknown"})

    def test_second_worker_cannot_claim_running_task(self):
        self.message()
        tasks.collect(self.cfg, self.store)
        task = tasks.tasks(self.store)[0]
        tasks.update(self.store, task["id"], status="running")
        other = Store(self.cfg["hub"]["database"])
        try:
            self.assertFalse(tasks.run_next(self.cfg, other, lambda _: self.fail("Duplicated execution")))
        finally:
            other.db.close()

    def test_non_command_mentions_cannot_starve_later_tasks(self):
        for i in range(100):
            self.message('A note about @infohub ' + str(i))
        self.message('@infohub A real task')
        self.assertEqual(tasks.collect(self.cfg, self.store), 1)

    def test_overview_explains_unqueued_owner_requests_and_ignores_other_people(self):
        mid = self.message(organized=False)
        uid = units.upsert(self.store, None, kind="todo", title="Existing organized task", origin="ai")
        units.attach(self.store, uid, [mid])
        other = self.message(from_me=0, organized=False)
        other_uid = units.upsert(self.store, None, kind="todo", title="Another person's request")
        units.attach(self.store, other_uid, [other])
        self.store.db.commit()
        self.cfg["agent_tasks"]["enabled"] = False
        overview = tasks.overview(self.cfg, self.store)
        self.assertEqual(len(overview["requests"]), 1)
        self.assertEqual(overview["requests"][0]["error"], "Automatic tasks are disabled")
        rows = {u["id"]: u for u in units.query(self.store, kind="todo")}
        self.assertEqual(rows[uid]["agent_task"]["status"], "not_queued")
        self.assertNotIn("agent_task", rows[other_uid])
        self.cfg["agent_tasks"].update(enabled=True, enabled_since=2000)
        self.assertEqual(tasks.overview(self.cfg, self.store)["requests"][0]["error"], "Message predates activation")
        self.assertEqual(tasks.collect(self.cfg, self.store), 0)

    def test_task_reuses_existing_todo_and_list_exposes_session_state(self):
        mid = self.message(organized=False)
        uid = units.upsert(self.store, None, kind="todo", title="Existing organized task", origin="ai")
        self.finish_organizing(mid, uid)
        self.assertEqual(tasks.collect(self.cfg, self.store), 1)
        task = tasks.tasks(self.store)[0]
        self.assertEqual(task["unit_id"], uid)
        tasks.update(self.store, task["id"], status="running", thread_id="thread-1")
        result = units.query(self.store, kind="todo")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["agent_task"]["status"], "running")
        self.assertEqual(result[0]["agent_task"]["thread_id"], "thread-1")
        overview = tasks.overview(self.cfg, self.store)
        self.assertEqual(overview["counts"], {"running": 1})
        self.assertEqual(overview["requests"], [])
        from infohub.apps.server import viewer_payload
        self.assertNotIn("agent_task", viewer_payload(self.store)["units"][0])

    def test_watch_groups_and_direct_chats_only_after_ai_context_is_ready(self):
        late = self.message(channel="group-A", organized=False)
        direct = self.message(channel="direct-B", is_group=0)
        for policy in ("store", "digest", "ignore"):
            self.message(channel=policy, policy=policy)
        self.message(channel="group-C", from_me=0)
        self.assertEqual(tasks.collect(self.cfg, self.store), 1)
        self.assertEqual(tasks.tasks(self.store)[0]["message_id"], direct)
        self.finish_organizing(late)
        self.assertEqual(tasks.collect(self.cfg, self.store), 1)
        self.assertEqual({t["message_id"] for t in tasks.tasks(self.store)}, {late, direct})

    def test_execution_includes_organized_context_from_the_same_chat(self):
        mid = self.message('@infohub Find this paper', organized=False)
        uid = self.finish_organizing(mid)
        followup = self.message('HumanPlus, include its project page', organized=False)
        unrelated = self.message('Private context in another chat', channel="elsewhere", organized=False)
        units.attach(self.store, uid, [followup, unrelated])
        units.update(self.store, uid, summary="Find HumanPlus and its project page")
        self.store.db.commit()
        self.assertEqual(tasks.collect(self.cfg, self.store), 1)
        client = FakeCodex()
        tasks.run_next(self.cfg, self.store, lambda _: client)
        sent = next(p["input"][0]["text"] for m,p in client.calls if m == "turn/start")
        self.assertTrue(sent.startswith('Find this paper\n'))
        self.assertIn('Find HumanPlus and its project page', sent)
        self.assertIn('HumanPlus, include its project page', sent)
        self.assertNotIn('Private context in another chat', sent)

    def test_ai_organizer_completion_unlocks_dispatch(self):
        from types import SimpleNamespace
        from infohub.organize import ai_organizer
        mid = self.message(organized=False)
        sid = self.store.q("SELECT segment_id FROM messages WHERE id=?", (mid,)).fetchone()[0]
        self.store.q("INSERT INTO jobs(kind,ref_id,status,created_at,updated_at) VALUES('segment',?,'pending',0,0)", (sid,))
        self.store.db.commit()
        result = {"units":[{"ref":"task", "kind":"todo", "title":"Prepare report", "summary":"Include the agreed context",
                            "message_ids":[mid]}], "close_units":[]}
        def complete(payload):
            self.assertTrue(payload["messages"][0]["from_me"])
            self.assertEqual(tasks.collect(self.cfg, self.store), 0)
            return SimpleNamespace(text=json.dumps(result),usage={},stop="end")
        job = self.store.q("SELECT * FROM jobs").fetchone()
        ai_organizer.process_job(self.cfg, self.store, SimpleNamespace(complete=complete), job)
        self.assertEqual(tasks.collect(self.cfg, self.store), 1)

    def test_context_selection_reads_related_nodes_before_codex_and_caches_brief(self):
        mid = self.message('@infohub Follow the agreed plan', organized=False)
        uid = self.finish_organizing(mid)
        parent = units.upsert(self.store, None, kind="topic", title="Research plan", summary="Agreed deliverables")
        pm = self.message('Use a two-page report', organized=False, from_me=0)
        units.attach(self.store, parent, [pm])
        units.link(self.store, parent, uid)
        resource = units.upsert(self.store, None, kind="resource", title="Paper materials")
        rm = self.message('https://example.org/paper.pdf', channel="research", organized=False, from_me=0)
        units.attach(self.store, resource, [rm])
        units.link(self.store, parent, resource)
        excluded = units.upsert(self.store, None, kind="note", title="Not selected")
        em = self.message('Unrelated raw evidence', channel="unrelated", organized=False, from_me=0)
        units.attach(self.store, excluded, [em])
        units.link(self.store, parent, excluded)
        hidden = units.upsert(self.store, None, kind="note", title="Store-only material")
        hm = self.message('Not watched', policy="store", organized=False, from_me=0)
        units.attach(self.store, hidden, [hm])
        units.link(self.store, parent, hidden)
        self.store.db.commit()
        tasks.collect(self.cfg, self.store)
        calls = []
        def complete(payload, **kwargs):
            calls.append(payload)
            if "candidates" in payload:
                ids = {r["id"] for r in payload["candidates"]}
                self.assertTrue({uid, parent, resource, excluded} <= ids)
                self.assertNotIn(hidden, ids)
                data = {"unit_ids": [parent, resource]}
            else:
                self.assertEqual({r["id"] for r in payload["related_units"]}, {uid, parent, resource})
                self.assertIn('https://example.org/paper.pdf', json.dumps(payload))
                self.assertNotIn('Unrelated raw evidence', json.dumps(payload))
                data = {"brief": f"Prepare two pages using the paper [unit:{resource}] [message:{pm}]."}
            return SimpleNamespace(text=json.dumps(data), usage={"input": 20, "output": 10}, stop="end")
        self.context_provider.complete = complete
        client = FakeCodex()
        tasks.run_next(self.cfg, self.store, lambda _: client)
        self.assertEqual(len(calls), 2)
        sent = next(p["input"][0]["text"] for m,p in client.calls if m == "turn/start")
        self.assertTrue(sent.startswith('Follow the agreed plan\n'))
        self.assertIn('Prepare two pages', sent)
        self.assertIn('https://example.org/paper.pdf', sent)
        task = tasks.tasks(self.store)[0]
        self.assertEqual(tasks.execution_prompt(self.cfg, self.store, task), sent)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.store.q('SELECT sum(requests) FROM llm_usage').fetchone()[0], 2)

    def test_context_failure_never_starts_codex(self):
        self.message()
        tasks.collect(self.cfg, self.store)
        task = tasks.tasks(self.store)[0]
        for result in ({"unit_ids": [999999]}, {"unit_ids": "all"}, {"brief": ""}):
            with self.subTest(result=result):
                tasks.update(self.store, task["id"], status="pending")
                def complete(payload, **kwargs):
                    data = {"unit_ids": []} if "brief" in result and "candidates" in payload else result
                    return SimpleNamespace(text=json.dumps(data), usage={}, stop="end")
                self.context_provider.complete = complete
                client = FakeCodex()
                tasks.run_next(self.cfg, self.store, lambda _: client)
                self.assertEqual(client.calls, [])
                self.assertEqual(tasks.tasks(self.store)[0]["status"], "needs_attention")
        tasks.update(self.store, task["id"], status="pending")
        self.cfg["ai"]["daily_budget_usd"] = 0
        client = FakeCodex()
        tasks.run_next(self.cfg, self.store, lambda _: client)
        self.assertEqual(client.calls, [])
        self.assertIn("budget", tasks.tasks(self.store)[0]["error"])

    def test_pause_or_delete_during_context_preparation_prevents_dispatch(self):
        mid = self.message()
        tasks.collect(self.cfg, self.store)
        task = tasks.tasks(self.store)[0]
        for action, expected in (("pause", "paused"), ("delete", "deleted")):
            with self.subTest(action=action):
                tasks.update(self.store, task["id"], status="pending", thread_id="previous", turn_id="previous-turn")
                def complete(payload, **kwargs):
                    tasks.control(self.cfg, self.store, mid, action)
                    return SimpleNamespace(text='{"unit_ids": []}', usage={}, stop="end")
                self.context_provider.complete = complete
                client = FakeCodex()
                tasks.run_next(self.cfg, self.store, lambda _: client)
                self.assertEqual(client.calls, [])
                self.assertEqual(self.store.q("SELECT status FROM agent_tasks WHERE id=?", (task["id"],)).fetchone()[0], expected)

    def test_manual_start_pause_and_delete_unqueued_requests(self):
        mid = self.message(organized=False, sent_at=999)
        started = tasks.control(self.cfg, self.store, mid, "start")
        self.assertEqual(started["status"], "waiting")
        self.assertTrue(started["organize"])
        self.assertFalse(tasks.control(self.cfg, self.store, mid, "start")["organize"])
        tasks.control(self.cfg, self.store, mid, "pause")
        self.finish_organizing(mid)
        tasks.collect(self.cfg, self.store)
        self.assertEqual(tasks.tasks(self.store)[0]["status"], "paused")
        self.assertEqual(tasks.control(self.cfg, self.store, mid, "start")["status"], "pending")
        tasks.control(self.cfg, self.store, mid, "delete")
        self.assertEqual(tasks.tasks(self.store), [])
        self.assertEqual(tasks.overview(self.cfg, self.store)["requests"], [])
        self.assertEqual(tasks.collect(self.cfg, self.store), 0)
        self.assertNotIn("agent_task", units.query(self.store, kind="todo")[0])
        with self.assertRaises(ValueError):
            tasks.control(self.cfg, self.store, mid, "start")
        uid = self.store.q("SELECT unit_id FROM agent_tasks WHERE message_id=?", (mid,)).fetchone()[0]
        fresh = self.message(organized=False)
        self.finish_organizing(fresh, uid)
        self.assertEqual(tasks.collect(self.cfg, self.store), 1)
        self.assertEqual(tasks.tasks(self.store)[0]["message_id"], fresh)

    def test_manual_waiting_request_promotes_after_organization(self):
        mid = self.message(organized=False)
        tasks.control(self.cfg, self.store, mid, "start")
        self.assertEqual(tasks.collect(self.cfg, self.store), 0)
        uid = self.finish_organizing(mid)
        tasks.collect(self.cfg, self.store)
        task = tasks.tasks(self.store)[0]
        self.assertEqual((task["status"],task["unit_id"]), ("pending",uid))

    def test_controls_cannot_authorize_other_senders_or_nonwatch_chats(self):
        for extra in ({"from_me":0}, {"policy":"store"}, {"policy":"ignore"}):
            mid = self.message(**extra)
            for action in ("start","pause","delete"):
                with self.assertRaises(ValueError):
                    tasks.control(self.cfg, self.store, mid, action)

    def test_running_pause_resumes_same_session_and_delete_waits_for_stop(self):
        for action,target in (("pause","paused"),("delete","deleted")):
            with self.subTest(action=action):
                mid = self.message()
                tasks.collect(self.cfg, self.store)
                client = FakeCodex()
                original = client.call
                def call(method,params):
                    response = original(method,params)
                    if method == "turn/start":
                        tasks.control(self.cfg, self.store, mid, action)
                    if method == "turn/interrupt":
                        state = self.store.q("SELECT status FROM agent_tasks WHERE message_id=?", (mid,)).fetchone()[0]
                        self.assertEqual(state, "pausing" if action=="pause" else "deleting")
                        client.events = [{"method":"turn/completed","params":{"threadId":"thread-1",
                            "turn":{"id":"turn-1","status":"interrupted"}}}]
                    return response
                client.call = call
                tasks.run_next(self.cfg, self.store, lambda _:client)
                task = dict(self.store.q("SELECT * FROM agent_tasks WHERE message_id=?", (mid,)).fetchone())
                self.assertEqual(task["status"], target)
                self.assertEqual(units.get(self.store,task["unit_id"])["status"], "open")
                self.assertIn("turn/interrupt", [m for m,p in client.calls])
                if action=="pause":
                    tasks.control(self.cfg,self.store,mid,"start")
                    resumed = FakeCodex()
                    tasks.run_next(self.cfg,self.store,lambda _:resumed)
                    self.assertEqual(resumed.calls[0][0], "thread/resume")
                    self.assertEqual(resumed.calls[0][1]["threadId"], "thread-1")

    def test_pause_during_thread_creation_does_not_start_a_turn(self):
        mid = self.message()
        tasks.collect(self.cfg,self.store)
        client = FakeCodex()
        original = client.call
        def call(method,params):
            if method=="thread/start":
                tasks.control(self.cfg,self.store,mid,"pause")
            return original(method,params)
        client.call = call
        tasks.run_next(self.cfg,self.store,lambda _:client)
        self.assertNotIn("turn/start", [m for m,p in client.calls])
        self.assertEqual(tasks.tasks(self.store)[0]["status"], "paused")

    def test_interrupt_retries_until_new_turn_is_active(self):
        mid = self.message()
        tasks.collect(self.cfg,self.store)
        client = FakeCodex(events=[TimeoutError()])
        original = client.call
        attempts = []
        def call(method,params):
            result = original(method,params)
            if method=="turn/start":
                tasks.control(self.cfg,self.store,mid,"delete")
            if method=="turn/interrupt":
                attempts.append(1)
                if len(attempts)==1:
                    raise RuntimeError("no active turn to interrupt")
                client.events = [{"method":"turn/completed","params":{"threadId":"thread-1",
                    "turn":{"id":"turn-1","status":"interrupted"}}}]
            return result
        client.call = call
        tasks.run_next(self.cfg,self.store,lambda _:client)
        self.assertEqual(len(attempts),2)
        self.assertEqual(self.store.q("SELECT status FROM agent_tasks WHERE message_id=?",(mid,)).fetchone()[0],"deleted")

    def test_delete_without_session_or_source_is_idempotent(self):
        mid = self.message(organized=False)
        self.assertEqual(tasks.control(self.cfg,self.store,mid,"delete")["status"],"deleted")
        self.store.q("DELETE FROM messages WHERE id=?",(mid,))
        self.store.db.commit()
        self.assertEqual(tasks.control(self.cfg,self.store,mid,"delete")["status"],"deleted")
        self.assertEqual(tasks.control(self.cfg,self.store,999999,"delete")["status"],"deleted")
        self.assertEqual(tasks.tasks(self.store),[])

    def test_delete_missing_codex_session_hides_item_but_other_errors_remain_visible(self):
        for error,expected in (("thread not found: thread-1","deleted"),
                               ("Session thread-1 not found","deleted"),
                               ("no rollout found for thread id thread-1","deleted"),
                               ("Connection failed","needs_attention"),
                               ("Tool executable not found","needs_attention")):
            with self.subTest(error=error):
                mid=self.message()
                tasks.collect(self.cfg,self.store)
                client=FakeCodex()
                original=client.call
                def call(method,params):
                    result=original(method,params)
                    if method=="turn/start": tasks.control(self.cfg,self.store,mid,"delete")
                    if method=="turn/interrupt": raise RuntimeError(error)
                    return result
                client.call=call
                tasks.run_next(self.cfg,self.store,lambda _:client)
                row=self.store.q("SELECT status,error FROM agent_tasks WHERE message_id=?",(mid,)).fetchone()
                self.assertEqual(row["status"],expected)
                if expected=="deleted": self.assertIsNone(row["error"])

    def test_http_delete_unqueued_item_without_codex_session(self):
        from infohub.apps import server
        mid = self.message(organized=False)
        self.cfg["hub"]["dashboard"]["port"] = 0
        self.cfg["hub"]["auth"]["users_file"] = str(self.root / "users.json")
        self.cfg["hub"]["backup"].update(enabled=False,directory=str(self.root / "backups"))
        captured, ready = [], threading.Event()
        factory = server.ThreadingHTTPServer
        def capture(address,handler):
            httpd = factory(address,handler)
            captured.append(httpd); ready.set()
            return httpd
        with patch.object(server,"ThreadingHTTPServer",side_effect=capture):
            worker = threading.Thread(target=server.serve,args=(self.cfg,self.store,lambda:{}),daemon=True)
            worker.start(); self.assertTrue(ready.wait(3))
            httpd = captured[0]
            try:
                for message_id in (mid,mid,999999):
                    conn = http.client.HTTPConnection("127.0.0.1",httpd.server_port,timeout=5)
                    conn.request("POST","/api/agent-tasks/control",
                        body=json.dumps({"message_id":message_id,"action":"delete"}),
                        headers={"Host":"127.0.0.1:0","Content-Type":"application/json"})
                    response = conn.getresponse()
                    result = json.loads(response.read()); conn.close()
                    self.assertEqual(response.status,200,result)
                    self.assertEqual(result["task"]["status"],"deleted")
                self.assertEqual(tasks.overview(self.cfg,self.store)["requests"],[])
                self.assertEqual(tasks.tasks(self.store),[])
            finally:
                httpd.shutdown(); worker.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
