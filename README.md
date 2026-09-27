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
- `voice.py`: Violet's voice. Every reply (saves, answers, reminders, confirmations, errors)
  goes through it with one shared system prompt (rules plus few-shot examples, the user's
  profile and reply style, their recent replies). Code keeps emoji occasional, trims repeated
  offers and stock openers, and strips markdown, em dashes and mentions of internals. Lists
  where every line matters (/week, items added from a file, /me) are attached word for word
  under a voiced lead-in. Optional `VOICE_MODEL` in `.env` (defaults to `LLM_MODEL`).
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
- Saves reply first (the acknowledgment is written in the same call as the classification)
  and store in the background, so a graph build in progress never holds up a reply.
- Cognee's session memory is turned off (`CACHING=false`): it fed old answers back in.
- Messages from one user that arrive within 3 seconds are joined, so a long paste that
  Telegram splits is handled as one message (every text reply waits that long).

## Conversation

- `agent.py`: every non-command message (typed or a voice transcript) goes to one tool-calling
  model (`CHAT_MODEL`, default gpt-5.4: about 0.7 s per call with tools; background jobs stay on
  the fast model). Tools wrap existing functions: get_agenda, get_next_items, search_memory,
  get_profile, find_events, respond_to_event, find_people, get_followups, save_memory,
  add_schedule_item, add_person, mark_done, update_profile, render_view, add_button.
- The last 12 messages (both sides) are kept in SQLite and sent with every call, plus what was last
  shown (event list, schedule range), so "what about Monday?" and "the second one" work.
- Rules: answer exactly what was asked (tomorrow means tomorrow), an empty day gets one sentence plus
  the next item and a [📅 Show my week] button, lists only when a list is the best answer, declines
  and corrections are understood, normal conversation is welcome, and only durable facts are saved.
- Fast paths stay in code: onboarding, profile edits, reflection answers, "going" / "not for me" on
  the last event list, and "I am <name>".
- Calendar titles are cleaned once by the fast model and cached (`display_title`); the original
  title is kept for matching.

## Actions (Google Calendar, Gmail)

- `/connect_google` runs Google's sign-in in the browser on this computer (calendar.events,
  gmail.compose, gmail.send) and saves `token.json` (owner-only, git-ignored, refreshed automatically).
- The agent can only propose: `actions.py` stores a pending card; nothing runs until you tap it or
  reply "yes" to that card. Cards expire after 24 hours and run at most once. [✏️ Edit] takes a plain-
  language change. After running: a link and [↩️ Undo] (calendar events Violet created, drafts).
- Emails: only to addresses you gave, in your style, [📤 Send] waits 10 seconds with Undo, at most 10 a
  day, never coursework. Tapping Going or adding a deadline by chat proposes a calendar card;
  `/settings calendar auto` adds those without asking (still logged). `/actions` shows the last 15.
- Events Violet adds to Google Calendar are skipped by the Google Calendar feed import.

## Email awareness (read-only)

- `inbox.py` with the gmail.readonly scope (older grants: send /connect_google again). First run reads
  14 days of inbox and sent mail, then only new mail every 10 minutes (Gmail history, date query as
  fallback). Skips promotions, social, spam, trash and newsletters unless NYU or someone you know.
  Stores metadata, a snippet, a one-line summary and extracted dates, never full bodies; only
  important facts go to memory. Never deletes, archives, labels or marks anything.
- Emails are untrusted: the model that reads them has no tools, dates are kept only if the email
  states them, and replies are written by a separate tool-free call and proposed as cards.
- Alerts (importance 80+, max 5 a day, held in quiet hours) with [📅 Update schedule] [✍️ Draft reply]
  [👍 Got it]. Deadlines and changes go on the schedule (a change moves the existing item) with
  Google Calendar cards. Nudges for threads waiting on you (48 h) and for no reply to you (5 days).
- /inbox, /email alerts on|off, /email quiet; the agent can search_email; 📬 in the brief.

## Formatting and the daily brief

- `fmt.py`: the one formatter (Telegram HTML). Section headers "📅 <b>TODAY</b>", bold times
  first, "↳" details, bold "TUE 29" day headers, "•" for deadlines and exams, icons with fixed
  meanings (🎓 class, 📌 deadline, 📝 exam, 🤝 meeting, 🎟 event, 👤 person, 💡 tip), "Register"
  links, titles trimmed to ~45 chars, no em dashes, all text escaped, ~25 lines per message with a
  "Show more" button. If Telegram rejects the HTML, the message is resent as plain text.
