"""info-hub command line.

Layer 1  sync          python -m infohub sync [--source <name>]   (no AI; prints counts only)
Layer 2  organize      python -m infohub organize [--ai] [--requeue DAYS] [--recompute] [--resplit]
         agent         python -m infohub agent next [--limit N] | submit FILE|- | context [--days 7]   (scheduled agents)
                       python -m infohub agent export --out jobs.jsonl | import results.jsonl | reset
Layer 3  serve         python -m infohub serve                          (dashboard + API only)
         brief         python -m infohub brief [--days 7] [--tags a,b]  (Markdown for agents)
         desktop       python -m infohub desktop [--hidden]         (Windows window + tray + service)
         backup        python -m infohub backup [--if-due]           (verified database snapshot)
         remind        python -m infohub remind
Service  run           python -m infohub run    (sync+organize+remind every poll_minutes; AI in its own thread; dashboard)
Admin    doctor | stats | source-init <name> (e.g. lark)
Chats    channels refresh                                      (re-read mute state for every chat)
         channels defaults [--apply] [--source <name>]   (set every chat's policy from the rules)
Access   user add|passwd|remove|list NAME     (login for the dashboard/API; password via hidden prompt or --stdin)
         token add|remove|list NAME           (API token for agents/scripts: Authorization: Bearer <token>)
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import traceback
from pathlib import Path

from . import config as config_mod
from .apps import brief, reminders
from .organize import agent_bridge, ai_organizer, polish, providers, rule_organizer
from .sources import discover as discover_sources
from .store import Store
from .sync.runner import sync_once

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def guarded(store, layer, name, fn):
    started = int(time.time())
    try:
        res = fn()
        store.log(layer, name, started, "ok", detail=res)
        return res
    except Exception as exc:
        msg = f"{type(exc).__name__}: {str(exc)[:200]}"
        store.log(layer, name, started, "error", error=msg)
        return {"error": msg}


def cycle(cfg, store) -> dict:
    """Layer 1 then the deterministic parts of Layers 2 and 3. Each step is isolated."""
    out = {"sync": guarded(store, "sync", "all", lambda: sync_once(cfg, store))}
    out["organize"] = guarded(store, "organize", "rules", lambda: rule_organizer.run(cfg, store))
    agent_bridge.reclaim(store)
    out["remind"] = guarded(store, "apps", "reminders", lambda: reminders.run(cfg, store))
    return out


def stats(store) -> dict:
    q = store.q
    day = int(time.time()) - 86400
    return {
        "messages_total": q("SELECT count(*) FROM messages").fetchone()[0],
        "last_24h_by_source_priority": {f"{r[0]}/{r[1]}": r[2] for r in q(
            """SELECT source || CASE WHEN account!='' THEN ':'||account ELSE '' END, priority, count(*)
               FROM messages WHERE sent_at>=? GROUP BY 1,2 ORDER BY 1,2""", (day,))},
        "channels_by_policy": {f"{r[0]}/{r[1]}": r[2] for r in q(
            "SELECT source || CASE WHEN account!='' THEN ':'||account ELSE '' END, policy, count(*) FROM channels GROUP BY 1,2")},
        "segments": dict(q("SELECT count(*) AS total, sum(closed) AS closed FROM segments").fetchone()),
        "units_by_kind": {r[0]: r[1] for r in q("SELECT kind, count(*) FROM units GROUP BY kind")},
        "top_tag_namespaces": {r[0]: r[1] for r in q(
            "SELECT substr(tag,1,instr(tag,':')-1), count(*) FROM unit_tags GROUP BY 1 ORDER BY 2 DESC")},
        "jobs": {r[0]: r[1] for r in q("SELECT status, count(*) FROM jobs GROUP BY status")},
        "ai_spent_today_usd": round(ai_organizer.spent_today(store), 4),
    }


def doctor(cfg) -> dict:
    ai = cfg["ai"]
    res = {"config": cfg["_path"], "database": cfg["hub"]["database"],
           "ai": {"enabled": ai["enabled"], "provider": ai["provider"], "model": ai["model"],
                  "key_present": bool(providers.api_key(ai)) if ai["provider"] != "anthropic" else None}}
    for name, mod in discover_sources().items():
        if cfg["sources"].get(name, {}).get("enabled") and hasattr(mod, "doctor"):
            res[name] = mod.doctor(cfg)
    if cfg["ai"]["enabled"]:
        try:
            providers.make(ai)
            res["ai"]["client"] = "ok"
        except Exception as exc:
            res["ai"]["client"] = str(exc)[:200]
    return res


def main():
    ap = argparse.ArgumentParser(prog="infohub", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor"); sub.add_parser("stats"); sub.add_parser("serve"); sub.add_parser("run"); sub.add_parser("remind")
    p = sub.add_parser("desktop"); p.add_argument("--hidden", action="store_true", help="start in the system tray")
    p = sub.add_parser("backup"); p.add_argument("--if-due", action="store_true")
    p = sub.add_parser("source-init"); p.add_argument("source")
    p.add_argument("--pid", type=int); p.add_argument("--db-root")
    p = sub.add_parser("sync"); p.add_argument("--source")
    p = sub.add_parser("poll"); p.add_argument("--source")            # alias kept from v1
    p = sub.add_parser("organize"); p.add_argument("--ai", action="store_true"); p.add_argument("--requeue", type=float)
    p.add_argument("--resplit", action="store_true"); p.add_argument("--recompute", action="store_true")
    p.add_argument("--reflag", action="store_true")
    p = sub.add_parser("brief"); p.add_argument("--days", type=int, default=7); p.add_argument("--tags", default="")
    p = sub.add_parser("channels"); p.add_argument("action", choices=["defaults", "list", "refresh"])
    p.add_argument("--apply", action="store_true"); p.add_argument("--source")
    p = sub.add_parser("user"); p.add_argument("action", choices=["add", "passwd", "remove", "list"])
    p.add_argument("name", nargs="?"); p.add_argument("--stdin", action="store_true")
    p = sub.add_parser("token"); p.add_argument("action", choices=["add", "remove", "list"]); p.add_argument("name", nargs="?")
    p = sub.add_parser("agent"); p.add_argument("action", choices=["next", "submit", "context", "export", "import", "reset"])
    p.add_argument("path", nargs="?"); p.add_argument("--out"); p.add_argument("--limit", type=int)
    p.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    cfg = config_mod.load(args.config)
    pr = lambda x: print(json.dumps(x, ensure_ascii=False, indent=2))

    if args.cmd == "source-init":
        name = args.source
        mod = discover_sources().get(name)
        if not mod or not hasattr(mod, "init"):
            raise SystemExit(f"source {name!r} has no init step in this build")
        res = mod.init(cfg, pid=args.pid, db_root=args.db_root)
        return pr({k: v for k, v in res.items() if k in ("status", "databases_verified", "databases_total")})
    if args.cmd == "desktop":
        from .apps.desktop import run as desktop_run
        return desktop_run(cfg, hidden=args.hidden)
    if args.cmd == "backup":
        from .backup import BackupManager
        result = BackupManager(cfg).run_once(force=not args.if_due)
        pr(result)
        if "error" in result:
            raise SystemExit(1)
        return
    if args.cmd == "doctor":
        return pr(doctor(cfg))
    if args.cmd == "channels":
        from .sync import policy as policy_mod
        store = Store(cfg["hub"]["database"])
        if args.action == "refresh":
            out = {}
            from .sync.runner import _recorder
            new_ids = []
            record = _recorder(store, out, new_ids)
            for name, mod in discover_sources().items():
                if cfg["sources"].get(name, {}).get("enabled") and hasattr(mod, "refresh"):
                    mod.refresh(cfg, store, record)
            return pr(out)
        if args.action == "defaults":
            return pr(policy_mod.apply(store, cfg, args.source, dry_run=not args.apply))
        return pr({"channels": len(store.q("SELECT 1 FROM channels").fetchall()),
                   "by_policy": {r[0]: r[1] for r in store.q("SELECT policy, count(*) FROM channels GROUP BY policy")}})
    if args.cmd in ("user", "token"):
        from .apps import auth
        if args.action == "list":
            data = auth.load(cfg)
            return pr({"users": sorted(data["users"]), "tokens": sorted(data["tokens"]), "login_required": auth.required(cfg)})
        if not args.name:
            raise SystemExit("name required")
        if args.cmd == "token":
            if args.action == "add":
                return pr({"name": args.name, "token": auth.add_token(cfg, args.name), "note": "shown once; store it securely"})
            return pr({"removed": auth.remove_token(cfg, args.name)})
        if args.action == "remove":
            found = auth.remove_user(cfg, args.name)
            return pr({"removed": found, "sessions_revoked": auth.Sessions(Store(cfg["hub"]["database"]), 1).revoke_user(args.name)})
        import getpass
        if args.stdin:
            pw = sys.stdin.readline().rstrip("\r\n")
        else:
            pw = getpass.getpass("New password (min 10 chars): ")
            if pw != getpass.getpass("Repeat: "):
                raise SystemExit("passwords differ")
        auth.set_password(cfg, args.name, pw)
        revoked = auth.Sessions(Store(cfg["hub"]["database"]), 1).revoke_user(args.name) if args.action == "passwd" else 0
        return pr({"user": args.name, "saved": True, "sessions_revoked": revoked, "login_required": auth.required(cfg)})
    store = Store(cfg["hub"]["database"])
    if args.cmd in ("sync", "poll"):
        out = sync_once(cfg, store, args.source)
        out["organize"] = rule_organizer.run(cfg, store)
        return pr(out)
    if args.cmd == "organize":
        out = {"rules": rule_organizer.run(cfg, store)}
        if args.recompute:
            from .organize.units import recompute_for_me, rescan_mentions
            out["mentions"] = rescan_mentions(store, cfg["me"]["names"])
            out["for_me"] = recompute_for_me(store, cfg.get("me", {}).get("names", []))
        if args.reflag:
            from .sync.triage import reclassify_recent
            out["reflag"] = reclassify_recent(cfg, store)
        if args.resplit:
            from .sync.preprocess import resplit
            out["resplit"] = resplit(store, int(cfg["segment_max_messages"]))
        if args.requeue:
            out["requeued"] = ai_organizer.requeue(cfg, store, args.requeue)
        if args.ai:
            out["ai"] = ai_organizer.run(cfg, store)
            out["polish"] = polish.run(cfg, store)
        return pr(out)
    if args.cmd == "agent":
        if args.action == "next":
            return pr(agent_bridge.lease(cfg, store, args.limit))
        if args.action == "submit":
            text = sys.stdin.read() if args.path in (None, "-") else Path(args.path).read_text(encoding="utf-8")
            return pr(agent_bridge.submit(cfg, store, text))
        if args.action == "context":
            return pr({"brief": brief.brief(cfg, store, args.days), "stats": stats(store)})
        if args.action == "export":
            return pr(agent_bridge.export(cfg, store, Path(args.out or "agent_jobs.jsonl"), args.limit or 20))
        if args.action == "import":
            return pr(agent_bridge.import_results(cfg, store, Path(args.path)))
        return pr({"reset": agent_bridge.reset(store)})
    if args.cmd == "brief":
        return print(brief.brief(cfg, store, args.days, [t for t in args.tags.split(",") if t]))
    if args.cmd == "remind":
        return pr(reminders.run(cfg, store))
    if args.cmd == "stats":
        return pr(stats(store))

    from .apps.server import serve
    if args.cmd == "run":
        def layer1_loop():
            s = Store(cfg["hub"]["database"])
            while True:
                try:
                    print(time.strftime("%H:%M:%S"), json.dumps(cycle(cfg, s), ensure_ascii=False), flush=True)
                except Exception:
                    traceback.print_exc()
                time.sleep(int(cfg["hub"]["poll_minutes"]) * 60)

        def ai_loop():  # separate thread: AI outages or slowness never delay sync
            s = Store(cfg["hub"]["database"])
            while True:
                res = guarded(s, "organize", "ai", lambda: ai_organizer.run(cfg, s))
                pol = guarded(s, "organize", "polish", lambda: polish.run(cfg, s))
                print(time.strftime("%H:%M:%S"), "ai", json.dumps(res, ensure_ascii=False),
                      "polish", json.dumps(pol, ensure_ascii=False), flush=True)
                time.sleep(int(cfg["ai"]["interval_minutes"]) * 60)

        def schedule_loop():
            from .apps.scheduler import loop
            loop(cfg)

        threading.Thread(target=layer1_loop, daemon=True).start()
        if cfg["ai"]["enabled"]:
            threading.Thread(target=ai_loop, daemon=True).start()
        if cfg["hub"].get("viewer", {}).get("enabled"):
            from .apps.server import serve_viewer
            threading.Thread(target=serve_viewer, args=(cfg, Store(cfg["hub"]["database"])),
                             name="infohub-viewer", daemon=True).start()
        threading.Thread(target=schedule_loop, daemon=True, name="infohub-scheduler").start()
    web_store = Store(cfg["hub"]["database"])
    serve(cfg, web_store, lambda: cycle(cfg, web_store))


if __name__ == "__main__":
    main()
