# Violet Memory 💜

A tiny Telegram memory bot. Text it anything and it remembers. End a message
with `?` and it answers from your memories. Each user's memory is private.

- `bot.py`: Telegram channel (long polling, no webhooks). Swap this file to change channels.
- `brain.py`: memory logic (Cognee). No Telegram code.
- `docs.py`: text from PDF, Word, .txt/.md and images (OpenAI vision for photos and scanned PDFs).
- `schedule.py`: deadline and class extraction, dedupe, reminders, week view (SQLite `data/violet.db`).
- `ingest.py`: file and chat pipelines that tie the above together.
- `assistant.py`: classifies each message (question, schedule item, memory), answers
  schedule questions from the calendar plus Cognee, builds class reminder context.
- `feeds.py`: Brightspace and Google Calendar iCal sync (now and every 3 hours).
- `selfmodel.py`: the user's profile (identity, academics, goals, people, preferences,
  patterns). Each fact has source, stated or inferred, confidence, first seen and last
  confirmed. Mirrored into Cognee as one "me" document tagged `node_set=["core_profile"]`.
  Other features use `get_profile(user_id)` and `profile_summary(user_id)`.
- `onboarding.py`: `/me setup`, seven questions, skip / later / resume, confirm at the end.
- `data/`: created on first run. Holds Cognee's databases and `messages.db`
  (SQLite log of every inbound and outbound message).

## Setup

Requires Python 3.11.

1. Create a virtualenv and install:

   ```bash
   cd ~/projects/violet-memory
   python3.11 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Create `.env` (copy `.env.example`) and fill in:

   ```
   TELEGRAM_BOT_TOKEN=<token from @BotFather>
   LLM_API_KEY=<your OpenAI API key>
   LLM_MODEL=openai/gpt-4o-mini
   ```

   Use the `openai/<model>` format for `LLM_MODEL` (cognee 1.6.1 reads the prefix
   as the provider). Cognee uses `LLM_API_KEY` for both the LLM and embeddings
   (OpenAI by default), so this must be an OpenAI key.

3. Run:

   ```bash
   python bot.py
   ```

Open your bot in Telegram and send `/start`.

## How it works

- Plain text: `cognee.add` into dataset `user_<telegram id>`, reply "💜 Saved",
  then `cognee.cognify` runs as a background task so the bot stays responsive.
- Text ending in `?`: `cognee.search` with `SearchType.GRAPH_COMPLETION` scoped
  to that user's dataset only. If cognify is still running, it answers with
  what's available and says new memories may take a few seconds to connect.

## Documents and reminders

Send a PDF, .docx, .txt, .md or photo (max 20 MB), ideally with a caption like
"OM syllabus". It is saved to `data/files/<user_id>/`, added to memory, and any
dated deadlines or weekly class times are added to your calendar (Fall 2026,
America/New_York). `/week` shows the next 7 days, `/undo` removes items from your
last file.

Reminders: assignments 3 days, 1 day and 3 hours before (exams also 1 week);
classes 30 minutes before with a line of context from memory; other timed events
1 hour before; all-day events none. LockDown Browser items say so. Reminders are
marked sent before sending, so restarts never repeat one, and reminders missed
while the bot was off for over an hour are dropped. `/demo_time 2026-09-29 13:30`
previews what would fire at that moment.

## Profile

- `/me` shows the profile ("you told me" vs "I noticed"), `/me setup` onboarding,
  `/me edit <correction>`, `/me reflect` for the weekly check-in now (it also runs Sunday
  from 6 PM), `/forget <text>` removes matching facts and saved chat messages.
- After each message a background check stores lasting facts and ignores one-offs.
- Resumes and LinkedIn exports sent as files seed the profile.
- Sensitive topics (health, finances, religion, politics, romantic life) are never stored,
  enforced in code, unless you explicitly ask ("remember that ...", or `/me edit`).
- Quiet hours delay non-urgent reminders (1 week, 3 days, 1 day) to the end of them.
- Everything saved to Cognee is anchored to the owner ("Chirag Ravishankar (the user)
  wrote:"), so "I", "me" and "my" resolve to them and other names stay other people.
  A name is only ever set from the user saying it, never guessed.
- Questions about the user ("what's my name", "who am I", "what am I working on") are
  answered from the profile first, then Cognee.
- Corrections ("I am X", "my name is X", "actually ...", "no, ...") update the profile,
  save the fix to Cognee and confirm.
- Messages from one user that arrive within 3 seconds are joined, so a long paste that
  Telegram splits is handled as one message (every text reply waits that long).

## Calendar feeds

Set `BRIGHTSPACE_ICS_URL`, `GOOGLE_ICS_URL` and `CALENDAR_USER_ID` (your Telegram
user id) in `.env`. Feed URLs are secrets: they are never logged, and sync errors
only report the error type. Brightspace items that match ones from an uploaded PDF
take them over, so nothing is duplicated.

Only one process can use the Cognee database at a time, so stop the bot before
running scripts that import `brain.py`.

## Inspect the message log

```bash
sqlite3 data/messages.db "select ts, direction, user_id, text from messages order by id desc limit 20"
```

## Reset all memories

Stop the bot and delete the `data/` folder.
