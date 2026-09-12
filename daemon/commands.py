"""`!` commands. These never spawn a turn -- except !compact, which queues one."""

from __future__ import annotations

import asyncio
import logging
import time

from . import config, harness as harness_mod, state as state_mod

log = logging.getLogger("rig.commands")

HELP = """```
!ping                    daemon uptime and load
!ctx                     this channel's session, model, transcript size
!model [alias]           show or switch model (opus, fable, sonnet, haiku)
!reset                   new session id -- fresh context, same workdir
!compact                 compact this session's context
!abort                   kill the turn running in this channel
!wake in 2h <text>       book a future turn in this channel
!wake at 18:30 <text>    same, at a clock time
!wake list | cancel <id>
```"""

WAKE_HELP = (
    "usage:\n"
    "```\n"
    "!wake in 2h check the deploy\n"
    "!wake at 18:30 stand up\n"
    "!wake list\n"
    "!wake cancel w7k2\n"
    "```"
)


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def _age(ts: float | None) -> str:
    if not ts:
        return "never"
    secs = int(time.time() - ts)
    for div, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if secs >= div:
            return f"{secs // div}{unit} ago"
    return f"{secs}s ago"


async def handle(bot, message, content: str) -> None:
    parts = content[1:].split()
    if not parts:
        return
    cmd, args = parts[0].lower(), parts[1:]
    channel = message.channel
    channel_id = channel.id
    name = getattr(channel, "name", str(channel_id))

    if cmd in ("help", "commands"):
        await channel.send(HELP)
        return

    if cmd == "ping":
        up = int(time.time() - bot.started_at)
        h, rem = divmod(up, 3600)
        m, s = divmod(rem, 60)
        queued = sum(q.qsize() for q in bot.queues.values())
        await channel.send(
            f"🏓 up {h}h{m:02d}m{s:02d}s · {bot.active_turns}/"
            f"{bot.cfg.max_concurrent_turns} turns active · {queued} queued"
            + (" · **draining**" if bot.draining else "")
        )
        return

    if cmd == "abort":
        killed = await bot.abort(channel_id)
        await channel.send("⛔ killed the running turn." if killed else "nothing running here.")
        return

    rec = bot.state.register(channel_id, name)

    if cmd == "ctx":
        # Off the event loop: this walks a transcript that reaches hundreds of
        # MB, and blocking here freezes every other channel and the gateway
        # heartbeat with it.
        size, lines = await asyncio.to_thread(
            state_mod.transcript_stats, rec["workdir"], rec["session_id"]
        )
        tpath = state_mod.transcript_path(rec["workdir"], rec["session_id"])
        compacted = harness_mod.detect_compaction(rec["workdir"], rec["session_id"])
        try:
            # An alias retired from MODELS must not crash the one command you
            # would reach for to find out why a channel is misbehaving.
            resolved = rec.get("model_resolved") or config.model_candidates(rec["model"])[0]
            if not rec.get("model_resolved"):
                resolved += "  (unresolved — will confirm on next turn)"
        except KeyError:
            resolved = f"UNKNOWN ALIAS {rec['model']!r}"
        await channel.send(
            "```\n"
            f"channel     #{rec['name']}\n"
            f"session     {rec['session_id']}\n"
            f"primed      {rec.get('primed')}\n"
            f"model       {rec['model']}  ->  {resolved}\n"
            f"harness     {rec.get('harness', 'cc')}\n"
            f"workdir     {rec['workdir']}\n"
            f"turns       {rec.get('turns', 0)}   last: {_age(rec.get('last_turn'))}\n"
            f"transcript  {_human(size)}, {lines:,} lines"
            f"{'  (COMPACTED)' if compacted else ''}\n"
            f"            {'exists' if tpath.exists() else 'MISSING'}  {tpath}\n"
            f"queued      {bot.queues[channel_id].qsize() if channel_id in bot.queues else 0}\n"
            "```"
        )
        return

    if cmd == "model":
        if not args:
            await channel.send(
                f"model: **{rec['model']}** (`{rec.get('model_resolved') or 'unresolved'}`)\n"
                f"available: {', '.join(config.MODELS)}"
            )
            return
        alias = args[0].lower()
        if alias not in config.MODELS:
            await channel.send(f"unknown alias `{alias}` — try: {', '.join(config.MODELS)}")
            return
        # Drop the cached resolution: the new alias may or may not have a [1m]
        # variant, and the harness discovers which on the next turn.
        bot.state.update(channel_id, model=alias, model_resolved=None)
        cands = config.model_candidates(alias)
        note = "" if len(cands) == 1 else f" (will try `{cands[0]}`, falling back to `{cands[1]}`)"
        await channel.send(f"model set to **{alias}**{note}")
        return

    if cmd == "reset":
        old = rec["session_id"]
        new = bot.state.reset_session(channel_id)
        bot.state.update(channel_id, compaction_notified=False)
        await channel.send(
            f"🧹 new session `{new['session_id']}`\n"
            f"old `{old}` is still on disk; `rig backup` keeps a copy."
        )
        return

    if cmd == "compact":
        if bot.draining:
            await channel.send("⏸️ daemon is shutting down.")
            return
        # raw=True: the CLI only treats /compact as a command when it IS the
        # entire prompt. Sent through the normal path it arrives as
        # "[via Discord #x]\navalon: /compact" -- ordinary conversation, and the
        # channel gets told "compaction queued" for a turn that compacts nothing.
        bot._queue_for(channel_id).put_nowait(
            {
                "author": message.author.display_name,
                "text": "/compact",
                "channel": channel,
                "raw": True,
            }
        )
        await channel.send("🗜️ compaction queued.")
        return

    if cmd == "wake":
        await _wake(bot, channel, rec, args)
        return

    await channel.send(f"unknown command `!{cmd}`\n{HELP}")


