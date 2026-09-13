---
name: collab
description: Message the agents in other Discord channels, and create new agent channels. Use to ask another channel's agent for status, hand it work that belongs to its project, answer a question it asked you, or spin up a channel for a project that doesn't have one. Each channel is a separate agent with its own memory — this is the only way they reach each other.
---

# collab

Every channel is its own agent session with its own transcript. They share the
memory graph, but they cannot see each other's conversations. This is how they
talk.

```bash
C=~/.claude/skills/collab/collab.py

$C send snooze --from orchestrator --text "what's the state of the PDF export?"
$C send orchestrator --from snooze --tag reply --text "shipped, tests green"
$C list
$C spawn pulse --prime "You own the Pulse codebase at ~/pulse.v1..." --reply-to orchestrator
```

`--from` is your own channel name. It appears as the speaker on the other side,
so get it right — the receiving agent decides what to do partly based on who
asked.

## Replying

Messages arrive with the reply command already in them. You do not need to
remember any of this — copy the line, fill in the text.

Use `--tag reply` when you are answering something, so the other side can tell a
response from a new request.

## When to send

- You need something only another channel's agent knows
- Work came up that belongs to a different project
- You finished something another channel was waiting on
- You were asked to check in

## When not to send

- **To relay what the operator just said.** They are right there; talking to
  them directly is faster and they can see it.
- **To think out loud.** A collab message spawns a real turn on the other side,
  which costs time and tokens.
- **In a loop.** If you and another channel are going back and forth more than
  twice, one of you should do the work or escalate to the operator. Two agents
  agreeing with each other is not progress.

## Delivery is confirmed

`send` waits for the daemon to pick the message up and exits non-zero if the
channel could not be resolved. **Read the result.** Reporting "I asked snooze"
when the send failed is worse than reporting the failure — the operator acts on
the first and investigates the second.

If the daemon is down, the message waits in the spool and is delivered when it
comes back. Say that, rather than claiming it was delivered.

## Spawning

`spawn` creates a Discord channel, registers a fresh session, and runs your
`--prime` as its first turn. That prime is the agent's entire charter — it has
no other context — so say what it owns, where its code lives, and what it should
do first.

A new channel is a new live agent with its own cost. Spawn one when a project
genuinely needs a standing place to work, not for a one-off task you could do
here. There is a rate limit; hitting it means something is looping.
