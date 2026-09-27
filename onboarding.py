"""Conversational onboarding, one question at a time. No channel code here.

Flows:
- welcome: a new user right after joining with an invite code: about them, then their calendars.
- setup: /me setup for an existing user (the getting-to-know-you questions, then a confirmation).
- calendars: /connect: just the Brightspace and Google Calendar links.

Calendar steps take a pasted feed link, check it by fetching it, save it encrypted (users.set_feed) and
sync it right away. The link itself never goes into a draft, a log or a model prompt.
"""

import re

import feeds
import selfmodel
import users

SETUP_QUESTIONS = [
    "What's your name, and what are you studying at NYU?",
    "What are you working on or building right now?",
    "What are your career goals?",
    "What do you want most out of your time at NYU?",
    "Who are the key people around you? Teammates, mentors, anyone I should know, and how you know them.",
    "Do you like my replies short or detailed?",
    "How do you like reminders, and do you have quiet hours when I shouldn't ping you? For example: no pings 11pm to 8am.",
]
WELCOME_QUESTIONS = [
    "First, what should I call you?",
    "What school and program are you in, and what year?",
    "What are you working on right now? Classes, projects, a startup, anything.",
    "What are your goals for this year?",
    "How do you like replies: short and quick, or more detailed?",
    "Any quiet hours when I shouldn't ping you? For example: no pings 11pm to 8am.",
]
FLOWS = {
    "setup": SETUP_QUESTIONS,
    "welcome": WELCOME_QUESTIONS + ["cal:brightspace", "cal:google"],
    "calendars": ["cal:brightspace", "cal:google"],
}

# Shown word for word, so the steps are exact.
CAL_STEPS = {
    "brightspace": ("Paste your Brightspace calendar link (it brings in your assignments and exams):\n"
                    "1. Open Brightspace and go to Calendar\n"
                    "2. Settings, then turn on Enable Calendar Feeds and save\n"
                    "3. Back in Calendar, tap Subscribe\n"
                    "4. Copy the URL and paste it here\n"
                    "Or say skip."),
    "google": ("Paste your Google Calendar secret iCal link (it brings in your classes and meetings):\n"
               "1. On a computer, open Google Calendar, then Settings (gear icon)\n"
               "2. On the left, pick your calendar under Settings for my calendars\n"
               "3. Scroll to Integrate calendar\n"
               "4. Copy Secret address in iCal format and paste it here\n"
               "Or say skip. (The link is private: I keep it encrypted and never show it.)"),
}

CAPABILITIES = ("Here's what I can do:\n"
                "• Remember anything you tell me or voice-note me (people, ideas, plans) and answer questions about it\n"
                "• Keep your schedule and deadlines, with reminders\n"
                "• Read files you send (syllabus, PDFs, photos) and pull out due dates\n"
                "• A short brief every morning, and NYU events picked for you\n"
                "• /week, /today, /brief, /events, /me, /connect for calendars, /help for everything")

SKIP = re.compile(r"^\s*(skip|pass|next|n/?a|none|no)\s*[.!]*\s*$", re.I)
LATER = re.compile(r"^\s*(later|not now|stop|pause|another time)\s*[.!]*\s*$", re.I)
YES = re.compile(r"^\s*(y|yes|yeah|yep|yup|looks good|correct|perfect|all good|that's right|thats right)\b", re.I)


def _state(user_id) -> dict:
    return selfmodel.meta(user_id).get("onboarding") or {"status": "new", "step": 0}


def _set(user_id, **state) -> None:
    selfmodel.set_meta(user_id, onboarding={**_state(user_id), **state})


def _flow(user_id) -> list[str]:
    return FLOWS.get(_state(user_id).get("flow") or "setup", SETUP_QUESTIONS)


def flow(user_id) -> str:
    return _state(user_id).get("flow") or "setup"


def is_active(user_id) -> bool:
    return _state(user_id)["status"] in ("active", "confirming")


def _questions(steps: list[str]) -> int:
    return sum(not s.startswith("cal:") for s in steps)


def _ask(steps: list[str], step: int) -> tuple[str, str | None]:
    """(instruction for the voice layer, text shown word for word or None)."""
    s = steps[step]
    if s.startswith("cal:"):
        source = s[4:]
        lead = ("Now the optional calendar part. " if step == 0 or not steps[step - 1].startswith("cal:") else "")
        return (f"{lead}Ask them to connect their {feeds.NAMES[source]} calendar (optional; they can say skip or do "
                "it later with /connect). One short sentence; the steps are shown below."), CAL_STEPS[source]
    n = _questions(steps)
    # The question itself is shown word for word, so it can never get lost in a rewording.
    return "The next question is shown right below your line: don't ask anything yourself.", f"({step + 1}/{n}) {s}"


# Each function returns a draft for the voice layer plus text shown word for word (or None).


