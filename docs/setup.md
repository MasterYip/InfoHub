# Setting up Info Hub

Info Hub runs on one machine (your computer, or an always-on box). The Lark/Feishu step is
interactive — you approve the app in a browser — so it can't be fully scripted. You can either
**let a coding agent drive it** or **do it by hand** (below).

Nothing leaves your machine: message data, tokens and chat IDs all stay in git-ignored local
files (`data/`, `config/*.local.*`, `logs/`).

---

## Option A — with a coding agent (Claude Code, Codex, …)

Open the cloned repo in your agent and paste:

```text
Please set up Info Hub for my Lark/Feishu organizations on this machine.
Follow docs/setup.md: run the non-interactive steps yourself, and for each browser
approval open the page and tell me what to click. I have N organizations: <names>.
```

The agent follows `agents/setup-info-hub.md`.

---

## Option B — manual setup

### 0. Requirements
- Windows, macOS or Linux.
- **Python 3.10+**, **Node 18+**, **Git**.

### 1. Clone and install
```bash
git clone https://github.com/MasterYip/InfoHub.git
cd InfoHub
python -m venv .venv
. .venv/bin/activate            # Windows: .\.venv\Scripts\activate
pip install -r requirements.txt
npm install                     # installs the official lark-cli
cp config/hub.example.yaml config/hub.local.yaml
python -m unittest discover -s tests
```

### 2. Lark / Feishu — once per organization
For each organization, create its own CLI app **while logged into that organization** in the
browser, then authorize read access. Use a short profile name per org (`org1`, `org2`, …):

```bash
npx lark-cli config init --new --name org1
npx lark-cli auth login --profile org1 --domain im,calendar,task,contact
```
Repeat with `--name org2`, `--profile org2`, etc. Then list your orgs in
`config/hub.local.yaml` under `sources.lark.accounts`:
```yaml
sources:
  lark:
    accounts:
      - {name: org1, profile: org1, calendar: true}
      - {name: org2, profile: org2, calendar: true}
```
> Calendar needs the scope `calendar:calendar.event:read`, which some organizations' admins must
> approve. If it isn't granted, messages still sync and the calendar shows "waiting"; re-run the
> `auth login` line after approval.

### 3. Check and start
```bash
python -m infohub doctor        # should list your Lark chats
python -m infohub run           # poll loop + dashboard on http://127.0.0.1:8765
```
Open http://127.0.0.1:8765/. In **Settings → Chats** pick which chats to Watch/Store/Ignore, set
your display names under **Settings** (for @mention detection), and mark VIP people.

### 4. Optional
- **AI organizing** (broader topic units): put a key in an env var (`DEEPSEEK_API_KEY`, or
  `ANTHROPIC_API_KEY` with `ai.provider: anthropic`) and set `ai.enabled: true`. See
  `config/hub.example.yaml` → `ai`.
- **Reach it from your phone / other devices:** `docs/remote-access.md` (create a login first).
- **Scheduled external agent** to do the AI organizing: `agents/scheduled-run.md`.

---

## Adding another source

Sources are plugins in `infohub/sources/`. To add one (Slack, a mail inbox, an RSS feed…),
drop in a module that defines `NAME`, `DEFAULT_CONFIG` and a `sync(cfg, store, overrides, record)`
function that writes normalized messages into the store. See `infohub/sources/__init__.py` for the
full contract and `infohub/sources/lark.py` as a worked example. Removing a source is just deleting
its file/folder and its config block.

## Optional Windows desktop

For a dedicated window, system tray and new-unit notifications, follow [desktop.md](desktop.md).
The existing browser dashboard and service commands remain available.

## Database backups

Automatic backups are enabled every seven days. Set the interval and see verified-copy
status under **Settings → Database backups**. See [backup configuration and recovery](backups.md).
