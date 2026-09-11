"""One live-editing Discord message per turn.

Discord rate-limits message edits, and an agentic turn emits events far faster
than you are allowed to write them. So the ticker coalesces: it accumulates
state continuously and repaints at most once every EDIT_INTERVAL seconds.
"""

from __future__ import annotations

import asyncio
import logging
import time

import discord

log = logging.getLogger("rig.ticker")

SAFE_LIMIT = 1900
EDIT_INTERVAL = 1.5
MAX_TOOL_LINES = 8
TEXT_TAIL = 900

# _repair_fences prepends a reopening fence and appends a closing one, so the
# splitter has to leave room or the repaired chunk blows past Discord's hard
# 2000-char limit and the send fails -- silently losing that piece of the answer.
# A fence info string is arbitrary model output, so the headroom is generous.
_FENCE_HEADROOM = 120


def split_message(text: str, limit: int = SAFE_LIMIT) -> list[str]:
    """Split for Discord on paragraph, then line, then character boundaries.

    Code fences are reopened across the seam so a split never leaves a dangling
    ``` that swallows the rest of the conversation in monospace.
    """
    text = text.rstrip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    limit -= _FENCE_HEADROOM

    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current.rstrip())
        current = ""

    def add(piece: str) -> None:
        nonlocal current
        if len(piece) > limit:
            flush()
            for i in range(0, len(piece), limit):
                chunks.append(piece[i : i + limit])
            return
        if len(current) + len(piece) + 2 > limit:
            flush()
        current = f"{current}\n\n{piece}" if current else piece

    for para in text.split("\n\n"):
        if len(para) <= limit:
            add(para)
            continue
        line_buf = ""
        for line in para.split("\n"):
            if len(line_buf) + len(line) + 1 > limit:
                add(line_buf)
                line_buf = line
            else:
                line_buf = f"{line_buf}\n{line}" if line_buf else line
        add(line_buf)
    flush()

    return _repair_fences(chunks)


def _repair_fences(chunks: list[str]) -> list[str]:
    out: list[str] = []
    carry = ""  # the fence language to reopen, e.g. "```python"
    for chunk in chunks:
        body = f"{carry}\n{chunk}" if carry else chunk
        fences = [ln for ln in body.split("\n") if ln.lstrip().startswith("```")]
        if len(fences) % 2 == 1:
            # The info string is arbitrary model output; cap it so the reopened
            # fence cannot itself push the next chunk over the limit.
            carry = fences[-1].strip()[:_FENCE_HEADROOM - 10]
            body = f"{body}\n```"
        else:
            carry = ""
        out.append(body)
    return out


class Ticker:
    def __init__(self, channel: discord.abc.Messageable, header: str) -> None:
        self.channel = channel
        self.header = header
        self.message: discord.Message | None = None
        self.tools: list[str] = []
        self.text = ""
        self._last_edit = 0.0
        self._dirty = False
        self._lock = asyncio.Lock()

    async def _try(self, coro, what: str) -> bool:
        """Discord calls are best-effort -- except the answer itself, which is
        why every failure is logged rather than silently passed over."""
        try:
            await coro
            return True
        except discord.HTTPException as exc:
            log.warning("discord %s failed: %s", what, exc)
            return False

    async def start(self) -> None:
        try:
            self.message = await self.channel.send(f"{self.header}\n⏳ thinking…")
        except discord.HTTPException as exc:
            log.warning("could not post ticker: %s", exc)
            self.message = None

    def reset(self) -> None:
        """A retry is happening; discard the failed attempt's output."""
        self.tools.clear()
        self.text = ""
        self._dirty = True

    async def on_tool(self, label: str) -> None:
        self.tools.append(label)
        self._dirty = True
        await self.maybe_edit()

    async def on_text(self, chunk: str) -> None:
        self.text += chunk
        self._dirty = True
        await self.maybe_edit()

    def _render(self) -> str:
        parts = [self.header]
        if self.tools:
            shown = self.tools[-MAX_TOOL_LINES:]
            hidden = len(self.tools) - len(shown)
            if hidden > 0:
                parts.append(f"…{hidden} earlier tool call{'s' if hidden != 1 else ''}")
            parts += [f"🔧 `{t}`" for t in shown]
        tail = self.text.strip()
        if tail:
            if len(tail) > TEXT_TAIL:
                tail = "…" + tail[-TEXT_TAIL:]
            parts.append(tail)
        if not self.tools and not tail:
            parts.append("⏳ thinking…")
        body = "\n".join(parts)
        return body[:SAFE_LIMIT] if len(body) > SAFE_LIMIT else body

    async def maybe_edit(self, force: bool = False) -> None:
        if self.message is None or not self._dirty:
            return
        now = time.monotonic()
        if not force and now - self._last_edit < EDIT_INTERVAL:
            return
        async with self._lock:
            self._last_edit = now
            self._dirty = False
            await self._try(self.message.edit(content=self._render()), "ticker edit")

    async def finish(self, footer: str, body: str) -> None:
        """Replace the ticker with a one-line footer, then post the answer.

        An empty footer deletes the ticker instead, leaving only the answer. The
        rig is a chat surface first, and a stats line under every reply turns a
        conversation into a dashboard.
        """
        if self.message is not None:
            await self._try(
                self.message.delete() if not footer else self.message.edit(content=footer),
                "ticker finish",
            )
        elif footer:
            await self._try(self.channel.send(footer), "footer")

        for i, chunk in enumerate(split_message(body)):
            if not await self._try(self.channel.send(chunk), f"answer chunk {i}"):
                # One retry: a transient 5xx here costs the user a piece of the
                # answer with no trace of what went missing.
                await asyncio.sleep(1.0)
                await self._try(self.channel.send(chunk), f"answer chunk {i} (retry)")
