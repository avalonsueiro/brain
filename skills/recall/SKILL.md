---
name: recall
description: Search this rig's full turn log — every message and every answer, across all channels, unsummarized. Use when you half-remember something from an earlier conversation but the detail is no longer in your context, or when the operator refers to something "we decided" that you cannot see. This survives compaction; your transcript does not.
---

# recall

Your visible transcript is summarized as it grows — that is what compaction
does, and it is why detail disappears. The daemon has separately logged every
inbound message and every answer since the rig started, and that log is never
summarized.

```bash
R=~/.claude/skills/recall/recall.py

$R search "deploy"                          # every channel
$R search "47" --channel general --days 7
$R channel general --last 30                # recent turns, verbatim
$R around 412 --context 5                   # what surrounded turn 412
$R stats
```

## When to use it

- The operator says "like we discussed" and you cannot see the discussion
- You need the exact wording of something, not your summary of it
- A `🗜️ this session compacted` notice appeared earlier in this channel
- You want what happened in a *different* channel

## How to use it well

Search is full-text over turn bodies. Start with a distinctive word — a name, a
number, an error string — rather than a phrase.

Results are truncated to keep them readable. When a hit looks right, use
`around <id>` to read the exchange in context, and `--full` for untruncated
text. Prefer `around` over `--full` on a broad search: one conversation read
properly beats twenty snippets.

## recall vs graph

Different jobs, and reaching for the wrong one wastes a turn:

- **`graph`** — what is *true*: who someone is, what a project is, a standing
  preference. Curated, durable, shared.
- **`recall`** — what was *said*: the raw record, with timestamps and channels.

Look in the graph first. Fall back to `recall` when the graph does not have it,
and when you find the answer there, **write it to the graph** so the next turn
does not have to search again.

## Note

Read-only — it cannot modify the log. If nothing matches, the thing may simply
predate the turn log, or have happened in a channel that was `!reset`. Say so
rather than guessing.
