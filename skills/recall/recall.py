#!/usr/bin/env python3
"""Search the rig's turn log -- what compaction took out of your context.

Your transcript gets summarized as it grows. The daemon has been writing every
inbound message and every answer to a separate log the whole time, and that log
is not summarized. When you half-remember something from this channel but the
detail is gone, it is still here.

    recall.py search "deploy"                       # across every channel
    recall.py search "47" --channel general --days 7
    recall.py channel general --last 30             # recent turns verbatim
    recall.py around 412 --context 5                # what surrounded turn 412

Read-only, stdlib only.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

DEFAULT_RIG_ROOT = "/opt/agent-rig"
SNIPPET = 400


def db_path() -> Path:
    return Path(os.environ.get("RIG_ROOT", DEFAULT_RIG_ROOT)) / "state" / "history.db"


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or db_path()
    if not path.exists():
        raise FileNotFoundError(f"no turn log at {path}")
    # Read-only on purpose: this is the daemon's live database, and a reader
    # should never be able to damage the record it exists to protect.
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def _stamp(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _line(row: sqlite3.Row, width: int = SNIPPET) -> str:
    text = " ".join((row["text"] or "").split())
    if len(text) > width:
        text = text[:width] + "…"
    who = row["author"] or ("agent" if row["direction"] == "out" else "?")
    return f"[{row['id']}] {_stamp(row['ts'])}  #{row['channel']}  {who}: {text}"


def search(db, text: str, channel: str | None = None, days: int | None = None,
           limit: int = 20) -> list[sqlite3.Row]:
    sql = ("SELECT t.* FROM turns_fts f JOIN turns t ON t.id = f.rowid"
           " WHERE turns_fts MATCH ?")
    params: list = [text]
    if channel:
        sql += " AND t.channel = ?"
        params.append(channel.lstrip("#"))
    if days:
        sql += " AND t.ts >= ?"
        params.append(int(time.time()) - days * 86400)
    sql += " ORDER BY t.ts DESC, t.id DESC LIMIT ?"
    params.append(limit)
    try:
        return db.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        # FTS5 absent, or a query string it cannot parse (a bare "AND", an
        # unbalanced quote). Fall back rather than handing the caller a
        # traceback for what is really just a search miss.
        like = f"%{text}%"
        sql = "SELECT * FROM turns WHERE text LIKE ?"
        params = [like]
        if channel:
            sql += " AND channel = ?"
            params.append(channel.lstrip("#"))
        if days:
            sql += " AND ts >= ?"
            params.append(int(time.time()) - days * 86400)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(limit)
        return db.execute(sql, params).fetchall()


def channel_tail(db, channel: str, limit: int = 30) -> list[sqlite3.Row]:
    # id breaks ties, and ties are the normal case: a question and its answer
    # routinely land in the same second, and ordering on ts alone lets SQLite
    # return them either way round -- so a conversation reads backwards at
    # random.
    rows = db.execute(
        "SELECT * FROM turns WHERE channel = ? ORDER BY ts DESC, id DESC LIMIT ?",
        (channel.lstrip("#"), limit),
    ).fetchall()
    return list(reversed(rows))          # read oldest-first, like a conversation


def around(db, turn_id: int, context: int = 5) -> list[sqlite3.Row]:
    row = db.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
    if row is None:
        return []
    before = db.execute(
        "SELECT * FROM turns WHERE channel_id = ? AND id < ? ORDER BY id DESC LIMIT ?",
        (row["channel_id"], turn_id, context),
    ).fetchall()
    after = db.execute(
        "SELECT * FROM turns WHERE channel_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
        (row["channel_id"], turn_id, context),
    ).fetchall()
    return list(reversed(before)) + [row] + list(after)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="recall", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("search", help="full-text across the turn log")
    p.add_argument("text")
    p.add_argument("--channel")
    p.add_argument("--days", type=int)
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--full", action="store_true", help="untruncated text")

    p = sub.add_parser("channel", help="recent turns in a channel, verbatim")
    p.add_argument("name")
    p.add_argument("--last", type=int, default=30)
    p.add_argument("--full", action="store_true")

    p = sub.add_parser("around", help="what surrounded a turn")
    p.add_argument("turn_id", type=int)
    p.add_argument("--context", type=int, default=5)
    p.add_argument("--full", action="store_true")

    sub.add_parser("stats", help="size and span of the log")

    args = parser.parse_args(argv)
    try:
        db = connect()
    except (FileNotFoundError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    width = 10_000 if getattr(args, "full", False) else SNIPPET

    if args.cmd == "search":
        rows = search(db, args.text, args.channel, args.days, args.limit)
        if not rows:
            print(f"nothing in the turn log matching {args.text!r}")
            return 0
        for row in rows:
            print(_line(row, width))
        print(f"\n{len(rows)} match(es). `recall.py around <id>` for surrounding turns.")
        return 0

    if args.cmd == "channel":
        rows = channel_tail(db, args.name, args.last)
        if not rows:
            print(f"no turns logged for #{args.name.lstrip('#')}")
            return 0
        for row in rows:
            print(_line(row, width))
        return 0

    if args.cmd == "around":
        rows = around(db, args.turn_id, args.context)
        if not rows:
            print(f"no turn with id {args.turn_id}")
            return 1
        for row in rows:
            marker = ">>" if row["id"] == args.turn_id else "  "
            print(f"{marker} {_line(row, width)}")
        return 0

    if args.cmd == "stats":
        total, first, last = db.execute(
            "SELECT COUNT(*), MIN(ts), MAX(ts) FROM turns").fetchone()
        print(f"{total} turns logged")
        if total:
            print(f"  {_stamp(first)}  ->  {_stamp(last)}")
            for row in db.execute(
                "SELECT channel, COUNT(*) n FROM turns GROUP BY channel ORDER BY n DESC"
            ):
                print(f"  #{row['channel']:<16} {row['n']}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
