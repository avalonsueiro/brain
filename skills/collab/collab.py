#!/usr/bin/env python3
"""Talk to the other agents in the fleet, and create new ones.

Each Discord channel is its own agent session with its own memory. This is how
they reach each other -- and the message carries a ready-to-run reply command,
so the receiving agent never has to learn any of the plumbing.

    collab.py send snooze --from orchestrator --text "status on the PDF export?"
    collab.py send orchestrator --from snooze --tag reply --text "shipped"
    collab.py spawn pulse --prime "You own the Pulse codebase..." --reply-to orchestrator
    collab.py list

Delivery is confirmed, not assumed: `send` waits for the spool file to be
consumed and exits non-zero if the channel could not be resolved.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

DEFAULT_RIG_ROOT = "/opt/agent-rig"
DELIVERY_TIMEOUT = 15
_LABEL_OK = re.compile(r"[^A-Za-z0-9_.\- ]+")

REPLY_HINT = "~/.claude/skills/collab/collab.py"


def rig_root() -> Path:
    return Path(os.environ.get("RIG_ROOT", DEFAULT_RIG_ROOT))


def _load(skill: str, module: str):
    """Import a sibling skill by path. No sys.path mutation -- a file dropped
    into a skill folder must never shadow stdlib for whatever loads this."""
    import importlib.util

    path = Path(__file__).resolve().parent.parent / skill / f"{module}.py"
    if not path.exists():
        raise RuntimeError(f"collab needs {path}, which is missing")
    spec = importlib.util.spec_from_file_location(f"collab_dep_{skill}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def clean_label(raw: str) -> str:
    """Same sanitizing the daemon applies to inject labels.

    A prompt is framed "[via Discord #x]\\n{author}: {text}", so a `--from`
    carrying a newline could forge a second speaker line and impersonate the
    operator in another channel.
    """
    return _LABEL_OK.sub("", raw or "").strip()[:32] or "agent"


def compose(sender: str, text: str, tag: str | None = None) -> str:
    """The message body, with the reply command embedded.

    That embedded line is the whole trick: the receiver replies by copying one
    command instead of being taught how the spool works.
    """
    head = f"[collab from {sender}]" + (f" [{tag}]" if tag else "")
    return (
        f"{head}\n{text}\n\n"
        f"To reply:  {REPLY_HINT} send {sender} --from <this-channel> --text \"...\""
    )


def _await_delivery(path: Path, timeout: int = DELIVERY_TIMEOUT) -> tuple[bool, str]:
    """Watch the spool file. Gone means delivered; .failed means it wasn't.

    An agent that reports "I told the other channel" when it did not is worse
    than one that reports an error -- the operator acts on the first and
    investigates the second.
    """
    failed = path.with_suffix(path.suffix + ".failed")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not path.exists():
            return (False, "the daemon rejected it — check the channel name") \
                if failed.exists() else (True, "delivered")
        if failed.exists():
            return False, "the daemon rejected it — check the channel name"
        time.sleep(0.4)
    return False, (
        f"not picked up within {timeout}s — the daemon may be down. "
        f"The message is queued at {path} and will be delivered when it returns."
    )


def send(to: str, sender: str, text: str, tag: str | None = None,
         wait: bool = True) -> int:
    inject = _load("inject", "inject")
    path = inject.inject(to.lstrip("#"), compose(clean_label(sender), text, tag),
                         label=f"collab:{clean_label(sender)}")
    if not wait:
        print(f"queued -> #{to} ({path.name})")
        return 0
    ok, detail = _await_delivery(path)
    print(f"{'sent to' if ok else 'FAILED sending to'} #{to}: {detail}")
    return 0 if ok else 1


def spawn(name: str, prime: str, reply_to: str | None, model: str | None,
          topic: str | None, wait: bool = True) -> int:
    control_dir = rig_root() / "control"
    control_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{int(time.time() * 1000)}-{os.getpid()}.json"
    tmp = control_dir / (fname + ".tmp")
    final = control_dir / fname
    payload = {"action": "create_channel", "name": name.lstrip("#"), "prime": prime}
    for key, value in (("reply_to", reply_to), ("model", model), ("topic", topic)):
        if value:
            payload[key] = value
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, final)

    if not wait:
        print(f"requested #{name} ({final.name})")
        return 0
    deadline = time.time() + DELIVERY_TIMEOUT
    while time.time() < deadline:
        if final.with_suffix(".json.done").exists():
            print(f"created #{name}")
            return 0
        if final.with_suffix(".json.failed").exists():
            print(f"FAILED to create #{name} — the daemon reported a problem; "
                  f"check {reply_to or 'the log'}", file=sys.stderr)
            return 1
        if not final.exists():
            print(f"requested #{name} (outcome not yet recorded)")
            return 0
        time.sleep(0.5)
    print(f"no response within {DELIVERY_TIMEOUT}s — is the daemon running?",
          file=sys.stderr)
    return 1


def fleet() -> int:
    state_file = rig_root() / "state" / "state.json"
    if not state_file.exists():
        print("no channels registered yet")
        return 0
    try:
        channels = json.loads(state_file.read_text(encoding="utf-8")).get("channels", {})
    except (json.JSONDecodeError, OSError) as exc:
        print(f"error: state.json unreadable ({exc})", file=sys.stderr)
        return 1
    if not channels:
        print("no channels registered yet")
        return 0
    for rec in sorted(channels.values(), key=lambda r: r.get("name") or ""):
        last = rec.get("last_turn")
        ago = f"{int((time.time() - last) // 60)}m ago" if last else "never"
        flag = " [paused]" if rec.get("archived") else ""
        print(f"  #{rec.get('name', '?'):<18} {rec.get('model', '?'):<8} "
              f"{rec.get('turns', 0):>4} turns   last {ago}{flag}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="collab", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("send", help="message another channel's agent")
    p.add_argument("to", help="channel name")
    p.add_argument("--from", dest="sender", required=True, help="your channel")
    p.add_argument("--text", required=True)
    p.add_argument("--tag", help="e.g. reply, question, fyi")
    p.add_argument("--no-wait", action="store_true", help="skip delivery confirmation")

    p = sub.add_parser("spawn", help="create a new agent channel")
    p.add_argument("name")
    p.add_argument("--prime", required=True, help="the new agent's charter")
    p.add_argument("--reply-to", help="where to report the outcome")
    p.add_argument("--model")
    p.add_argument("--topic")
    p.add_argument("--no-wait", action="store_true")

    sub.add_parser("list", help="every channel in the fleet")

    args = parser.parse_args(argv)
    try:
        if args.cmd == "send":
            return send(args.to, args.sender, args.text, args.tag, not args.no_wait)
        if args.cmd == "spawn":
            return spawn(args.name, args.prime, args.reply_to, args.model,
                         args.topic, not args.no_wait)
        if args.cmd == "list":
            return fleet()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
