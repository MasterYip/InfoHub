# Today

Today is the home page. It replaces the separate Today / Todos / Calendar tabs with one page of
three lanes that share a single **For me / Everything** switch. Old `?tab=todos` and
`?tab=calendar` links open Today.

| Lane | Shows | Time rule |
| --- | --- | --- |
| **To do** | Open `todo` and `question` units | None. Grouped as Overdue, the date ranges below, No due date |
| **Schedule** | Open `event` units, plus open units tagged `type:deadline` or `type:homework` that are not todos (e.g. an announcement with a due date) | None. Grouped as Happening now, the date ranges below, Time to be decided, Past but not closed (collapsed) |
| **Recent** | Flagged messages not organized yet, then one flat list of topics, decisions, announcements, resources and notes, newest first | The only window: **1 day / 7 days / 30 days / All time**, remembered per device. Open items only; done and ignored ones stay in Explore |

Date ranges cover every future date and follow local calendar days: **Within 7 days** (today and
the next six days) and **Future** (everything after). Empty ranges are not shown; all
ranges start open and remember being folded (`localStorage["todaySections.v2"]`). The server
returns one date-sorted `upcoming` list per lane and the dashboard splits it, so the ranges use
the viewer's time zone.

Each lane has **type chips** on top (all on by default; **All** turns every type on, or off when
they are all on). A unit's types are its kind plus type tags such as `deadline` or `homework`; it
stays visible while any of its types is on. Chips show counts before filtering, the choice is kept
per lane in `localStorage.todayHide`, and Recent filters on the server before paging so totals stay
exact. Flagged messages are not units and are not affected.

Rules that keep this predictable:

- **Unfinished items never age out.** An open todo or question stays in To do until it is
  marked done or ignored, however old its last message is. Time only decides its group.
- **Nothing is cut before it is counted.** Every group header shows its full size. To do and
  Schedule return every match. Recent pages through the total with **Show more**.
- **Every lane uses one scope.** For me includes units you marked as for me and excludes units you
  marked as not for me (right-click a card). Those choices outlive automatic re-detection.
- The scope switch appears only on Today and Explore, the tabs it actually filters. Until a device
  picks one, Settings → *Today shows* is the default.
- A `decision` is a record, not a task, so it appears in Recent rather than To do.
- Homework is a tag, not a kind: `organize.tag_rules` adds `type:homework` when a message says
  作业 / homework / assignment, and the AI organizer may add it too. A homework todo sits in To do;
  a homework announcement or note sits in Schedule. Both count as for me, so no separate Homework or
  Deadlines view is needed; Explore filters by those tags when you want to see only them.
- Completed items are one click away: **Completed in the last 30 days →** under To do (todos and
  questions) and under Schedule (events) opens Explore with that filter.

API: `GET /api/today?scope=mine|all&days=1|7|30|0&limit=30&msg_limit=30&hide_todo=&hide_schedule=&hide_recent=` returns
`{todo: {overdue, upcoming, undated}, schedule: {now, upcoming, tbd, past},
recent: {days, units, units_total, messages, messages_total}, types: {todo, schedule, recent}, scope, now}`.
`POST /api/unit/<id>` with `{"for_me": true | false | null}` pins or unpins the relevance.

## Language

The dashboard, read-only viewer and login page switch between English and Chinese with the
**EN | 中** switch in the header. The first visit follows the browser language, and the choice is
stored in `localStorage.lang`. English strings are the keys. Chinese lives in `I18N_ZH` in
`infohub/apps/web/index.html`, and a missing entry falls back to English. Stored data (kinds,
tags, policies) is never rewritten; only its display is translated.

Saved views (`apps.views`) take their Chinese tab name from `name_zh`, falling back to the built-in table.

## Header

Page tabs sit on the left. Assistant, Status and Settings are icon buttons on the right (tooltip
and screen-reader label give the name, the current page is highlighted), followed by the
EN | 中 switch, the theme toggle and Log out.
