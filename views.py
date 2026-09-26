"""Structured views, rendered by code from templates with fmt.py. The LLM only supplies short
human lines (a lead-in, an event's "why", a reminder's context line). No channel code here."""

from datetime import date, datetime, timedelta

import fmt
import schedule
from fmt import View, esc
from schedule import TZ, Entry

WEEK_BUTTONS = [[("◀ This week", "week:0"), ("Next week ▶", "week:1")]]
BRIEF_BUTTONS = [[("📅 Full week", "week:0:new"), ("🎟 Events", "events")]]


# ---------- Agenda ----------


def entry_lines(e: Entry, today: date | None = None, extra: str | None = None) -> list[str]:
    """One agenda entry: bold time line, then ↳ details."""
    if e.kind in ("deadline", "exam"):
        lines = [fmt.time_line(f"due {e.time_text}" if e.time_text != "any time" else "due today",
                               fmt.short_title(e.title, e.course), e.kind)]
        lines.append(fmt.sub(e.course, "🔒 Needs LockDown Browser" if e.note else None))
    else:
        title = fmt.short_title(e.title)
        lines = [fmt.time_line(e.time_text, title, e.kind)]
        lines.append(fmt.sub(e.place, e.who))
    if extra:
        lines.append(fmt.sub(extra))
    return [l for l in lines if l]


def deadline_bullet(e: Entry, today: date, compact: bool = False) -> list[str]:
    """"• 📌 Title · <b>Thu 11:59 PM</b>" with ↳ course and LockDown note. Compact: one line,
    with a short course name and 🔒 inline."""
    when = fmt.day_time(e.day, e.item.due_time if e.item else None, today)
    if compact:
        course = fmt.short_title(e.course, limit=20) if e.course else ""
        extra = (f" · {esc(course)}" if course else "") + (" · 🔒" if e.note else "")
        return [fmt.bullet(f"{esc(fmt.short_title(e.title, e.course, limit=38))} · <b>{esc(when)}</b>{extra}", e.kind)]
    lines = [fmt.bullet(f"{esc(fmt.short_title(e.title, e.course))} · <b>{esc(when)}</b>", e.kind)]
    s = fmt.sub(e.course, "🔒 Needs LockDown Browser" if e.note else None)
    if s:
        lines.append("  " + s)
    return lines


def days_block(entries: list[Entry], today: date, extras: dict | None = None) -> list[str]:
    """Day-grouped agenda: bold "TUE 29" headers, entries under each, a blank line between days."""
    sections, cur_day, cur = [], None, []
    for e in entries:
        if e.day != cur_day:
            if cur:
                sections.append("\n".join(cur))
            cur_day, cur = e.day, [fmt.day_header(e.day) + (" · today" if e.day == today else "")]
        cur += entry_lines(e, today, (extras or {}).get(id(e)))
    if cur:
        sections.append("\n".join(cur))
    return sections


def _range(start: date, end: date) -> str:
    return f"{start:%b} {start.day}" if start == end else f"{start:%b} {start.day} to {end:%b} {end.day}"


def agenda_view(user_id, start: date, end: date, now: datetime, title: str, kinds: set[str] | None = None,
                buttons=None, lead: str | None = None, empty: str | None = None) -> View:
    today = now.date()
    entries = schedule.agenda_entries(user_id, start, end, now, kinds)
    head = fmt.header("📅", title, _range(start, end))
    if not entries:
        return fmt.view(fmt.join(head, esc(empty or "Nothing on your calendar then.")), buttons)
    only_deadlines = kinds is not None and kinds <= schedule.DEADLINE_KINDS
    if only_deadlines:
        body = [fmt.section(head, [l for e in entries for l in deadline_bullet(e, today)])]
    else:
        body = [head] + days_block(entries, today)
    return fmt.view(fmt.join(esc(lead) if lead else None, *body), buttons)


def week_view(user_id, offset: int = 0, now: datetime | None = None) -> View:
    now = now or datetime.now(TZ)
    start = now.date() + timedelta(days=7 * offset)
    return agenda_view(user_id, start, start + timedelta(days=6), now,
                       "This week" if offset == 0 else "Next week", buttons=WEEK_BUTTONS)


def today_view(user_id, now: datetime | None = None) -> View:
    now = now or datetime.now(TZ)
    d = now.date()
    entries = schedule.agenda_entries(user_id, d, d, now)
    head = fmt.header("📅", "Today", f"{d:%a} {d:%b} {d.day}")
    if not entries:
        return fmt.view(fmt.join(head, "Nothing on today."), BRIEF_BUTTONS)
    lines = [l for e in entries for l in entry_lines(e, d)]
    return fmt.view(fmt.section(head, lines), BRIEF_BUTTONS)


def period_view(user_id, start: date, end: date, period: str, kinds: set[str] | None, now: datetime,
                lead: str | None = None) -> View:
    """Answer to a schedule question, same layout as /week. Empty period: say so, then what's next."""
    what = "due" if kinds and kinds <= schedule.DEADLINE_KINDS else ("meetings" if kinds == {"event"} else "on")
    title = {"due": "Due", "meetings": "Meetings", "on": "Schedule"}[what] + f" {period}"
    entries = schedule.agenda_entries(user_id, start, end, now, kinds)
    if entries:
        return agenda_view(user_id, start, end, now, title, kinds, lead=lead)
    empty = {"due": "Nothing due", "meetings": "No meetings", "on": "Nothing on"}[what] + f" {period} ({_range(start, end)})."
    nxt_start, nxt_end = end + timedelta(days=1), end + timedelta(days=7)
    nxt = agenda_view(user_id, nxt_start, nxt_end, now, "Next up", kinds)
    if not schedule.agenda_entries(user_id, nxt_start, nxt_end, now, kinds):
        return fmt.view(fmt.join(fmt.header("📅", title), esc(empty)))
    return fmt.view(fmt.join(fmt.header("📅", title), esc(empty), nxt.html + ("\n\n" + "\n\n".join(nxt.more) if nxt.more else "")))


