"""Info units: the Layer 2 data model.

An info unit is one *main idea* (a task, an event, a topic people discuss, a decision, a
resource...), not a sentence. Units form a graph (a "knot" map): `unit_links(parent, child)`
allows several parents and children, so a todo can sit under both a course and a chat-day;
cycles are refused. Tags are namespaced strings; filtering by tags reorganizes the same
units into different views (todos, homework, a project, a person...).

Tag namespaces (see TAXONOMY): type:, topic:, project:, course:, person:, source:, channel:, status:
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

KINDS = ["topic", "todo", "event", "decision", "question", "announcement", "resource", "digest", "note"]

TAXONOMY = {
    "type": "what the unit is: todo, deadline, event, homework, decision, question, announcement, "
            "resource, discussion, digest, note",
    "topic": "subject in a few words, e.g. topic:gpu-scheduling, topic:paper-revision",
    "project": "a long-running effort, e.g. project:predactor",
    "course": "a class, e.g. course:ai-engineering",
    "person": "people involved (display names)",
    "source": "lark:<org> or another source",
    "channel": "chat or group name",
    "status": "derived: open | done",
}


def norm_tag(tag: str) -> str | None:
    tag = (tag or "").strip()
    if not tag:
        return None
    ns, _, val = tag.partition(":")
    if not val:                                   # bare word -> topic
        ns, val = "topic", ns
    ns = ns.strip().lower()
    val = " ".join(val.split())
    if ns in ("type", "topic", "project", "course", "status"):
        val = val.lower().replace(" ", "-")
    return f"{ns}:{val[:60]}" if val else None


def upsert(store, key: str | None, *, kind: str, title: str, summary: str | None = None, status: str | None = None,
           priority: str | None = None, due_at=None, start_at=None, end_at=None, origin="rule",
           confidence=None, activity_at=None, for_me: bool | None = None) -> int:
    now = int(time.time())
    act = int(activity_at or now)
    row = store.q("SELECT id FROM units WHERE key=?", (key,)).fetchone() if key else None
    if row:
        store.q("""UPDATE units SET title=coalesce(?,title), summary=coalesce(?,summary), status=coalesce(?,status),
                   priority=coalesce(?,priority), due_at=coalesce(?,due_at), start_at=coalesce(?,start_at),
                   end_at=coalesce(?,end_at), for_me=max(for_me, ?), updated_at=?,
                   last_activity_at=max(coalesce(last_activity_at,0),?) WHERE id=?""",
                (title, summary, status, priority, due_at, start_at, end_at, int(bool(for_me)), now, act, row["id"]))
        return row["id"]
    return store.q("""INSERT INTO units(key,kind,title,summary,status,priority,due_at,start_at,end_at,origin,confidence,
                      created_at,updated_at,last_activity_at,for_me) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (key, kind, title, summary, status or "open", priority, due_at, start_at, end_at, origin, confidence,
                    now, now, act, int(bool(for_me)))).lastrowid


def update(store, uid: int, **fields):
    allowed = {k: v for k, v in fields.items() if k in ("title", "summary", "status", "priority", "due_at", "start_at", "end_at", "kind")}
    if not allowed:
        return
    sets = ", ".join(f"{k}=?" for k in allowed)
    store.q(f"UPDATE units SET {sets}, updated_at=? WHERE id=?", (*allowed.values(), int(time.time()), uid))


def add_tags(store, uid: int, tags) -> None:
    for t in tags or []:
        if (t := norm_tag(t)):
            store.q("INSERT OR IGNORE INTO unit_tags VALUES(?,?)", (uid, t))


def remove_tags(store, uid: int, tags) -> None:
    for t in tags or []:
        if (t := norm_tag(t)):
            store.q("DELETE FROM unit_tags WHERE unit_id=? AND tag=?", (uid, t))