- `views.py`: every structured view (/today, /week, schedule answers, events, reminders) is
  rendered by code; the model only writes short lines (a lead-in, an event's why, a context line).
  Buttons: /week [◀ This week] [Next week ▶]; events [✅ Going] [⏰ Later] [✖ Not for me].
- `brief.py`: the morning brief at 8:00 AM New York (/brief time 7:30, /brief off, /brief on),
  Sundays the 7 PM weekly preview instead. Data comes from code (agenda, due soon, exams,
  LockDown items, follow-ups, one event pick scoring 80+ with no clash); memory lookups per class
  or meeting run in parallel with a 6 s timeout. Never sent twice a day (SQLite `briefs`).
  /brief (now), /brief tomorrow, /brief week. Buttons [📅 Full week] [🎟 Events].

## Event discovery

- `eventsources.py`: the pluggable layer (shared `Event` shape, source registry, polite HTTP
  client) and the per-source lead times in one place (`LEAD_DAYS`: nyu_events 3 to 4 days,
  luma 6 to 8, default 3 to 4). A new source (Luma, Engage, Meetup) is one module listed in
  `sources()`.
- `nyu_events.py`: events.nyu.edu (LiveWhale). JSON feeds for the next 30 days: all events
  (2 pages of 1000), tag `ihub`, and the groups Engineering (Tandon), Stern, Wasserman-Center,
  wasserman-center-feed-only, Entrepreneurial-Institute (runs the Leslie eLab) and
  Berkley-Center-for-Entrepreneurship. Every 6 hours, or `/events refresh`.
- `luma.py`: Luma iCal feeds from `LUMA_ICS_URLS` (comma-separated; add more with
  `/events add_luma <url>`). Link from "Get up-to-date information at:", address from the
  "Address:" block (or "address after registering" plus the borough from GEO), host from
  "Hosted by" / ORGANIZER CN, flags needs_approval / waitlist / paid (with price) / hackathon.
  A keyword prefilter drops obvious mismatches (yoga, crafts, parties...) before scoring unless
  they mention AI, startups, VC and similar.
- `events.py`: events table (separate from the schedule, deduped by id), scoring 0 to 100
  against the profile in batches with the fast model (cached per event), calendar conflicts,
  suggestion windows, and replies.
- Suggestions: 9 to 11 AM New York, one message, up to 3 events scoring 70+ across all sources
  (best first; the same event on NYU Events and Luma counts once), never clashing with a class
  or exam, paid events over $50 only at 90+, inside the source's lead window (NYU 3 to 4 days,
  Luma 6 to 8, approval or waitlist events up to 10) (pulled earlier to land a day before a stated
  registration deadline). Events found after their window opened are suggested once only if
  they score 90+ ("Heads up, this is Thursday"), at most one a day after on-time picks.
  Score boosts never lift an event past 89: only the model can call a must-see. Never the same event twice. Quiet hours respected.
- `/events` top 5 for the next 7 days, `/events all` by day, `/events refresh`. Reply "going"
  (adds it to the calendar, reminders 1 day and 2 hours before, plus a check-your-approval
  reminder 2 days before for approval-only events, saves to memory), "not for me",
  "more like this" or "remind me later" (back the day before). Questions like "any career events
  this week?" are answered from the events table.

## Calendar feeds

