#!/usr/bin/env python3
"""Read your Gmail. Read-only -- this cannot send, archive, or delete.

    gmail.py list                             # recent inbox
    gmail.py list --query "is:unread" --limit 10
    gmail.py list --query "from:akeils@andrew.cmu.edu" --days 30
    gmail.py read <message-id>
    gmail.py thread <thread-id>

`list` returns senders, subjects and snippets only. Bodies come from `read`,
one message at a time and truncated -- a page of full HTML mail would blow
straight through the rig's don't-slurp-large-files rule.
"""

from __future__ import annotations

import argparse
import base64
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auth  # noqa: E402

API = "https://gmail.googleapis.com/gmail/v1/users/me"
BODY_LIMIT = 8000
_TAGS = re.compile(r"<[^>]+>")
_BLANKS = re.compile(r"\n{3,}")


def _header(payload: dict, name: str) -> str:
    for h in (payload.get("headers") or []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def _body(payload: dict) -> str:
    """Prefer text/plain; fall back to stripped HTML.

    Walks the MIME tree rather than assuming a shape -- multipart/alternative
    with a nested multipart/related is completely ordinary mail.
    """
    plain, html = [], []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data:
            if mime == "text/plain":
                plain.append(_decode(data))
            elif mime == "text/html":
                html.append(_decode(data))
        for child in part.get("parts") or []:
            walk(child)

    walk(payload)
    text = "\n".join(plain) if plain else _TAGS.sub(" ", "\n".join(html))
    return _BLANKS.sub("\n\n", text).strip()


def _when(internal_ms: str | int) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(internal_ms) / 1000))
    except (TypeError, ValueError):
        return "?"


def list_messages(query: str, limit: int, days: int | None) -> int:
    if days:
        query = f"{query} newer_than:{days}d".strip()
    listing = auth.api_get(f"{API}/messages", {"q": query or "in:inbox", "maxResults": limit})
    ids = [m["id"] for m in listing.get("messages", [])]
    if not ids:
        print(f"no messages matching {query or 'in:inbox'!r}")
        return 0

    for msg_id in ids:
        msg = auth.api_get(f"{API}/messages/{msg_id}", {
            "format": "metadata",
            "metadataHeaders": ["From", "Subject", "Date"],
        })
        payload = msg.get("payload") or {}
        sender = _header(payload, "From")
        snippet = " ".join((msg.get("snippet") or "").split())[:160]
        unread = "●" if "UNREAD" in (msg.get("labelIds") or []) else " "
        print(f"{unread} [{msg_id}] {_when(msg.get('internalDate'))}  {sender}")
        print(f"    {_header(payload, 'Subject') or '(no subject)'}")
        if snippet:
            print(f"    {snippet}…")
    print(f"\n{len(ids)} message(s). `gmail.py read <id>` for a body.")
    return 0


def read_message(msg_id: str, full: bool) -> int:
    msg = auth.api_get(f"{API}/messages/{msg_id}", {"format": "full"})
    payload = msg.get("payload") or {}
    print(f"From:    {_header(payload, 'From')}")
    print(f"To:      {_header(payload, 'To')}")
    print(f"Date:    {_header(payload, 'Date')}")
    print(f"Subject: {_header(payload, 'Subject')}")
    print(f"Thread:  {msg.get('threadId')}\n")

    body = _body(payload)
    if not full and len(body) > BODY_LIMIT:
        # Say it was cut. Silent truncation is how an agent confidently
        # summarizes the half of a message it happened to receive.
        print(body[:BODY_LIMIT])
        print(f"\n[truncated — {len(body):,} chars total; --full for everything]")
    else:
        print(body or "(no text body)")
    return 0


def read_thread(thread_id: str, full: bool) -> int:
    thread = auth.api_get(f"{API}/threads/{thread_id}", {"format": "full"})
    messages = thread.get("messages", [])
    print(f"{len(messages)} message(s) in thread {thread_id}\n")
    for i, msg in enumerate(messages, 1):
        payload = msg.get("payload") or {}
        print(f"--- {i}/{len(messages)}  {_header(payload, 'From')}"
              f"  {_when(msg.get('internalDate'))} ---")
        body = _body(payload)
        cap = BODY_LIMIT if full else 1500
        print(body[:cap] + ("…" if len(body) > cap else "") or "(no text body)")
        print()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gmail", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="headers and snippets")
    p.add_argument("--query", default="", help="Gmail search syntax")
    p.add_argument("--limit", type=int, default=15)
    p.add_argument("--days", type=int)

    p = sub.add_parser("read", help="one message body")
    p.add_argument("id"); p.add_argument("--full", action="store_true")

    p = sub.add_parser("thread", help="a whole thread")
    p.add_argument("id"); p.add_argument("--full", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.cmd == "list":
            return list_messages(args.query, args.limit, args.days)
        if args.cmd == "read":
            return read_message(args.id, args.full)
        if args.cmd == "thread":
            return read_thread(args.id, args.full)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
