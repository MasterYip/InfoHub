#!/usr/bin/env python3
"""Build a realistic but entirely fictional demo database for Info Hub.

    python scripts/seed_demo.py [--db data/demo.db]

Then point the hub at it (config/hub.local.yaml -> hub.database: data/demo.db) and run
`python -m infohub serve` to explore, or use it to produce the screenshots in the README.

The data is themed on a robotics / reinforcement-learning research group chat — world models,
motion imitation, reward shaping, humanoid locomotion, sim-to-real, hardware and paper talk — but
every person, project and message is invented. No real names, platforms or conversations.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infohub.organize import units as U            # noqa: E402
from infohub.store import Store                     # noqa: E402

SOURCE = "team"                                     # a generic, platform-neutral source
PEOPLE = ["Tom", "Mia", "Andy", "Lena", "Kai", "Priya", "Diego", "Milo",
          "Noah", "Ava", "Leo", "Zoe", "Omar", "Nina"]
CHANNELS = [
    ("rl-control", "Robotics RL & Control", "watch"),
    ("paper-club", "Paper Club", "watch"),
    ("hardware", "Hardware & Motors", "digest"),
    ("weekend", "Weekend Plans", "store"),
]

DAY = 86400
# Each unit: (ref, kind, channel, title, summary, tags, people, n_msgs, days_ago, parent_ref, importance)
UNITS = [
    # --- world models thread ---
    ("wm", "topic", "rl-control", "World-model RL vs model-free: when does imagination pay off?",
     "Long thread comparing learning a latent world model against model-free RL for legged control; "
     "consensus that imagination helps most when real rollouts are expensive.",
     ["topic:world-model", "project:WorldRL"], ["Tom", "Mia", "Kai", "Ava"], 34, 0, None, "high"),
    ("wm-rssm", "note", "rl-control", "RSSM's real job is compressing high-dim obs into a usable state",
     "Takeaway from the thread: the recurrent state-space model matters because it compresses perception, "
     "not because it predicts pixels well.", ["topic:world-model"], ["Kai"], 5, 0, "wm", None),
    ("wm-reward", "question", "rl-control", "How should collision / contact rewards be handled in imagination?",
     "Open question on shaping contact rewards when rolling out inside a learned model.",
     ["topic:world-model", "topic:reward-shaping"], ["Ava", "Tom"], 7, 0, "wm", None),
    ("wm-qfn", "note", "rl-control", "Q-function can stand in for the world model for short horizons",
     "", ["topic:world-model"], ["Mia"], 3, 1, "wm", None),
    ("wm-bptt", "topic", "rl-control", "BPTT gradient quality, state parameterisation and sim replay",
     "Discussion of backprop-through-time gradient noise and how state parameterisation changes it.",
     ["topic:world-model", "topic:optimization"], ["Kai", "Leo", "Tom"], 18, 2, None, None),
    ("wm-actseq", "todo", "rl-control", "Try optimising only the action sequence, then regress a policy",
     "Action-plan-then-distill idea to try next.", ["project:WorldRL"], ["Tom"], 2, 2, "wm-bptt", "medium"),

    # --- motion imitation thread ---
    ("mimic", "topic", "rl-control", "MimicNet reproduction and cross-domain transfer",
     "Group reproduction effort for the MimicNet motion-imitation method and where it transfers.",
     ["topic:motion-imitation", "project:MimicNet"], ["Andy", "Lena", "Noah", "Diego"], 41, 0, None, "high"),
    ("mimic-site", "resource", "rl-control", "MimicNet reproduction write-up and demo site",
     "", ["topic:motion-imitation", "project:MimicNet"], ["Andy"], 3, 0, "mimic", None),
    ("mimic-anchor", "note", "rl-control", "Adding a motion-anchor orientation to the obs helped a lot",
     "", ["topic:motion-imitation"], ["Diego"], 4, 1, "mimic", None),
    ("mimic-imag", "note", "rl-control", "Using imagined frames instead of ground truth for tracking reward",
     "", ["topic:motion-imitation", "topic:reward-shaping"], ["Noah"], 3, 1, "mimic", None),
    ("mimic-cmp", "decision", "rl-control", "Merge the RGMT-style variant into MimicNet",
     "Team decided to fold the retargeting variant into the main repo.",
     ["project:MimicNet"], ["Lena", "Andy"], 6, 2, "mimic", None),
    ("mimic-todo", "todo", "rl-control", "Compare the two retargeting implementations and note trade-offs",
     "", ["project:MimicNet"], ["Diego"], 2, 2, "mimic", "medium"),
    ("mimic-vision", "topic", "rl-control", "Vision-conditioned MimicNet trains ~20x faster",
     "A vision-based variant reportedly trains far faster; group wants to confirm.",
     ["topic:motion-imitation"], ["Milo", "Andy"], 12, 3, None, None),

    # --- locomotion thread ---
    ("loco", "topic", "rl-control", "Heightmap-only navigation reaches hardest terrain in ~0.7h",
     "Locomotion result: a heightmap-only policy climbs to max terrain difficulty quickly.",
     ["topic:locomotion"], ["Kai", "Zoe"], 15, 1, None, None),
    ("loco-vel", "question", "rl-control", "How to define 'success' for multi-terrain velocity tracking?",
     "", ["topic:locomotion"], ["Zoe"], 4, 1, "loco", None),
    ("loco-sgs", "topic", "rl-control", "Success-guided sampling: reproduction and where it applies",
     "", ["topic:locomotion", "topic:rl-methods"], ["Leo", "Kai"], 9, 2, None, None),
    ("loco-dwbc", "note", "rl-control", "DWBC + parkour only fits obstacle tasks, data is scarce",
     "", ["topic:locomotion"], ["Leo"], 2, 3, "loco-sgs", None),

    # --- manipulation / tactile ---
    ("tac", "resource", "rl-control", "From contact mechanics to data-driven manipulation: a primer",
     "A long introductory note on tactile sensing for manipulation.",
     ["topic:manipulation", "topic:tactile"], ["Priya"], 4, 2, None, None),
    ("tac-q", "question", "rl-control", "Has anyone tried force-sensing + interaction work?",
     "", ["topic:tactile"], ["Priya", "Omar"], 3, 2, "tac", None),

    # --- optimal control ---
    ("mpc", "topic", "rl-control", "MPC, multiple shooting and direct collocation",
     "Thread comparing trajectory-optimisation formulations for control.",
     ["topic:optimization", "topic:control"], ["Leo", "Tom", "Kai"], 16, 1, None, None),

    # --- paper club ---
    ("pc", "topic", "paper-club", "Latent world-model agents: an evolution recap",
     "Paper-club recap tracing the lineage of latent world-model agents.",
     ["topic:world-model", "topic:papers"], ["Mia", "Ava"], 11, 3, None, None),
    ("pc-arxiv1", "resource", "paper-club", "Paper: latent imagination for legged control (arXiv)",
     "", ["topic:papers"], ["Ava"], 2, 3, "pc", None),
    ("pc-arxiv2", "resource", "paper-club", "Paper: success-guided exploration for RL (arXiv)",
     "", ["topic:papers", "topic:rl-methods"], ["Leo"], 2, 4, "pc", None),
    ("pc-wmp", "topic", "paper-club", "WMP vs RSSM: which world-model backbone to build on?",
     "", ["topic:world-model", "topic:papers"], ["Kai", "Mia"], 8, 4, None, None),
    ("pc-best", "resource", "paper-club", "Narrative recap of 35 best-paper awards",
     "", ["topic:papers"], ["Mia"], 3, 5, None, None),

    # --- hardware ---
    ("hw", "topic", "hardware", "Teardown of the DX-6216 actuator",
     "Hardware channel: a motor teardown and notes on torque density.",
     ["topic:hardware"], ["Omar", "Nina"], 7, 2, None, None),
    ("hw-beni", "topic", "hardware", "New dev board arrived; next firmware milestone planned",
     "", ["topic:hardware"], ["Nina", "Omar"], 6, 1, None, None),
    ("hw-sim", "resource", "hardware", "A new open sim platform for manipulation",
     "", ["topic:hardware", "topic:sim"], ["Omar"], 2, 3, None, None),

    # --- tooling / assistants ---
    ("llm", "topic", "rl-control", "Coding-assistant workflow: goal mode and token budgets",
     "Tips on using an LLM coding assistant efficiently for research code.",
     ["topic:tooling"], ["Tom", "Milo", "Leo"], 13, 0, None, None),
    ("llm-note", "note", "rl-control", "Spell out acronyms on first use in the group",
     "", ["topic:tooling"], ["Mia"], 2, 2, "llm", None),
    ("infohub", "resource", "rl-control", "Info Hub: open-source repo link",
     "Someone shared the Info Hub project the group is using to organise all this.",
     ["topic:tooling", "project:infohub"], ["Tom"], 3, 0, None, None),

    # --- todos / deadlines / events ---
    ("todo-readme", "todo", "rl-control", "Add a disclaimer to the open-source repo",
     "", ["project:MimicNet"], ["Andy"], 2, 1, None, "low"),
    ("todo-star", "note", "rl-control", "Repo reached 400 stars",
     "", ["project:MimicNet"], ["Andy"], 2, 1, None, None),
    ("hw-dl", "deadline", "rl-control", "Submit the workshop paper draft",
     "Shared deadline for the group's workshop submission.",
     ["type:deadline"], ["Lena", "Tom"], 4, 0, None, "high"),
    ("ev-visit", "event", "rl-control", "Lab invites the group to visit the new campus next week",
     "", ["type:event"], ["Zoe"], 3, 0, None, None),
    ("ev-box", "event", "rl-control", "Online humanoid-boxing competition: registration open",
     "An online sim humanoid-boxing contest opened registration for students.",
     ["type:event", "topic:competition"], ["Noah"], 5, 1, None, None),
    ("ann-rel", "announcement", "rl-control", "Milo's bipedal-humanoid work to be released / open-sourced",
     "", ["topic:locomotion"], ["Milo"], 4, 2, None, None),
    ("ev-weekend", "event", "weekend", "Let's not grind this weekend — do something fun",
     "", ["type:event"], ["Zoe", "Ava", "Leo"], 6, 1, None, None),

    # --- robot sports sub-thread (children of a topic) ---
    ("fight", "topic", "rl-control", "Robot fighting: feasibility and spectacle",
     "A playful but technical thread on humanoid-combat demos.",
     ["topic:competition"], ["Noah", "Diego", "Kai"], 20, 1, None, None),
    ("fight-lock", "note", "rl-control", "Locking onto and landing hits is the hard technical part",
     "", ["topic:competition"], ["Kai"], 3, 1, "fight", None),
    ("fight-biz", "topic", "rl-control", "Audience and 'mech-sports' commercialisation",
     "", ["topic:competition"], ["Diego"], 5, 1, "fight", None),
    ("fight-ref", "resource", "rl-control", "A pack of 800 motion clips for choreographed moves",
     "", ["topic:competition", "topic:motion-imitation"], ["Noah"], 2, 2, "fight", None),
]


def seed(db_path: str) -> None:
    p = Path(db_path)
    for f in (p, p.with_suffix(p.suffix + "-wal"), p.with_suffix(p.suffix + "-shm")):
        if f.exists():
            f.unlink()
    p.parent.mkdir(parents=True, exist_ok=True)
    store = Store(p)
    now = int(time.time())
    chan_name = {cid: name for cid, name, _ in CHANNELS}
    for cid, name, policy in CHANNELS:
        store.q("INSERT OR REPLACE INTO channels(source,account,channel,channel_name,is_group,policy,unread,last_at,muted,chat_mode)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)", (SOURCE, "", cid, name, 1, policy, 0, now, 0, "group"))
    mid = 0
    refs = {}
    for (ref, kind, chan, title, summary, tags, people, n_msgs, days_ago, parent, importance) in UNITS:
        act = now - days_ago * DAY - 3600
        uid = U.upsert(store, f"demo:{ref}", kind=kind, title=title, summary=summary or None,
                       origin="ai", activity_at=act)
        store.q("UPDATE units SET created_at=?, updated_at=? WHERE id=?", (act, act, uid))
        all_tags = [f"type:{kind}", f"source:{SOURCE}", f"channel:{chan_name[chan]}"] + list(tags) \
            + [f"person:{p}" for p in people] + ([f"importance:{importance}"] if importance else [])
        U.add_tags(store, uid, all_tags)
        msg_ids = []
        for i in range(n_msgs):
            mid += 1
            sender = people[i % len(people)]
            sent = act - (n_msgs - i) * 90
            store.q("INSERT INTO messages(source,account,item_key,channel,channel_name,is_group,sender,sender_name,"
                    "sent_at,kind,text,policy,collected_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (SOURCE, "", f"m{mid}", chan, chan_name[chan], 1, sender.lower(), sender,
                     sent, "text", "", "watch", now))
            msg_ids.append(store.q("SELECT last_insert_rowid()").fetchone()[0])
        U.attach(store, uid, msg_ids)
        refs[ref] = uid
    for (ref, kind, chan, title, summary, tags, people, n_msgs, days_ago, parent, importance) in UNITS:
        if parent and parent in refs:
            U.link(store, refs[parent], refs[ref])
    for ref in ("hw-dl", "ev-visit", "ev-box", "wm", "mimic", "hw-dl"):
        if ref in refs:
            store.q("UPDATE units SET for_me=1 WHERE id=?", (refs[ref],))
    store.db.commit()
    nu = store.q("SELECT count(*) FROM units").fetchone()[0]
    nl = store.q("SELECT count(*) FROM unit_links").fetchone()[0]
    nm = store.q("SELECT count(*) FROM messages").fetchone()[0]
    print(f"demo database written to {p}: {nu} units, {nl} links, {nm} messages, {len(PEOPLE)} people.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/demo.db")
    seed(ap.parse_args().db)
