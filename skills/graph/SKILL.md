---
name: graph
description: The rig's long-term memory — a shared, typed knowledge graph of people, projects, companies, tools, decisions and facts. Query it when you do not know who or what the user is referring to, and write to it whenever you learn something durable. Unlike a channel transcript, this survives !reset, compaction, and is visible from every channel.
---

# graph

Your transcript is private to this channel and only lasts until it compacts.
The graph is neither: it is shared across every channel and it persists. If a
fact should still be true next month, it belongs here.

```bash
G=~/.claude/skills/graph/graph.py

$G query "outlate"                       # search first, always
$G get "Akeil"                           # entity + its connections + observations
$G path "Akeil" "Avalon"                 # how two things relate

$G upsert-node --type Person --name "Akeil" --alias "Akeil M"
$G link "Akeil" co-founder-of "outlate"
$G observe "Akeil" --content "prefers async updates over calls" --source "discord/#general"
```

## When to read

Before answering anything that turns on who or what the user means. A name you
don't recognize, a project referred to by shorthand, a decision you're assumed
to remember — those are lookups, not questions to bounce back.

`query` is the cheap first move. `get` when you have the right entity and want
its context. `path` when the question is about a relationship.

## When to write

Whenever a turn produces something durable:

- A person, project, company, or tool you hadn't recorded
- A relationship between two of them
- A stated preference, constraint, decision, or commitment
- A correction to something already in the graph

Write it **as it happens**. A fact you postpone recording is a fact the next
session doesn't have.

## Writing well

**Search before you create.** `upsert-node` dedupes on names and aliases, and
tells you whether it created or matched — but it can only match what it can
recognize. A quick `query` first prevents "Akeil" and "Akeil Mohammed" becoming
two people.

**Add aliases generously.** Every form you have seen: nicknames, full names,
handles, old names. Aliases are how future lookups land.

**Node types** — Person, Company, Project, Product, Topic, Fact, Event, Meeting,
System, Tool, Reference, Resource, Automation, Unknown. Use `Unknown` rather
than stalling on classification; a wrong type is a one-line fix, an unrecorded
fact is gone.

**Relationship names read as a sentence**: `src rel dst`. `"Akeil" co-founder-of
"outlate"` reads correctly; `"Akeil" related-to "outlate"` says nothing. Use
`--symmetric` only when the relation genuinely reads the same both ways
(`colleague-of`, not `manages`).

**Observations are dated and append-only** — use them for things that change or
accumulate (preferences, decisions, what happened). Use `--notes` on the node
for what the entity *is*. Pass `--source` so a future reader can trace it.

## Repair

```bash
$G merge "Akeil Mohammed" "Akeil"     # you created a duplicate; fold it in
$G rename "outlate" "Outlate"         # old name is kept as an alias
$G forget "Some Wrong Fact"           # tombstones — stops surfacing, still on disk
$G stats                              # how big the graph has gotten
```

Prefer `merge` over creating a second node, and `forget` over silence — if
something in here is wrong, correcting it is part of the turn.

## Don't

- Don't dump a whole conversation into an observation. One fact per observation.
- Don't record transient state ("the build is running") — that's what the
  channel is for.
- Don't ask the user something the graph already answers.
