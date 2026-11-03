# Local Agent Tasks

In Settings, expand **Local agent tasks** and enable **@infohub tasks from me**. In a Watch group or direct chat, send a new message that
starts with `@infohub` or `@InfoHub`, followed by the task:

```text
@InfoHub Summarize the PDF in D:/Reading into a short Markdown note.
```

After sync collects the message, the service checks for owner requests every three seconds,
queues the conversation immediately and prioritizes it ahead of ordinary pending segments.
It does not wait for a quiet chat or the regular AI interval. Existing AI calls must finish
first; daily budget limits and retry backoff still apply. Sync itself keeps its configured interval.
The AI organizer first prepares a Todo with its relevant messages and task context.
After that conversation segment finishes AI organization, the local worker tags the
Todo `type:agent-task`, creates a persistent Codex session, and runs
the instruction. **Status > Codex tasks** lists queued, starting, running, completed,
cancelled, and attention-needed tasks, with results and **Open in Codex** links.
Both Settings and Status agent sections are collapsed initially; expanding them keeps
them open during in-page updates.
It also explains why owner requests have not been queued. The panel and Todo
status badges update every four seconds without replacing the page or input fields.
The Todo's **Session status** link opens Status. Finished turns close the Todo.

Each queue row has Start/Resume, Pause, Delete, and Open in Codex controls as applicable.
Start can explicitly queue an older request; it still requires an owner message in a
Watch chat and waits for AI organization if the task context is not ready. Pause
interrupts the current turn and retains its session; Resume continues that session.
Delete stops a running turn before hiding the row. A local deletion record prevents
the same source message from being queued again. Source messages, ordinary Todos,
workspace files, and Codex history are retained.

Only the source adapter's `from_me` identity flag is accepted. Display names and
quoted mentions do not authorize execution. Store, Digest and Ignore chats are excluded.
There is no message-type restriction on an owner's decoded `@infohub` request.
The current account's messages must already be syncing into Info Hub.
Source adapters that do not populate `from_me` cannot trigger tasks.
The names in Settings identify mentions and personal tags; they do not authenticate
the sender of an executable task. Other senders' mentions are not queued or listed
as unqueued agent requests.
Before Codex starts, the AI selects relevant graph neighbours, recent conversation units,
and units with shared subject tags, then reads their messages and prepares a cited brief.
Codex receives the original request, that brief and the selected evidence. These messages are supporting
context, not permission to follow instructions from other people. While organization
is pending, Status shows **Waiting for AI organizer**; it does not bypass the AI queue.

Activation records a timestamp: older messages are not dispatched. Re-organizing
the same source message does not create another task. Tasks marked Done or Ignore
before dispatch are not started. A restarted service does not replay uncertain
work; it marks it as needing attention instead.

With an empty working-directory setting, each task gets a directory under
`agent-workspaces/<message-id>` beside the configured database. Files are retained
for review. Choose an existing absolute directory to use another workspace.
Permissions can be Read only, Workspace, or Full Access. Full Access permits work
outside the task directory. Requests requiring user input or approval are
interrupted and marked as needing attention; continue them in Codex.

## Runtime

Run Info Hub with `python -m infohub run` or the desktop launcher. Codex must be
installed and signed in as the same OS user as the desktop app, with the same
`CODEX_HOME`. The integration uses `codex app-server` over stdio, persistent
`thread/start` and `turn/start` requests, and the default configured model.
It does not require an OpenAI API key in Info Hub's Settings.

**Settings > Local agent tasks > Codex executable** accepts a command name or an absolute
executable path, without arguments or surrounding quotes. Empty means `codex`: search PATH,
then the current user's installed Windows Codex desktop bundles. No user-specific path is
hardcoded. On Windows, select the actual `.exe`, not an npm `.cmd` wrapper. Changes apply
to the next task launch without restarting the service. Missing executables produce a
Settings-directed error, and failed tasks can be retried with Start.
The local YAML can also override `agent_tasks.timeout_seconds` (default 3600). Prompts are sent as JSON data,
never interpolated into shell commands. Settings overrides remain in the ignored
`config/settings.local.json`; task records and workspace files remain local.

The initial backend is Codex only. A stopped task can be resumed from its Codex
session; Info Hub does not yet track turns started manually in that session.

Protocol reference: [Codex App Server](https://learn.chatgpt.com/docs/app-server).
