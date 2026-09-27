---
name: violet
description: Chirag's personal memory and NYU assistant. Use for anything about his life, schedule, people, notes, email, events or NYU.
metadata:
  {
    "openclaw":
      {
        "emoji": "💜",
        "requires": { "bins": ["python3"], "env": ["VIOLET_CLAWMAX_TOKEN", "VIOLET_CLAWMAX_URL"] },
        "primaryEnv": "VIOLET_CLAWMAX_TOKEN",
      },
  }
---

# Violet

Violet is Chirag's own assistant. It holds his memory, schedule, deadlines, classes, people, follow-ups,
notes, documents, email summaries, event picks and NYU knowledge. It is the only source of truth for those.

## When to use

Use Violet for **anything about Chirag's life or NYU**, including:

- his schedule, classes, deadlines, exams, reminders, "what's due", "what's tomorrow", `/week`, `/today`, `/brief`
- people he knows, who he met, follow-ups, what he promised someone
- anything he told Violet before, his notes, documents, projects, goals, his name or profile
- his email or inbox, events and suggestions, study rooms, professors, NYU offices, policies, deadlines
- telling Violet something to remember ("met Priya at the mixer…", "remember that…")
- replies to something Violet asked (for example "yes", "no", "send", "draft", or an answer to its question)

## How to use

Send his message **exactly as he wrote it**, with the `exec` tool, through a quoted heredoc so nothing in the
text is interpreted by the shell:

```bash
python3 {baseDir}/scripts/violet.py <<'VIOLET_MSG'
<his exact message, unchanged>
VIOLET_MSG
```

Then reply with Violet's answer **exactly as returned**: same words, same Markdown. Don't summarize, rephrase,
add to it or wrap it in commentary.

## Rules

- Never answer questions about his life or NYU from your own memory or general knowledge, and never guess.
  If it's about him or NYU, ask Violet.
- Send only his message. Don't add names, ids or context to it.
- If Violet's answer asks him to reply (for example "Reply yes to confirm or no to cancel"), send his next
  message to Violet the same way, unchanged.
- If the script prints an error, tell him Violet is unreachable right now and show the error line. Don't make
  up an answer.
- Don't copy Violet's answers into your own long-term memory.
