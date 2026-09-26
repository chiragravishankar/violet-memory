"""One formatter for every structured reply (Telegram HTML). No channel code here: views are
returned as View objects (HTML text plus abstract buttons) and bot.py turns them into messages.

Rules, used by every view:
- Section headers: emoji + bold uppercase ("📅 <b>TODAY</b>"). One blank line between sections;
  empty sections are skipped.
- Times bold and first: "<b>2:00 PM</b>   Operations Management". Details on the next line with "↳ ".
- Day headers bold: "<b>TUE 29</b>". Bullets "•" for deadlines and exams.
- Icons: 🎓 class 📌 deadline 📝 exam 🤝 meeting 🎟 event 👤 person 💡 tip.
- Links as tappable text ("Register"). Titles trimmed to ~45 chars. No em dashes.
- All user and event text is escaped. Messages longer than ~25 lines get a "Show more" button.
"""

import html
import re
from dataclasses import dataclass, field
from datetime import date, time

ICON = {"class": "🎓", "deadline": "📌", "exam": "📝", "meeting": "🤝", "event": "🎟", "person": "👤", "tip": "💡"}
MAX_LINES = 25
TITLE_MAX = 45
GAP = "   "  # between a bold time and the title


@dataclass
class View:
    """A structured message: HTML text and rows of buttons [(label, action)]."""
    html: str
    buttons: list[list[tuple[str, str]]] = field(default_factory=list)
    more: list[str] = field(default_factory=list)  # further pages for "Show more"

    @property
    def plain(self) -> str:
        return to_plain(self.html)


def esc(text) -> str:
    """Escape user and event text for Telegram HTML (&, <, >) and remove em dashes."""
    return html.escape(nodash(str(text or "")), quote=False)


def nodash(text: str) -> str:
    return text.replace("—", ", ").replace("–", "-")


def to_plain(text: str) -> str:
    """HTML to plain text: the fallback if Telegram rejects the formatting."""
    text = re.sub(r'<a href="([^"]+)">([^<]*)</a>', r"\2: \1", text)
    return html.unescape(re.sub(r"<[^>]+>", "", text))


_EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\uFE0F\u200D]")
_CODE = re.compile(r"^(Proctoring Enabled:\s*)?([A-Z]{2,4}-[A-Z]{2}\s*\d{3,4}\s+((Fall|Spring|Summer)\s+\d{4}\s+)?)", re.I)


def short_title(title: str, course: str = "", limit: int = TITLE_MAX) -> str:
    """Clean and trim a title: no " - Due", no course code, no "(Course)" if the course is shown."""
    t = _EMOJI.sub("", title.strip())  # icons carry meaning here; emoji in titles would muddle them
    t = re.sub(r"\s*-\s*Due\s*$", "", t, flags=re.I)
    t = _CODE.sub("", t)
    if course:
        t = re.sub(rf"\s*\({re.escape(course)}\)\s*", " ", t, flags=re.I).strip()
    t = re.sub(r"\s+", " ", nodash(t)).strip(" -,")
    return t if len(t) <= limit else t[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,:-") + "…"


def header(kind_or_emoji: str, title: str, extra: str = "") -> str:
    emoji = ICON.get(kind_or_emoji, kind_or_emoji)
    return f"{emoji} <b>{esc(title.upper())}</b>" + (f" · {esc(extra)}" if extra else "")


def time_line(when: str, title: str, icon: str = "") -> str:
    """"<b>2:00 PM</b>   🎓 Operations Management" (title already escaped or plain)."""
    ic = f"{ICON.get(icon, icon)} " if icon else ""
    return f"<b>{esc(when)}</b>{GAP}{ic}{esc(title)}"


def sub(*parts: str | None) -> str | None:
    """"↳ 2 MetroTech · Prof. Jacoby", or None if every part is empty."""
    seen, out = set(), []
    for p in parts:
        if p and p.strip().lower() not in seen:
            seen.add(p.strip().lower())
            out.append(esc(p))
    return f"↳ {' · '.join(out)}" if out else None


def sub_html(html_text: str) -> str:
    """A ↳ line whose content is already HTML (for links)."""
    return f"↳ {html_text}"


def bullet(text: str, icon: str = "") -> str:
    ic = f"{ICON.get(icon, icon)} " if icon else ""
    return f"• {ic}{text}"


def link(url: str | None, label: str = "Register") -> str:
    if not url:
        return ""
    return f'<a href="{html.escape(url, quote=True)}">{esc(label)}</a>'


def day_header(d: date) -> str:
    return f"<b>{d:%a} {d.day}</b>".replace(f"{d:%a}", f"{d:%a}".upper())


def fmt_time(t: time) -> str:
    return f"{t.hour % 12 or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def day_time(d: date, t: time | None, today: date | None = None) -> str:
    """"today 2:00 PM", "Thu 11:59 PM", "Fri Oct 9"."""
    if today and d == today:
        name = "today"
    elif today and (d - today).days == 1:
        name = "tomorrow"
    elif today and 0 < (d - today).days < 7:
        name = f"{d:%a}"
    else:
        name = f"{d:%a} {d:%b} {d.day}"
    return f"{name} {fmt_time(t)}" if t else name


def section(head: str, lines: list[str | None]) -> str | None:
    body = [l for l in lines if l]
    return "\n".join([head] + body) if body else None


def join(*sections: str | None) -> str:
    """Sections separated by one blank line; empty ones skipped."""
    return "\n\n".join(s for s in sections if s)


def paginate(text: str, max_lines: int = MAX_LINES) -> list[str]:
    """Split on blank lines (never inside a block) into pages of about max_lines."""
    blocks = text.split("\n\n")
    pages, cur = [], []
    for b in blocks:
        n = len(b.splitlines())
        if cur and sum(len(x.splitlines()) + 1 for x in cur) + n > max_lines:
            pages.append("\n\n".join(cur))
            cur = []
        cur.append(b)
    if cur:
        pages.append("\n\n".join(cur))
    return pages


def view(text: str, buttons: list[list[tuple[str, str]]] | None = None, max_lines: int = MAX_LINES) -> View:
    pages = paginate(nodash(text), max_lines)
    return View(pages[0], buttons or [], pages[1:])