def ancestors(store, uid: int, limit=None) -> set[int]:
    seen, frontier = set(), [uid]
    while frontier and (limit is None or len(seen) < limit):
        nxt = []
        for u in frontier:
            for (p,) in store.q("SELECT parent_id FROM unit_links WHERE child_id=?", (u,)):
                if p not in seen:
                    seen.add(p)
                    nxt.append(p)
        frontier = nxt
    return seen


def link(store, parent: int, child: int, relation="part_of") -> bool:
    """Add parent -> child. Refuses self-links and links that would create a cycle."""
    if parent == child or child in ancestors(store, parent):
        return False
    store.q("INSERT OR IGNORE INTO unit_links VALUES(?,?,?)", (parent, child, relation))
    return True


def attach(store, uid: int, message_ids) -> None:
    store.db.executemany("INSERT OR IGNORE INTO unit_messages VALUES(?,?)", [(uid, m) for m in message_ids])


def rollup_stats(store) -> dict[int, dict]:
    """Own messages + each child's total, counted once for every parent/child path.

    Shared descendants contribute to every containing branch, and a message attached
    to two units counts twice. Memoized iterative traversal avoids enumerating paths
    or hitting Python's recursion limit. Invalid legacy cycle back-edges are ignored.
    """
    stats = {r["id"]: {"n_direct_messages": r["n"], "n_messages": r["n"],
                       "n_children": 0, "n_descendants": 0} for r in store.q(
        "SELECT u.id, count(m.id) AS n FROM units u LEFT JOIN unit_messages um ON um.unit_id=u.id "
        "LEFT JOIN messages m ON m.id=um.message_id GROUP BY u.id")}
    children = {uid: [] for uid in stats}
    for parent, child in store.q("SELECT DISTINCT parent_id, child_id FROM unit_links ORDER BY parent_id, child_id"):
        if parent in stats and child in stats:
            children[parent].append(child)
    state = {}
    for root in stats:
        stack = [(root, False)]
        while stack:
            uid, finish = stack.pop()
            if finish:
                done = [child for child in children[uid] if state.get(child) == 2]
                stats[uid].update(n_children=len(children[uid]),
                                  n_messages=stats[uid]["n_direct_messages"] + sum(stats[c]["n_messages"] for c in done),
                                  n_descendants=sum(1 + stats[c]["n_descendants"] for c in done))
                state[uid] = 2
            elif not state.get(uid):
                state[uid] = 1
                stack.append((uid, True))
                stack.extend((child, False) for child in reversed(children[uid]) if not state.get(child))
    return stats


def _rows(store, rows, stats=None) -> list[dict]:
    data = [dict(r) for r in rows]
    if not data:
        return []
    stats = rollup_stats(store) if stats is None else stats
    tags = {d["id"]: [] for d in data}
    evidence_times = _evidence_times(store, tuple(tags))
    placeholders = ",".join("?" for _ in tags)
    for uid, tag in store.q(f"SELECT unit_id, tag FROM unit_tags WHERE unit_id IN ({placeholders}) ORDER BY tag", tuple(tags)):
        tags[uid].append(tag)
    targets = source_targets(store, tuple(tags))
    for d in data:
        d.update(stats.get(d["id"], {}))
        first_at, last_at = evidence_times.get(d["id"], (None, None))
        d["first_activity_at"] = first_at
        d["last_evidence_at"] = last_at
        d["tags"] = tags[d["id"]]
        d.update(targets.get(d["id"], {"open_link": None, "open_channel": None}))
    return data


def _row(store, r, stats=None) -> dict:
    return _rows(store, [r], stats)[0]


def _evidence_times(store, ids) -> dict[int, tuple[int | None, int | None]]:
    """Earliest and latest evidence times, including messages in child branches."""
    ids = tuple(ids or ())
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    rows = store.q(f"""
        WITH RECURSIVE branch(root,id) AS (
            SELECT id,id FROM units WHERE id IN ({marks})
            UNION
            SELECT b.root,l.child_id FROM unit_links l JOIN branch b ON l.parent_id=b.id
        )
        SELECT b.root, min(m.sent_at) AS first_at, max(m.sent_at) AS last_at
        FROM branch b
        LEFT JOIN unit_messages um ON um.unit_id=b.id
        LEFT JOIN messages m ON m.id=um.message_id
        GROUP BY b.root
    """, ids)
    return {r["root"]: (r["first_at"], r["last_at"]) for r in rows}


