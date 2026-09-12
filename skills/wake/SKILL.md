---
name: wake
description: Schedule a future turn in a Discord channel. Use when you need to check something later, wait out a slow process (build, deploy, scrape), follow up on something you cannot finish now, or remind the operator at a specific time. You cannot stay resident between turns — booking a wake is how you continue work later.
---

# wake

Your process exits at the end of every turn. You cannot sleep, poll, or wait.
What you *can* do is book a future turn and exit — the daemon resumes this
session later with your full context intact.

```bash
~/.claude/skills/wake/wake.py add --in 20m --channel <this-channel> --prompt "check if the build finished"
~/.claude/skills/wake/wake.py add --at 08:00 --channel <this-channel> --prompt "morning brief"
~/.claude/skills/wake/wake.py list
~/.claude/skills/wake/wake.py cancel w7k2
```

`--in` takes `45s`, `20m`, `2h`, `3d`, `1h30m`. `--at` takes `18:30`, `6:30pm`,
or `2026-09-20 18:30`, in the box's local timezone. A bare `HH:MM` that has
already passed today means tomorrow.

`--channel` is the channel name without the `#`. Your prompt line says which
channel you are in.

## Writing the prompt

The prompt is what *you* will read when you wake up. You will have the session
transcript, so you do not need to restate context — but you do need to state the
**action**, because a vague prompt wakes you up with nothing to do.

Weak: `"follow up"`
Strong: `"check whether PR 41 finished CI; if it passed, tell Avalon, if it failed, summarize the failing job"`

## Polling something

There is no recurring schedule, on purpose. To watch something, check it and
book the next wake only if there is still something to watch:

1. Wake, check the thing
2. Changed or done → report to the channel, book nothing, stop
3. Still pending → book another wake and exit

This means a watch loop ends by itself. Give it a stopping condition — a
deadline or an attempt count you track in the channel — so it cannot run
forever.

## Don't

- Book a wake under a minute out to simulate waiting. If you need a result now,
  run the thing and wait for it inside this turn.
- Book many wakes at once to cover a range of times. Book one, then re-book.
- Book a wake for a channel that is not yours unless you were asked to.
