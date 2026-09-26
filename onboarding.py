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


def start(user_id) -> str:
    st = _state(user_id)
    if st["status"] == "paused" and st["step"] < len(QUESTIONS):
        _set(user_id, status="active")
        return "Welcome back 💜 Picking up where we left off.\n\n" + _ask(st["step"])
    _set(user_id, status="active", step=0)
    return (
        "Let's get to know each other 💜 A few quick questions. "
        'Type "skip" to skip one, or "later" to stop and finish another time.\n\n' + _ask(0)
    )


def _summary_prompt(user_id) -> str:
    summary = selfmodel.profile_summary(user_id) or "Nothing yet."
    return f"Here's what I've got:\n\n{summary}\n\nDoes this look right? Reply yes, or tell me what to change."


async def handle(user_id, text: str) -> tuple[str, bool]:
    """Handle a reply during onboarding. Returns (reply, finished)."""
    st = _state(user_id)
    if st["status"] == "confirming":
        if YES.match(text):
            _set(user_id, status="done")
            return "Perfect, you're all set 💜 I'll keep learning as we talk. Send /me any time to see or change this.", True
        changes = await selfmodel.edit(user_id, text)
        done = "\n".join(changes) if changes else "I couldn't tell what to change, could you say it another way?"
        return f"{done}\n\n{_summary_prompt(user_id)}", False

    step = st["step"]
    if LATER.match(text):
        _set(user_id, status="paused")
        return "No problem. Send /me setup any time and I'll pick up where we left off 💜", False
    if not SKIP.match(text):
        await selfmodel.extract_onboarding(user_id, QUESTIONS[step], text)
    step += 1
    if step < len(QUESTIONS):
        _set(user_id, step=step)
        return "Got it 💜\n\n" + _ask(step), False
    _set(user_id, step=step, status="confirming")
    return _summary_prompt(user_id), False
