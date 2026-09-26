"""/me setup: a short conversational onboarding, one question at a time. No channel code here."""

import re

import selfmodel

QUESTIONS = [
    "What's your name, and what are you studying at NYU?",
    "What are you working on or building right now?",
    "What are your career goals?",
    "What do you want most out of your time at NYU?",
    "Who are the key people around you? Teammates, mentors, anyone I should know, and how you know them.",
    "Do you like my replies short or detailed?",
    "How do you like reminders, and do you have quiet hours when I shouldn't ping you? For example: no pings 11pm to 8am.",
]

SKIP = re.compile(r"^\s*(skip|pass|next|n/?a|none)\s*[.!]*\s*$", re.I)
LATER = re.compile(r"^\s*(later|not now|stop|pause|another time)\s*[.!]*\s*$", re.I)
YES = re.compile(r"^\s*(y|yes|yeah|yep|yup|looks good|correct|perfect|all good|that's right|thats right)\b", re.I)


def _state(user_id) -> dict:
    return selfmodel.meta(user_id).get("onboarding") or {"status": "new", "step": 0}


def _set(user_id, **state) -> None:
    selfmodel.set_meta(user_id, onboarding={**_state(user_id), **state})


def is_active(user_id) -> bool:
    return _state(user_id)["status"] in ("active", "confirming")


def _ask(step: int) -> str:
    return f"({step + 1}/{len(QUESTIONS)}) {QUESTIONS[step]}"


# Each function returns a draft for the voice layer: (text, list shown word for word or None).


def start(user_id) -> tuple[str, str | None]:
    st = _state(user_id)
    if st["status"] == "paused" and st["step"] < len(QUESTIONS):
        _set(user_id, status="active")
        return f"Welcome him back and pick up the setup where you left off. Ask exactly this next: {_ask(st['step'])}", None
    _set(user_id, status="active", step=0)
    return ("Start a quick getting-to-know-you setup: a few questions, one at a time. He can say "
            f'"skip" for any question, or "later" to finish another time. Ask exactly this first: {_ask(0)}'), None


def _confirm(user_id, lead: str) -> tuple[str, str | None]:
    summary = selfmodel.profile_summary(user_id) or "Nothing yet."
    return (f"{lead} Show what you've got about him (below) and ask him to reply yes if it's right, "
            "or tell you what to change."), summary


async def handle(user_id, text: str) -> tuple[str, str | None, bool]:
    """Handle a reply during onboarding. Returns (draft, attach, finished)."""
    st = _state(user_id)
    if st["status"] == "confirming":
        if YES.match(text):
            _set(user_id, status="done")
            return ("Setup's done. You'll keep learning as you talk; /me shows what you know and lets him "
                    "change it."), None, True
        changes = await selfmodel.edit(user_id, text)
        lead = ("Updated: " + "; ".join(changes) + ".") if changes else "Couldn't tell what to change, ask him to say it another way."
        draft, attach = _confirm(user_id, lead)
        return draft, attach, False

    step = st["step"]
    if LATER.match(text):
        _set(user_id, status="paused")
        return "No problem, pause the setup. /me setup picks it up where you left off.", None, False
    skipped = bool(SKIP.match(text))
    if not skipped:
        await selfmodel.extract_onboarding(user_id, QUESTIONS[step], text)
    step += 1
    ack = "He skipped that one." if skipped else "Acknowledge his answer in a few words, referring to what he said."
    if step < len(QUESTIONS):
        _set(user_id, step=step)
        return f"{ack} Then ask exactly this next: {_ask(step)}", None, False
    _set(user_id, step=step, status="confirming")
    draft, attach = _confirm(user_id, ack)
    return draft, attach, False
