#!/usr/bin/env python3
"""The rig's memory: a typed property graph.

Every channel's transcript is private to that channel and only survives until it
compacts. This is the opposite -- one graph, shared by every session, that
outlives `!reset` and the transcript garbage collector both.

    graph.py upsert-node --type Person --name "Akeil" --alias "Akeil M"
    graph.py link "Akeil" founded "outlate" --symmetric
    graph.py observe "Akeil" --content "prefers async updates over calls"
    graph.py query "outlate"
    graph.py get "Akeil"
    graph.py profile "Akeil"
    graph.py path "Akeil" "Discord"

Stdlib only, so it works under any harness that can run a shell command.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from collections import deque
from pathlib import Path

# Resolved per call, never snapshotted at import -- the daemon imports skills
# before load_env() has run, and a frozen path silently ignores agent.env.
DEFAULT_RIG_ROOT = "/opt/agent-rig"


def db_path() -> Path:
    return Path(os.environ.get("RIG_ROOT", DEFAULT_RIG_ROOT)) / "state" / "graph.db"


# The PRD's 14. `Unknown` exists so an agent never has to stall on
# classification -- a node with the wrong type is fixable, a fact never written
# down is gone.
NODE_TYPES = (
    "Person", "Company", "Project", "Product", "Topic", "Fact", "Event",
    "Meeting", "System", "Tool", "Reference", "Resource", "Automation", "Unknown",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  type    TEXT NOT NULL,
  name    TEXT NOT NULL,
  aliases TEXT DEFAULT '',        -- newline separated
  notes   TEXT DEFAULT '',
  props   TEXT DEFAULT '{}',      -- JSON
  status  TEXT DEFAULT 'active',  -- active | forgotten | merged
  created INTEGER,
  updated INTEGER
);
CREATE TABLE IF NOT EXISTS edges (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  src       INTEGER NOT NULL REFERENCES nodes(id),
  rel       TEXT NOT NULL,
  dst       INTEGER NOT NULL REFERENCES nodes(id),
  symmetric INTEGER DEFAULT 0,
  props     TEXT DEFAULT '{}',
  note      TEXT DEFAULT '',
  source    TEXT DEFAULT '',
  created   INTEGER
);
CREATE TABLE IF NOT EXISTS observations (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id INTEGER REFERENCES nodes(id),
  edge_id INTEGER REFERENCES edges(id),
  content TEXT NOT NULL,
  source  TEXT DEFAULT '',
  context TEXT DEFAULT '',
  ts      INTEGER
);
CREATE INDEX IF NOT EXISTS nodes_status ON nodes(status, type);
CREATE INDEX IF NOT EXISTS edges_src    ON edges(src);
CREATE INDEX IF NOT EXISTS edges_dst    ON edges(dst);
CREATE INDEX IF NOT EXISTS obs_node     ON observations(node_id, ts);
CREATE INDEX IF NOT EXISTS obs_edge     ON observations(edge_id, ts);

-- A normalized lookup key is what keeps "Avalon", "avalon" and "Avalon Sueiro"
-- from becoming three people. One row per name or alias, all pointing at the
-- same node.
CREATE TABLE IF NOT EXISTS keys (
  key     TEXT PRIMARY KEY,
  node_id INTEGER NOT NULL REFERENCES nodes(id)
);
"""

FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS search
  USING fts5(body, kind UNINDEXED, ref UNINDEXED);
