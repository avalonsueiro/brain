# Setup

The agent rig: one Discord channel is one durable Claude Code session, resumed
one turn per message — plus an inject spool and wake scheduler so a turn can
start without you. Budget an evening.

Order matters. Step 1 is the one that silently destroys sessions a month from
now if you skip it.

---

## 1. Stop Claude Code garbage-collecting your sessions

In the `~/.claude/settings.json` of **whichever user the daemon runs as**:

```json
"cleanupPeriodDays": 365
```

Claude Code deletes session transcripts under `~/.claude/projects` after **30
days** by default. Every channel resumes by reading that file. Leave the default
and any channel you don't touch for a month loses its entire memory, with no
error — it just starts fresh and acts like it never knew you.

`cleanupPeriodDays` only stops the garbage collector. It does nothing about disk
loss, a mistaken `!reset`, or a bad migration — that's what `rig backup` is for.

`rig doctor` checks this.

---

## 2. Create the Discord application

1. <https://discord.com/developers/applications> → **New Application**.
2. **Bot** → **Reset Token** → copy it. This is the only time it's shown.
3. On the same page, enable **MESSAGE CONTENT INTENT** under *Privileged Gateway
   Intents*. Without it the bot receives empty message bodies and nothing works.
4. **OAuth2 → URL Generator**: scope `bot`; permissions `Send Messages` and
   `Read Message History`. (The ticker edits and deletes only its *own*
   messages, which every bot may do — `Manage Messages` is not needed.) Open the
   generated URL and invite it to a **private** server you own.
5. Collect two ids — Discord **Settings → Advanced → Developer Mode**, then
   right-click to copy:
   - the **server id** → `DISCORD_GUILD_ID`
   - **your own user id** → `DISCORD_ALLOWED_USER_IDS`

---

## 3. Bootstrap the runtime tree

```bash
sudo /path/to/brain/bin/rig bootstrap
```

This creates `/opt/agent-rig`, a template `agent.env`, and — only if you asked
for a separate account (see below) — the user to own them.

### Who the daemon runs as, and what that costs you

The daemon runs `claude` with `--dangerously-skip-permissions`. That is not
optional: a daemon cannot answer a permission prompt, and a turn that stalls on
one is a dead channel.

**By default the daemon runs as you.** `RIG_USER` defaults to whoever runs
`bootstrap`. Be clear-eyed about what that means: a shell command inside any
turn runs with your privileges and can read `~/.ssh`, every private repo in your
home directory, and your browser profiles. **On macOS your only containment is
`DISCORD_ALLOWED_USER_IDS`** — treat the Discord server as a credential and keep
exactly one human in it.

**Why not a dedicated user on macOS.** A non-GUI account has no login keychain,
and that is where Claude Code stores its OAuth credentials. Such an account
cannot complete the login, at setup or at turn time. `bootstrap` warns you if
you try.

**On Linux, do use one:**

```bash
sudo RIG_USER=agentrig bin/rig bootstrap
```

That creates a real least-privilege system account, and the systemd unit adds
`ProtectHome=read-only`, `ProtectSystem=full`, and `NoNewPrivileges`. This is
the configuration to run once the rig moves off the laptop.

### Why `/opt/agent-rig`

`--resume` finds a session's transcript at
`~/.claude/projects/<cwd with / and . replaced by ->/<uuid>.jsonl`. The *working
directory string* is part of the session's identity. `/Users/you/...` and
`/home/you/...` encode differently, so a home-relative layout would destroy
every session the day you move to a Linux box. `/opt/agent-rig` is
byte-identical on both.

Then finish the moves `bootstrap` prints:

```bash
sudo mv /path/to/brain /opt/agent-rig/brain
ln -s /opt/agent-rig/brain ~/brain          # optional, for convenience
```

---

## 4. Configure

```bash
$EDITOR /opt/agent-rig/state/agent.env     # chmod 600; keep it that way
```

