"""The Discord client: one channel, one session, one turn per message.

Two layers of concurrency control, doing different jobs:

  * a per-channel queue + worker, so a channel never runs two turns at once and
    messages that arrive mid-turn fold into the *next* turn rather than
    interrupting the current one;
  * a global semaphore, so the box never has more `claude` processes resident
    than its RAM can hold.
"""

from __future__ import annotations

import asyncio
import logging
import time

import discord

from . import commands, config, harness as harness_mod, spool as spool_mod, state as state_mod
from .ticker import Ticker

log = logging.getLogger("rig.bot")


class RigBot(discord.Client):
    def __init__(self, cfg: config.Config, st: state_mod.State, history=None) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # privileged; see docs/SETUP.md
        intents.guilds = True
        intents.messages = True
        super().__init__(intents=intents)

        self.cfg = cfg
        self.state = st
        self.history = history
        self.harness = harness_mod.ClaudeHarness(cfg, st)

        self.sem = asyncio.Semaphore(cfg.max_concurrent_turns)
        self.queues: dict[int, asyncio.Queue] = {}
        self.workers: dict[int, asyncio.Task] = {}
        self.running: dict[int, object] = {}   # channel_id -> live subprocess
        self.aborted: set[int] = set()
        # Channels whose worker has claimed work but may not have spawned yet.
        # Set synchronously at the top of the worker loop; this, not
        # active_turns, is what drain() must wait on.
        self.busy: set[int] = set()
        self.active_turns = 0
        self.draining = False
        self.started_at = time.time()
        self.spool = spool_mod.Spool(self)

    # --- lifecycle ---------------------------------------------------------

    async def setup_hook(self) -> None:
        """Runs exactly once, before the first connection.

        The spool loops belong here rather than in on_ready, which re-fires on
        every gateway reconnect and would stack a duplicate poller each time.
        """
        self.spool.start()

    async def on_ready(self) -> None:
        log.info("connected as %s", self.user)
        for guild in self.guilds:
            marker = "  <- serving" if guild.id == self.cfg.guild_id else ""
            log.info("  guild %s (%s)%s", guild.name, guild.id, marker)
        if not any(g.id == self.cfg.guild_id for g in self.guilds):
            log.warning(
                "not a member of DISCORD_GUILD_ID=%s -- every message will be ignored",
                self.cfg.guild_id,
            )
        log.info(
            "max_concurrent_turns=%d turn_timeout=%ds channels_known=%d",
            self.cfg.max_concurrent_turns,
            self.cfg.turn_timeout,
            len(self.state.all()),
        )

    async def drain(self, timeout: float = 300.0) -> None:
        """Stop accepting work, let in-flight turns finish, then close.

        A restart that kills live turns is how you lose a channel mid-thought.
        systemd gets `KillMode=mixed`; this is the same contract on macOS.
        """
        self.draining = True
        # Stop pulling new work off the spool. Anything already enqueued is
        # counted below and still gets answered.
        self.spool.stop()

        def outstanding() -> int:
            # Queued-but-unstarted counts too: a worker between turns has
            # active_turns == 0 while still holding messages nobody has answered.
            return len(self.busy) + sum(q.qsize() for q in self.queues.values())

        log.info("draining: %d turn(s) in flight, %d queued", self.active_turns, outstanding())
        deadline = time.monotonic() + timeout
        while outstanding() > 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.5)
        if outstanding():
            log.warning("drain timed out with %d turn(s) unfinished", outstanding())
        await self.close()

    # --- intake ------------------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.author.id == getattr(self.user, "id", None):
            return
        if message.guild is None or message.guild.id != self.cfg.guild_id:
            return

        # Guild membership is not authorization. With --dangerously-skip-permissions
        # in play, anyone who can post here can otherwise run commands as this user.
        if message.author.id not in self.cfg.allowed_user_ids:
            log.warning(
                "dropped message from unauthorized user %s (%s) in #%s",
                message.author, message.author.id, message.channel,
            )
            return

        name = getattr(message.channel, "name", "") or ""
        if name.lower() in self.cfg.ignore_channels:
            return

        content = (message.content or "").strip()
        for att in message.attachments:
            content = f"{content}\n{att.url}".strip()
        if not content:
            return

        if content.startswith("!"):
            await commands.handle(self, message, content)
            return

        if self.draining:
            await message.channel.send("⏸️ daemon is shutting down; message not queued.")
            return

        rec = self.state.register(message.channel.id, name)
        if self.history:
            self.history.log_inbound(message.channel.id, name, rec, message.author, content)

        self._queue_for(message.channel.id).put_nowait(
            {
                "author": message.author.display_name,
                "text": content,
                # Carry the live channel object. Resolving it later from the
                # guild cache returns None during a gateway reconnect, and the
                # coalesced messages would be dropped with no reply.
                "channel": message.channel,
            }
        )

    def _queue_for(self, channel_id: int) -> asyncio.Queue:
        q = self.queues.get(channel_id)
        if q is None:
            q = self.queues[channel_id] = asyncio.Queue()
        task = self.workers.get(channel_id)
        if task is None or task.done():
            self.workers[channel_id] = asyncio.create_task(self._worker(channel_id))
        return q

    # --- the turn loop -----------------------------------------------------

    async def _worker(self, channel_id: int) -> None:
        q = self.queues[channel_id]
        while True:
            items = [await q.get()]
            # Claim the work synchronously, before any await. drain() watches
            # this set: active_turns is only incremented several awaits deep in
            # _run_turn, so a shutdown landing in that window would see "0 turns
            # in flight" and close the client out from under a live message.
            self.busy.add(channel_id)
            # Coalesce everything queued since the last turn into ONE turn.
            # Three messages typed in two seconds cost one turn, not three.
            # A raw item (a slash command) must stay alone in its turn, so it
            # neither absorbs neighbours nor gets absorbed -- it is put back and
            # runs on the next pass instead of being dropped.
            deferred = None
            if not items[0].get("raw"):
                while True:
                    try:
                        nxt = q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if nxt.get("raw"):
                        deferred = nxt
                        break
                    items.append(nxt)
            try:
                await self._run_turn(channel_id, items)
            except Exception:
                log.exception("turn failed in channel %s", channel_id)
            finally:
                for _ in items:
                    q.task_done()
                if deferred is not None:
                    q.put_nowait(deferred)
                    q.task_done()
                self.busy.discard(channel_id)

    async def _run_turn(self, channel_id: int, items: list[dict]) -> None:
        channel = items[0].get("channel") or self.get_channel(channel_id)
        if channel is None:
            log.warning("channel %s vanished; dropping %d message(s)", channel_id, len(items))
            return

        name = getattr(channel, "name", str(channel_id))
        rec = self.state.register(channel_id, name)

        if items[0].get("raw"):
            # !compact and friends: the CLI only honours a slash command when it
            # IS the whole prompt, so this one must bypass the [via Discord]
            # wrapper and the coalescing that would bury it mid-text.
            prompt = items[0]["text"]
        else:
            lines = "\n".join(f"{i['author']}: {i['text']}" for i in items)
            prompt = f"[via Discord #{name}]\n{lines}"

        header = (
            f"**{rec['model']}** · {'resume' if rec.get('primed') else 'new session'}"
            + (f" · {len(items)} messages" if len(items) > 1 else "")
        )
        tick = Ticker(channel, header)
        await tick.start()

        async def on_event(kind: str, payload) -> None:
            if kind == "text":
                await tick.on_text(payload)
            elif kind == "tool":
                await tick.on_tool(payload)
            elif kind == "reset":
                tick.reset()

        self.aborted.discard(channel_id)
        self.active_turns += 1
        started = time.monotonic()
        try:
            async with self.sem:
                async with channel.typing():
                    result = await self.harness.run_turn(
                        channel_id=channel_id,
                        rec=rec,
                        prompt=prompt,
                        on_event=on_event,
                        register_proc=lambda p: self.running.update({channel_id: p}),
                    )
        finally:
            self.active_turns -= 1
            self.running.pop(channel_id, None)

        await tick.maybe_edit(force=True)
        await self._deliver(channel, channel_id, rec, tick, result, started)

    async def _deliver(self, channel, channel_id, rec, tick, result, started) -> None:
        elapsed = time.monotonic() - started

        if channel_id in self.aborted:
            self.aborted.discard(channel_id)
            await tick.finish(f"⛔ aborted after {elapsed:.0f}s", "")
            return

        self.state.bump_turn(channel_id)
        if self.history:
            # Log failures too. The whole point of this table is to survive what
            # the transcript loses, and a timed-out turn may have done real work
            # before it died.
            self.history.log_outbound(channel_id, rec, result)

        if result.is_error:
            detail = result.text or ""
            if result.stderr_tail and result.error_kind != "missing_transcript":
                tail = result.stderr_tail.strip()[-1200:]
                detail = f"{detail}\n\n```\n{tail}\n```" if detail else f"```\n{tail}\n```"
            icon = {"timeout": "⏱️", "missing_transcript": "🧠", "spawn": "💥"}.get(
                result.error_kind, "⚠️"
            )
            label = result.error_kind or "error"
            await tick.finish(
                f"{icon} turn failed ({label}, exit={result.exit_code}) · {elapsed:.0f}s",
                detail or "No output.",
            )
            return

        await tick.finish(self._footer(result, elapsed), result.text or "_(no text output)_")

        size, lines = await asyncio.to_thread(
            state_mod.transcript_stats, rec["workdir"], rec["session_id"]
        )
        log.info(
            "#%s turn %d ok in %.0fs · transcript %.1fMB / %d lines",
            rec["name"], rec.get("turns", 0), elapsed, size / 1048576, lines,
        )

        # Compaction is invisible otherwise: answers just quietly get worse.
        if harness_mod.detect_compaction(rec["workdir"], rec["session_id"]):
            if not rec.get("compaction_notified"):
                self.state.update(channel_id, compaction_notified=True)
                await channel.send(
                    "🗜️ this session compacted — earlier detail is summarized now. "
                    "`!ctx` shows transcript size."
                )
        elif rec.get("compaction_notified"):
            self.state.update(channel_id, compaction_notified=False)

    def _footer(self, result, elapsed: float) -> str:
        """Stats line under a successful answer. RIG_FOOTER=off|minimal|full.

        Empty string means the ticker is deleted outright, leaving just the
        reply -- which is what you want when you are texting, not monitoring.
        """
        if self.cfg.footer == "off":
            return ""
        if self.cfg.footer == "minimal":
            return f"✅ {elapsed:.0f}s"
        parts = [f"✅ {elapsed:.0f}s"]
        if result.output_tokens:
            parts.append(f"{result.output_tokens:,} out")
        if result.cost_usd:
            parts.append(f"${result.cost_usd:.3f}")
        if result.tools:
            parts.append(f"{len(result.tools)} tools")
        return " · ".join(parts)

    # --- abort -------------------------------------------------------------

    async def abort(self, channel_id: int) -> bool:
        proc = self.running.get(channel_id)
        if proc is None:
            return False
        self.aborted.add(channel_id)
        await self.harness.kill(proc)
        return True
