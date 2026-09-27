"""Violet's voice: every user-facing reply goes through here. No channel code here.

compose() runs a task with the shared system prompt (used when the reply itself needs the
model, e.g. answering a question). say() turns a draft of what to get across into Violet's
voice; the draft is also the fallback if the model call fails.
"""

import logging
import os
import re
from datetime import datetime

import llm
import selfmodel
from schedule import TZ, fmt_time

log = logging.getLogger("voice")


def voice_model() -> str:
    """Fast model for the voice layer. VOICE_MODEL in .env, else LLM_MODEL."""
    m = os.getenv("VOICE_MODEL") or llm.model()
    return m.split("/", 1)[1] if m.startswith("openai/") else m


SYSTEM = """You're Violet, {name}'s sharp, warm friend who remembers everything. Talk to {name} as "you", and never use gendered words for
{name}. For anyone else (professors, classmates), don't assume pronouns from a name: use their name or "they".
You talk like a person
texting, not an assistant.

Rules:
- Answer first, in the first few words. No preamble.
- Never mention internals: no "memory", "memory result", "database", "Cognee", "records",
  "according to my records", "I have stored", "saved to", "profile", "context", "system".
  You just know things.
- Never repeat the same fact twice in one reply.
- Match their tone: casual, short, value-first. 1 to 3 sentences for most replies. When you list
  things, one short line per item, no bullets or numbers unless asked. No markdown, no asterisks,
  no em dashes, no filler ("Great question!", "Sure!", "Absolutely!").
- Use their name rarely, only when it feels natural.
- Emoji: occasional, at most one per message, not on every message. 💜 is your signature,
  use it sparingly.
- When they tell you something to keep, acknowledge the specific thing. Sometimes add one helpful
  beat (a follow-up offer, a link to something they already told you). Most of the time, don't.
  Never end two replies in a row with a question or an offer. Never say "Need help" or "Need any help".
- If you don't know, say so plainly and ask for the missing piece.
- Only state facts you were given (in the task or in what you know about them). Never invent
  dates, times, names or plans.
- Vary how you word acknowledgments. Don't open the way your recent replies opened.

Examples (a made-up student, Sam Rivera; these people and facts are NOT the user's: never use them as facts):
User: whats my name?
Violet: Sam Rivera. Pretty sure you knew that one 😄

User: Met Dana at the design mixer, she's building a climate app
Violet: Noted, Dana from the design mixer, climate. Want me to nudge you to follow up next week?

User: marketing draft due next Friday 11:59pm
Violet: Added. Marketing draft due Fri Oct 9, 11:59 PM. I'll ping you a few days before so you can chase Omar and Lin for their parts.

User: who is Jordan?
Violet: Jordan Lee, your lab mentor. You're sharing your research poster with them, and you meet Monday at 1.

User: bro, i am sam rivera
Violet: My bad, got it. You're Sam. Fixed.

Reminder (class in 30 min):
Violet: Stats with Prof. Kim in 30, Bobst 4th floor. Heads up, your marketing section is due Friday.

Now: {now} (America/New_York).
What you know about them: {profile}
{style}
Your recent replies (vary from these): {recent}
{openers}"""


def system_prompt(user_id, recent: list[str] | None = None) -> str:
    now = datetime.now(TZ)
    name = selfmodel.owner_name(user_id) or "your user"
    first = name.split()[0] if name != "your user" else "them"
    style_pref = selfmodel.pref(user_id, "reply_style")
    timing = selfmodel.pref(user_id, "reminder_timing")
    style = []
    if style_pref:
        style.append(f"Their reply style preference: {style_pref['text']}.")
    if timing:
        style.append(f"Their reminder preference: {timing['text']}.")
    return SYSTEM.format(
        name=first,
        now=f"{now:%A %b} {now.day}, {now.year}, {fmt_time(now.time())}",
        profile=selfmodel.profile_summary(user_id, max_words=120) or "not much yet",
        style=" ".join(style),
        recent=" | ".join(r.replace("\n", " ")[:120] for r in (recent or [])[-5:]) or "none",
        openers=_opener_rule(recent),
    )


def _opener(text: str) -> str:
    words = re.findall(r"[A-Za-z']+", text.split("\n")[0])[:2]
    return " ".join(words)


def _opener_rule(recent: list[str] | None) -> str:
    """Name the exact openings to avoid: the model ignores "vary" but follows a list."""
    used = []
    for r in (recent or [])[-4:]:
        o = _opener(r)
        first = o.split()[0] if o else ""
        for w in (o, first):
            if w and w.lower() not in {u.lower() for u in used}:
                used.append(w)
    return f"Do not start your reply with any of these: {', '.join(used)}." if used else ""


# ---------- Cleanup ----------

_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF⭐⬆↔-↪⏰-⏺]"
    "(?:️|‍[\U0001F300-\U0001FAFF♀♂❤]️?)*"
)
_INTERNALS = re.compile(
    r"\b(memory results?|in my memory|my memory|database|cognee|my records|according to (my|the) records|"
    r"i have stored|i've stored|stored in|saved to memory|saved in memory|the context|knowledge graph|"
    r"your profile says|profile says)\b",
    re.I,
)
_FILLER = re.compile(r"^(great question|good question|sure thing|sure|absolutely|of course|certainly)[!,.]\s*", re.I)


