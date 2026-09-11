"""Paths, environment, and the model dial.

Every path that is part of a session's *identity* -- workdirs and state --
resolves from RIG_ROOT and nowhere else. (The repo path does not: it comes from
__file__, and transcripts live under the rig user's home.) That is the whole
portability story: `--resume` finds a session transcript at
`~/.claude/projects/<cwd with / and . replaced by ->/<uuid>.jsonl`, so if a
channel's working directory changes, its session is gone. RIG_ROOT defaults to
/opt/agent-rig, a path that can be byte-identical on macOS and Linux, which is
what lets the whole tree move to a box later without losing sessions.
"""

from __future__ import annotations

import os
from pathlib import Path

# --- paths -----------------------------------------------------------------

RIG_ROOT = Path(os.environ.get("RIG_ROOT", "/opt/agent-rig"))

REPO_DIR = Path(__file__).resolve().parent.parent
WORKDIRS_DIR = RIG_ROOT / "workdirs"
STATE_DIR = RIG_ROOT / "state"

ENV_FILE = STATE_DIR / "agent.env"
STATE_FILE = STATE_DIR / "state.json"
HISTORY_DB = STATE_DIR / "history.db"
BACKUP_DIR = STATE_DIR / "backups"
LOG_DIR = STATE_DIR / "logs"

TEMPLATE_CLAUDE_MD = REPO_DIR / "templates" / "workdir-CLAUDE.md"


def ensure_dirs() -> None:
    for d in (WORKDIRS_DIR, STATE_DIR, BACKUP_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)


# --- environment -----------------------------------------------------------


def load_env(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from agent.env into os.environ.

    Real environment variables always win, so `RIG_ROOT=/tmp/x python -m daemon`
    and one-off overrides behave the way you would expect.
    """
    path = path or ENV_FILE
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _id_set(name: str) -> set[int]:
    out: set[int] = set()
    for part in (os.environ.get(name) or "").replace(",", " ").split():
        try:
            out.add(int(part))
        except ValueError:
            pass
    return out


def _name_set(name: str) -> set[str]:
    return {
        p.strip().lstrip("#").lower()
        for p in (os.environ.get(name) or "").replace(",", " ").split()
        if p.strip()
    }


class Config:
    """Snapshot of the environment, read once at startup."""

    def __init__(self) -> None:
        self.discord_token = os.environ.get("DISCORD_BOT_TOKEN", "")
        self.guild_id = _int("DISCORD_GUILD_ID", 0)
        self.allowed_user_ids = _id_set("DISCORD_ALLOWED_USER_IDS")
        self.ignore_channels = _name_set("DISCORD_IGNORE_CHANNELS")

        self.max_concurrent_turns = _int("MAX_CONCURRENT_TURNS", 3)
        self.turn_timeout = _int("TURN_TIMEOUT", 2500)
        self.tz = os.environ.get("RIG_TZ", "America/New_York")
        self.default_model = os.environ.get("DEFAULT_MODEL", "opus")

        # off | minimal | full -- what to show under a successful answer.
        # Errors always get a footer regardless: a silent failure is worse than
        # a noisy success.
        self.footer = os.environ.get("RIG_FOOTER", "off").lower()

        self.claude_bin = os.environ.get("CLAUDE_BIN", "claude")
        self.log_level = os.environ.get("RIG_LOG_LEVEL", "INFO").upper()
        self.history_enabled = (
            os.environ.get("RIG_HISTORY", "1").lower() not in ("0", "false", "no")
        )

    def problems(self) -> list[str]:
        """Startup validation. Refuse to boot half-configured."""
        out = []
        if not self.discord_token:
            out.append("DISCORD_BOT_TOKEN is not set")
        if not self.guild_id:
            out.append("DISCORD_GUILD_ID is not set")
        if not self.allowed_user_ids:
            out.append(
                "DISCORD_ALLOWED_USER_IDS is empty — with --dangerously-skip-permissions "
                "that would let any guild member run commands as this user"
            )
        if self.default_model not in MODELS:
            out.append(
                f"DEFAULT_MODEL={self.default_model!r} is not a known alias "
                f"({', '.join(MODELS)})"
            )
        if self.max_concurrent_turns < 1:
            out.append("MAX_CONCURRENT_TURNS must be >= 1")
        return out


# --- the model dial --------------------------------------------------------
#
# Keyed by bare alias. The `[1m]` suffix is a property of the entry, composed at
# spawn time. A channel's `model` field stores the ALIAS, so a model rename is a
# config edit rather than a migration across every channel; `model_resolved` is
# a discardable cache of whichever composed string actually worked, and
# _model_order drops it the moment it is no longer a valid candidate.
#
#   long_ctx True  -> only the [1m] form
#   long_ctx None  -> untested here: try [1m], fall back to bare
#   long_ctx False -> no [1m] variant

MODELS: dict[str, dict] = {
    "opus": {"id": "claude-opus-5", "long_ctx": True},
    "fable": {"id": "claude-fable-5", "long_ctx": None},
    "sonnet": {"id": "claude-sonnet-5", "long_ctx": None},
    "haiku": {"id": "claude-haiku-4-5-20251001", "long_ctx": False},
}


def model_candidates(alias: str) -> list[str]:
    """Model strings to try for an alias, best first.

    The [1m] window matters more here than in an interactive session: a channel
    is resumed for months, and a 200k window starts compacting far sooner.
    """
    entry = MODELS.get(alias)
    if entry is None:
        raise KeyError(alias)
    base, long_ctx = entry["id"], entry["long_ctx"]
    if long_ctx is True:
        return [f"{base}[1m]"]
    if long_ctx is False:
        return [base]
    return [f"{base}[1m]", base]