| key | default | meaning |
|---|---|---|
| `DISCORD_BOT_TOKEN` | — | from step 2 |
| `DISCORD_GUILD_ID` | — | the one server this daemon serves |
| `DISCORD_ALLOWED_USER_IDS` | — | space/comma separated; **only these can trigger turns** |
| `DISCORD_IGNORE_CHANNELS` | — | channels that receive posts but never run a turn |
| `MAX_CONCURRENT_TURNS` | `3` | global cap. Each turn is 400–650MB |
| `TURN_TIMEOUT` | `2500` | seconds before a hung turn is killed |
| `RIG_TZ` | `America/New_York` | passed to every turn as `TZ`, so the agent knows the date |
| `DEFAULT_MODEL` | `opus` | alias: `opus`, `fable`, `sonnet`, `haiku` |
| `RIG_FOOTER` | `off` | stats under a successful answer: `off` \| `minimal` \| `full`. Errors always get one |
| `RIG_HISTORY` | `1` | set `0` to disable the SQLite turn log |
| `RIG_LOG_LEVEL` | `INFO` | daemon log verbosity |
| `CLAUDE_BIN` | `claude` | path to the CLI, if not on `PATH` |
| `INJECT_POLL` | `5` | seconds between inject spool sweeps |
| `WAKE_POLL` | `20` | seconds between wake scheduler sweeps |
| `WAKE_MAX_LATE` | `43200` | a wake later than this (12h) is dropped, not fired |

Read by `bin/rig` rather than the daemon, so set them in the environment, not
this file: `RIG_ROOT` (default `/opt/agent-rig`), `RIG_USER`, `RIG_GROUP`,
`RIG_BACKUP_KEEP` (default `48`).

The daemon refuses to start if the token, guild, or allowlist is missing, and
tells you which.

---

## 5. Install and start

```bash
/opt/agent-rig/brain/bin/rig venv     # .venv + discord.py
/opt/agent-rig/brain/bin/rig install  # service + hourly backup
/opt/agent-rig/brain/bin/rig start
/opt/agent-rig/brain/bin/rig logs
```

**No `sudo` on macOS** — these are your own LaunchAgents, and `rig install`
refuses if you try. On Linux they are system units and do need `sudo`.

`rig doctor` checks the whole install if anything looks wrong. `rig run` runs it
in the foreground, which is what you want while changing code.

### Why a LaunchAgent and not a LaunchDaemon (macOS)

Claude Code keeps its OAuth credentials in the login keychain, and a
system-domain job has no user session to unlock one — every turn dies with
`Not logged in · Please run /login`, even when the job is configured to run as
you. A LaunchAgent in `gui/<uid>` runs inside your session and can read the
keychain. This is the same constraint that rules out a dedicated non-GUI rig
user on this platform.

The cost: it starts at **login**, not at boot. A reboot needs someone to log in.

**launchd will not run this with the lid shut**, either. For a laptop, pair it
with `caffeinate -is` or `sudo pmset -c disablesleep 1`. Both constraints
disappear on Linux, where systemd has no keychain and a real system service
works — which is the honest reason to move the rig to a box eventually.

---

## 6. Use it

Post in any channel in the guild. The first message registers it, creates
`/opt/agent-rig/workdirs/<name>/`, seeds a `CLAUDE.md` of standing rules, and
starts a session. Every later message resumes it.

```
!ping                    daemon uptime and load
!ctx                     session id, model, transcript size, compaction state
!model [alias]           show or switch model
!reset                   new session id — fresh context, same workdir
!compact                 compact this session
!abort                   kill the turn running in this channel
!wake in 2h <text>       book a future turn in this channel
!wake at 18:30 <text>    same, at a clock time
!wake list | cancel <id> see and cancel pending wakes
```

## Waking an agent from outside Discord

Two verbs, both plain files on disk — a producer needs nothing but a filesystem.

**Now**, from cron, a git hook, or any script:

```bash
skills/inject/inject.py --channel general --text "morning brief" --label cron
```

**Later**, from a terminal or from an agent that needs to continue its own work:

```bash
skills/wake/wake.py add --in 20m --channel general --prompt "check the build"
```

