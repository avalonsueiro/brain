"""`!` commands. These never spawn a turn -- except !compact, which queues one."""

from __future__ import annotations

import asyncio
import logging
import time

from . import config, harness as harness_mod, state as state_mod

log = logging.getLogger("rig.commands")

HELP = """```
!ping             daemon uptime and load
!ctx              this channel's session, model, transcript size
!model [alias]    show or switch model (opus, fable, sonnet, haiku)
!reset            new session id -- fresh context, same workdir
!compact          compact this session's context
!abort            kill the turn running in this channel
```"""


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

    await channel.send(f"unknown command `!{cmd}`\n{HELP}")