def polish(text: str, max_emoji: int = 1) -> str:
    text = text.strip().strip('"')
    text = re.sub(r"^\s*(violet|assistant)\s*:\s*", "", text, flags=re.I)
    text = text.replace("—", ", ").replace("–", "-").replace("**", "").replace("__", "")
    text = re.sub(r"^\s*#+\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*[-*•]\s+", "", text, flags=re.M)
    text = _FILLER.sub("", text)
    seen = 0

    def keep_first(m):
        nonlocal seen
        seen += 1
        return m.group(0) if seen <= max_emoji else ""

    text = _EMOJI.sub(keep_first, text)
    text = re.sub(r"[ \t]+$", "", text, flags=re.M)
    return re.sub(r" {2,}", " ", text).strip()


def emoji_budget(recent: list[str] | None, wanted: int = 1) -> tuple[int, bool]:
    """(max emoji for this reply, whether 💜 is allowed). Emoji stay occasional: none if 2 of
    the last 3 replies had one; 💜 at most once in the last 4."""
    last = [r.split("\n\n")[0] for r in (recent or [])]
    with_emoji = sum(1 for r in last[-3:] if _EMOJI.search(r))
    heart_recent = any("💜" in r for r in last[-4:])
    return (0 if with_emoji >= 2 else wanted), not heart_recent


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def trim_offer(text: str, recent: list[str] | None) -> str:
    """Drop a trailing offer/question if the last reply already ended with one, and any
    "Need help ...?" line. Keeps replies from all sounding like the same upsell."""
    parts = _SENTENCE_END.split(text.strip())
    last_ended_q = bool(recent) and recent[-1].split("\n\n")[0].rstrip().endswith("?")
    while len(parts) > 1 and parts[-1].rstrip().endswith("?") and (
        last_ended_q or re.match(r"^(need|want) (any )?help", parts[-1].strip(), re.I)
    ):
        parts.pop()
        last_ended_q = False
    return " ".join(parts)


_GENERIC_OPENER = re.compile(
    r"^(got it|noted|done|added|okay|ok|cool|sure|saved|gotcha|nice|perfect|all set|on it)\b[!.,:]*\s*", re.I
)


def vary_opener(text: str, recent: list[str] | None) -> str:
    """If this reply opens with the same stock word as a recent one ("Got it", "Noted"), drop it
    and start with the substance instead."""
    m = _GENERIC_OPENER.match(text)
    if not m or not recent:
        return text
    word = m.group(1).lower()
    if not any(re.match(rf"^{re.escape(word)}\b", r.strip(), re.I) for r in recent[-4:]):
        return text
    rest = text[m.end():].strip()
    return (rest[:1].upper() + rest[1:]) if len(rest.split()) >= 3 else text


def mentions_internals(text: str) -> bool:
    return bool(_INTERNALS.search(text))


# ---------- The one voice function ----------


async def compose(user_id, task: str, recent: list[str] | None = None, max_emoji: int = 1) -> str:
    """Run a task in Violet's voice. Raises if the model call fails (callers fall back).
    max_emoji: extra emoji allowed for list markers (📌 🎓); the voice budget still applies."""
    messages = [
        {"role": "system", "content": system_prompt(user_id, recent)},
        {"role": "user", "content": task},
    ]
    text = await llm.chat(messages, model_name=voice_model(), temperature=0.7)
    if mentions_internals(text):
        messages += [
            {"role": "assistant", "content": text},
            {"role": "user", "content": "Rewrite that without mentioning memory, records, profiles, databases or context. Just say it."},
        ]
        text = await llm.chat(messages, model_name=voice_model(), temperature=0.5)
    budget, heart_ok = emoji_budget(recent)
    if not heart_ok:
        text = text.replace("💜", "")
    markers = len(re.findall("📌|🎓", text)) if max_emoji > 1 else 0
    text = vary_opener(polish(text, budget + markers), recent)
    return text if "\n" in text else trim_offer(text, recent)


async def say(user_id, draft: str, kind: str = "reply", user_message: str | None = None,
              attach: str | None = None, recent: list[str] | None = None) -> str:
    """Say the draft in Violet's voice. kind: save, answer, schedule, correction, reminder,
    confirmation, error, question (asking them something), info. attach is shown word for word
    below the reply (lists where every line matters). If the model fails, the draft is sent."""
    task = (
        f"Kind of message: {kind}\n"
        + (f'Their message: "{user_message}"\n' if user_message else "")
        + f"What to get across (keep every fact, add no new facts):\n{draft}\n"
        + (("Some text is shown word for word right below your reply (a question or steps, or a list). Don't repeat "
            "it and don't describe it as something it isn't; if it's a question, don't introduce it at all.\n") if attach else "")
        + "Write your reply."
    )
    try:
        text = await compose(user_id, task, recent)
    except Exception as e:
        log.warning("voice failed (%s), sending the draft", type(e).__name__)
        text = polish(draft)
    return f"{text}\n\n{attach}" if attach else text
