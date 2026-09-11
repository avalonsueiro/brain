"""Spawning `claude` and parsing its stream-json output.

Nothing else in the rig knows what the CLI is called or which flags it takes.

The shape of a turn: a process spawns, runs, streams events, and exits. Sessions
are not resident. That is what makes the whole thing reboot-proof and cheap in
RAM -- and it is why a channel idle for a month costs nothing at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config, state as state_mod

log = logging.getLogger("rig.harness")

# These three classifiers are matched against STDERR ONLY, never the model's own
# prose. A turn that merely discusses sessions or models would otherwise trip
# them: a false _SESSION_EXISTS marks a channel primed with no transcript behind
# it, which bricks it on the missing-transcript gate until someone runs !reset.

# `--session-id` refuses a uuid whose transcript already exists. That happens
# whenever a first turn dies after init, so we translate it into a resume.
# The gap has to clear a uuid (36 chars) plus filler, so keep it generous but
# line-bounded -- a 40-char window silently missed the real message,
# "session <uuid> is already in use".
_SESSION_EXISTS = re.compile(
    r"session\b[^\n]{0,120}?(already in use|already exists|is in use|duplicate session)"
    r"|(already in use|already exists)[^\n]{0,120}?\bsession\b",
    re.I,
)

# A model alias whose [1m] variant does not exist falls back to the bare id.
_MODEL_ERROR = re.compile(
    r"(unknown|invalid|not found|does not exist|unsupported).{0,60}model"
    r"|model.{0,60}(unknown|invalid|not found|does not exist|unsupported)",
    re.I,
)

# A resume that cannot find its transcript. Must never look like a fresh start.
_NO_CONVERSATION = re.compile(
    r"no conversation found|could not find.{0,30}session|session.{0,30}not found", re.I
)

_STDERR_KEEP = 4000

# stream-json emits one JSON object per line, and a single line carries a whole
# content block -- a long answer or a Write tool input is routinely megabytes.
# asyncio's default StreamReader limit is 64KB, and overrunning it drops the
# line: a >64KB `result` event would vanish and the turn would be delivered as
# a successful "(no text output)".
_STREAM_LIMIT = 32 * 1024 * 1024

_STDIN_TIMEOUT = 30


@dataclass
class TurnResult:
    text: str = ""
    is_error: bool = False
    error_kind: str | None = None  # timeout | missing_transcript | spawn | cli | truncated
    exit_code: int | None = None
    stderr_tail: str = ""
    duration_ms: int = 0
    cost_usd: float | None = None
    output_tokens: int | None = None
    model_used: str | None = None
    resumed: bool = False
    dropped_events: int = 0
    tools: list[str] = field(default_factory=list)


class ClaudeHarness:
    def __init__(self, cfg: config.Config, st: state_mod.State) -> None:
        self.cfg = cfg
        self.state = st

    # --- argv --------------------------------------------------------------

    def _argv(self, rec: dict, model: str, resume: bool) -> list[str]:
        session_flag = ["--resume", rec["session_id"]] if resume else [
            "--session-id",
            rec["session_id"],
        ]
        return [
            self.cfg.claude_bin,
            "-p",
            *session_flag,
            "--model",
            model,
            # A daemon cannot answer permission prompts, and a turn that stalls
            # on one is a dead channel. Containment therefore comes from the
            # Discord allowlist (and, on Linux, an optional dedicated user plus
            # the systemd sandboxing directives) -- not from the CLI.
            "--dangerously-skip-permissions",
            "--output-format",
            "stream-json",
            "--verbose",
        ]

    def _child_env(self) -> dict[str, str]:
        env = dict(os.environ)
        # The box clock is UTC on Linux. Handing the child a real timezone kills
        # an entire class of "what day is it" bugs at the source.
        env["TZ"] = self.cfg.tz
        env["RIG_TZ"] = self.cfg.tz
        return env

    # --- one turn ----------------------------------------------------------

    async def run_turn(
        self,
        *,
        channel_id: int,
        rec: dict,
        prompt: str,
        on_event,
        register_proc=None,
    ) -> TurnResult:
        workdir = rec["workdir"]

        # A resume whose transcript has vanished must be loud. Silently starting
        # a fresh session here is indistinguishable from amnesia, and you would
        # not find out until the agent forgot something it should have known.
        if rec.get("primed"):
            tpath = state_mod.transcript_path(workdir, rec["session_id"])
            if not tpath.exists():
                return TurnResult(
                    is_error=True,
                    error_kind="missing_transcript",
                    text=(
                        f"Session transcript is missing:\n`{tpath}`\n\n"
                        "This channel's memory cannot be resumed. Restore it from "
                        "`rig backup`, or run `!reset` to start a fresh session "
                        "(the old context is not recoverable that way)."
                    ),
                )

        candidates = self._model_order(rec)
        resume = bool(rec.get("primed"))
        result = TurnResult()

        for attempt in range(3):
            if attempt:
                await on_event("reset", None)

            result = await self._attempt(
                channel_id=channel_id,
                rec=rec,
                model=candidates[0],
                resume=resume,
                prompt=prompt,
                on_event=on_event,
                register_proc=register_proc,
            )

            # Classified from stderr only -- see the comment on the regexes.
            diagnostics = result.stderr_tail

            # First turn died after init on a previous run: resume instead.
            if result.is_error and not resume and _SESSION_EXISTS.search(diagnostics):
                self.state.mark_primed(channel_id, rec["session_id"])
                resume = True
                continue

            # The [1m] variant of this alias does not exist. Fall back.
            if result.is_error and len(candidates) > 1 and _MODEL_ERROR.search(diagnostics):
                candidates = candidates[1:]
                continue

            if result.is_error and resume and _NO_CONVERSATION.search(diagnostics):
                result.error_kind = "missing_transcript"

            break

        if not result.is_error and result.model_used:
            # Remember which candidate actually worked so untested aliases cost
            # one failed spawn, once, ever.
            if rec.get("model_resolved") != result.model_used:
                self.state.update(channel_id, model_resolved=result.model_used)

        return result

    def _model_order(self, rec: dict) -> list[str]:
        alias = rec.get("model") or self.cfg.default_model
        try:
            candidates = config.model_candidates(alias)
        except KeyError:
            candidates = config.model_candidates(self.cfg.default_model)
        cached = rec.get("model_resolved")
        if cached in candidates:
            candidates = [cached] + [c for c in candidates if c != cached]
        return candidates

    async def _attempt(
        self,
        *,
        channel_id: int,
        rec: dict,
        model: str,
        resume: bool,
        prompt: str,
        on_event,
        register_proc,
    ) -> TurnResult:
        argv = self._argv(rec, model, resume)
        started = time.monotonic()
        result = TurnResult(model_used=model, resumed=resume)

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=rec["workdir"],
                env=self._child_env(),
                limit=_STREAM_LIMIT,
                # Its own process group, so a timeout kills the whole tree
                # instead of orphaning subagents. POSIX; identical on Linux.
                start_new_session=True,
            )
        except OSError as exc:
            result.is_error = True
            result.error_kind = "spawn"
            result.stderr_tail = f"could not spawn {self.cfg.claude_bin!r}: {exc}"
            return result

        if register_proc:
            register_proc(proc)

        stderr_buf: list[str] = []
        stdout_task = asyncio.create_task(
            self._read_stdout(proc, channel_id, rec, result, on_event)
        )
        stderr_task = asyncio.create_task(self._read_stderr(proc, stderr_buf))

        await self._write_prompt(proc, prompt)

        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=self.cfg.turn_timeout)
        except asyncio.TimeoutError:
            timed_out = True
            await self.kill(proc)
        except asyncio.CancelledError:
            await self.kill(proc)
            stdout_task.cancel()
            stderr_task.cancel()
            raise

        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

        result.exit_code = proc.returncode
        result.duration_ms = int((time.monotonic() - started) * 1000)
        result.stderr_tail = "".join(stderr_buf)[-_STDERR_KEEP:]

        if timed_out:
            result.is_error = True
            result.error_kind = "timeout"
            result.text = (
                f"Turn exceeded TURN_TIMEOUT ({self.cfg.turn_timeout}s) and was killed."
            )
        elif proc.returncode:
            # Any nonzero exit is a failure, even when text was streamed first.
            # Delivering a half-written answer with a success footer is worse
            # than saying plainly that the turn died partway through.
            result.is_error = True
            result.error_kind = result.error_kind or "cli"
        elif result.dropped_events and not result.text:
            result.is_error = True
            result.error_kind = "truncated"
            result.text = (
                f"The CLI emitted {result.dropped_events} event(s) too large to read "
                "and no final text survived. This is a bug in the rig, not your prompt."
            )

        return result

    async def _write_prompt(self, proc, prompt: str) -> None:
        """Feed the prompt on stdin, bounded.

        A child wedged before it reads stdin would otherwise block `drain()`
        forever on a prompt larger than the pipe buffer -- holding a semaphore
        slot with nothing to time it out, since TURN_TIMEOUT only wraps wait().
        """
        if proc.stdin is None:
            return
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            await asyncio.wait_for(proc.stdin.drain(), timeout=_STDIN_TIMEOUT)
        except (BrokenPipeError, ConnectionResetError):
            pass  # child exited early; the exit code will tell the real story
        except asyncio.TimeoutError:
            log.warning("timed out writing prompt to stdin; killing child")
            await self.kill(proc)
        finally:
            # Always close: without EOF a live child waits for more input until
            # the full turn timeout expires.
            try:
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError, RuntimeError):
                pass

    # --- streams -----------------------------------------------------------

    async def _read_stdout(self, proc, channel_id: int, rec: dict, result, on_event) -> None:
        assert proc.stdout is not None
        text_parts: list[str] = []
        while True:
            try:
                raw = await proc.stdout.readline()
            except (asyncio.LimitOverrunError, ValueError):
                # Past _STREAM_LIMIT. Count it: a silently dropped event used to
                # surface as a successful turn with no answer.
                result.dropped_events += 1
                log.warning("dropped a stream-json line over %d bytes", _STREAM_LIMIT)
                continue
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            kind = event.get("type")

            if kind == "system" and event.get("subtype") == "init":
                # By init the transcript exists on disk. Commit primed NOW: if
                # we waited for the result and the turn then died, the next turn
                # would retry --session-id against a live uuid, forever.
                self.state.mark_primed(channel_id, rec["session_id"])
                await on_event("init", event)

            elif kind == "assistant":
                for block in (event.get("message") or {}).get("content") or []:
                    btype = block.get("type")
                    if btype == "text":
                        chunk = block.get("text") or ""
                        if chunk:
                            text_parts.append(chunk)
                            await on_event("text", chunk)
                    elif btype == "tool_use":
                        label = _tool_label(block)
                        result.tools.append(label)
                        await on_event("tool", label)

            elif kind == "result":
                result.text = event.get("result") or "".join(text_parts)
                result.is_error = (
                    bool(event.get("is_error")) or event.get("subtype") != "success"
                )
                result.cost_usd = event.get("total_cost_usd")
                result.output_tokens = (event.get("usage") or {}).get("output_tokens")
                await on_event("result", event)

        if not result.text and text_parts:
            result.text = "".join(text_parts)

    @staticmethod
    async def _read_stderr(proc, buf: list[str]) -> None:
        assert proc.stderr is not None
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            buf.append(chunk.decode("utf-8", "replace"))
            if sum(len(c) for c in buf) > _STDERR_KEEP * 4:
                joined = "".join(buf)[-_STDERR_KEEP:]
                buf.clear()
                buf.append(joined)

    # --- killing -----------------------------------------------------------

    @staticmethod
    async def kill(proc) -> None:
        """SIGTERM the group, give it 5s, then SIGKILL. Never leave orphans."""
        if proc.returncode is not None:
            return
        for sig, wait in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 2.0)):
            try:
                os.killpg(os.getpgid(proc.pid), sig)
            except (ProcessLookupError, PermissionError):
                return
            try:
                await asyncio.wait_for(proc.wait(), timeout=wait)
                return
            except asyncio.TimeoutError:
                continue


def _tool_label(block: dict) -> str:
    name = block.get("name") or "tool"
    inp = block.get("input") or {}
    hint = ""
    for key in ("command", "file_path", "path", "pattern", "query", "url"):
        val = inp.get(key)
        if isinstance(val, str) and val.strip():
            hint = val.strip().splitlines()[0][:60]
            break
    return f"{name}({hint})" if hint else name


def detect_compaction(workdir: str | Path, session_id: str) -> bool:
    """Did the last turn end in a compaction?

    Read backwards over the tail only. These transcripts reach hundreds of MB
    and the standing rule against slurping large files applies to us too.

    Matched as JSON keys, not bare substrings: a conversation *about* compaction
    (or about this very file) would otherwise announce a compaction that never
    happened.
    """
    path = state_mod.transcript_path(workdir, session_id)
    if not path.exists():
        return False
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 65536))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return False
    return ('"isCompactSummary":true' in tail.replace(" ", "")) or (
        '"subtype":"compact_boundary"' in tail.replace(" ", "")
    )
