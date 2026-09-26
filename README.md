# Violet Memory 💜

A tiny Telegram memory bot. Text it anything and it remembers. End a message
with `?` and it answers from your memories. Each user's memory is private.

- `bot.py`: Telegram channel (long polling, no webhooks). Swap this file to change channels.
- `brain.py`: memory logic (Cognee). No Telegram code.
- `docs.py`: text from PDF, Word, .txt/.md and images (OpenAI vision for photos and scanned PDFs).
- `schedule.py`: deadline and class extraction, dedupe, reminders, week view (SQLite `data/violet.db`).
- `ingest.py`: file and chat pipelines that tie the above together.
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
America/New_York). Deadlines remind you 1 day and 2 hours before; exams also
1 week before. `/week` shows the next 7 days, `/undo` removes items from your
last file.

Only one process can use the Cognee database at a time, so stop the bot before
running scripts that import `brain.py`.

## Inspect the message log

```bash
sqlite3 data/messages.db "select ts, direction, user_id, text from messages order by id desc limit 20"
```

## Reset all memories

Stop the bot and delete the `data/` folder.