Each user connects their own Brightspace and Google Calendar feeds in chat: during onboarding, with
`/connect`, or just by pasting the link. Violet fetches the link to check it ("Found 42 upcoming
items"), then stores it encrypted (`VIOLET_SECRET_KEY`) in that user's feed settings.

Feed URLs are secrets:
- They're redacted from the message log and never sent to a model.
- They're only ever shown masked.
- Sync errors report only the error type.

Brightspace items that match ones from an uploaded PDF take them over, so nothing is duplicated.

Only one process can use the Cognee database at a time, so stop the bot before
running scripts that import `brain.py`.

## Inspect the message log

```bash
sqlite3 data/messages.db "select ts, direction, user_id, text from messages order by id desc limit 20"
```

## Reset all memories

Stop the bot and delete the `data/` folder.

## Memory backend: Cognee Cloud

Memory lives in Cognee Cloud by default (`COGNEE_MODE=cloud`, with `COGNEE_SERVICE_URL` and
`COGNEE_API_KEY` in `.env`). `brain.py` is the only module that touches Cognee. At startup it calls
`cognee.serve()` on the bot's event loop.

- **Writes** go into a `memory_outbox` table in `data/violet.db` and are sent with `remember()` in
  the background, retried with backoff. A sleeping workspace never loses a memory or blocks a reply.
- **Reads** use `recall()` and are time-boxed (about 8 to 10 seconds for replies). If memory doesn't
  answer in time, Violet answers from the profile and schedule and says memory is catching up.
- **Rollback:** set `COGNEE_MODE=local` (or `git checkout local-cognee`). The local Cognee data
  under `data/` is untouched.
- **Migration:** run `scripts/migrate_to_cloud.py`, with `--dry-run` first. Stop the bot before
  running it.
- **Checks:** `scripts/memory_check.py` runs a round trip and measures latency.

### Fast, consistent memory

- **Routing.** `agent.route_for` sorts each message into one of three routes:
  - **local:** your name, program and goals, schedule, events, follow-ups and email. Answered from
    the profile, the people store and SQLite, with no cloud call.
  - **memory:** past conversations, documents, what someone said.
  - **mixed:** people and "working on". The cloud lookup starts in parallel with the first model
    call, and local data wins if it's enough.

  Each reply logs `answer: route=… path=… via=… in Ns`.
- **Cache.** Recall answers are cached per user for 10 minutes. Any new save for that user clears it.
- **Short-term buffer.** Memories from the last 15 minutes that the cloud hasn't built yet go into the
  agent's prompt and tool results. They leave the buffer once the cloud reports the dataset processed.
- **Corrections.** `correct_memory` and `brain.correct` work in this order:
  1. Queue the new fact immediately, but hold its upload.
  2. Find the items stating the old fact, using word overlap plus a small model.
  3. `cognee.forget` those items.
  4. Release the new fact.

  Items that mix the old fact with other things are kept, and the new fact goes in as a
  correction note.
- **Warm-up.** The workspace is woken 4 minutes before the brief and class reminders, and when a user
  starts messaging (only if it's been idle for 4 minutes or more).
- **Timings.** `scripts/timing.py --label <name>` writes to `data/timings.json`.

## Multiple users

Violet is invite-only.
- **Access.** A `users` table holds status (invited, active, waitlist, blocked), role (owner or user)
  and per-user daily limits. The owner is `VIOLET_OWNER_ID`.
- **Joining.** A new user sends `/start <code>`, then goes through a short onboarding: name, program,
  what they're working on, goals, reply style, quiet hours, then optional calendar links.
- **Strangers.** Anyone without a valid code gets one waitlist reply, rate-limited (once a day each,
  30 an hour overall). Their message never reaches the agent or memory, and isn't logged.
- **The gate.** A `TypeHandler` in group -1 runs before every handler. It only allows private chats
  from active users, and marks the acting user for usage tracking.
- **Owner commands:** `/invite [uses] [days]`, `/users`, `/block <id>`, `/unblock <id>`, `/usage`.

**Isolation.**
- Every table is keyed by `user_id`, and every query filters by it.
- Each user has their own Cognee dataset (`user_<telegram id>`), and `brain.dataset_for` refuses
  anything that isn't a plain id.
- Agent tools get the user from the message sender, never from the model.
- Buttons check ownership: transcripts, "show more" pages, action cards and emails.
- The shared prompts only use a made-up example student, never real users' details.
- `tests/test_isolation.py` must pass before committing. It runs the real handlers with a fake
  Telegram and a fake Cognee, and checks every command, question, button, agent tool and background
  job for cross-user leaks.

**Shared vs personal.**
- NYU Events and Luma are fetched once and shared.
- Scores, picks and "going" are per user.
- Calendar feeds, reminders, briefs and follow-ups run per active user.

**Owner-only for now:** Gmail awareness, Google Calendar writes, sending email, and any action card.
Other users hear "coming soon".

**Limits.** Daily limits per user cover messages, voice minutes, documents and new memories
(`users.DEFAULT_LIMITS`; the owner has none). Tokens and Cognee saves and recalls are tracked per
user per day, and the owner can see them with `/usage`.

**Email-derived schedule changes.** Deadlines and changes from trusted senders are applied
automatically and announced with an Undo button. Trusted means NYU addresses, Brightspace/D2L, and
people in the people store, which includes professors. Anyone else gets an "Add it?" card instead.

**Demo.**
- `tests/demo_multiuser.py new`: a new user from invite code to first `/week`.
- `tests/demo_multiuser.py real`: the friend and the owner on real data, with the test turns
  cleaned up afterwards.

## NYU Helper

Violet answers NYU questions and helps resolve them. Every answer ends with its source links, and says
how recent they are when that matters.

**Knowledge base (shared by everyone).**
- `nyu_kb.py` crawls the public NYU sites listed in `config/nyu_sources.json` (edit freely).
- The crawl respects robots.txt and crawl-delay, sends a clear User-Agent, waits 2 s between requests,
  goes at most 3 links deep, and caps pages per source.
- Page text goes into a local full-text index (fast lookups with exact URLs) and into the shared Cognee
  Cloud dataset `nyu_knowledge`. That dataset is separate from every user's private dataset, and only
  the crawler writes to it.
- It refreshes weekly (`nyu_loop` in the bot, or run `scripts/nyu_crawl.py`).
- **www.nyu.edu refuses crawlers** (HTTP 403), so Violet doesn't crawl it. Those topics (OGS,
  Registrar, Bursar, Health, Housing, IT, transit, NYU Card) come from the live search below.

**Live search (fallback).** When the knowledge base isn't confident, Violet runs OpenAI's `web_search`
tool restricted to `nyu.edu` (`filters.allowed_domains`). It uses the same `LLM_API_KEY`; no other
key is needed.

**Offices.** `config/nyu_offices.json` lists the official contact for each office, every detail with
an nyu.edu source. `scripts/nyu_offices_refresh.py` rebuilds it. Email cards may use these addresses.

**Professors.** `find_professor` looks in the user's own documents, their mail (owner only), and NYU
faculty pages that it fetches and checks. It shows where each email came from, never guesses or builds
an address, and saves what it finds on the professor's entry in the people store.

**Study rooms.** `libcal.py` reads nyu.libcal.com without logging in and never books.
- Room lists come from `/spaces?lid=…`, availability from `POST /spaces/availability/grid`, cached for
  5 minutes, with requests at least 10 s apart per robots.txt.
- Replies give a deep link to `/space/<room>?date=…`, where the student picks the time, signs in with
  their NetID and confirms.
- Dibner is the default. The library's booking rules are built in.
- `add_study_room_reminder` puts the room on the schedule with a 15-minute reminder.

**Library hours.** Today's hours and "open now" come from the LibCal hours feed.

**Safety.**
- Violet never asks for or accepts a password. A message containing one is refused, redacted from the
  log, and never reaches the model.
- On immigration, health, legal and financial aid questions, Violet gives the official information and
  the right office, never personal advice.

**Tools:** `nyu_search`, `nyu_office`, `find_professor`, `find_study_room`, `library_hours`,
`add_study_room_reminder`.

**Test:** `tests/nyu_live_demo.py` sends the test questions through the real handlers, and Violet's
replies arrive in the owner's Telegram.

## Reliability and the people loop

**Email sorting.**
- Emails are stored before they're sorted. Every sync retries up to 36 stored-but-unsorted emails
  (`inbox.retry_unsorted`), re-fetching their text read-only.
- Each failure is recorded per message (`classify_attempts`, `classify_error`), including invalid JSON
  from the model; nothing is skipped silently. An email stops being retried after 5 attempts.
- Email deadlines are applied only if their date hasn't passed.
- Needs-reply threads found more than 7 days late are never pushed as nudges.
- Automated no-reply senders never count as needing a reply.
- Booking and notification services (Cal.com, Calendly, Zoom…) never become a person's email address.

**Nothing is sent twice.**
- `delivered.py` is a delivery log: reminders, the daily brief, the weekly preview, event picks,
  check-ins, follow-up reminders and the weekly reflection are each claimed before they go out.
- Keys are stable, so they survive restarts and items that are deleted and re-created (for example by
  a feed sync).
- `/demo_time` fires reminders into the same log under a separate `reminder_demo` kind, so a test never
  blocks a real reminder.
- `tests/test_no_resend.py` checks this across a real restart.

**Brief prepared ahead.** 15 minutes before the brief, `brief.prepare` wakes Cognee and fetches each
agenda item's memory context. The brief then builds in about 2 seconds. If memory still isn't ready,
the brief goes out without it.

**People loop.**
- **`/people`:** who you've met, grouped by where, newest first, 15 per page.
- **`/followups`:** real follow-up records (person, promise, due date, status open/done/snoozed). They
  replace title matching; existing follow-up-like schedule items are migrated at startup.
- **Follow-up reminders:** sent on the due day from 10 AM, with ✍️ Draft message / ✅ Done / ⏰ Snooze
  2 days. Done updates the person's `last_contact`.
- **Post-event check-in:** the morning after an event you said "going" to, or a networking-type
  calendar event, the brief asks "Who'd you meet at X?" with 🎙 Tell you now / Didn't go / Nobody new,
  once per event.
- **Agent tools:** `add_person` (where met, field), `add_followup`, `followup_done`, `people_search`,
  and `get_followups` (optionally for one person).

**NYU answers in parallel.** The local index, the Cognee `nyu_knowledge` recall and the live nyu.edu
search start together, and the first confident answer within 20 s wins. A late Cognee answer is
never waited for.

**Tests:** `tests/test_isolation.py`, `tests/test_no_resend.py`, `tests/test_people_loop.py`.
