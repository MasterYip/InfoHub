# Scheduled info-hub run (for Codex, Claude Code or any coding agent)

You are run on a schedule inside the info-hub repository. Work only through the commands below.
Do not read `data/`, edit code, commit, push, send messages on any platform, or open URLs.
Use `.venv/Scripts/python.exe` on Windows or `.venv/bin/python` elsewhere as `PY`.

1. Lease work: `PY -m infohub agent next --limit 20`
   The JSON contains `instructions`, `schema` and `jobs` (each with `job_id` and `input`).
   If `jobs` is empty, skip to step 4.
2. For every job, follow `instructions` exactly and produce one object that matches `schema`:
   main-idea info units with tags, nesting (`parent`), evidence `message_ids`, and `close_units`.
   Reuse `existing_units` ids instead of creating duplicates. Use an empty `units` list when nothing is worth keeping.
3. Write all answers to `logs/agent-results.json` as
   `{"results": [{"job_id": <id>, "result": {...}}, ...]}` and run
   `PY -m infohub agent submit logs/agent-results.json`.
   If some are rejected, fix only those and submit again (finished jobs are ignored).
4. Run `PY -m infohub agent context --days 1` and finish with a short summary (max 10 lines):
   jobs processed, units created, and the most important open todos or events due in the next 48 hours.

Leased jobs that you do not submit return to the queue automatically after the lease expires.
