# Violet on ClawMax: personal agent only

ClawMax's `violet` agent uses the **violet** skill to ask Violet, which runs on this Mac, and returns
Violet's answer unchanged. It's for Chirag only. Students keep using the Python Telegram bot, unchanged.

## How it works

```
ClawMax agent "violet" ──skill script──▶ https://<tunnel>/agent/message ──Cloudflare Tunnel──▶ 127.0.0.1:8766
                                            Authorization: Bearer VIOLET_CLAWMAX_TOKEN            (inside bot.py)
```

- **The endpoint** (`clawmax.py`, started by `bot.py` when `VIOLET_CLAWMAX_TOKEN` is set) takes
  `POST /agent/message {"text": "..."}` and returns `{"reply": "..."}`.
- **Always the owner.** It always answers as the owner (`VIOLET_OWNER_ID`) and ignores any user or sender id in
  the request. ClawMax skills can't prove who is talking, so this path is personal-only by design.
- **Same handlers as Telegram.** Commands, questions, memory, schedule, NYU Helper, corrections and limits all
  work. Replies come back as Markdown. Action cards come back as text ("Reply **yes** to confirm or **no** to
  cancel"; for emails, **send**, **draft** or **no**), and your next message goes through the same approval
  logic.
- **Guarded.** Token auth, 20 requests a minute and 300 a day, and a 150-second timeout. Logs contain timing
  and reply length only, never message text or the token.
- **Its own chat.** It runs in a separate internal chat, so it never mixes with your Telegram chat's state (last
  card shown, split messages). Your conversation history is shared, since it's the same you.
- **Only one path is exposed.** The server listens on 127.0.0.1 only and serves nothing but `/agent/message`;
  everything else returns 404. The tunnel points only at that server.

## Tunnel: start and stop

The bot must be running (`.venv/bin/python bot.py`); the endpoint lives inside it.

```bash
clawmax/tunnel.sh start
```

`start` runs a quick tunnel: no Cloudflare account, and it prints the URL. The URL changes every time, so
update the `VIOLET_CLAWMAX_URL` secret in ClawMax after each start.

```bash
clawmax/tunnel.sh url
```

```bash
clawmax/tunnel.sh stop
```

**Stable address (recommended for daily use): a named tunnel.** This needs a free Cloudflare account and a
domain on Cloudflare. Run these once:

```bash
cloudflared tunnel login
```

```bash
cloudflared tunnel create violet-clawmax
```

```bash
cloudflared tunnel route dns violet-clawmax violet.<your-domain>
```

Then create `clawmax/cloudflared.yml` (it's git-ignored):

```yaml
tunnel: violet-clawmax
credentials-file: /Users/<you>/.cloudflared/<tunnel-id>.json
ingress:
  - hostname: violet.<your-domain>
    path: ^/agent/message$          # only this path is routed
    service: http://127.0.0.1:8766
  - service: http_status:404        # everything else: 404 at Cloudflare's edge
```

Start it with `clawmax/tunnel.sh named`, and set `VIOLET_CLAWMAX_URL=https://violet.<your-domain>` once.

**Quick test from the Mac.** This reads the token without printing it:

```bash
T=$(grep '^VIOLET_CLAWMAX_TOKEN=' .env | cut -d= -f2-); curl -s -X POST "$(clawmax/tunnel.sh url)/agent/message" -H "authorization: Bearer $T" -H 'content-type: application/json' -d '{"text":"whats my name?"}'; unset T
```

## ClawMax setup

**1. Create the skill** (Skills → Create). Either:
- **Upload** `clawmax/violet-skill.zip`, which holds `violet/SKILL.md` and `violet/scripts/violet.py`; or
- **Paste**: name `violet`, and paste the whole of `clawmax/skills/violet/SKILL.md` (frontmatter included).
  Then add a file `scripts/violet.py` with the contents of `clawmax/skills/violet/scripts/violet.py`.

The skill needs `python3` and the `exec` tool on the agent (standard library only, no packages).

**2. Keys & Secrets.** Add two secrets:
- `VIOLET_CLAWMAX_TOKEN`: the value of `VIOLET_CLAWMAX_TOKEN` from this Mac's `.env`. Copy it from the file;
  don't paste it into any chat.
- `VIOLET_CLAWMAX_URL`: the tunnel address (from `clawmax/tunnel.sh url`, or your named-tunnel hostname),
  without `/agent/message`.

Authorize both for the agent **violet** and the skill **violet** only.

**3. Assign the skill** to the `violet` agent (Agents → violet → Skills → add `violet`), then start a new
session so the agent loads it.

**4. Test in ClawMax's own chat first.**
- "whats my name?" should give your name.
- "what's due this week?" should give your real deadlines.
- "who is Keith?" should give Keith Mauppa.
- If the agent answers without calling the skill, or makes something up, ask it to "use the violet skill".
  If that keeps happening, check the skill is assigned and the secrets are authorized.
- If you see "Violet error: couldn't reach Violet", the bot or the tunnel isn't running.
- If you see "HTTP 401", the token secret is wrong.

**5. Only after that, a Telegram bot for ClawMax, locked to you.** Anyone who can message this agent gets your
data (memory, schedule, email summaries, owner actions), so it must accept only your Telegram id,
`1067732196`. In OpenClaw terms:
- `channels.telegram.dmPolicy: "allowlist"`
- `channels.telegram.allowFrom: ["1067732196"]`
- `channels.telegram.groupPolicy: "disabled"`

Use a **new** bot from BotFather, not Violet's main bot or the OpenClaw test bot. Before relying on it, verify
the lock with a second Telegram account: it must get no reply. **If ClawMax doesn't let you set a DM allowlist
to your id with groups disabled, don't connect Telegram; use ClawMax's web chat only.**

## Notes

- **Your data passes through ClawMax.** Violet's answers pass through ClawMax's model and are stored in its
  transcripts.
- **If the token leaks,** anyone with the tunnel URL can read your Violet data. Rotate it: new value in `.env`,
  restart the bot, update the ClawMax secret.
- **Tests:** `tests/test_clawmax.py`, 15 checks over real HTTP in a temp data folder, with Google Calendar
  stubbed.
