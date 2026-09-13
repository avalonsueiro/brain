# Skills

A skill is a folder with a `SKILL.md` and a plain CLI. That is the whole design,
and it is deliberate: **a capability that is a command-line tool is
model-independent.** The same skill works under Claude Code, Codex, or OpenCode
— swap the brain, keep the tools.

`rig link-skills` symlinks each one into the rig user's `~/.claude/skills/`,
where Claude Code discovers it. It refuses to overwrite anything it does not
own, so your personal skills are safe.

## What's here

| Skill | Does | Needs |
|---|---|---|
| `graph` | The shared memory graph — people, projects, decisions | nothing |
| `recall` | Full-text search over the turn log | nothing |
| `wake` | Book a future turn | nothing |
| `inject` | Start a turn from cron or a script | nothing |
| `google` | Read-only Gmail and Calendar | one-time OAuth |

The first four are stdlib-only and work the moment they are linked.

---

## Setting up `google` (once)

Read-only Gmail and Calendar, on your own account. The PRD recommends dedicated
bot identities and that is right for outward or client work — but a bot account
cannot read *your* inbox. Least privilege here means narrow scopes, revocable in
one click.

**1. A Google Cloud project** — <https://console.cloud.google.com/projectcreate>

**2. Enable two APIs** — *APIs & Services → Library*: **Gmail API** and
**Google Calendar API**.

**3. OAuth consent screen** — *External*, and add your own address under **Test
users**. Leave it in Testing; publishing is for apps other people use.

**4. Credentials** → *Create credentials → OAuth client ID → **Desktop app***.
Copy the client ID and secret.

**5. Store them and authorize:**

```bash
umask 077
cat > /opt/agent-rig/state/google.env <<'ENV'
CLIENT_ID=...apps.googleusercontent.com
CLIENT_SECRET=GOCSPX-...
ENV
chmod 600 /opt/agent-rig/state/google.env

/opt/agent-rig/brain/skills/google/auth.py login
```

A browser opens. Google will warn the app is **unverified** — expected for a
personal Desktop client you created. *Advanced → Go to (project)*.

```bash
skills/google/auth.py status     # confirms the address and message count
skills/google/auth.py revoke     # invalidates the refresh token
```

**Where the credentials live:** `$RIG_ROOT/state/google.env`, mode `0600`,
outside the repo. `rig doctor` checks the mode; `rig backup` includes it (like
`agent.env`, which is why backups are `0600` too).

**Revoking for real:** `auth.py revoke` clears the local token.
<https://myaccount.google.com/permissions> is the authoritative switch.

---

## Writing a new skill

```
skills/<name>/
  SKILL.md        YAML frontmatter (name, description) + how to use it
  <name>.py       the CLI
  .env            optional, chmod 600, gitignored
```

Then `rig link-skills`.

Four things that make a skill actually get used:

**The `description` is a routing decision.** It is what the model sees when
choosing whether to reach for this. Write when to use it, not what it is —
"use when you need X" beats "a tool for X".

**Resolve paths at call time, never at import.** The daemon imports skills
before `load_env()` runs, so a module-level `os.environ.get("RIG_ROOT")` freezes
the wrong value and silently ignores `agent.env`. Use a function.

**Stdlib only where you can.** Skills are meant to be standalone and
harness-agnostic; a dependency is a thing that breaks when someone runs the CLI
outside the venv.

**Bound your output.** An agent pastes your stdout into a context window. Return
what was asked for, truncate the rest, and say that you truncated — silent
truncation is how an agent confidently summarizes half a document.