async def _wake(bot, channel, rec, args: list[str]) -> None:
    """`!wake` — the same parsing and the same jobs.json the CLI uses.

    Imported from skills/wake rather than reimplemented, so a fix to duration
    parsing lands in both places at once.
    """
    from .spool import wake_mod

    if not args:
        await channel.send(WAKE_HELP)
        return

    sub = args[0].lower()

    if sub == "list":
        jobs = wake_mod.listing(rec["name"])
        if not jobs:
            await channel.send("no wake jobs pending for this channel.")
            return
        now = time.time()
        lines = []
        for job in jobs:
            delta = job["at"] - now
            when = (
                f"in {wake_mod.human_delta(delta)}"
                if delta >= 0
                else f"{wake_mod.human_delta(delta)} LATE"
            )
            lines.append(
                f"{job['id']}  {wake_mod.local(job['at'])}  ({when})  {job['prompt'][:70]}"
            )
        await channel.send("```\n" + "\n".join(lines) + "\n```")
        return

    if sub == "cancel":
        if len(args) < 2:
            await channel.send("which one? `!wake cancel <id>` — `!wake list` shows ids.")
            return
        removed = wake_mod.cancel(args[1])
        if removed is None:
            await channel.send(f"no wake job `{args[1]}`.")
        else:
            await channel.send(f"🗑️ cancelled `{args[1]}` ({wake_mod.local(removed['at'])})")
        return

    if sub not in ("in", "at") or len(args) < 3:
        await channel.send(WAKE_HELP)
        return

    when_arg, prompt = args[1], " ".join(args[2:]).strip()
    try:
        at = (
            int(time.time()) + wake_mod.parse_duration(when_arg)
            if sub == "in"
            else wake_mod.parse_at(when_arg)
        )
    except ValueError as exc:
        await channel.send(f"⚠️ {exc}")
        return

    job = wake_mod.add(rec["name"], prompt, at, label="wake")
    await channel.send(
        f"⏰ `{job['id']}` — {wake_mod.local(at)} "
        f"(in {wake_mod.human_delta(at - time.time())})"
    )
