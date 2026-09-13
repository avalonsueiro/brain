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
import re
import time
from pathlib import Path

import discord

from . import _skills, config

log = logging.getLogger("rig.spool")

# A turn's prompt is framed as "[via Discord #x]\n{author}: {text}", and that
# framing is how an agent tells a human from an automated ping. An unsanitized
# label could carry newlines and forge a second speaker line -- letting a turn
# in one channel impersonate the operator in another via `wake --label Avalon`.
_LABEL_OK = re.compile(r"[^A-Za-z0-9_.\- ]+")
MAX_LABEL = 32
MAX_INJECT_TEXT = 100_000


def _clean_label(raw) -> str:
    if not isinstance(raw, str):
        return "inject"
    return _LABEL_OK.sub("", raw).strip()[:MAX_LABEL] or "inject"

# The CLIs agents shell out to ARE the daemon's implementations -- loaded by
# path rather than by mutating sys.path, so a file dropped into a skill folder
# cannot shadow stdlib for the whole daemon.
wake_mod = _skills.load("wake")
_inject_mod = _skills.load("inject")

# One writer, three callers (daemon, inject CLI, collab). Aliased rather than
# reimplemented: a spool format that three files agree on only by coincidence
# drifts the first time one of them changes.
write_inject = _inject_mod.inject