# ---------- Events ----------


def event_lines(s, today: date, why: str | None = None) -> list[str]:
    """One event suggestion: bold day and time, then place and host, why, notes, Register link."""
    import events

    e = s.event
    when = "all day " + fmt.day_time(e.start.date(), None, today) if e.all_day else fmt.day_time(e.start.date(), e.start.time(), today)
    lines = [fmt.time_line(when, fmt.short_title(e.title), "event")]
    lines.append(fmt.sub(events.where(e), fmt.short_title(e.organizer or "", limit=40) or None))
    lines.append(fmt.sub(why or s.reason))
    notes = events.flag_notes(e)
    if s.deadline:
        notes.insert(0, f"register by {s.deadline:%a %b} {s.deadline.day}")
    notes.append(f"clashes with {s.conflicts[0]}" if s.conflicts else "you're free")
    status = " · ".join(esc(n[:1].upper() + n[1:]) for n in notes)
    lines.append(fmt.sub_html(status + (f" · {fmt.link(e.url, 'Register')}" if e.url else "")))
    if s.label == "last-minute":
        lines[0] += " · <i>heads up, it's soon</i>"
    return [l for l in lines if l]


def event_buttons(items) -> list[list[tuple[str, str]]]:
    rows = []
    for s in items:
        name = fmt.short_title(s.event.title, limit=18)
        rows.append([(f"✅ Going · {name}", f"ev:going:{s.event.id}"),
                     ("⏰ Later", f"ev:later:{s.event.id}"), ("✖ Not for me", f"ev:hide:{s.event.id}")])
    return rows


def events_view(items, now: datetime, title: str = "Worth your time", lead: str | None = None,
                whys: dict | None = None) -> View:
    today = now.date()
    blocks = ["\n".join(event_lines(s, today, (whys or {}).get(s.event.id))) for s in items]
    return fmt.view(fmt.join(fmt.header("event", title), esc(lead) if lead else None, *blocks), event_buttons(items))


def events_by_day_view(items, now: datetime) -> View:
    import events

    today = now.date()
    head = fmt.header("event", "Events this week", f"{len(items)} picks")
    sections, cur_day, cur = [], None, []
    for s in sorted(items, key=lambda s: s.event.start):
        d = s.event.start.date()
        if d != cur_day:
            if cur:
                sections.append("\n".join(cur))
            cur_day, cur = d, [fmt.day_header(d)]
        e = s.event
        t = "All day" if e.all_day else fmt.fmt_time(e.start.time())
        cur.append(fmt.time_line(t, fmt.short_title(e.title), "event"))
        cur.append(fmt.sub_html(" · ".join(x for x in [esc(events.where(e)), fmt.link(e.url, "Register")] if x)))
    if cur:
        sections.append("\n".join(cur))
    return fmt.view(fmt.join(head, *sections))


# ---------- Reminders ----------

_LEAD_HEAD = {
    "1w": "In 1 week", "3d": "In 3 days", "1d": "Tomorrow", "3h": "In 3 hours", "2h": "In 2 hours",
    "1h": "In 1 hour", "30m": "In 30 min", "approval": "Check your approval",
}


def reminder_view(it: schedule.Item, lead: str, context: str | None = None) -> View:
    """Compact reminder: "⏰ <b>In 30 min</b>", the item with its time, ↳ details, ↳ context."""
    head = f"⏰ <b>{esc(_LEAD_HEAD.get(lead, 'Reminder'))}</b>"
    if it.kind in ("class", "class_session"):
        title, who = schedule.split_class_title(it.title) if it.kind == "class_session" else (it.course or it.title, None)
        t = it.start_time if it.kind == "class" else it.due_time
        line = f"🎓 {esc(fmt.short_title(title))} · {fmt.fmt_time(t)}" if t else f"🎓 {esc(title)}"
        details = [fmt.sub(it.room, who)]
    elif it.kind in schedule.DEADLINE_KINDS:
        icon = "📝" if it.kind == "exam" else "📌"
        line = f"{icon} {esc(fmt.short_title(it.title, it.course))} · {esc(schedule.fmt_due(it))}"
        details = [fmt.sub(it.course, "🔒 Needs LockDown Browser" if it.note and "LockDown" in it.note else None)]
    else:
        icon = "🎟" if it.source and it.source not in schedule.CALENDAR_SOURCES else "🤝"
        when = fmt.fmt_time(it.due_time) if it.due_time else "all day"
        if lead in ("1d", "approval", "1w", "3d"):
            when = schedule.fmt_due(it)
        line = f"{icon} {esc(fmt.short_title(it.title))} · {esc(when)}"
        details = [fmt.sub(it.room)]
        if lead == "approval":
            details.append(fmt.sub("Needs host approval, check you got in"))
    if context:
        details.append(fmt.sub(context))
    return fmt.view("\n".join([head, line] + [d for d in details if d]))
