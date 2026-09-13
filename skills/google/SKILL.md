---
name: google
description: Read the operator's Gmail and Google Calendar. Use when a question turns on real email or real schedule — who someone is and how they know the operator, what was actually agreed in a thread, what is on today, when the next meeting is. Read-only: these cannot send mail, reply, archive, delete, or change any event.
---

# google

Two read-only CLIs over one authorization.

```bash
D=~/.claude/skills/google

$D/gmail.py list --query "is:unread" --limit 10
$D/gmail.py list --query "from:someone@example.com" --days 30
$D/gmail.py read <message-id>
$D/gmail.py thread <thread-id>

$D/gcal.py today
$D/gcal.py next
$D/gcal.py week
$D/gcal.py range --from 2026-09-20 --to 2026-09-27
```

## What these cannot do

Send, reply, archive, label, delete, or touch a calendar event. The scopes are
`gmail.readonly` and `calendar.readonly`. If the operator asks you to send
something, draft it in the channel and say plainly that you cannot send it.

## Reading email without drowning

`list` returns senders, subjects and snippets — never bodies. Fetch a body with
`read` only when you actually need it, and only for the messages that matter.
An inbox page of full HTML mail is exactly the kind of thing the
don't-slurp-large-files rule exists for, and `read` truncates and tells you when
it did.

Gmail search syntax works in `--query`: `from:`, `to:`, `subject:`,
`is:unread`, `has:attachment`, `newer_than:7d`, `label:`, and quoted phrases.
Prefer a narrow query over a big `--limit`.

## What to do with what you find

Email is the *source*; it is not memory. When you learn something durable from
it — who a person is, what was agreed, a commitment with a date — **write it to
the graph**, with `--source` naming where it came from:

```bash
~/.claude/skills/graph/graph.py observe "Akeil Smith" \
  --content "CMU, same cohort; co-founder on outlate" --source "gmail 2026-09-13"
```

Otherwise the next session searches the same inbox for the same answer.

## Care

- **This is the operator's real mail.** Read what the question needs, not the
  neighbourhood around it. Do not go browsing.
- **Quote precisely.** If you are reporting what someone said, `read` the
  message rather than paraphrasing a snippet.
- **Say when something is absent.** "Nothing in your email mentions X" is a
  finding. Do not fill the gap with a guess — a wrong fact written to the graph
  outlives the turn that invented it.
- Calendar times render in the box's configured timezone. Trust them over any
  raw timestamp you happen to see.

## Setup

Already authorized if `auth.py status` prints an address. If not, the operator
runs `auth.py login` once — it needs a browser, so you cannot do it for them.