class Spool:
    def __init__(self, bot) -> None:
        self.bot = bot
        self.cfg = bot.cfg
        self.tasks: list[asyncio.Task] = []
        self.control = Control(self)

    def start(self) -> None:
        """Called from setup_hook, which runs exactly once.

        Not on_ready: that re-fires on every gateway reconnect, and each
        reconnect would stack another copy of both loops.
        """
        self.tasks = [
            asyncio.create_task(self._inject_loop(), name="rig-inject"),
            asyncio.create_task(self._wake_loop(), name="rig-wake"),
            asyncio.create_task(self._control_loop(), name="rig-control"),
        ]
        log.info(
            "spool started: inject every %ds from %s, wake every %ds",
            self.cfg.inject_poll, config.INJECT_DIR, self.cfg.wake_poll,
        )

    async def stop(self) -> None:
        """Cancel the loops AND wait for them to actually stop.

        Cancelling without awaiting let an in-flight sweep resume after drain
        had sampled "nothing outstanding", enqueue its item and unlink the spool
        file -- losing a message that was never answered.
        """
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []

    # --- inject ------------------------------------------------------------

    async def _inject_loop(self) -> None:
        # Nothing may sweep before the gateway is ready. setup_hook runs several
        # seconds ahead of READY, and until then the guild cache is empty -- so
        # an early sweep saw every waiting file as "no such channel" and renamed
        # it .failed forever, destroying exactly the backlog the spool exists to
        # hold across a restart.
        await self.bot.wait_until_ready()
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

        # Valid JSON is not a valid payload: a list or a string would sail past
        # .get() straight into an AttributeError.
        if not isinstance(payload, dict):
            log.warning("inject %s: payload is %s, not an object", path.name, type(payload).__name__)
            _fail(path)
            return

        raw_text = payload.get("text")
        raw_target = payload.get("channel")
        if not isinstance(raw_text, str) or not isinstance(raw_target, (str, int)):
            log.warning("inject %s: 'channel' and 'text' must be strings", path.name)
            _fail(path)
            return

        text = raw_text.strip()[:MAX_INJECT_TEXT]
        target = str(raw_target).strip()
        label = _clean_label(payload.get("label"))

        if not text or not target:
            log.warning("inject %s: needs both 'channel' and 'text'", path.name)
            _fail(path)
            return

        if self.bot.get_guild(self.cfg.guild_id) is None:
            # Guild cache not populated (startup or a gateway reconnect). This
            # is NOT "channel doesn't exist" -- leave the file alone and retry.
            log.info("inject %s: guild unavailable, retrying next sweep", path.name)
            return

        channel = self._resolve(target)
        if channel is None:
            # Load-bearing rename: Phase 4's collab confirms real delivery by
            # watching for exactly this.
            log.warning("inject %s: no such channel %r", path.name, target)
            _fail(path)
            return

        name = getattr(channel, "name", target)
        if name.lower() in self.cfg.ignore_channels:
            # The one channel-level containment knob has to apply to the ungated
            # path too, or it isn't containment.
            log.warning("inject %s: #%s is in DISCORD_IGNORE_CHANNELS", path.name, name)
            _fail(path)
            return

        # Post before enqueueing, so an injected turn is never invisible -- the
        # spool is an unauthenticated path to running commands, and the channel
        # log is what makes that auditable. The label is shown: without it, a
        # forged speaker inside `text` has nothing to contradict it.
        if not await self._audit_post(channel, label, text):
            # An unannounced turn is worse than a late one. Leave the file for
            # the next sweep rather than running invisibly.
            log.warning("inject %s: audit post failed, deferring", path.name)
            return

        rec = self.bot.state.register(channel.id, name)
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
        log.info("inject -> #%s as %r (%d chars)", name, label, len(text))

    async def _audit_post(self, channel, label: str, text: str) -> bool:
        from .ticker import SAFE_LIMIT

        body = text if len(text) <= SAFE_LIMIT - 80 else text[: SAFE_LIMIT - 83] + "…"
        try:
            await channel.send(f"📥 **{label}**\n{body}")
            return True
        except discord.HTTPException as exc:
            log.warning("could not post the injection notice to #%s (%s)",
                        getattr(channel, "name", "?"), exc)
            return False

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

    # --- control -----------------------------------------------------------

    async def _control_loop(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.draining:
            try:
                await self._drain_control_dir()
            except Exception:
                log.exception("control sweep failed")
            await asyncio.sleep(self.cfg.inject_poll)

    async def _drain_control_dir(self) -> None:
        if not config.CONTROL_DIR.exists():
            return
        for path in sorted(config.CONTROL_DIR.glob("*.json")):
            if self.bot.draining:
                return
            await self._handle_control(path)

    async def _handle_control(self, path: Path) -> None:
        """Every request ends .done or .failed, and says why.

        A spawn that quietly failed is how an orchestrator ends up talking to a
        channel that does not exist, so the outcome is both recorded on disk and
        posted back to whoever asked.
        """
        req: dict = {}
        try:
            req = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(req, dict):
                raise ControlError(f"payload is {type(req).__name__}, not an object")
            action = req.get("action")
            handler = self.control.actions.get(action)
            if handler is None:
                raise ControlError(
                    f"unknown action {action!r} "
                    f"(have: {', '.join(self.control.actions)})"
                )
            outcome = await handler(req)
            ok = True
        except ControlError as exc:
            outcome, ok = str(exc), False
        except (json.JSONDecodeError, OSError) as exc:
            outcome, ok = f"unreadable request: {exc}", False
        except Exception as exc:
            log.exception("control %s blew up", path.name)
            outcome, ok = f"internal error: {exc!r}", False

        try:
            path.rename(path.with_suffix(f".json.{'done' if ok else 'failed'}"))
        except OSError:
            pass

        log.info("control %s: %s", "ok" if ok else "FAILED", outcome)
        reply_to = req.get("reply_to") if isinstance(req, dict) else None
        if reply_to:
            channel = self._resolve(str(reply_to))
            if channel is not None:
                icon = "🌱" if ok else "⚠️"
                try:
                    await channel.send(f"{icon} control: {outcome}")
                except discord.HTTPException:
                    pass

    # --- wake --------------------------------------------------------------

    async def _wake_loop(self) -> None:
        # A wake fires by writing an inject file, so firing before the gateway
        # is ready would hand it to a sweep that cannot resolve channels -- and
        # take_due has already deleted the job by then.
        await self.bot.wait_until_ready()
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
            # Per-job, so one malformed record cannot raise out of the sweep and
            # silently stop every wake in the system forever.
            try:
                self._fire_one(job, now)
            except Exception:
                log.exception("wake %s could not be fired; dropping it", job.get("id"))

    def _fire_one(self, job: dict, now: int) -> None:
        late = now - int(job.get("at", now))
        text = str(job.get("prompt") or "")
        channel = str(job.get("channel") or "")
        label = _clean_label(job.get("label") or "wake")

        if late > self.cfg.wake_max_late:
            log.warning(
                "wake %s dropped: %s late (over WAKE_MAX_LATE=%ds) — %r",
                job.get("id"), wake_mod.human_delta(late), self.cfg.wake_max_late, text[:80],
            )
            # Tell the channel. A silently dropped wake stops an unattended
            # workflow with no visible cause -- the agent that booked it and the
            # operator reading along both see nothing at all.
            write_inject(
                channel,
                f"A scheduled wake was dropped: it came due {wake_mod.human_delta(late)} ago, "
                f"past the {wake_mod.human_delta(self.cfg.wake_max_late)} staleness limit. "
                f"It said: {text[:500]}\n\nDo not act on it — just report that it was missed.",
                "rig",
            )
            return

        # This machine sleeps, so jobs routinely come due with nothing running.
        # Say so, rather than letting the agent assume it is on time.
        if late > 90:
            text = f"(late by {wake_mod.human_delta(late)}) {text}"

        write_inject(channel, text, label)
        log.info("wake %s fired -> #%s", job.get("id"), channel)


def _fail(path: Path) -> None:
    """Rename to .failed rather than deleting: a spool file is evidence."""
    try:
        path.rename(path.with_suffix(path.suffix + ".failed"))
    except OSError:
        pass


# --- CONTROL ---------------------------------------------------------------
#
# The privileged verb. Creating a channel means creating a Discord channel,
# minting a session uuid, seeding a workdir and running a first turn -- all of
# which only the daemon can do, because only the daemon holds the Discord client
# and state.json. Everything else asks by dropping a file here.


def write_control(action: str, control_dir: Path | None = None, **fields) -> Path:
    """Publish a control request. Same atomic discipline as the inject spool."""
    control_dir = control_dir or config.CONTROL_DIR
    control_dir.mkdir(parents=True, exist_ok=True)
    name = f"{int(time.time() * 1000)}-{os.getpid()}-{random.randint(1000, 9999)}"
    tmp = control_dir / f"{name}.json.tmp"
    final = control_dir / f"{name}.json"
    tmp.write_text(json.dumps({"action": action, **fields}), encoding="utf-8")
    os.replace(tmp, final)
    return final


class ControlError(Exception):
    """A request that cannot be honoured, with a reason worth reporting."""


class Control:
    """Handlers for the control spool. Split from Spool so the dispatch table
    is a dict of named methods rather than a branching if-chain."""

    def __init__(self, spool: "Spool") -> None:
        self.spool = spool
        self.bot = spool.bot
        self.cfg = spool.cfg
        self._spawns: list[float] = []      # timestamps, for the rate limit

    @property
    def actions(self) -> dict:
        return {
            "create_channel": self.create_channel,
            "archive_channel": self.archive_channel,
            "set_model": self.set_model,
        }

    # --- rate limit --------------------------------------------------------

    def _check_spawn_budget(self) -> None:
        cutoff = time.time() - 3600
        self._spawns = [t for t in self._spawns if t > cutoff]
        if len(self._spawns) >= self.cfg.max_spawns_per_hour:
            raise ControlError(
                f"spawn rate limit reached ({self.cfg.max_spawns_per_hour}/hour). "
                "Something is probably looping — check !fleet."
            )

    # --- actions -----------------------------------------------------------

    async def create_channel(self, req: dict) -> str:
        name = _clean_channel_name(req.get("name"))
        if not name:
            raise ControlError("create_channel needs a 'name'")

        guild = self.bot.get_guild(self.cfg.guild_id)
        if guild is None:
            raise ControlError("guild unavailable")

        existing = next((c for c in guild.text_channels if c.name == name), None)
        if existing is not None:
            raise ControlError(f"#{name} already exists")

        self._check_spawn_budget()
        try:
            channel = await guild.create_text_channel(
                name, topic=(req.get("topic") or "")[:1024] or None
            )
        except discord.Forbidden:
            raise ControlError(
                "the bot lacks MANAGE_CHANNELS — re-invite it with that permission "
                "(see docs/SETUP.md)"
            ) from None
        except discord.HTTPException as exc:
            raise ControlError(f"Discord refused to create #{name}: {exc}") from None

        self._spawns.append(time.time())
        rec = self.bot.state.register(channel.id, name)
        if req.get("model"):
            self.bot.state.update(channel.id, model=req["model"])

        await self.spool._audit_post(
            channel, "rig",
            f"This channel is now an agent session.\n"
            f"Workdir `{rec['workdir']}` · session `{rec['session_id'][:8]}`",
        )

        prime = (req.get("prime") or "").strip()
        if prime:
            # raw=True: the prime is this agent's charter, not something a
            # colleague said. Wrapping it in "[via Discord #x]\nsomeone: ..."
            # would make its founding instruction read as passing chatter.
            self.bot._queue_for(channel.id).put_nowait(
                {"author": "rig", "text": prime, "channel": channel, "raw": True}
            )
        log.info("control: created #%s (%s)", name, channel.id)
        return f"created #{name} and primed it" if prime else f"created #{name}"

    async def archive_channel(self, req: dict) -> str:
        """Stop serving a channel. The transcript and the Discord channel stay.

        Reversible on purpose -- a channel that has gone wrong should be
        stoppable without destroying what it knew.
        """
        name = _clean_channel_name(req.get("name"))
        target = self.spool._resolve(name)
        if target is None:
            raise ControlError(f"no channel #{name}")
        self.bot.state.update(target.id, archived=True)
        return f"#{name} archived — it will not run turns until un-archived"

    async def set_model(self, req: dict) -> str:
        name = _clean_channel_name(req.get("name"))
        alias = (req.get("model") or "").strip()
        if alias not in config.MODELS:
            raise ControlError(f"unknown model {alias!r} ({', '.join(config.MODELS)})")
        target = self.spool._resolve(name)
        if target is None:
            raise ControlError(f"no channel #{name}")
        self.bot.state.update(target.id, model=alias, model_resolved=None)
        return f"#{name} now uses {alias}"


_CHANNEL_OK = re.compile(r"[^a-z0-9\-]+")


def _clean_channel_name(raw) -> str:
    """Discord lowercases and hyphenates anyway; do it up front so the name we
    record in state matches the name Discord actually creates."""
    if not isinstance(raw, str):
        return ""
    return _CHANNEL_OK.sub("-", raw.strip().lower().lstrip("#")).strip("-")[:90]
