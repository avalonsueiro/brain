#!/usr/bin/env python3
"""Wake an agent by dropping a file in the inject spool.

This is the seam between the rig and everything else on the box. Cron, a git
hook, a CI callback, a shell one-liner — anything that can write a file can
start a turn in a Discord channel.

    inject.py --channel general --text "morning brief: what's on today?" --label cron
    inject.py --channel general --text "$(git log -1 --oneline)" --label git

Stdlib only, no daemon contact: this writes a file and exits. If the daemon is
down the file waits in the spool and is picked up when it comes back.

Delivery is confirmed by the file disappearing. If the channel cannot be
resolved the daemon renames it to `.failed` and leaves it there as evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path

DEFAULT_RIG_ROOT = "/opt/agent-rig"


# Resolved per call, never snapshotted at import: the daemon loads this module
# before config.load_env() runs, and a frozen path would silently ignore
# agent.env -- and make the test sandbox an accident of import ordering.
def rig_root() -> Path:
    return Path(os.environ.get("RIG_ROOT", DEFAULT_RIG_ROOT))


def spool_dir() -> Path:
    return rig_root() / "inject"


def inject(channel: str, text: str, label: str = "inject",
           inject_dir: Path | None = None) -> Path:
    """Publish an inject file. THE single implementation of the spool format.

    The daemon imports this rather than keeping its own copy: collab would have
    made a third, and a format that three files agree on only by coincidence
    drifts the first time one of them changes.
    """
    inject_dir = inject_dir or spool_dir()
    inject_dir.mkdir(parents=True, exist_ok=True)
    # Millisecond timestamp for FIFO ordering, pid+random so two writers in the
    # same millisecond cannot clobber each other.
    name = f"{int(time.time() * 1000)}-{os.getpid()}-{random.randint(1000, 9999)}"
    tmp = inject_dir / f"{name}.json.tmp"
    final = inject_dir / f"{name}.json"
    # Write .tmp then rename: the poller globs *.json, so it can never read a
    # half-written file.
    tmp.write_text(
        json.dumps({"channel": channel, "text": text, "label": label}),
        encoding="utf-8",
    )
    os.replace(tmp, final)
    return final


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="inject", description=__doc__.split("\n")[0])
    parser.add_argument("--channel", required=True, help="channel name or id, no '#'")
    parser.add_argument("--text", help="the message; omit to read stdin")
    parser.add_argument(
        "--label",
        default="inject",
        help="who the agent sees as the speaker — use it to say where this came from",
    )
    args = parser.parse_args(argv)

    text = args.text if args.text is not None else sys.stdin.read()
    text = text.strip()
    if not text:
        print("error: nothing to inject (empty --text and empty stdin)", file=sys.stderr)
        return 2

    path = inject(args.channel, text, args.label)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
