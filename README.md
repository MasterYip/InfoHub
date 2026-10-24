<h1 align="center">Info Hub</h1>

<p align="center">
  <b>One private dashboard for everything coming at you</b> — your chat and messaging accounts,
  turned into tidy <i>info units</i> (tasks, events, topics) you can actually read.
</p>

<p align="center">
  <a href="#get-started"><img alt="Get started" src="https://img.shields.io/badge/Get_Started-2563EB?style=for-the-badge&logo=rocket&logoColor=white"></a>
  <a href="docs/architecture.md"><img alt="Architecture" src="https://img.shields.io/badge/Architecture-5A5A57?style=for-the-badge"></a>
  <a href="docs/setup.md"><img alt="Setup guide" src="https://img.shields.io/badge/Setup_Guide-7895A6?style=for-the-badge&logo=readthedocs&logoColor=white"></a>
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/License-MIT-ECECEA?style=for-the-badge&labelColor=2F2F2D&color=ECECEA"></a>
</p>

<p align="center">
  <img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white">
  <img alt="Node 18+" src="https://img.shields.io/badge/Node-18+-339933?logo=nodedotjs&logoColor=white">
  <img alt="Windows" src="https://img.shields.io/badge/Windows-0078D6?logo=windows&logoColor=white">
  <img alt="Local-first" src="https://img.shields.io/badge/Local--first-no_cloud-15803D">
  <img alt="Runs without AI" src="https://img.shields.io/badge/Runs_without_AI-optional_LLM-6D28D9">
  <img alt="Tests" src="https://img.shields.io/badge/tests-23_passing-2E7D32">
</p>

<p align="center">
  <img src="assets/dashboard-today.png" width="100%"
       alt="Info Hub dashboard: a Today view with to-dos, events and flagged messages organized from chat groups.">
  <br>
  <sub><i>The dashboard’s <b>Today</b> view — to-dos, events and flagged messages organized from your chats. Synthetic demo data (invented people; no real messages).</i></sub>
</p>

<p align="center">
  <img src="assets/graph.png" width="100%"
       alt="Explore tab in graph mode: a live force-directed graph of info units, colored by kind, with physics and filter controls.">
  <br>
  <sub><i><b>Explore → Graph</b> — a live, force-directed map of your info units (color by kind, size by activity), with tunable physics and per-kind / time filters. Click any node to open it. Synthetic demo data.</i></sub>
</p>

> Chat groups are noisy. Info Hub reads your own Lark/Feishu accounts (and any source plugin you add) **locally and
> read-only**, groups the chatter into main-idea **info units**, and shows you what matters on one
> dashboard — todos, events, a calendar, and topic summaries — with everything staying on your machine.

<details>
<summary><b>More screenshots</b></summary>
<br>
<p align="center">
  <img src="assets/dashboard-explore.png" width="100%"
       alt="Explore tab: filter info units by tags (person, topic, channel, type) over a recursive unit graph.">
  <br>
  <sub><i><b>Explore</b> — filter the info-unit graph by person, topic, channel or type. Synthetic demo data.</i></sub>
</p>
<p align="center">
  <img src="assets/chat.png" width="100%"
       alt="Chat tab: ask the assistant about your hub; it answers in Markdown with clickable references to units, groups and people.">
  <br>
  <sub><i><b>Chat</b> — ask the built-in assistant about your hub. It streams Markdown answers and cites clickable references to the exact units, groups and people. Optional, powered by any OpenAI-compatible LLM. Synthetic demo data.</i></sub>
</p>
</details>

> **Try it with the demo data:** `python scripts/seed_demo.py` writes a fictional example database to
> `data/demo.db`; point `hub.database` at it in `config/hub.local.yaml` and run `python -m infohub serve`.

## How it works

```mermaid
flowchart TB
  subgraph L1["1 - Sync (local, read-only, no AI)"]
    direction LR
    SRC["Your accounts<br/>Lark/Feishu x N orgs · pluggable sources"]
    PRE["preprocess<br/>flags · dates · segments · triage P0-P3"]
    SRC --> PRE
  end
  subgraph L2["2 - Organize into info units"]
    direction LR
    RULE["rule organizer<br/>always on, deterministic"]
    AI["AI organizer<br/>optional · budget · retries"]
    AGENT["agent bridge<br/>Codex / Claude Code"]
  end
  subgraph L3["3 - Apps"]
    direction LR
    DASH["Dashboard<br/>Today · Todos · Calendar · Groups · Explore"]
    REM["Reminders"]
    APIX["Agent API + brief"]
  end
  PRE --> DB[("SQLite + FTS<br/>messages")]
  DB --> RULE & AI & AGENT
  RULE & AI & AGENT --> UG[("info-unit graph<br/>tags · links · evidence")]
  UG --> DASH & REM & APIX
  DB -. "search, flagged lane" .-> DASH
```

