#!/usr/bin/env python3
"""Book a future turn in a Discord channel.

A session is a uuid plus a transcript, not a running process -- so an agent
cannot "wait" for anything. Instead it books a wake and exits. The daemon fires
the job later and the agent resumes with full context.

This is both the CLI agents shell out to and the library the daemon imports, so
`!wake` in Discord and `wake.py add` in a terminal cannot drift apart.

    wake.py add --in 20m  --channel general --prompt "check if the build passed"
    wake.py add --at 18:30 --channel general --prompt "stand up"
    wake.py list
    wake.py cancel w7k2

Stdlib only. Safe to run from anywhere -- it talks to the filesystem, not the
daemon.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import random
import re
import string
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

RIG_ROOT = Path(os.environ.get("RIG_ROOT", "/opt/agent-rig"))
JOBS_FILE = RIG_ROOT / "state" / "jobs.json"
INJECT_DIR = RIG_ROOT / "inject"
TZ = os.environ.get("RIG_TZ", "America/New_York")

_DURATION = re.compile(r"(\d+)\s*([smhdw])", re.I)
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


# --- parsing ---------------------------------------------------------------


def parse_duration(text: str) -> int:
    """'90s' '20m' '2h' '3d' '1h30m' -> seconds. Raises ValueError otherwise.

    Deliberately strict: every character must be consumed. Silently reading
    '2huor' as two hours is how a typo becomes a wake that never fires when you
    expect it to.
    """
    # Whitespace is stripped everywhere first, so the consumed-length check
    # below compares like with like ("2 h" and "2h" are the same input).
    s = re.sub(r"\s+", "", (text or "").lower())
    if not s:
        raise ValueError("empty duration")
    total, consumed = 0, 0
    for match in _DURATION.finditer(s):
        total += int(match.group(1)) * _UNITS[match.group(2)]
        consumed += len(match.group(0))
    if not total or consumed != len(s):
        raise ValueError(f"bad duration {text!r} — try 45s, 20m, 2h, 3d, 1h30m")
    return total


def parse_at(text: str, now: datetime | None = None) -> int:
    """'18:30' or '2026-09-20 18:30' in RIG_TZ -> epoch seconds.

    A bare HH:MM already past today means tomorrow -- asking for 08:00 at 9am
    always means tomorrow morning, never nine hours ago.
    """
    zone = ZoneInfo(TZ)
    now = now.astimezone(zone) if now else datetime.now(zone)
    s = (text or "").strip()

    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return int(datetime.strptime(s, fmt).replace(tzinfo=zone).timestamp())
        except ValueError:
            pass

    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M%p", "%I%p"):
        try:
            parsed = datetime.strptime(s.upper().replace(" ", ""), fmt)
        except ValueError:
            continue
        when = now.replace(
            hour=parsed.hour, minute=parsed.minute, second=parsed.second, microsecond=0
        )
        if when <= now:
            when += timedelta(days=1)
        return int(when.timestamp())

    raise ValueError(f"bad time {text!r} — try 18:30, 6:30pm, or 2026-09-20 18:30")


def local(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, ZoneInfo(TZ)).strftime("%a %b %d %H:%M %Z")


def human_delta(seconds: float) -> str:
    """Rounded, not truncated: a 2h wake booked a millisecond ago is 7199.9s,
    and flooring that to '1h' makes the confirmation look wrong."""
    seconds = abs(seconds)
    for div, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= div * 0.95:
            return f"{round(seconds / div)}{unit}"
    return f"{round(seconds)}s"


# --- jobs.json -------------------------------------------------------------
#
# The PRD keeps wake jobs in one jobs.json, and both the daemon and this CLI
# write it. Two processes doing read-modify-write on the same file drop jobs, so
# every mutation happens under an flock. POSIX; identical on macOS and Linux.


class _Locked:
    """Exclusive lock on jobs.json, held across a read-modify-write."""

    def __init__(self, path: Path = JOBS_FILE) -> None:
        self.path = path
        self.lock_path = path.with_suffix(".lock")

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.lock_path, "w")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()
        return False

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        return data if isinstance(data, list) else []

    def write(self, jobs: list[dict]) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(jobs, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


def _new_id(existing: set[str]) -> str:
    alphabet = string.ascii_lowercase + string.digits
    while True:
        candidate = "w" + "".join(random.choice(alphabet) for _ in range(3))
        if candidate not in existing:
            return candidate


def add(channel: str, prompt: str, at: int, label: str = "wake") -> dict:
    job = {"id": "", "at": int(at), "channel": channel, "prompt": prompt, "label": label}
    with _Locked() as jobs_file:
        jobs = jobs_file.read()
        job["id"] = _new_id({j.get("id") for j in jobs})
        jobs.append(job)
        jobs.sort(key=lambda j: j.get("at", 0))
        jobs_file.write(jobs)
    return job


def listing(channel: str | None = None) -> list[dict]:
    with _Locked() as jobs_file:
        jobs = jobs_file.read()
    if channel:
        jobs = [j for j in jobs if j.get("channel") == channel]
    return sorted(jobs, key=lambda j: j.get("at", 0))


def cancel(job_id: str) -> dict | None:
    with _Locked() as jobs_file:
        jobs = jobs_file.read()
        keep = [j for j in jobs if j.get("id") != job_id]
        if len(keep) == len(jobs):
            return None
        removed = next(j for j in jobs if j.get("id") == job_id)
        jobs_file.write(keep)
    return removed


def take_due(now: int | None = None) -> list[dict]:
    """Remove and return every job that is due. Called only by the daemon.

    Removal happens before the inject files are written, so a crash in that gap
    loses a wake rather than firing it twice. Deliberate: a double-fired
    self-continuation can take a real action twice.
    """
    now = int(now if now is not None else time.time())
    with _Locked() as jobs_file:
        jobs = jobs_file.read()
        due = [j for j in jobs if int(j.get("at", 0)) <= now]
        if due:
            jobs_file.write([j for j in jobs if int(j.get("at", 0)) > now])
    return due


# --- CLI -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wake", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="book a future turn")
    when = p_add.add_mutually_exclusive_group(required=True)
    when.add_argument("--in", dest="in_", metavar="DUR", help="45s, 20m, 2h, 1h30m")
    when.add_argument("--at", metavar="TIME", help="18:30, 6:30pm, 2026-09-20 18:30")
    p_add.add_argument("--channel", required=True)
    p_add.add_argument("--prompt", required=True)
    p_add.add_argument("--label", default="wake", help="shown to the agent as the speaker")

    p_list = sub.add_parser("list", help="show pending jobs")
    p_list.add_argument("--channel")

    p_cancel = sub.add_parser("cancel", help="cancel a job by id")
    p_cancel.add_argument("id")

    args = parser.parse_args(argv)

    if args.cmd == "add":
        try:
            at = int(time.time()) + parse_duration(args.in_) if args.in_ else parse_at(args.at)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        job = add(args.channel, args.prompt, at, args.label)
        print(f"{job['id']}  {local(at)}  (in {human_delta(at - time.time())})  #{args.channel}")
        return 0

    if args.cmd == "list":
        jobs = listing(args.channel)
        if not jobs:
            print("no pending wake jobs")
            return 0
        now = time.time()
        for job in jobs:
            delta = job["at"] - now
            when = f"in {human_delta(delta)}" if delta >= 0 else f"{human_delta(delta)} LATE"
            print(f"{job['id']}  {local(job['at'])}  ({when})  #{job['channel']}  {job['prompt'][:60]}")
        return 0

    if args.cmd == "cancel":
        removed = cancel(args.id)
        if removed is None:
            print(f"no job with id {args.id}", file=sys.stderr)
            return 1
        print(f"cancelled {args.id} ({local(removed['at'])})")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