def get(store, uid: int, depth: int = 1, with_messages=True, _stats=None) -> dict | None:
    r = store.q("SELECT * FROM units WHERE id=?", (uid,)).fetchone()
    if not r:
        return None
    _stats = rollup_stats(store) if _stats is None else _stats
    d = _row(store, r, _stats)
    d["parents"] = [dict(x) for x in store.q(
        "SELECT u.id, u.kind, u.title, l.relation FROM unit_links l JOIN units u ON u.id=l.parent_id WHERE l.child_id=?", (uid,))]
    kids = store.q("""SELECT DISTINCT u.* FROM unit_links l JOIN units u ON u.id=l.child_id WHERE l.parent_id=?
                      ORDER BY coalesce(u.due_at, u.start_at, u.last_activity_at)""", (uid,)).fetchall()
    d["children"] = ([get(store, k["id"], depth - 1, False, _stats) for k in kids] if depth > 1
                     else _rows(store, kids, _stats))
    if with_messages:
        d["messages"] = [dict(x) for x in store.q(
            """SELECT m.id, m.source, m.account, m.channel_name, m.sender_name, m.sent_at, m.text, m.link
               FROM unit_messages um JOIN messages m ON m.id=um.message_id WHERE um.unit_id=? ORDER BY m.sent_at LIMIT 200""", (uid,))]
    return d


def source_targets(store, ids) -> dict:
    """One jump target per unit: latest linked direct evidence, then descendant evidence."""
    if not ids:
        return {}
    rows = store.q(f"""
        WITH RECURSIVE branch(root,id) AS (
            SELECT id,id FROM units WHERE id IN ({','.join('?' for _ in ids)})
            UNION
            SELECT b.root,u.id FROM unit_links l JOIN branch b ON l.parent_id=b.id
            JOIN units u ON u.id=l.child_id
        ), targets AS (
            SELECT b.root, trim(m.link) AS link, coalesce(m.channel_name,m.channel,m.source) AS channel,
                   row_number() OVER (PARTITION BY b.root ORDER BY (b.id=b.root) DESC,
                                      coalesce(m.sent_at,0) DESC, m.id DESC) AS rank
            FROM branch b JOIN unit_messages um ON um.unit_id=b.id JOIN messages m ON m.id=um.message_id
            WHERE lower(trim(m.link)) LIKE 'https://%' OR lower(trim(m.link)) LIKE 'http://%'
               OR lower(trim(m.link)) LIKE 'lark:%' OR lower(trim(m.link)) LIKE 'feishu:%'
        )
        SELECT root,link,channel FROM targets WHERE rank=1
        """, tuple(ids))
    return {r["root"]: {"open_link": r["link"], "open_channel": r["channel"]} for r in rows}


def _top_level_matches(store, rows):
    """Keep matches without a matching ancestor, including across nonmatching bridges."""
    if not rows:
        return rows
    children = {}
    for link in store.q("SELECT DISTINCT l.parent_id,l.child_id FROM unit_links l "
                        "JOIN units p ON p.id=l.parent_id JOIN units c ON c.id=l.child_id"):
        children.setdefault(link["parent_id"], set()).add(link["child_id"])
    # One shared traversal finds all descendants of any match before the result limit.
    pending = [child for row in rows for child in children.get(row["id"], ())]
    descendants = set()
    while pending:
        child = pending.pop()
        if child in descendants:
            continue
        descendants.add(child)
        pending.extend(children.get(child, ()))
    return [row for row in rows if row["id"] not in descendants]