**It runs every day without AI.** Layer 1 and the rule organizer are deterministic. The AI organizer is a separate, optional worker: if the API is down, over budget or not configured, its jobs wait in a queue and nothing else is affected. Full design in [docs/architecture.md](docs/architecture.md).

## What it never does
- It sends, posts or edits nothing on any platform. Dashboard actions only change local state.
- It never commits data. `data/`, `config/*.local.yaml` and `logs/` are git-ignored.
- It sends no message text to an AI service unless `ai.enabled: true`, and then only for chats whose policy is listed in `ai.policies` (default: chats you marked **Watch**).
- It listens only on 127.0.0.1 by default, rejects foreign Host headers and cross-origin writes, and requires login once an account exists.

## Get started

**With a coding agent** (Claude Code, Codex, …) — clone the repo, open it in your agent and paste:

```text
Please set up Info Hub for my Lark/Feishu organizations on this machine.
Follow docs/setup.md: run the non-interactive steps yourself, and for each browser
approval open the page and tell me what to click. I have N organizations: <names>.
```

**By hand** — follow [docs/setup.md](docs/setup.md). The short version (Git, Python 3.10+, Node 18+):

```bash
git clone https://github.com/MasterYip/InfoHub.git
cd InfoHub
python -m venv .venv && . .venv/bin/activate      # Windows: .\.venv\Scripts\activate
pip install -r requirements.txt && npm install    # npm installs the official lark-cli
cp config/hub.example.yaml config/hub.local.yaml
# once per Lark/Feishu organization, approve in the browser inside that organization:
npx lark-cli config init --new --name org1
npx lark-cli auth login --profile org1 --domain im,calendar,task,contact
python -m infohub doctor
python -m infohub run
```
Then open http://127.0.0.1:8765/ and set your chats and names in **Settings**. Full steps,
multiple organizations, AI, autostart and remote access are in [docs/setup.md](docs/setup.md).

## Scheduled agents (Codex, Claude Code, ...)
An external agent can do the Layer 2 work on a schedule instead of, or alongside, the API worker:
- `python -m infohub agent next` leases jobs as JSON (`instructions`, `schema`, `jobs`); `agent submit results.json` applies the results. Leases expire, so an abandoned run never loses work.
- `python -m infohub agent context --days 1` returns a brief plus counts for other agent-team tasks. HTTP equivalents: `/api/agent/next`, `/api/agent/submit`, `/api/agent/context`.
- `agents/scheduled-run.md` is the instruction file. `scripts/agent-run.ps1` (or `.sh`) runs `agent.command` from your config, e.g. `codex exec --full-auto`. `scripts/install-agent-schedule.ps1 -Hours 2` registers a Task Scheduler entry; on Linux use the cron line in `agent-run.sh`.
- Set `ai.queue_for_agents: true` to queue work for agents while the API worker is off.

## Commands
| Layer | Command | Purpose |
|---|---|---|
| 1 | `python -m infohub sync [--source <name>]` | One sync round, plus the rule organizer (prints counts only) |
| 2 | `python -m infohub organize [--ai] [--requeue DAYS]` | Rule organizer; `--ai` runs queued AI jobs; `--requeue` queues past chats |
| 2 | `python -m infohub agent next` / `agent submit FILE` / `agent context` | Scheduled or external agents (lease protocol) |
| 3 | `python -m infohub brief --days 7 --tags course:ai-engineering` | Markdown brief for agent-team tasks |
| 3 | `python -m infohub remind` | Toast reminders for due todos and upcoming events |
| — | `python -m infohub run` | The service: all layers on their own schedules, plus the dashboard |
| — | `python -m infohub stats` / `doctor` | Counts and health (no message text) |

The HTTP API is documented at the top of [infohub/apps/server.py](infohub/apps/server.py).

## Remote access and login
To open the dashboard from other devices, create an account (`python -m infohub user add <name>`), forward the port through your public server, and use HTTPS. Once an account exists, every page and API call requires login; the browser keeps a 30-day session cookie. See [docs/remote-access.md](docs/remote-access.md).

## Running on an always-on machine
The hub runs anywhere `lark-cli` does. See [docs/architecture.md § Deployment](docs/architecture.md#deployment); reach it from other devices via a tunnel with login ([docs/remote-access.md](docs/remote-access.md)).
