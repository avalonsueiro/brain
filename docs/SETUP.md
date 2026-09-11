# Setup

Phase 1 of the agent rig: one Discord channel, one durable Claude Code session,
one turn per message. Budget an evening.

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

Read by `bin/rig` rather than the daemon, so set them in the environment, not
this file: `RIG_ROOT` (default `/opt/agent-rig`), `RIG_USER`, `RIG_GROUP`,
`RIG_BACKUP_KEEP` (default `48`).

The daemon refuses to start if the token, guild, or allowlist is missing, and
tells you which.

---

## 5. Install and start

```bash
/opt/agent-rig/brain/bin/rig venv          # .venv + discord.py
sudo /opt/agent-rig/brain/bin/rig install  # service + hourly backup
sudo /opt/agent-rig/brain/bin/rig start
/opt/agent-rig/brain/bin/rig logs
```

`rig doctor` checks the whole install if anything looks wrong. `rig run` runs it
in the foreground, which is what you want while changing code.

On macOS this is a **system-domain LaunchDaemon**, so it comes back after a
reboot without anyone logging in.

**launchd will not run this with the lid shut.** For a laptop, pair it with
`caffeinate -is` or set Energy Saver to prevent sleep on power. That constraint
is the honest reason Phase 4 moves this to a Linux box.

---

## 6. Use it

Post in any channel in the guild. The first message registers it, creates
`/opt/agent-rig/workdirs/<name>/`, seeds a `CLAUDE.md` of standing rules, and
starts a session. Every later message resumes it.

```
!ping             daemon uptime and load
!ctx              session id, model, transcript size, compaction state
!model [alias]    show or switch model
!reset            new session id — fresh context, same workdir
!compact          compact this session
!abort            kill the turn running in this channel
```

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
.venv/bin/python tests/test_rig.py    # 74 offline checks, no Discord, no tokens
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