def query(store, tags=(), kind=None, status=None, q=None, since=None, due_before=None, roots_only=False,
          order="activity", limit=200, mine=False, unscheduled=False) -> list[dict]:
    where, args = [], []
    groups = {}                                   # namespace -> [tags]: OR within a namespace, AND across
    for t in tags or ():
        if (t := norm_tag(t)):
            groups.setdefault(t.split(":", 1)[0], []).append(t)
    prefix = ""
    source_tags = groups.pop("source", [])
    if source_tags:
        placeholders = ','.join('?' * len(source_tags))
        # Sources describe the evidence contained in a unit, including all child branches.
        # Walk upwards from matching tags/evidence; UNION handles shared parents and cycles.
        prefix = (f"WITH RECURSIVE source_matches(id) AS ("
                  f"SELECT t.unit_id FROM unit_tags t JOIN units s ON s.id=t.unit_id "
                  f"WHERE t.tag IN ({placeholders}) "
                  f"UNION SELECT um.unit_id FROM unit_messages um JOIN units s ON s.id=um.unit_id "
                  f"JOIN messages m ON m.id=um.message_id WHERE "
                  f"m.source || CASE WHEN coalesce(m.account,'')='' THEN '' ELSE ':' || m.account END "
                  f"IN ({placeholders}) "
                  f"UNION SELECT l.parent_id FROM unit_links l JOIN source_matches s ON s.id=l.child_id "
                  f"JOIN units p ON p.id=l.parent_id) ")
        args += source_tags + [tag.removeprefix("source:") for tag in source_tags]
        where.append("u.id IN (SELECT id FROM source_matches)")
    for _, gtags in groups.items():
        where.append(f"u.id IN (SELECT unit_id FROM unit_tags WHERE tag IN ({','.join('?' * len(gtags))}))")
        args += gtags
    if kind:
        kinds = kind if isinstance(kind, (list, tuple)) else [kind]
        where.append(f"u.kind IN ({','.join('?' * len(kinds))})")
        args += list(kinds)
    if status and status != "all":
        where.append("u.status=?")
        args.append(status)
    elif not status:
        where.append("u.status != 'dropped'")
    if since:
        where.append("coalesce(u.last_activity_at, u.updated_at) >= ?")
        args.append(int(since))
    if due_before:
        where.append("coalesce(u.due_at, u.start_at) IS NOT NULL AND coalesce(u.due_at, u.start_at) <= ?")
        args.append(int(due_before))
    if mine:
        where.append("u.for_me=1")
    if unscheduled:
        where.append("u.start_at IS NULL")
    # Whitespace-separated literal keywords use AND across title/summary matches.
    for term in (q or "").split():
        if len(term) >= 3:
            where.append("u.id IN (SELECT rowid FROM units_fts WHERE units_fts MATCH ?)")
            args.append('"' + term.replace('"', '""') + '"')
        else:
            where.append("(u.title LIKE ? ESCAPE '\\' OR u.summary LIKE ? ESCAPE '\\')")
            pattern = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            args.extend([pattern, pattern])
    order_sql = {
        "activity": "coalesce(u.last_activity_at,u.updated_at) DESC, u.id DESC",
        "oldest": "coalesce(u.last_activity_at,u.updated_at) ASC, u.id ASC",
        "due": "coalesce(u.due_at,u.start_at) IS NULL, coalesce(u.due_at,u.start_at), u.id DESC",
        "created": "u.created_at DESC, u.id DESC",
        "title": "u.title COLLATE NOCASE ASC, u.id ASC",
    }.get(order, "coalesce(u.last_activity_at,u.updated_at) DESC, u.id DESC")
    sql = prefix + f"SELECT u.* FROM units u {'WHERE ' + ' AND '.join(where) if where else ''}"
    metric = {"messages": "n_messages", "children": "n_children", "descendants": "n_descendants"}.get(order)
    if metric or roots_only:
        # Select the outermost matches and sort all candidates BEFORE applying the limit.
        rows = store.q(sql + f" ORDER BY {order_sql}", args).fetchall()
        if roots_only:
            rows = _top_level_matches(store, rows)
        if not rows:
            return []
        stats = rollup_stats(store)
        if metric:
            rows.sort(key=lambda row: (stats[row["id"]][metric], row["last_activity_at"] or row["updated_at"] or 0,
                                       row["id"]), reverse=True)
        return _rows(store, rows[:max(0, int(limit))], stats)
    return _rows(store, store.q(sql + f" ORDER BY {order_sql} LIMIT ?", (*args, int(limit))))


