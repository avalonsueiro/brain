# brain

An agent rig: one Discord channel is one durable Claude Code session, resumed
one turn per message, from your phone.

Phase 1 of the build described in *The Agent Rig — Replication Blueprint*
(Chad Ozgur). Setup lives in **[docs/SETUP.md](docs/SETUP.md)**.

## The core idea

A session is a **uuid plus its on-disk transcript**, not a running process.

```
first message in a channel:   claude -p --session-id <uuid> --model <m> ...
every message after that:     claude -p --resume     <uuid> --model <m> ...
```

The process spawns, streams, and exits at the end of every turn. Nothing stays
resident. That's what makes the whole thing reboot-proof, and why 18 idle
channels cost nothing at all.

## Shape

```
Discord  ──►  daemon  ──►  per-channel queue + worker  ──►  claude -p  ──►  ticker
                │                                              │
             state.json                                   stream-json
        (uuid, workdir, model)                     (init / text / tool_use / result)
```

- **Per-channel queue + worker** — a channel never runs two turns at once, and
  everything queued since the last turn folds into *one* turn.
- **Global semaphore** — caps how many `claude` processes exist at once. Each is
  400–650MB.
- **Ticker** — one Discord message, edited at most every 1.5s as tools and text
  arrive. When the answer lands the ticker is deleted and only the answer
  remains; `RIG_FOOTER=minimal|full` keeps a duration/tokens/cost line instead.
  Failed turns always report a footer.

```
daemon/
  config.py    paths from RIG_ROOT, agent.env, the model dial
  state.py     state.json — durable session ids and immutable workdirs
  harness.py   argv, spawn, stream-json parsing, retries, killpg
  bot.py       Discord client, queues, workers, semaphore
  ticker.py    the live-editing message and Discord-safe splitting
  commands.py  ! commands
  history.py   SQLite WAL + FTS5 log of every turn
bin/rig        bootstrap · venv · install · start/stop · logs · backup · doctor
infra/         launchd plists (macOS) and systemd units (Linux)
templates/     the CLAUDE.md seeded into every channel workdir
tests/         74 offline checks — no Discord, no tokens
```

## Two settings that will bite you

**`"cleanupPeriodDays": 365`** in the rig user's `~/.claude/settings.json`. The default is 30,
after which Claude Code deletes the transcripts every channel resumes from. You
find out by watching an agent forget a month of context, with no error.

**The workdir path is part of session identity.** `--resume` locates the
transcript by the working directory with `/` and `.` replaced by `-`. Move the
workdir and the session is gone. That's why everything lives under
`/opt/agent-rig` — a path that can be byte-identical on macOS and Linux, so the
eventual move to a box keeps every session.

## Rules the agents are given

Seeded into every channel's `CLAUDE.md`, because a README only reaches the
operator:

- Never stop, kill, or restart the daemon from inside a turn — it's serving that
  turn and every other channel.
- Run `TZ=$RIG_TZ date` before any date reasoning.
- Never grep or regex-parse a file over ~100KB, or any minified file. Stream it.

## Commands

```
!ping   !ctx   !model [alias]   !reset   !compact   !abort
```

## Status

Phase 1 (daemon + session-per-message loop) is built. Later phases — the
inject/wake spool, the memory graph, the agent fleet, ticket→PR, the automation
layer — are deliberately not started until this has survived a week of real use.
