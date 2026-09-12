"""The channel registry: state.json.

A session is a durable uuid plus its on-disk transcript, not a resident process.
This module owns both halves of that identity -- the uuid and the working
directory the transcript is filed under -- and guarantees the working directory
never changes once assigned.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from . import config

log = logging.getLogger("rig.state")

_SAFE = re.compile(r"[^a-z0-9._-]+")


def _slug(name: str) -> str:
    s = _SAFE.sub("-", (name or "channel").lower()).strip("-._")
    return s or "channel"


class State:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config.STATE_FILE
        self.data: dict = {"channels": {}}
        self.load()

    # --- persistence -------------------------------------------------------

    def load(self) -> None:
        if self.path.exists():
            problem = None
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
                # Valid JSON is not necessarily a valid state file: `[]` or
                # `null` parses fine and then explodes on setdefault below.
                if not isinstance(self.data, dict):
                    problem = f"top level is {type(self.data).__name__}, not an object"
            except (json.JSONDecodeError, OSError) as exc:
                problem = str(exc)
            if problem:
                # Never start against a bad file. Keep the corpse for forensics
                # rather than overwriting it -- and say so loudly: booting with
                # an empty registry means every channel silently gets a fresh
                # session, which is indistinguishable from total amnesia.
                backup = self.path.with_suffix(f".corrupt.{int(time.time())}")
                try:
                    shutil.copy2(self.path, backup)
                except OSError:
                    backup = None
                log.error(
                    "state.json is unreadable (%s) — starting with an EMPTY channel "
                    "registry; every channel will begin a new session. Previous file "
                    "saved as %s. Restore from `rig backup` to recover.",
                    problem, backup,
                )
                self.data = {"channels": {}}
        self.data.setdefault("channels", {})

    def save(self) -> None:
        """Atomic: write .tmp, then os.replace. Survives a crash mid-write."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    # --- channels ----------------------------------------------------------

    def get(self, channel_id: int) -> dict | None:
        return self.data["channels"].get(str(channel_id))

    def all(self) -> dict[str, dict]:
        return self.data["channels"]

    def register(self, channel_id: int, name: str) -> dict:
        """Return the channel record, creating it on first sight."""
        key = str(channel_id)
        rec = self.data["channels"].get(key)

        if rec is not None:
            # Discord channels can be renamed. Track the new label, but NEVER
            # recompute workdir from it -- --resume locates the transcript by
            # the encoded cwd, so a moved workdir is a wiped session.
            if name and rec.get("name") != name:
                rec["name"] = name
                self.save()
            return rec

        rec = {
            "name": name,
            "workdir": str(self._new_workdir(channel_id, name)),
            "session_id": str(uuid.uuid4()),
            "model": os.environ.get("DEFAULT_MODEL", "opus"),
            "harness": "cc",  # carried from day one; the swap lands in Phase 6
            "primed": False,
            "started": int(time.time()),
            "turns": 0,
            "last_turn": None,
        }
        self.data["channels"][key] = rec
        self.save()
        return rec

    def _new_workdir(self, channel_id: int, name: str) -> Path:
        taken = {c.get("workdir") for c in self.data["channels"].values()}
        base = config.WORKDIRS_DIR / _slug(name)
        # Loop rather than trying one suffix: a channel literally named
        # "general-222" can already own the path that channel 222 would fall
        # back to, and two channels sharing a workdir means two sessions
        # sharing a transcript directory.
        path = base
        suffix = 0
        while str(path) in taken:
            suffix += 1
            path = base.with_name(f"{base.name}-{channel_id}" + (f"-{suffix}" if suffix > 1 else ""))
        path.mkdir(parents=True, exist_ok=True)
        self._seed_workdir(path)
        return path

    @staticmethod
    def _seed_workdir(path: Path) -> None:
        """Drop the standing rules where the agent will actually read them.

        A README reaches the operator. CLAUDE.md reaches the thing that caused
        the incidents these rules exist for.
        """
        target = path / "CLAUDE.md"
        if target.exists() or not config.TEMPLATE_CLAUDE_MD.exists():
            return
        text = config.TEMPLATE_CLAUDE_MD.read_text(encoding="utf-8")
        text = text.replace("__RIG_TZ__", os.environ.get("RIG_TZ", "America/New_York"))
        target.write_text(text, encoding="utf-8")

    # --- mutations ---------------------------------------------------------

    def update(self, channel_id: int, **fields) -> dict:
        rec = self.data["channels"][str(channel_id)]
        rec.update(fields)
        self.save()
        return rec

    def mark_primed(self, channel_id: int, session_id: str) -> None:
        """Commit primed the moment `system/init` arrives, not at end of turn.

        By init the transcript exists on disk. If we waited for the result event
        and the turn then crashed or timed out, the next turn would retry
        --session-id against a uuid that already exists and fail forever.

        Keyed by session_id, because commands bypass the turn queue: a `!reset`
        landing mid-turn swaps in a new uuid, and a blind write would then mark
        THAT uuid primed even though its transcript will never exist -- bricking
        the channel on the hard missing-transcript gate until another reset.
        """
        rec = self.data["channels"].get(str(channel_id))
        if rec is None or rec.get("session_id") != session_id:
            return
        if not rec.get("primed"):
            rec["primed"] = True
            self.save()

    def bump_turn(self, channel_id: int) -> None:
        rec = self.data["channels"].get(str(channel_id))
        if rec is not None:
            rec["turns"] = int(rec.get("turns") or 0) + 1
            rec["last_turn"] = int(time.time())
            self.save()

    def reset_session(self, channel_id: int) -> dict:
        """Fresh context, same workdir. The old transcript is left on disk."""
        return self.update(
            channel_id,
            session_id=str(uuid.uuid4()),
            primed=False,
            turns=0,
            started=int(time.time()),
        )


# --- transcript location ---------------------------------------------------


def encode_workdir(workdir: str | Path) -> str:
    """Claude Code's project-directory encoding: '/' and '.' both become '-'."""
    return str(workdir).replace("/", "-").replace(".", "-")


def transcript_path(workdir: str | Path, session_id: str) -> Path:
    return (
        Path.home()
        / ".claude"
        / "projects"
        / encode_workdir(workdir)
        / f"{session_id}.jsonl"
    )


def transcript_stats(workdir: str | Path, session_id: str) -> tuple[int, int]:
    """(bytes, lines) for a session transcript; (0, 0) if it does not exist.

    Streamed, never read whole: these files reach hundreds of megabytes.
    """
    p = transcript_path(workdir, session_id)
    if not p.exists():
        return 0, 0
    size = p.stat().st_size
    lines = 0
    with p.open("rb") as fh:
        for _ in fh:
            lines += 1
    return size, lines