def tag_counts(store, since=None, prefix=None, status=None, limit=300) -> list[dict]:
    where, args = [], []
    if since:
        where.append("coalesce(u.last_activity_at, u.updated_at) >= ?")
        args.append(int(since))
    if prefix:
        where.append("t.tag LIKE ?")
        args.append(prefix + "%")
    if status:
        where.append("u.status=?")
        args.append(status)
    sql = (f"SELECT t.tag, count(*) AS n FROM unit_tags t JOIN units u ON u.id=t.unit_id "
           f"{'WHERE ' + ' AND '.join(where) if where else ''} GROUP BY t.tag ORDER BY n DESC LIMIT ?")
    return [dict(r) for r in store.q(sql, (*args, limit))]


def iso_to_epoch(value, tz) -> int | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return int(dt.timestamp())


def apply_result(store, result: dict, *, origin: str, job_key: str, tz=timezone.utc,
                 allowed_message_ids: set[int] | None = None, default_parent: int | None = None,
                 base_tags: list[str] | None = None, me_names: list[str] | None = None) -> dict:
    """Apply an organizer result (AI or external agent) to the graph. Shared by both paths.

    result = {"units": [{ref, existing_id, kind, title, summary, tags, people, due, start, status, parent, message_ids}],
              "close_units": [id, ...]}
    `parent` is another ref in the same result, "unit:<id>" for an existing unit, or null.
    """
    refs, created, updated, linked = {}, 0, 0, 0
    items = result.get("units") or []
    for u in items:
        kind = u.get("kind") if u.get("kind") in KINDS else "topic"
        title = (u.get("title") or "").strip()[:200]
        if not title:
            continue
        due, start = iso_to_epoch(u.get("due"), tz), iso_to_epoch(u.get("start"), tz)
        status = u.get("status") if u.get("status") in ("open", "done") else None
        ex = u.get("existing_id")
        if ex and store.q("SELECT 1 FROM units WHERE id=?", (int(ex),)).fetchone():
            uid = int(ex)
            update(store, uid, **{k: v for k, v in dict(title=title, summary=u.get("summary"), status=status,
                                                       due_at=due, start_at=start).items() if v})
            updated += 1
        else:
            uid = upsert(store, f"{job_key}:{u.get('ref') or len(refs)}", kind=kind, title=title, summary=u.get("summary"),
                         status=status, due_at=due, start_at=start, origin=origin)
            created += 1
        refs[u.get("ref") or str(uid)] = uid
        add_tags(store, uid, [f"type:{kind}"] + list(base_tags or []) + list(u.get("tags") or [])
                 + [f"person:{p}" for p in u.get("people") or []])
        mids = [int(m) for m in u.get("message_ids") or [] if str(m).isdigit()]
        if allowed_message_ids is not None:
            mids = [m for m in mids if m in allowed_message_ids]
        attach(store, uid, mids)
        mark_for_me(store, uid, me_names)
    for u in items:
        uid = refs.get(u.get("ref"))
        if not uid:
            continue
        p = u.get("parent")
        pid = None
        if isinstance(p, str) and p.startswith("unit:") and p[5:].isdigit():
            pid = int(p[5:]) if store.q("SELECT 1 FROM units WHERE id=?", (int(p[5:]),)).fetchone() else None
        elif p in refs:
            pid = refs[p]
        pid = pid or default_parent
        if pid and link(store, pid, uid):
            linked += 1
    closed = 0
    for cid in result.get("close_units") or []:
        if str(cid).isdigit():
            closed += store.q("UPDATE units SET status='done', updated_at=? WHERE id=? AND status='open'",
                              (int(time.time()), int(cid))).rowcount
    store.db.commit()
    return {"created": created, "updated": updated, "linked": linked, "closed": closed}