def start(user_id, which: str = "setup") -> tuple[str, str | None]:
    st = _state(user_id)
    if which == "setup" and st["status"] == "paused" and st.get("flow", "setup") == "setup" and st["step"] < len(SETUP_QUESTIONS):
        _set(user_id, status="active")
        draft, attach = _ask(SETUP_QUESTIONS, st["step"])
        return f"Welcome them back and pick up the setup where you left off. {draft}", attach
    _set(user_id, status="active", step=0, flow=which)
    steps = FLOWS[which]
    draft, attach = _ask(steps, 0)
    if which == "welcome":
        return ("They just joined Violet with an invite. Welcome them in one short, warm line: you're Violet, "
                "their memory and schedule sidekick at NYU, and a few quick questions will set things up (they can "
                f"say skip for any). {draft}"), attach
    if which == "calendars":
        return draft, attach
    return ("Start a quick getting-to-know-you setup: a few questions, one at a time. They can say "
            f'"skip" for any question, or "later" to finish another time. {draft}'), attach


def _name(text: str) -> str | None:
    """ "call me Maya" / "I'm Maya Patel" / "Maya" -> "Maya Patel"; None if it doesn't look like a name."""
    t = re.sub(r"^\s*(call me|i'?m|i am|it'?s|my name is|name'?s|hi,? i'?m)\s+", "", text.strip(), flags=re.I)
    t = re.sub(r"[.!]+$", "", t).strip()
    words = t.split()
    if not 1 <= len(words) <= 4 or not all(re.fullmatch(r"[A-Za-z][A-Za-z'\-]*", w) for w in words):
        return None
    return " ".join(w[:1].upper() + w[1:] for w in words)


def _confirm(user_id, lead: str) -> tuple[str, str | None]:
    summary = selfmodel.profile_summary(user_id) or "Nothing yet."
    return (f"{lead} Show what you've got about them (below) and ask them to reply yes if it's right, "
            "or tell you what to change."), summary


async def _calendar_step(user_id, source: str, text: str) -> tuple[bool, str]:
    """(moved on, draft). Validates and stores a pasted link; never puts the link in the draft."""
    found, url = feeds.find_link(text)
    if not url:
        return False, (f"That wasn't a calendar link. Ask them to paste the {feeds.NAMES[source]} link from the "
                       "steps above, or say skip.")
    if found and found != source:
        return False, (f"That looks like a {feeds.NAMES[found]} link, but this step is for {feeds.NAMES[source]}. "
                       "Ask for the right one, or skip.")
    try:
        n = await feeds.validate(source, url)
    except feeds.FeedError as e:
        return False, (f"The link didn't work ({e}). Ask them to copy the full link again (it's long), or say skip.")
    users.set_feed(user_id, source, url, items=n)
    try:
        await feeds.sync_feed(str(user_id), source)
    except Exception:
        pass  # the regular sync tries again
    return True, f"✅ Found {n} upcoming items in your {feeds.NAMES[source]}."


async def handle(user_id, text: str) -> tuple[str, str | None, bool]:
    """Handle a reply during onboarding. Returns (draft, attach, finished)."""
    st = _state(user_id)
    steps = _flow(user_id)
    if st["status"] == "confirming":
        if YES.match(text):
            _set(user_id, status="done")
            return ("Setup's done. You'll keep learning as you talk; /me shows what you know and lets them "
                    "change it."), None, True
        changes = await selfmodel.edit(user_id, text)
        lead = ("Updated: " + "; ".join(changes) + ".") if changes else "Couldn't tell what to change, ask them to say it another way."
        draft, attach = _confirm(user_id, lead)
        return draft, attach, False

    step = st["step"]
    if LATER.match(text):
        _set(user_id, status="paused")
        resume = "/connect" if steps[step].startswith("cal:") else "/me setup"
        return f"No problem, pause the setup. {resume} picks it up whenever they like.", None, flow(user_id) != "setup"
    skipped = bool(SKIP.match(text))
    s = steps[step]
    found = None  # the calendar result, shown word for word
    if s.startswith("cal:"):
        if skipped:
            ack = f"They skipped {feeds.NAMES[s[4:]]} (they can add it later with /connect). Say okay in a few words."
        else:
            moved, result = await _calendar_step(user_id, s[4:], text)
            if not moved:
                return result, None, False
            ack, found = "Say it worked in a few words (the count is shown below).", result
    else:
        if not skipped and s == WELCOME_QUESTIONS[0]:
            name = _name(text)
            if name:
                selfmodel.set_identity(user_id, name=name)
                users.touch(user_id, name=name)
            else:
                await selfmodel.extract_onboarding(user_id, s, text)
        elif not skipped:
            await selfmodel.extract_onboarding(user_id, s, text)
        ack = "They skipped that one." if skipped else "Acknowledge their answer in a few words, referring to what they said."
    step += 1
    if step < len(steps):
        _set(user_id, step=step)
        draft, attach = _ask(steps, step)
        attach = f"{found}\n\n{attach}" if found else attach
        return f"{ack} {draft}", attach, False
    if flow(user_id) == "setup":
        _set(user_id, step=step, status="confirming")
        draft, attach = _confirm(user_id, ack)
        return draft, attach, False
    _set(user_id, step=step, status="done")
    closing = ("All set. Say you're ready and that their week is below." if flow(user_id) == "welcome"
               else "Say their calendars are connected (or skipped) and that their week is below.")
    shown = "\n\n".join(x for x in (found, CAPABILITIES if flow(user_id) == "welcome" else None) if x) or None
    return f"{ack} {closing}", shown, True
