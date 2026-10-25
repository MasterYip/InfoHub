# Explore: shared sorting and filters

List and Graph use one sidebar and the same backend query. Switching views preserves
keywords, tags, kinds, activity window, status, scope, top-level selection and sorting.
Preferences are saved in the current browser or desktop profile.

## Sidebar

- **Keywords** match the unit's title or summary. Press Enter or Apply. Space-separated
  terms use AND; short Chinese terms work too. `%` and `_` are literal characters.
- **Sort** includes activity, due date, creation time, title, total message count,
  subtopic count across all levels, and direct subtopic count.
- **Scope** selects Everything or For me in the header, including in Explore.
- **Activity**, **Status**, **Kinds**, **Top-level matches only** and **Tags** apply equally
  to both views. Multiple kinds use OR. Tags use OR within a namespace and AND across
  namespaces; all other filter categories are combined with AND.
- **Source** includes a unit's own source tags and attached evidence, plus the sources
  of linked subtopics at every depth. A parent topic containing Slack subtopics matches
  `source:slack` even without its own source tag. A topic spanning multiple sources
  matches any of those sources. This is calculated from the current links and evidence.
  Other tag namespaces and the For me scope still match the unit itself; a parent's
  sources do not assign those sources to unrelated child or sibling units.
- **Top-level matches only** keeps matches with no other matching ancestor, after
  applying scope, tags, kinds, keywords, activity and status. An excluded digest or
  a parent outside For me does not hide a matching topic. Matching ancestors still
  suppress descendants across intermediate units that do not match.
- **Clear filters** clears Explore filters and keeps the header scope and sorting.
- Clicking a tag keeps the sidebar, results and page at their current scroll positions
  while the results refresh. If a shorter result cannot fill that position, scrolling
  is limited to its new maximum.

Both views request the same ordered matching units and the same 400-unit limit.
The List shows matching units directly; Expand still allows inspection of a unit's
children and direct evidence. Graph uses the same matches as nodes and draws the
existing links between those nodes. Its spatial layout continues to follow the links;
query ordering controls result order and which units are included when the limit is reached.

## Strict filtering and optional graph context

Graph is strict by default. Filtering to `topic` shows matching topic units; it does
not automatically add a parent `digest`, or children of another kind, that fail the filter.

Enable **Show linked context (Graph)** to add one hop of parents/children around the
matches. Context nodes are faded and their labels say `(context)`. The graph counter
separates matches from context nodes. This option does not change the List's matches.
Physics controls stay in the graph panel because they affect layout, not filtering.

Click a node to open its floating card. Cards stay within the viewport when opened,
expanded, resized or dragged; long content scrolls inside the card. Clicking/dragging
back on the graph, scrolling to zoom, or changing physics controls dismisses the card
and cancels any pending open request. Moving the pointer alone keeps the card open.

The HTTP `/api/units` and `/api/graph` endpoints accept the same `tags`, `kind` (comma
separated), `status`, `q`, `days`, `mine`, `roots`, `order` and `limit` parameters.
`context=1` explicitly adds graph neighbors; default `context=0` is strict.
Graph responses include `matched_count`, `context_count` and a per-node `seed` flag.

## Message and subtopic totals

A unit's total message count follows its child branches:

```text
total_messages(unit) = directly_attached_messages(unit)
                     + sum(total_messages(child) for each direct child)
```

This is a count of message attribution across branches. It deliberately allows repeated
counting: the same message attached to two subtopics contributes twice. A shared subtopic
under two different parent branches contributes through both branches to a containing
ancestor. A message attached directly to the ancestor and to a descendant contributes
in both places. Direct attachments within one unit remain unique, as defined by the store.
Parallel relation labels for the same parent/child pair do not create another child placement.

For example, an ancestor has one direct message. Its left child has two direct messages,
its right child has one, and both children contain the same two-message subtopic:

```text
left total = 2 + 2 = 4
right total = 1 + 2 = 3
ancestor total = 1 + 4 + 3 = 8
```

Counts include all descendant levels independently of the current display filters.
**Most subtopics (all levels)** likewise counts each containing branch; **Most direct
subtopics** counts immediate children. Aggregate sorting is done before applying the
result limit. The API exposes `n_messages` (total), `n_direct_messages`, `n_children`
and `n_descendants` on units and graph nodes.

Cards show total messages and subtopics. Their count tooltips distinguish direct
attachments. Graph node area increases with total message count, normalized to the
largest visible count; child count does not separately inflate node size. Hovering a
node shows its total/direct message counts and subtopic count.