def mark_for_me(store, uid: int, names: list[str] | None = None) -> bool:
    """A unit concerns you when its evidence is directed at you (an @mention of you, an @everyone
    announcement, your own message or a one-to-one chat), or it is a homework/deadline obligation."""
    row = store.q("""SELECT max(m.mentions_me), max(m.from_me), max(m.is_group=0) FROM unit_messages um
                     JOIN messages m ON m.id=um.message_id WHERE um.unit_id=?""", (uid,)).fetchone()
    obligation = store.q("SELECT 1 FROM unit_tags WHERE unit_id=? AND tag IN ('type:homework','type:deadline')", (uid,)).fetchone()
    people = {str(n).strip() for n in (names or []) if str(n).strip()}
    assigned = bool(people and store.q(
        "SELECT 1 FROM unit_tags WHERE unit_id=? AND tag LIKE 'person:%' "
        "AND substr(tag,8) IN ({}) LIMIT 1".format(",".join("?" * len(people))),
        (uid, *people)).fetchone()) if people else False
    if (row and any(row)) or obligation or assigned:
        store.q("UPDATE units SET for_me=1 WHERE id=?", (uid,))
        return True
    return False


def _like_value(value: str) -> str:
    """Escape LIKE wildcards so a name is matched literally."""
    return str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def rescan_mentions(store, names: list[str], reset: bool = False) -> int:
    """Re-flag stored messages that @mention you or @everyone, after me.names changed.

    With ``reset`` the flags are recomputed from scratch, so names removed from the
    list stop matching as well."""
    from ..sync.triage import ALL_MENTIONS
    if reset:
        store.q("UPDATE messages SET mentions_me=0")
    changed = 0
    for name in names or []:
        name = str(name).strip()
        if not name:
            continue
        changed += store.q("UPDATE messages SET mentions_me=1 WHERE mentions_me=0 AND text LIKE ? ESCAPE '\\'",
                           (f"%@{_like_value(name)}%",)).rowcount
    for tok in ALL_MENTIONS:
        changed += store.q("UPDATE messages SET mentions_me=1 WHERE mentions_me=0 AND text LIKE ? ESCAPE '\\'",
                           (f"%{_like_value(tok)}%",)).rowcount
    store.db.commit()
    return changed


def apply_me_names(store, names: list[str] | None = None) -> dict:
    """Apply a changed me.names list to existing messages and unit 'For me' flags."""
    names = [str(n).strip() for n in (names or []) if str(n).strip()]
    changed = rescan_mentions(store, names, reset=True)
    total = recompute_for_me(store, names)
    return {"mentions": changed, "for_me": total}


def recompute_for_me(store, names: list[str] | None = None) -> int:
    """Recompute 'for me' authoritatively. A unit is yours when its evidence is directed at you
    (an @mention of you, an @everyone announcement, your own message, or a one-to-one chat), is
    explicitly assigned to one of your configured names, or is a homework/deadline obligation."""
    people = {str(n).strip() for n in (names or []) if str(n).strip()}
    args = []
    assigned = ""
    if people:
        assigned = " OR id IN (SELECT unit_id FROM unit_tags WHERE tag LIKE 'person:%' AND substr(tag,8) IN (" \
                   + ",".join("?" * len(people)) + "))"
        args.extend(people)
    store.q("""UPDATE units SET for_me = CASE WHEN (
                 id IN (SELECT um.unit_id FROM unit_messages um JOIN messages m ON m.id=um.message_id
                        WHERE m.mentions_me=1 OR m.from_me=1 OR m.is_group=0)
                 OR id IN (SELECT unit_id FROM unit_tags WHERE tag IN ('type:homework','type:deadline'))"""
            + assigned + ") THEN 1 ELSE 0 END", tuple(args))
    store.db.commit()
    return store.q("SELECT count(*) FROM units WHERE for_me=1").fetchone()[0]
