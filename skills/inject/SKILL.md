---
name: inject
description: Send a message into a Discord channel and start a turn there, by dropping a file in the inject spool. Use for cron jobs, git hooks, CI callbacks, or any script that needs to reach an agent. To schedule something for later rather than now, use the wake skill instead.
---

# inject

Starts a turn in a Discord channel from outside Discord.

```bash
~/.claude/skills/inject/inject.py --channel general --text "morning brief" --label cron
echo "deploy finished" | ~/.claude/skills/inject/inject.py --channel general --label ci
```

Writes a JSON file to `$RIG_ROOT/inject/` and exits. The daemon picks it up
within ~5 seconds, posts the text to the channel, and runs a turn.

- `--channel` — name (no `#`) or numeric id
- `--text` — the message; omitted means read stdin
- `--label` — who the agent sees as the speaker. Use it to say where this came
  from (`cron`, `ci`, `git`) so the agent can tell an automated ping from a human

## Notes

- **The daemon does not need to be running.** The file waits in the spool and
  is consumed when the daemon starts.
- **Delivery is confirmed by the file disappearing.** An unresolvable channel
  gets renamed `.failed` and left in place.
- **This starts a real turn**, which costs tokens and can take minutes. Do not
  call it in a loop.
- For "do this later", use `wake` instead — it schedules one and fires it
  through this same path.

## Example: a morning brief

```cron
0 8 * * *  /opt/agent-rig/brain/skills/inject/inject.py --channel general \
             --text "Morning. What's on my calendar today, and anything I said I'd follow up on?" \
             --label cron
```
