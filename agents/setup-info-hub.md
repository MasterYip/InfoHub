# Agent playbook: set up Info Hub

You are a coding agent setting up Info Hub on the user's machine. Follow this in order.
Full reference: `docs/setup.md`.

Ground rules:
- Message data and tokens stay on this machine. Never print a token or device code. Never commit
  `data/`, `config/*.local.*` or `logs/`.
- Browser-approval steps cannot be automated. For each, start the command, retrieve the
  verification URL it prints, give the user the URL and what to click, then continue once they confirm.
- Ask the user up front: how many Lark/Feishu organizations, and their names.

Steps:
1. **Install.** Create a venv, `pip install -r requirements.txt`, `npm install`, copy
   `config/hub.example.yaml` to `config/hub.local.yaml`, and run the tests. Confirm they pass.
2. **Lark, once per organization.** For each org with profile name `orgN`:
   - `npx lark-cli config init --new --name orgN` — open the printed URL; tell the user to
     **switch to that organization** in Feishu before approving.
   - `npx lark-cli auth login --profile orgN --domain im,calendar,task,contact` — open the printed
     URL for them to approve the read scopes.
   Then write the `sources.lark.accounts` list in `config/hub.local.yaml`.
   If calendar reports `missing_scope`, tell the user their org admin must approve
   `calendar:calendar.event:read`; messages still work meanwhile.
3. **Verify & start.** `python -m infohub doctor` (expect Lark chats), then `python -m infohub run`.
   Confirm `http://127.0.0.1:8765/api/health` answers.
4. **Hand off.** Tell the user to open the dashboard, set their display names and chat policies in
   Settings, and mark VIP people. Mention the optional steps (AI key, remote access, scheduled
   agent) but don't do them unless asked.

Do not enable AI, publish, push, or expose the dashboard publicly without the user asking.
