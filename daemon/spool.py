"""The inject spool and the wake scheduler.

Phase 1 could only be started by a human typing in Discord. These two loops are
what let anything else start a turn:

  INJECT -- drop a JSON file in the spool and a turn runs. This is how cron,
            a git hook, or any script on the box reaches an agent.
  WAKE   -- a job in jobs.json fires later, through INJECT. This is how an agent
            that cannot stay resident continues its own work: it books a wake and
            exits, and the daemon resumes the session when the time comes.

Both are plain files on disk, published write-tmp-then-rename, so a producer
needs nothing but a filesystem -- no client library, no socket, no daemon
contact.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from pathlib import Path

import discord

from . import config

log = logging.getLogger("rig.spool")

# Import the wake module from skills/ -- the CLI agents call and the daemon are
# the same implementation, so `!wake` and `wake.py add` cannot drift apart.
import sys

sys.path.insert(0, str(config.REPO_DIR / "skills" / "wake"))
import wake as wake_mod  # noqa: E402


def write_inject(
    channel: str,
    text: str,
    label: str = "inject",
    inject_dir: Path | None = None,
) -> Path:
    """Publish an inject file. Atomic: .tmp first, then rename into place."""
    inject_dir = inject_dir or config.INJECT_DIR
    inject_dir.mkdir(parents=True, exist_ok=True)
    name = f"{int(time.time() * 1000)}-{random.randint(1000, 9999)}"
    tmp = inject_dir / f"{name}.json.tmp"
    final = inject_dir / f"{name}.json"
    tmp.write_text(
        json.dumps({"channel": channel, "text": text, "label": label}),
        encoding="utf-8",
    )
    os.replace(tmp, final)
    return final


class Spool:
    def __init__(self, bot) -> None:
        self.bot = bot
        self.cfg = bot.cfg
        self.tasks: list[asyncio.Task] = []

    def start(self) -> None:
        """Called from setup_hook, which runs exactly once.

        Not on_ready: that re-fires on every gateway reconnect, and each
        reconnect would stack another copy of both loops.
        """
        self.tasks = [
            asyncio.create_task(self._inject_loop(), name="rig-inject"),
            asyncio.create_task(self._wake_loop(), name="rig-wake"),
        ]
        log.info(
            "spool started: inject every %ds from %s, wake every %ds",
            self.cfg.inject_poll, config.INJECT_DIR, self.cfg.wake_poll,
        )

    def stop(self) -> None:
        for task in self.tasks:
            task.cancel()

    # --- inject ------------------------------------------------------------

    async def _inject_loop(self) -> None:
        while not self.bot.draining:
            try:
                await self._drain_inject_dir()
            except Exception:
                # One bad file must never stop the loop.
                log.exception("inject sweep failed")
            await asyncio.sleep(self.cfg.inject_poll)

    async def _drain_inject_dir(self) -> None:
        if not config.INJECT_DIR.exists():
            return
        # Sorted by filename, which is millisecond-prefixed -- the spool is FIFO.
        # *.tmp is skipped by the glob, so a half-written file is never read.
        for path in sorted(config.INJECT_DIR.glob("*.json")):
            if self.bot.draining:
                return
            try:
                await self._consume(path)
            except Exception:
                log.exception("inject %s failed", path.name)
                _fail(path)

    async def _consume(self, path: Path) -> None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("inject %s: unreadable (%s)", path.name, exc)
            _fail(path)
            return

        text = (payload.get("text") or "").strip()
        target = str(payload.get("channel") or "").strip()
        label = (payload.get("label") or "inject").strip() or "inject"

        if not text or not target:
            log.warning("inject %s: needs both 'channel' and 'text'", path.name)
            _fail(path)
            return

        channel = self._resolve(target)
        if channel is None:
            # Load-bearing rename: Phase 4's collab confirms real delivery by
            # watching for exactly this.
            log.warning("inject %s: no such channel %r", path.name, target)
            _fail(path)
            return

        # Post before enqueueing, so an injected turn is never invisible -- the
        # spool is an unauthenticated path to running commands, and the channel
        # log is what makes that auditable.
        try:
            await channel.send(f"📥 {text[:1900]}")
        except discord.HTTPException as exc:
            log.warning("inject %s: could not post to #%s (%s)", path.name, target, exc)

        rec = self.bot.state.register(channel.id, getattr(channel, "name", target))
        if self.bot.history:
            # on_message logs inbound for typed messages; the spool bypasses it,
            # so without this the history shows an answer with nothing that
            # provoked it -- and "what woke this turn" is exactly what you go
            # looking for when reading back.
            self.bot.history.log_inbound(
                channel.id, getattr(channel, "name", target), rec, label, text
            )
        self.bot._queue_for(channel.id).put_nowait(
            {"author": label, "text": text, "channel": channel}
        )
        path.unlink(missing_ok=True)
        log.info("inject -> #%s as %r (%d chars)", getattr(channel, "name", target), label, len(text))

    def _resolve(self, target: str):
        """Channel by id or name.

        Resolved against the live guild, not state.json: a channel that has
        never run a turn is not in state yet, and injecting into a fresh channel
        is a normal thing to want.
        """
        guild = self.bot.get_guild(self.cfg.guild_id)
        if guild is None:
            return None
        if target.isdigit():
            return guild.get_channel(int(target))
        name = target.lstrip("#").lower()
        for channel in guild.text_channels:
            if channel.name.lower() == name:
                return channel
        return None

    # --- wake --------------------------------------------------------------

    async def _wake_loop(self) -> None:
        while not self.bot.draining:
            try:
                self._fire_due()
            except Exception:
                log.exception("wake sweep failed")
            await asyncio.sleep(self.cfg.wake_poll)

    def _fire_due(self) -> None:
        now = int(time.time())
        # take_due removes the jobs under an flock before we write any inject
        # file. A crash in that gap loses a wake rather than firing it twice --
        # deliberate, since a double-fired continuation can act twice.
        for job in wake_mod.take_due(now):
            late = now - int(job.get("at", now))
            text = job.get("prompt") or ""

            if late > self.cfg.wake_max_late:
                log.warning(
                    "wake %s dropped: %s late (over WAKE_MAX_LATE=%ds) — %r",
                    job.get("id"), wake_mod.human_delta(late), self.cfg.wake_max_late, text[:80],
                )
                continue

            # This machine sleeps, so jobs routinely come due with nothing
            # running. Say so, rather than letting the agent assume it is on time.
            if late > 90:
                text = f"(late by {wake_mod.human_delta(late)}) {text}"

            write_inject(job.get("channel", ""), text, job.get("label") or "wake")
            log.info("wake %s fired -> #%s", job.get("id"), job.get("channel"))


def _fail(path: Path) -> None:
    """Rename to .failed rather than deleting: a spool file is evidence."""
    try:
        path.rename(path.with_suffix(path.suffix + ".failed"))
    except OSError:
        pass