`--label` is what the agent sees as the speaker, so it can tell an automated
ping from a human one. A wake fires *through* inject, so both land in the same
place.

Wakes survive restarts (`jobs.json` is on disk) and sleep. A wake that came due
while the laptop was closed fires on wake with its text prefixed
`(late by 3h)`; past `WAKE_MAX_LATE` it is dropped rather than acted on stale.

> **The spool is an unauthenticated path to running turns.** Anything that can
> write to `/opt/agent-rig/inject/` gets a turn with
> `--dangerously-skip-permissions` and no allowlist check — that gate only
> covers messages arriving over Discord. The directory is `0700`, and every
> injected turn posts a visible `📥` line so nothing runs silently.

Three things worth knowing:

- **Messages coalesce.** Three messages typed in two seconds become one turn.
  Messages that arrive *during* a turn fold into the next one — they never
  interrupt.
- **Renaming a channel in Discord is safe.** The workdir is pinned at creation
  and never recomputed, precisely because moving it would orphan the session.
- **`!reset` is not undoable** from the daemon's side. The old transcript stays
  on disk and in backups, but the channel will not resume it.

---

## 7. Verify

```bash
cd /opt/agent-rig/brain
.venv/bin/python tests/test_rig.py    # 286 offline checks, no Discord, no network
bin/rig doctor
```

Then, in Discord:

| # | do | expect |
|---|---|---|
| 1 | `hi, what directory are you in?` | ticker updates, answer names the workdir |
| 2 | `remember the number 47`, `rig restart`, then ask for it | **47** — this is the whole point |
| 3 | three messages in ~2s | one turn in the logs, all three lines in it |
| 4 | long prompt, then another message mid-turn | second folds into the next turn |
| 5 | `!reset`, send a prompt, `!abort` it right after it starts | next message resumes cleanly, no duplicate-session error |
| 6 | `TURN_TIMEOUT=20`, send a long task | killed, timeout message posted |
| 7 | stop daemon, rename a channel's `.jsonl`, start, post | 🧠 error, **not** a silent fresh session |
| 8 | post from a non-allowlisted account | dropped and logged |
| 9 | `rig backup`, delete a transcript, `rig restore <archive>` | session resumes again |

On Linux with `RIG_USER=agentrig`, one more gate worth running — it should be
**denied**:

```bash
sudo -u agentrig cat ~/.ssh/id_*
```

There is no equivalent on the default macOS install, because there is no
separate account to be denied by.

---

## Skills — what your agents can actually do

Linked into the rig user's `~/.claude/skills/` by `rig link-skills`:

| Skill | Does |
|---|---|
| `graph` | The shared memory graph. Survives `!reset`, visible from every channel |
| `recall` | Full-text search over the turn log — what compaction took out of context |
| `wake` | Book a future turn |
| `inject` | Start a turn from cron or a script |
| `google` | Read-only Gmail and Calendar (one-time OAuth — see [SKILLS.md](SKILLS.md)) |

Every channel's `CLAUDE.md` carries a standing rule to **investigate before
answering** — graph first, then `recall`, then email and calendar — and to write
what it learns back to the graph. That instruction is why the memory compounds
instead of just existing.

`rig reseed` refreshes every existing channel's `CLAUDE.md` from the template.
Run it after changing the standing rules: `_seed_workdir` only writes the file
when it is absent, so channels created earlier would never see the change.

## Hard-won rules

These came from real outages in the rig this is modelled on. They're also
written into every channel's `CLAUDE.md`, where the agent will actually read
them.

- **Never restart the daemon from inside a turn.** It's serving that turn and
  every other channel. Use `rig restart` from a terminal.
- **Never regex-parse a file over ~100KB or any minified file.** A single-line
  HTML file OOM'd the box this design came from. Stream it.
- **Code changes and deploys are two separate conversations.** Always.

## Backups contain secrets

`/opt/agent-rig/state/backups/*.tar.gz` include `agent.env`, and therefore your
bot token. They are written `0600`. Do not sync that directory anywhere.