"""


def _now() -> int:
    return int(time.time())


_PUNCT = re.compile(r"[^a-z0-9]+")


def normalize(name: str) -> str:
    """Lookup key. Case, punctuation and spacing all collapse.

    Deliberately lossy: 'Akeil M.' and 'akeil m' must collide, because a graph
    that stores them separately is worse than no graph at all.
    """
    return _PUNCT.sub(" ", (name or "").lower()).strip()


class Graph:
    def __init__(self, path: Path | None = None) -> None:
        # Named db_file, not path: `path` is one of the CLI verbs, and an
        # attribute by that name shadows the method.
        self.db_file = path or db_path()
        self.db_file.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.db_file)
        self.db.row_factory = sqlite3.Row
        # Several channels write concurrently. WAL plus a real busy timeout is
        # enough -- SQLite owns its own locking, so this needs no flock (unlike
        # jobs.json, which is a plain file two processes rewrite).
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        try:
            self.db.executescript(FTS)
            self.fts = True
        except sqlite3.OperationalError:
            self.fts = False
        self.db.commit()

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    # --- search index ------------------------------------------------------

    def _reindex_node(self, node_id: int) -> None:
        if not self.fts:
            return
        row = self.db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        if row is None:
            return
        self.db.execute("DELETE FROM search WHERE kind='node' AND ref=?", (node_id,))
        if row["status"] != "active":
            return  # a forgotten node stops surfacing, but still exists
        body = " ".join(filter(None, [row["name"], row["aliases"], row["notes"]]))
        self.db.execute(
            "INSERT INTO search (body, kind, ref) VALUES (?, 'node', ?)", (body, node_id)
        )

    def _index_observation(self, obs_id: int, node_id: int | None, content: str) -> None:
        if self.fts and node_id:
            self.db.execute(
                "INSERT INTO search (body, kind, ref) VALUES (?, 'obs', ?)",
                (content, node_id),
            )

    # --- nodes -------------------------------------------------------------

    def resolve(self, name: str) -> int | None:
        """Node id for a name or alias, or None."""
        row = self.db.execute(
            "SELECT node_id FROM keys WHERE key=?", (normalize(name),)
        ).fetchone()
        return row["node_id"] if row else None

    def require(self, name: str) -> int:
        node_id = self.resolve(name)
        if node_id is None:
            raise LookupError(f"no node matching {name!r} — create it with upsert-node")
        return node_id

    def upsert_node(
        self,
        name: str,
        type_: str = "Unknown",
        aliases: list[str] | None = None,
        notes: str = "",
        props: dict | None = None,
    ) -> tuple[int, bool]:
        """Create or update. Returns (node_id, created).

        The `created` flag is returned rather than swallowed so the caller can
        tell "I made a new person" from "that was already someone I knew" --
        an agent that cannot tell those apart will keep making duplicates.
        """
        aliases = [a for a in (aliases or []) if a.strip()]
        node_id = self.resolve(name)
        for alias in aliases:                 # an alias may already identify it
            node_id = node_id or self.resolve(alias)

        now = _now()
        if node_id is None:
            cur = self.db.execute(
                "INSERT INTO nodes (type, name, aliases, notes, props, status, created, updated)"
                " VALUES (?,?,?,?,?,'active',?,?)",
                (type_, name, "\n".join(aliases), notes,
                 json.dumps(props or {}), now, now),
            )
            node_id, created = cur.lastrowid, True
        else:
            row = self.db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
            merged = [a for a in row["aliases"].split("\n") if a]
            for alias in aliases:
                if normalize(alias) not in {normalize(m) for m in merged}:
                    merged.append(alias)
            new_props = json.loads(row["props"] or "{}")
            new_props.update(props or {})
            self.db.execute(
                "UPDATE nodes SET type=?, aliases=?, notes=?, props=?, updated=?,"
                " status=CASE WHEN status='forgotten' THEN 'active' ELSE status END"
                " WHERE id=?",
                (type_ if type_ != "Unknown" else row["type"],
                 "\n".join(merged),
                 notes or row["notes"],
                 json.dumps(new_props), now, node_id),
            )
            created = False

        for key in {normalize(name), *(normalize(a) for a in aliases)}:
            if key:
                self.db.execute(
                    "INSERT OR IGNORE INTO keys (key, node_id) VALUES (?,?)", (key, node_id)
                )
        self._reindex_node(node_id)
        self.db.commit()
        return node_id, created

    def node(self, node_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()

    # --- edges -------------------------------------------------------------

    def link(self, src: str, rel: str, dst: str, symmetric: bool = False,
             note: str = "", source: str = "") -> int:
        src_id, dst_id = self.require(src), self.require(dst)
        existing = self.db.execute(
            "SELECT id FROM edges WHERE src=? AND rel=? AND dst=?", (src_id, rel, dst_id)
        ).fetchone()
        if existing:
            return existing["id"]
        cur = self.db.execute(
            "INSERT INTO edges (src, rel, dst, symmetric, props, note, source, created)"
            " VALUES (?,?,?,?,'{}',?,?,?)",
            (src_id, rel, dst_id, 1 if symmetric else 0, note, source, _now()),
        )
        self.db.commit()
        return cur.lastrowid

    def neighbors(self, node_id: int) -> list[dict]:
        """Edges in both directions. A symmetric edge reads the same either way."""
        out = []
        for row in self.db.execute(
            "SELECT e.*, n.name AS other_name, n.type AS other_type FROM edges e"
            " JOIN nodes n ON n.id = e.dst WHERE e.src=?", (node_id,)
        ):
            out.append({"rel": row["rel"], "dir": "->", "other": row["other_name"],
                        "type": row["other_type"], "note": row["note"]})
        for row in self.db.execute(
            "SELECT e.*, n.name AS other_name, n.type AS other_type FROM edges e"
            " JOIN nodes n ON n.id = e.src WHERE e.dst=?", (node_id,)
        ):
            out.append({"rel": row["rel"],
                        "dir": "<->" if row["symmetric"] else "<-",
                        "other": row["other_name"], "type": row["other_type"],
                        "note": row["note"]})
        return out

    # --- observations ------------------------------------------------------

    def observe(self, target: str, content: str, source: str = "", context: str = "",
                edge_id: int | None = None) -> int:
        node_id = None if edge_id else self.require(target)
        cur = self.db.execute(
            "INSERT INTO observations (node_id, edge_id, content, source, context, ts)"
            " VALUES (?,?,?,?,?,?)",
            (node_id, edge_id, content, source, context, _now()),
        )
        if node_id:
            self.db.execute("UPDATE nodes SET updated=? WHERE id=?", (_now(), node_id))
        self._index_observation(cur.lastrowid, node_id, content)
        self.db.commit()
        return cur.lastrowid

    def observations(self, node_id: int, limit: int = 50) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM observations WHERE node_id=? ORDER BY ts DESC LIMIT ?",
            (node_id, limit),
        ).fetchall()

    # --- reads -------------------------------------------------------------

    def query(self, text: str, limit: int = 20) -> list[dict]:
        """Full-text over names, aliases, notes and observations."""
        seen, out = set(), []
        if self.fts:
            try:
                rows = self.db.execute(
                    "SELECT DISTINCT ref FROM search WHERE search MATCH ? LIMIT ?",
                    (text, limit * 3),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []                      # malformed FTS query; fall through
            for row in rows:
                node = self.node(row["ref"])
                if node and node["status"] == "active" and node["id"] not in seen:
                    seen.add(node["id"])
                    out.append(dict(node))
        if len(out) < limit:                   # LIKE fallback, and for no-FTS builds
            like = f"%{text}%"
            for node in self.db.execute(
                "SELECT * FROM nodes WHERE status='active' AND"
                " (name LIKE ? OR aliases LIKE ? OR notes LIKE ?) LIMIT ?",
                (like, like, like, limit),
            ):
                if node["id"] not in seen:
                    seen.add(node["id"])
                    out.append(dict(node))
        return out[:limit]

    def path(self, src: str, dst: str, max_hops: int = 6) -> list[str] | None:
        """Shortest connection between two entities, breadth-first."""
        start, goal = self.require(src), self.require(dst)
        if start == goal:
            return [self.node(start)["name"]]
        seen, queue = {start}, deque([(start, [start])])
        while queue:
            current, trail = queue.popleft()
            if len(trail) > max_hops:
                break
            for row in self.db.execute(
                "SELECT dst AS other FROM edges WHERE src=?"
                " UNION SELECT src AS other FROM edges WHERE dst=?", (current, current)
            ):
                other = row["other"]
                if other in seen:
                    continue
                if other == goal:
                    return [self.node(n)["name"] for n in trail + [other]]
                seen.add(other)
                queue.append((other, trail + [other]))
        return None

    # --- repair ------------------------------------------------------------

    def forget(self, name: str) -> int:
        """Tombstone, never delete.

        A wrong fact should stop surfacing without destroying the record that
        you once believed it -- which is usually the thing you need when you go
        looking for how the mistake happened.
        """
        node_id = self.require(name)
        self.db.execute(
            "UPDATE nodes SET status='forgotten', updated=? WHERE id=?", (_now(), node_id)
        )
        self._reindex_node(node_id)
        self.db.commit()
        return node_id

    def rename(self, old: str, new: str) -> int:
        node_id = self.require(old)
        row = self.node(node_id)
        aliases = [a for a in row["aliases"].split("\n") if a]
        # The old name becomes an alias: anything that already refers to the
        # entity by it must keep resolving.
        if normalize(row["name"]) not in {normalize(a) for a in aliases}:
            aliases.append(row["name"])
        self.db.execute(
            "UPDATE nodes SET name=?, aliases=?, updated=? WHERE id=?",
            (new, "\n".join(aliases), _now(), node_id),
        )
        self.db.execute(
            "INSERT OR IGNORE INTO keys (key, node_id) VALUES (?,?)",
            (normalize(new), node_id),
        )
        self._reindex_node(node_id)
        self.db.commit()
        return node_id

    def merge(self, loser: str, winner: str) -> int:
        """Fold one node into another. Edges, observations and keys all move."""
        loser_id, winner_id = self.require(loser), self.require(winner)
        if loser_id == winner_id:
            return winner_id
        self.db.execute("UPDATE edges SET src=? WHERE src=?", (winner_id, loser_id))
        self.db.execute("UPDATE edges SET dst=? WHERE dst=?", (winner_id, loser_id))
        self.db.execute("UPDATE observations SET node_id=? WHERE node_id=?",
                        (winner_id, loser_id))
        self.db.execute("UPDATE keys SET node_id=? WHERE node_id=?", (winner_id, loser_id))

        lose_row, win_row = self.node(loser_id), self.node(winner_id)
        aliases = [a for a in win_row["aliases"].split("\n") if a]
        for candidate in [lose_row["name"], *lose_row["aliases"].split("\n")]:
            if candidate and normalize(candidate) not in {normalize(a) for a in aliases}:
                aliases.append(candidate)
        self.db.execute(
            "UPDATE nodes SET aliases=?, notes=?, updated=? WHERE id=?",
            ("\n".join(aliases),
             "\n".join(filter(None, [win_row["notes"], lose_row["notes"]])),
             _now(), winner_id),
        )
        self.db.execute("UPDATE nodes SET status='merged', updated=? WHERE id=?",
                        (_now(), loser_id))
        # Self-edges are the usual artifact of a merge; drop them rather than
        # leaving "outlate founded outlate" in the graph forever.
        self.db.execute("DELETE FROM edges WHERE src=dst")
        self._reindex_node(loser_id)
        self._reindex_node(winner_id)
        self.db.commit()
        return winner_id

    # --- rendering ---------------------------------------------------------

    def profile(self, name: str) -> str:
        """Markdown dossier -- what an agent pastes into a turn."""
        node_id = self.require(name)
        row = self.node(node_id)
        out = [f"# {row['name']}  ({row['type']})"]
        aliases = [a for a in row["aliases"].split("\n") if a]
        if aliases:
            out.append(f"*also:* {', '.join(aliases)}")
        if row["status"] != "active":
            out.append(f"**status: {row['status']}**")
        if row["notes"]:
            out.append(f"\n{row['notes']}")
        props = json.loads(row["props"] or "{}")
        if props:
            out.append("\n## Properties")
            out += [f"- **{k}**: {v}" for k, v in props.items()]

        links = self.neighbors(node_id)
        if links:
            out.append("\n## Connections")
            for link in links:
                arrow = {"->": "", "<-": " (inbound)", "<->": " (mutual)"}[link["dir"]]
                note = f" — {link['note']}" if link["note"] else ""
                out.append(f"- {link['rel']} **{link['other']}** "
                           f"({link['type']}){arrow}{note}")

        obs = self.observations(node_id)
        if obs:
            out.append("\n## Observations")
            for entry in obs:
                stamp = time.strftime("%Y-%m-%d", time.localtime(entry["ts"]))
                src = f" _{entry['source']}_" if entry["source"] else ""
                out.append(f"- `{stamp}`{src} {entry['content']}")
        return "\n".join(out)

    def recent(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM nodes WHERE status='active' ORDER BY updated DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def stats(self) -> dict:
        one = lambda sql: self.db.execute(sql).fetchone()[0]
        by_type = {
            row["type"]: row["n"] for row in self.db.execute(
                "SELECT type, COUNT(*) n FROM nodes WHERE status='active'"
                " GROUP BY type ORDER BY n DESC"
            )
        }
        return {
            "nodes": one("SELECT COUNT(*) FROM nodes WHERE status='active'"),
            "forgotten": one("SELECT COUNT(*) FROM nodes WHERE status!='active'"),
            "edges": one("SELECT COUNT(*) FROM edges"),
            "observations": one("SELECT COUNT(*) FROM observations"),
            "by_type": by_type,
            "path": str(self.db_file),
        }

    def export(self) -> dict:
        return {
            "nodes": [dict(r) for r in self.db.execute("SELECT * FROM nodes")],
            "edges": [dict(r) for r in self.db.execute("SELECT * FROM edges")],
            "observations": [dict(r) for r in self.db.execute("SELECT * FROM observations")],
        }


# --- CLI -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="graph", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("upsert-node", help="create or update an entity")
    p.add_argument("--name", required=True)
    p.add_argument("--type", default="Unknown", choices=NODE_TYPES)
    p.add_argument("--alias", action="append", default=[])
    p.add_argument("--notes", default="")
    p.add_argument("--prop", action="append", default=[], metavar="K=V")

    p = sub.add_parser("link", help="connect two entities")
    p.add_argument("src"); p.add_argument("rel"); p.add_argument("dst")
    p.add_argument("--symmetric", action="store_true")
    p.add_argument("--note", default="")
    p.add_argument("--source", default="")

    p = sub.add_parser("observe", help="append a dated observation")
    p.add_argument("name"); p.add_argument("--content", required=True)
    p.add_argument("--source", default=""); p.add_argument("--context", default="")

    for name, help_text in (("get", "an entity and its neighborhood"),
                            ("profile", "markdown dossier"),
                            ("forget", "tombstone (never deletes)")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("name")

    p = sub.add_parser("query", help="full-text search")
    p.add_argument("text"); p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("path", help="how A connects to B")
    p.add_argument("src"); p.add_argument("dst")

    p = sub.add_parser("rename", help="rename, keeping the old name as an alias")
    p.add_argument("old"); p.add_argument("new")

    p = sub.add_parser("merge", help="fold one entity into another")
    p.add_argument("loser"); p.add_argument("winner")

    p = sub.add_parser("recent", help="recently touched entities")
    p.add_argument("--limit", type=int, default=20)

    sub.add_parser("stats", help="size of the graph")
    sub.add_parser("export", help="dump everything as JSON")

    args = parser.parse_args(argv)
    g = Graph()
    try:
        return _dispatch(g, args)
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        g.close()


def _dispatch(g: Graph, args) -> int:
    if args.cmd == "upsert-node":
        props = {}
        for pair in args.prop:
            key, _, value = pair.partition("=")
            props[key.strip()] = value.strip()
        node_id, created = g.upsert_node(args.name, args.type, args.alias, args.notes, props)
        # Say which it was: an agent that cannot tell "new person" from "already
        # knew them" will keep creating near-duplicates.
        print(f"{'created' if created else 'matched existing'} "
              f"{g.node(node_id)['type']} #{node_id}: {g.node(node_id)['name']}")
        return 0

    if args.cmd == "link":
        edge_id = g.link(args.src, args.rel, args.dst, args.symmetric, args.note, args.source)
        arrow = "<->" if args.symmetric else "->"
        print(f"edge #{edge_id}: {args.src} {arrow} [{args.rel}] {args.dst}")
        return 0

    if args.cmd == "observe":
        obs_id = g.observe(args.name, args.content, args.source, args.context)
        print(f"observation #{obs_id} on {args.name}")
        return 0

    if args.cmd in ("get", "profile"):
        print(g.profile(args.name))
        return 0

    if args.cmd == "query":
        rows = g.query(args.text, args.limit)
        if not rows:
            print(f"nothing matching {args.text!r}")
            return 0
        for row in rows:
            aliases = [a for a in (row["aliases"] or "").split("\n") if a]
            extra = f"  (aka {', '.join(aliases)})" if aliases else ""
            print(f"{row['type']:<11} {row['name']}{extra}")
        return 0

    if args.cmd == "path":
        trail = g.path(args.src, args.dst)
        print(" -> ".join(trail) if trail else
              f"no connection between {args.src!r} and {args.dst!r}")
        return 0 if trail else 1

    if args.cmd == "forget":
        g.forget(args.name)
        print(f"forgot {args.name} (tombstoned, not deleted)")
        return 0

    if args.cmd == "rename":
        g.rename(args.old, args.new)
        print(f"renamed {args.old} -> {args.new} (old name kept as an alias)")
        return 0

    if args.cmd == "merge":
        g.merge(args.loser, args.winner)
        print(f"merged {args.loser} into {args.winner}")
        return 0

    if args.cmd == "recent":
        for row in g.recent(args.limit):
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["updated"]))
            print(f"{stamp}  {row['type']:<11} {row['name']}")
        return 0

    if args.cmd == "stats":
        s = g.stats()
        print(f"{s['nodes']} nodes, {s['edges']} edges, {s['observations']} observations"
              f"  ({s['forgotten']} forgotten)")
        for type_, count in s["by_type"].items():
            print(f"  {type_:<11} {count}")
        print(f"  {s['path']}")
        return 0

    if args.cmd == "export":
        print(json.dumps(g.export(), indent=2))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
