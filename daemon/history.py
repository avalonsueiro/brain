"""The surface log: every turn, searchable, independent of the transcripts.

The `--resume` transcript is the source of truth for a session's context, but it
is exactly the thing compaction eats. This table keeps the raw surface of every
turn regardless, which is what a future `recall` skill reads to recover detail
the model can no longer see.

It can only log turns that happen after it exists, which is the whole argument
for building it now rather than later.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger("rig.history")

SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  channel    TEXT,
  channel_id TEXT,
  session_id TEXT,
  ts         INTEGER,
  direction  TEXT,      -- in | out
  author     TEXT,
  kind       TEXT,      -- message | result | error
  tool_name  TEXT,
  text       TEXT,
  tokens     INTEGER
);
CREATE INDEX IF NOT EXISTS turns_channel_ts ON turns(channel_id, ts);
CREATE INDEX IF NOT EXISTS turns_session    ON turns(session_id, ts);
"""

# Insert-only by design: nothing in the rig ever UPDATEs or DELETEs a turn, so
# the matching FTS sync triggers would be dead weight. Add them the day
# something starts rewriting history.
FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts
  USING fts5(text, content='turns', content_rowid='id');

CREATE TRIGGER IF NOT EXISTS turns_ai AFTER INSERT ON turns BEGIN
  INSERT INTO turns_fts(rowid, text) VALUES (new.id, new.text);
END;
"""

_COLUMNS = (
    "channel", "channel_id", "session_id", "ts",
    "direction", "author", "kind", "tool_name", "text", "tokens",
)
_INSERT = (
    f"INSERT INTO turns ({', '.join(_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in _COLUMNS)})"
)


class History:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.fts = True
        try:
            self.db.executescript(FTS)
        except sqlite3.OperationalError as exc:
            # A python built against a SQLite without FTS5. Log and keep going;
            # the raw table is the part that matters.
            self.fts = False
            log.warning("FTS5 unavailable, full-text search disabled: %s", exc)
        self.db.commit()

    def close(self) -> None:
        try:
            self.db.commit()
            self.db.close()
        except sqlite3.Error:
            pass

    # --- writes ------------------------------------------------------------

    def _insert(self, row: tuple) -> None:
        try:
            self.db.execute(_INSERT, row)
            self.db.commit()
        except sqlite3.Error:
            log.exception("history insert failed")

    def log_inbound(self, channel_id: int, name: str, rec: dict, author, text: str) -> None:
        self._insert((
            name, str(channel_id), rec.get("session_id"), int(time.time()),
            "in", getattr(author, "display_name", str(author)), "message",
            None, text, None,
        ))

    def log_outbound(self, channel_id: int, rec: dict, result) -> None:
        self._insert((
            rec.get("name"), str(channel_id), rec.get("session_id"), int(time.time()),
            "out", "agent", "error" if result.is_error else "result",
            ", ".join(result.tools[:20]) or None, result.text, result.output_tokens,
        ))
