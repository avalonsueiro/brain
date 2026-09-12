"""Offline tests. No Discord, no real `claude`, no tokens spent.

Run:  .venv/bin/python tests/test_rig.py

The interesting cases here are the three failure modes the source rig's incident
list is about: a first turn that dies after init, a resume whose transcript has
vanished, and a turn that hangs. Each is cheap to test with a stub and expensive
to discover in production.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="rig-test-"))
os.environ["RIG_ROOT"] = str(TMP / "rig")
os.environ["HOME"] = str(TMP / "home")
os.environ.setdefault("DEFAULT_MODEL", "opus")
(TMP / "home").mkdir(parents=True, exist_ok=True)

from daemon import config, harness as harness_mod, spool as spool_mod, state as state_mod  # noqa: E402
from daemon.ticker import split_message  # noqa: E402

wake_mod = spool_mod.wake_mod
# wake resolves RIG_ROOT/RIG_TZ per call now, so the sandbox is real rather than
# an accident of import order. Assert it loudly -- a regression here would have
# the suite writing the operator's live jobs.json.
assert str(TMP) in str(wake_mod.jobs_file()), (
    f"tests would write the real jobs file: {wake_mod.jobs_file()}")

PASS = FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \033[32mok\033[0m   {label}")
    else:
        FAIL += 1
        print(f"  \033[31mFAIL\033[0m {label}" + (f"\n        {detail}" if detail else ""))


# --- stub claude -----------------------------------------------------------

STUB = r'''#!/usr/bin/env python3
import json, os, sys, time
argv = sys.argv[1:]
mode = os.environ.get("STUB_MODE", "ok")
resume = "--resume" in argv
model = argv[argv.index("--model") + 1] if "--model" in argv else "?"
sid = (argv[argv.index("--resume") + 1] if resume
       else argv[argv.index("--session-id") + 1] if "--session-id" in argv else "none")

if mode == "session_exists" and not resume:
    sys.stderr.write("Error: session %s is already in use\n" % sid); sys.exit(1)
if mode == "model_error" and model.endswith("[1m]"):
    sys.stderr.write("Error: unknown model %s\n" % model); sys.exit(1)
if mode == "hang":
    print(json.dumps({"type": "system", "subtype": "init", "session_id": sid}), flush=True)
    time.sleep(120); sys.exit(0)
if mode == "die_after_init":
    print(json.dumps({"type": "system", "subtype": "init", "session_id": sid}), flush=True)
    sys.exit(3)
if mode == "partial_then_die":
    print(json.dumps({"type": "system", "subtype": "init", "session_id": sid}), flush=True)
    print(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "half an answer"}]}}), flush=True)
    sys.exit(1)
if mode == "prose_about_session":
    # An errored turn whose TEXT discusses sessions and models, with NO init --
    # so the only thing that could set primed here is a classifier wrongly
    # matching the model's own prose.
    print(json.dumps({"type": "result", "subtype": "error_during_execution",
                      "is_error": True, "session_id": sid,
                      "result": "The session is already in use and the model was not found."
                      }), flush=True)
    sys.exit(0)
if mode == "silent_success":
    print(json.dumps({"type": "system", "subtype": "init", "session_id": sid}), flush=True)
    sys.exit(0)
if mode == "big":
    big = "z" * 200000
    print(json.dumps({"type": "system", "subtype": "init", "session_id": sid}), flush=True)
    print(json.dumps({"type": "result", "subtype": "success", "result": big,
                      "is_error": False, "session_id": sid}), flush=True)
    sys.exit(0)

print(json.dumps({"type": "system", "subtype": "init", "session_id": sid}), flush=True)
print(json.dumps({"type": "assistant", "message": {"content": [
    {"type": "text", "text": "Hello "}]}}), flush=True)
print(json.dumps({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "name": "Bash", "input": {"command": "git status"}}]}}), flush=True)
print(json.dumps({"type": "assistant", "message": {"content": [
    {"type": "text", "text": "world"}]}}), flush=True)
print(json.dumps({"type": "result", "subtype": "success", "result": "Hello world",
                  "is_error": False, "total_cost_usd": 0.0123, "session_id": sid,
                  "usage": {"input_tokens": 10, "output_tokens": 5}}), flush=True)
# Prove the resume flag round-trips to the caller.
sys.stderr.write("resume=%s model=%s\n" % (resume, model))
'''


def write_stub() -> Path:
    p = TMP / "claude-stub"
    p.write_text(STUB, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return p


# --- fixtures --------------------------------------------------------------


def fresh_state() -> state_mod.State:
    config.ensure_dirs()
    if config.STATE_FILE.exists():
        config.STATE_FILE.unlink()
    return state_mod.State()


def make_cfg(stub: Path, timeout: int = 30) -> config.Config:
    os.environ["CLAUDE_BIN"] = str(stub)
    os.environ["TURN_TIMEOUT"] = str(timeout)
    cfg = config.Config()
    cfg.claude_bin = str(stub)
    cfg.turn_timeout = timeout
    return cfg


async def run(cfg, st, rec, channel_id=1, prompt="hi"):
    h = harness_mod.ClaudeHarness(cfg, st)
    events: list[tuple] = []

    async def on_event(kind, payload):
        events.append((kind, payload))

    res = await h.run_turn(
        channel_id=channel_id, rec=rec, prompt=prompt, on_event=on_event
    )
    return res, events


def touch_transcript(rec: dict, body: str = '{"x":1}\n') -> Path:
    p = state_mod.transcript_path(rec["workdir"], rec["session_id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


# --- tests -----------------------------------------------------------------


def test_splitter() -> None:
    print("\nsplit_message")
    check("short text is one chunk", split_message("hello") == ["hello"])
    check("empty text is no chunks", split_message("   ") == [])

    long = "\n\n".join(f"paragraph {i} " + "x" * 200 for i in range(40))
    chunks = split_message(long)
    check("long text splits", len(chunks) > 1)
    check("every chunk under the Discord limit", all(len(c) <= 2000 for c in chunks),
          f"max={max(len(c) for c in chunks)}")
    check("no content lost", sum(c.count("paragraph ") for c in chunks) == 40)

    fenced = "before\n\n```python\n" + "\n".join(f"line_{i} = {i}" for i in range(300)) + "\n```\n\nafter"
    fchunks = split_message(fenced)
    check("fenced text splits", len(fchunks) > 1)
    check("every chunk has balanced fences",
          all(len([l for l in c.split("\n") if l.lstrip().startswith("```")]) % 2 == 0
              for c in fchunks),
          str([len([l for l in c.split("\n") if l.lstrip().startswith("```")]) for c in fchunks]))

    hard = "y" * 6000
    hchunks = split_message(hard)
    check("unbreakable text still splits", all(len(c) <= 2000 for c in hchunks))


def test_state() -> None:
    print("\nstate")
    st = fresh_state()
    rec = st.register(111, "orchestrator")
    wd = rec["workdir"]
    check("workdir created", Path(wd).is_dir())
    check("CLAUDE.md seeded into workdir", (Path(wd) / "CLAUDE.md").exists())
    check("standing rules made it in",
          "Never stop, kill, or restart" in (Path(wd) / "CLAUDE.md").read_text())
    check("timezone placeholder substituted",
          "__RIG_TZ__" not in (Path(wd) / "CLAUDE.md").read_text())
    check("harness carried from day one", rec.get("harness") == "cc")
    check("starts unprimed", rec["primed"] is False)

    # The one that silently destroys a session if it ever regresses.
    renamed = st.register(111, "orchestrator-renamed")
    check("workdir survives a channel rename", renamed["workdir"] == wd,
          f"{renamed['workdir']} != {wd}")
    check("name tracks the rename", renamed["name"] == "orchestrator-renamed")

    other = st.register(222, "orchestrator")
    check("colliding slug gets a distinct workdir", other["workdir"] != wd)

    # Two channels sharing a workdir means two sessions sharing a transcript
    # directory, so the fallback name has to be checked for collisions too.
    st.register(333, "orchestrator-222")
    workdirs = [c["workdir"] for c in st.all().values()]
    check("every channel has its own workdir", len(workdirs) == len(set(workdirs)),
          str(workdirs))

    old_sid = rec["session_id"]
    # A !reset landing mid-turn swaps the uuid; the in-flight turn's init event
    # must not then mark THAT uuid primed, or the channel bricks on the
    # missing-transcript gate.
    st.mark_primed(111, "a-different-uuid")
    check("mark_primed ignores a stale session id", st.get(111)["primed"] is False)
    st.mark_primed(111, old_sid)
    check("mark_primed persists for the live session", st.get(111)["primed"] is True)
    reset = st.reset_session(111)
    check("reset mints a new session id", reset["session_id"] != old_sid)
    check("reset clears primed", reset["primed"] is False)
    check("reset keeps the workdir", reset["workdir"] == wd)

    st2 = state_mod.State()
    check("state survives a reload", st2.get(111)["session_id"] == reset["session_id"])

    check("workdir encoding matches Claude Code",
          state_mod.encode_workdir("/opt/agent-rig/workdirs/a.b")
          == "-opt-agent-rig-workdirs-a-b")


def test_happy_path() -> None:
    print("\nharness: normal turn")
    stub = write_stub()
    os.environ["STUB_MODE"] = "ok"
    st = fresh_state()
    cfg = make_cfg(stub)
    rec = st.register(300, "happy")

    res, events = asyncio.run(run(cfg, st, rec, 300))
    check("turn succeeded", not res.is_error, f"{res.error_kind} {res.stderr_tail}")
    check("final text captured", res.text == "Hello world", repr(res.text))
    check("tool call captured", res.tools == ["Bash(git status)"], str(res.tools))
    check("cost captured", res.cost_usd == 0.0123)
    check("output tokens captured", res.output_tokens == 5)
    check("first turn used --session-id", res.resumed is False)
    check("primed committed on init", st.get(300)["primed"] is True)
    check("resolved model cached", st.get(300)["model_resolved"] == "claude-opus-5[1m]")
    kinds = [k for k, _ in events]
    check("init/text/tool/result all streamed",
          kinds.count("init") == 1 and kinds.count("tool") == 1
          and kinds.count("text") == 2 and kinds.count("result") == 1, str(kinds))

    touch_transcript(st.get(300))
    res2, _ = asyncio.run(run(cfg, st, st.get(300), 300))
    check("second turn resumes", res2.resumed is True)


def test_session_exists_retry() -> None:
    print("\nharness: first turn died after init (the --session-id trap)")
    stub = write_stub()
    os.environ["STUB_MODE"] = "session_exists"
    st = fresh_state()
    cfg = make_cfg(stub)
    rec = st.register(400, "crashed")

    res, _ = asyncio.run(run(cfg, st, rec, 400))
    check("recovers by resuming instead of erroring", not res.is_error,
          f"{res.error_kind} {res.stderr_tail}")
    check("the successful attempt was a resume", res.resumed is True)
    check("primed repaired in state", st.get(400)["primed"] is True)


def test_model_fallback() -> None:
    print("\nharness: [1m] variant does not exist")
    stub = write_stub()
    os.environ["STUB_MODE"] = "model_error"
    st = fresh_state()
    cfg = make_cfg(stub)
    rec = st.register(500, "fallback")
    st.update(500, model="fable", model_resolved=None)

    res, _ = asyncio.run(run(cfg, st, st.get(500), 500))
    check("falls back to the bare model id", not res.is_error,
          f"{res.error_kind} {res.stderr_tail}")
    check("bare id was the one used", res.model_used == "claude-fable-5", str(res.model_used))
    check("fallback cached so it costs one spawn ever",
          st.get(500)["model_resolved"] == "claude-fable-5")

    print("\nharness: alias with no fallback still fails loudly")
    st.update(500, model="opus", model_resolved=None)
    res2, _ = asyncio.run(run(cfg, st, st.get(500), 500))
    check("opus[1m] error is not silently swallowed", res2.is_error)


def test_missing_transcript() -> None:
    print("\nharness: resume with a vanished transcript")
    stub = write_stub()
    os.environ["STUB_MODE"] = "ok"
    st = fresh_state()
    cfg = make_cfg(stub)
    rec = st.register(600, "amnesia")
    st.update(600, primed=True)

    res, events = asyncio.run(run(cfg, st, st.get(600), 600))
    check("reported as an error", res.is_error)
    check("classified as missing_transcript", res.error_kind == "missing_transcript",
          str(res.error_kind))
    check("never spawned a process", events == [], str(events))
    check("message names the missing file", "transcript is missing" in res.text)

    touch_transcript(st.get(600))
    res2, _ = asyncio.run(run(cfg, st, st.get(600), 600))
    check("succeeds once the transcript is restored", not res2.is_error)


def test_timeout() -> None:
    print("\nharness: hung turn")
    stub = write_stub()
    os.environ["STUB_MODE"] = "hang"
    st = fresh_state()
    cfg = make_cfg(stub, timeout=2)
    rec = st.register(700, "hung")

    res, _ = asyncio.run(run(cfg, st, rec, 700))
    check("classified as a timeout", res.error_kind == "timeout", str(res.error_kind))
    check("reported as an error", res.is_error)
    check("bounded by TURN_TIMEOUT", res.duration_ms < 20000, f"{res.duration_ms}ms")
    check("message names the limit", "TURN_TIMEOUT" in res.text)


def test_large_events() -> None:
    print("\nharness: event larger than asyncio's default 64KB line limit")
    st = fresh_state()
    cfg = make_cfg(write_stub())
    os.environ["STUB_MODE"] = "big"
    rec = st.register(900, "big")

    res, _ = asyncio.run(run(cfg, st, rec, 900))
    check("succeeded", not res.is_error, f"{res.error_kind} {res.stderr_tail}")
    check("nothing dropped", res.dropped_events == 0, str(res.dropped_events))
    check("the whole 200KB answer survived", len(res.text) == 200000, f"got {len(res.text)}")


def test_partial_answer_is_an_error() -> None:
    print("\nharness: streamed text then nonzero exit")
    st = fresh_state()
    cfg = make_cfg(write_stub())
    os.environ["STUB_MODE"] = "partial_then_die"
    rec = st.register(910, "partial")

    res, _ = asyncio.run(run(cfg, st, rec, 910))
    # Delivering half an answer under a success footer is worse than saying the
    # turn died partway through.
    check("nonzero exit is an error even with text", res.is_error)
    check("exit code preserved", res.exit_code == 1, str(res.exit_code))


def test_classifiers_ignore_model_prose() -> None:
    print("\nharness: retry classifiers must read stderr, not the model's prose")
    st = fresh_state()
    cfg = make_cfg(write_stub())
    os.environ["STUB_MODE"] = "prose_about_session"
    rec = st.register(920, "prose")
    sid = rec["session_id"]

    res, events = asyncio.run(run(cfg, st, rec, 920))
    check("reported as an error", res.is_error)
    check("not misfiled as missing_transcript", res.error_kind != "missing_transcript",
          str(res.error_kind))
    # The brick: primed=True with no transcript behind it fails the hard gate on
    # every subsequent turn until someone runs !reset.
    check("channel not bricked by a false session-exists match",
          st.get(920)["primed"] is False)
    check("did not retry on prose", [k for k, _ in events].count("reset") == 0)
    check("session id untouched", st.get(920)["session_id"] == sid)


def test_wake_parsing() -> None:
    print("\nwake: duration and clock-time parsing")
    check("45s", wake_mod.parse_duration("45s") == 45)
    check("20m", wake_mod.parse_duration("20m") == 1200)
    check("2h", wake_mod.parse_duration("2h") == 7200)
    check("3d", wake_mod.parse_duration("3d") == 259200)
    check("1h30m compounds", wake_mod.parse_duration("1h30m") == 5400)
    check("whitespace tolerated", wake_mod.parse_duration(" 2 h ") == 7200)

    # Strict on purpose: a typo silently read as a partial duration is a wake
    # that fires at the wrong time and looks like the scheduler's fault.
    for bad in ("2huor", "", "abc", "20", "h", "-5m"):
        try:
            wake_mod.parse_duration(bad)
            check(f"rejects {bad!r}", False, "parsed instead of raising")
        except ValueError:
            check(f"rejects {bad!r}", True)

    from datetime import datetime
    from zoneinfo import ZoneInfo
    zone = ZoneInfo(wake_mod.tz())
    noon = datetime(2026, 6, 15, 12, 0, tzinfo=zone)

    at = wake_mod.parse_at("18:30", now=noon)
    check("18:30 from noon is today", datetime.fromtimestamp(at, zone).day == 15)
    check("18:30 lands at 18:30", datetime.fromtimestamp(at, zone).hour == 18)

    # The rollover is the point: asking for 08:00 at noon means tomorrow.
    at = wake_mod.parse_at("08:00", now=noon)
    check("08:00 from noon rolls to tomorrow", datetime.fromtimestamp(at, zone).day == 16)

    check("6:30pm parses", datetime.fromtimestamp(wake_mod.parse_at("6:30pm", now=noon), zone).hour == 18)
    at = wake_mod.parse_at("2026-09-20 18:30", now=noon)
    check("absolute date parses", datetime.fromtimestamp(at, zone).strftime("%Y-%m-%d %H:%M")
          == "2026-09-20 18:30")
    for bad in ("tomorrow", "25:00", ""):
        try:
            wake_mod.parse_at(bad, now=noon)
            check(f"rejects {bad!r}", False, "parsed instead of raising")
        except ValueError:
            check(f"rejects {bad!r}", True)

    check("human_delta rounds, not truncates", wake_mod.human_delta(7199.9) == "2h",
          wake_mod.human_delta(7199.9))
    # Single-unit rounding called 89 minutes "1h", which misinforms an agent
    # about how stale its own wake is.
    check("human_delta shows two units", wake_mod.human_delta(89 * 60) == "1h29m",
          wake_mod.human_delta(89 * 60))
    check("human_delta on days", wake_mod.human_delta(35 * 3600) == "1d11h",
          wake_mod.human_delta(35 * 3600))


def test_wake_jobs() -> None:
    print("\nwake: jobs.json")
    config.ensure_dirs()
    config.JOBS_FILE.unlink(missing_ok=True)
    now = int(time.time())

    a = wake_mod.add("general", "later", now + 3600)
    b = wake_mod.add("general", "sooner", now + 60)
    check("ids are unique", a["id"] != b["id"])
    check("stored sorted by time", [j["prompt"] for j in wake_mod.listing()] == ["sooner", "later"])
    check("listing filters by channel", wake_mod.listing("nope") == [])

    check("cancel returns the job", wake_mod.cancel(a["id"])["prompt"] == "later")
    check("cancel removes it", [j["id"] for j in wake_mod.listing()] == [b["id"]])
    check("cancel of a missing id is None", wake_mod.cancel("zzzz") is None)

    print("\nwake: due selection")
    config.JOBS_FILE.unlink(missing_ok=True)
    wake_mod.add("general", "past", now - 30)
    wake_mod.add("general", "exactly now", now)
    wake_mod.add("general", "future", now + 3600)
    due = wake_mod.take_due(now)
    check("past and exactly-now fire", sorted(j["prompt"] for j in due) == ["exactly now", "past"])
    check("future is left alone", [j["prompt"] for j in wake_mod.listing()] == ["future"])
    check("take_due is idempotent", wake_mod.take_due(now) == [])


def test_wake_concurrent_writers() -> None:
    print("\nwake: two processes writing jobs.json at once (the flock case)")
    config.ensure_dirs()
    config.JOBS_FILE.unlink(missing_ok=True)

    # Real subprocesses, not threads -- flock is a kernel lock between
    # processes, and a threaded test would pass even without it.
    script = TMP / "adder.py"
    script.write_text(
        "import sys, time\n"
        f"sys.path.insert(0, {str(ROOT / 'skills' / 'wake')!r})\n"
        "import wake\n"
        "wake.add('general', sys.argv[1], int(time.time()) + 3600)\n",
        encoding="utf-8",
    )
    procs = [
        subprocess.Popen([sys.executable, str(script), f"job{i}"])
        for i in range(12)
    ]
    for p in procs:
        p.wait()

    jobs = wake_mod.listing()
    check("all 12 concurrent writes survived", len(jobs) == 12, f"got {len(jobs)}")
    check("no id collisions", len({j["id"] for j in jobs}) == len(jobs))
    check("file is still valid JSON", isinstance(json.loads(config.JOBS_FILE.read_text()), list))


class _FakeChannel:
    def __init__(self, cid: int, name: str) -> None:
        self.id, self.name, self.sent = cid, name, []

    async def send(self, content):
        self.sent.append(content)


class _FakeHistory:
    def __init__(self) -> None:
        self.inbound: list[tuple] = []

    def log_inbound(self, channel_id, name, rec, author, text):
        self.inbound.append((channel_id, name, author, text))


class _FakeBot:
    """Just enough surface for Spool: a guild that resolves channels, a queue."""

    def __init__(self, cfg, st, channels) -> None:
        self.cfg, self.state, self.draining = cfg, st, False
        self.channels = channels
        self.queued: list[dict] = []
        self.history = _FakeHistory()

    def get_guild(self, gid):
        return self if gid == self.cfg.guild_id else None

    @property
    def text_channels(self):
        return list(self.channels.values())

    def get_channel(self, cid):
        return self.channels.get(cid)

    def _queue_for(self, cid):
        bot = self

        class _Q:
            def put_nowait(self, item):
                bot.queued.append(item)

        return _Q()


def _spool_fixture():
    config.ensure_dirs()
    for leftover in config.INJECT_DIR.iterdir():
        leftover.unlink()
    os.environ.update({"DISCORD_BOT_TOKEN": "x", "DISCORD_GUILD_ID": "7",
                       "DISCORD_ALLOWED_USER_IDS": "42"})
    cfg = config.Config()
    st = fresh_state()
    channel = _FakeChannel(555, "general")
    bot = _FakeBot(cfg, st, {555: channel})
    return spool_mod.Spool(bot), bot, channel


def test_inject_spool() -> None:
    print("\ninject: spool lifecycle")
    spool, bot, channel = _spool_fixture()

    spool_mod.write_inject("general", "hello there", "cron")
    asyncio.run(spool._drain_inject_dir())
    check("file consumed", list(config.INJECT_DIR.glob("*.json")) == [])
    check("posted to the channel", any("hello there" in s for s in channel.sent), str(channel.sent))
    check("turn enqueued", len(bot.queued) == 1, str(bot.queued))
    check("label becomes the speaker", bot.queued[0]["author"] == "cron")
    check("text preserved", bot.queued[0]["text"] == "hello there")
    check("channel registered in state", bot.state.get(555) is not None)
    # Without this the history shows an answer with nothing that provoked it.
    check("injected turn logged inbound to history", bot.history.inbound == [
        (555, "general", "cron", "hello there")], str(bot.history.inbound))

    print("\ninject: resolution by id")
    spool_mod.write_inject("555", "by numeric id", "test")
    asyncio.run(spool._drain_inject_dir())
    check("numeric channel id resolves", bot.queued[-1]["text"] == "by numeric id")

    print("\ninject: failure modes")
    bad = config.INJECT_DIR / "9-bad.json"
    bad.write_text("{not json", encoding="utf-8")
    asyncio.run(spool._drain_inject_dir())
    check("bad JSON renamed .failed", (config.INJECT_DIR / "9-bad.json.failed").exists())

    spool_mod.write_inject("does-not-exist", "nowhere", "test")
    asyncio.run(spool._drain_inject_dir())
    # Load-bearing: Phase 4's collab confirms delivery by watching for this.
    check("unresolvable channel renamed .failed",
          len(list(config.INJECT_DIR.glob("*.json.failed"))) == 2)

    missing = config.INJECT_DIR / "9-empty.json"
    missing.write_text(json.dumps({"channel": "general"}), encoding="utf-8")
    asyncio.run(spool._drain_inject_dir())
    check("missing text renamed .failed", (config.INJECT_DIR / "9-empty.json.failed").exists())

    print("\ninject: half-written files and ordering")
    for f in config.INJECT_DIR.glob("*.failed"):
        f.unlink()
    (config.INJECT_DIR / "1-partial.json.tmp").write_text('{"channel":"gen', encoding="utf-8")
    before = len(bot.queued)
    asyncio.run(spool._drain_inject_dir())
    check(".tmp is never read", len(bot.queued) == before)
    check(".tmp left in place", (config.INJECT_DIR / "1-partial.json.tmp").exists())
    (config.INJECT_DIR / "1-partial.json.tmp").unlink()

    for i, word in enumerate(["first", "second", "third"]):
        (config.INJECT_DIR / f"{1000 + i}-x.json").write_text(
            json.dumps({"channel": "general", "text": word, "label": "t"}), encoding="utf-8"
        )
    bot.queued.clear()
    asyncio.run(spool._drain_inject_dir())
    check("spool is FIFO by filename",
          [q["text"] for q in bot.queued] == ["first", "second", "third"],
          str([q["text"] for q in bot.queued]))


def test_wake_firing() -> None:
    print("\nwake: firing, lateness, and the drop cap")
    spool, bot, channel = _spool_fixture()
    config.JOBS_FILE.unlink(missing_ok=True)
    now = int(time.time())

    wake_mod.add("general", "on time", now - 5)
    wake_mod.add("general", "not yet", now + 3600)
    spool._fire_due()
    files = sorted(config.INJECT_DIR.glob("*.json"))
    check("due job wrote one inject file", len(files) == 1, str(files))
    payload = json.loads(files[0].read_text())
    check("no late prefix when on time", payload["text"] == "on time", payload["text"])
    check("label defaults to wake", payload["label"] == "wake")
    check("future job untouched", [j["prompt"] for j in wake_mod.listing()] == ["not yet"])

    for f in config.INJECT_DIR.glob("*.json"):
        f.unlink()
    config.JOBS_FILE.unlink(missing_ok=True)

    # The case that matters on a laptop: it slept through the scheduled time.
    wake_mod.add("general", "slept through this", now - 3 * 3600)
    spool._fire_due()
    payload = json.loads(next(config.INJECT_DIR.glob("*.json")).read_text())
    check("late job fires", "slept through this" in payload["text"])
    check("late job is labelled late", payload["text"].startswith("(late by 3h)"), payload["text"])

    for f in config.INJECT_DIR.glob("*.json"):
        f.unlink()
    config.JOBS_FILE.unlink(missing_ok=True)

    wake_mod.add("general", "stale", now - 13 * 3600)
    spool._fire_due()
    files = list(config.INJECT_DIR.glob("*.json"))
    check("dropped job is removed from jobs.json", wake_mod.listing() == [])
    # A silently dropped wake stops an unattended workflow with no visible
    # cause: the agent that booked it and the operator both see nothing.
    check("the drop is announced, not silent", len(files) == 1, str(files))
    notice = json.loads(files[0].read_text())
    check("notice says it was dropped", "dropped" in notice["text"])
    check("notice carries the original text", "stale" in notice["text"])
    check("notice tells the agent not to act on it", "Do not act on it" in notice["text"])
    check("notice is labelled as coming from the rig", notice["label"] == "rig")

    for f in config.INJECT_DIR.glob("*.json"):
        f.unlink()

    print("\nwake: survives a daemon restart")
    config.JOBS_FILE.unlink(missing_ok=True)
    wake_mod.add("general", "after restart", now + 1)
    reloaded = spool_mod.Spool(bot)          # a fresh Spool, as on restart
    time.sleep(1.1)
    reloaded._fire_due()
    check("job booked before the restart still fires",
          any("after restart" in json.loads(f.read_text())["text"]
              for f in config.INJECT_DIR.glob("*.json")))


# --- bot.py and commands.py -------------------------------------------------
#
# These two files own the entire Discord surface -- the authorization gate, the
# coalescing loop, drain, and every ! command -- and had no coverage at all, not
# even import coverage. discord.Client constructs fine offline, so the only
# stubs needed are the message/channel objects.


class _FakeAuthor:
    def __init__(self, uid: int, name: str = "avalon", bot: bool = False) -> None:
        self.id, self.display_name, self.bot = uid, name, bot

    def __str__(self) -> str:
        return self.display_name


class _FakeGuild:
    def __init__(self, gid: int) -> None:
        self.id, self.name = gid, "test"


class _FakeMessage:
    def __init__(self, content, channel, author, guild) -> None:
        self.content, self.channel, self.author = content, channel, author
        self.guild, self.attachments = guild, []


def _make_bot(allowed=(42,), ignore=""):
    os.environ.update({
        "DISCORD_BOT_TOKEN": "x", "DISCORD_GUILD_ID": "7",
        "DISCORD_ALLOWED_USER_IDS": " ".join(str(u) for u in allowed),
        "DISCORD_IGNORE_CHANNELS": ignore,
    })
    from daemon.bot import RigBot
    cfg = config.Config()
    bot = RigBot(cfg, fresh_state(), None)
    channel = _FakeChannel(555, "general")
    guild = _FakeGuild(7)
    bot.get_channel = lambda cid: channel if cid == 555 else None
    return bot, channel, guild


def _msg(bot, channel, guild, content, uid=42, name="avalon", is_bot=False):
    return _FakeMessage(content, channel, _FakeAuthor(uid, name, is_bot), guild)


def test_authorization_gate() -> None:
    print("\nbot: the authorization gate")

    async def scenario():
        bot, channel, guild = _make_bot()
        # The allowlist is the ONLY containment for a daemon running with
        # --dangerously-skip-permissions, and nothing tested it.
        await bot.on_message(_msg(bot, channel, guild, "hi", uid=999, name="stranger"))
        check("non-allowlisted user is dropped", bot.queues == {} and channel.sent == [])

        await bot.on_message(_msg(bot, channel, guild, "hi", uid=42, is_bot=True))
        check("a bot author is ignored", bot.queues == {})

        outsider = _FakeMessage("hi", channel, _FakeAuthor(42), _FakeGuild(999))
        await bot.on_message(outsider)
        check("wrong guild is ignored", bot.queues == {})

        dm = _FakeMessage("hi", channel, _FakeAuthor(42), None)
        await bot.on_message(dm)
        check("a DM (no guild) is ignored", bot.queues == {})

        await bot.on_message(_msg(bot, channel, guild, "   "))
        check("empty content is ignored", bot.queues == {})

        await bot.on_message(_msg(bot, channel, guild, "hello"))
        check("an allowlisted user is queued", bot.queues[555].qsize() == 1)
        for task in bot.workers.values():
            task.cancel()

        # DISCORD_IGNORE_CHANNELS
        bot2, ch2, g2 = _make_bot(ignore="general")
        await bot2.on_message(_msg(bot2, ch2, g2, "hello"))
        check("an ignored channel never runs a turn", bot2.queues == {})
        for task in bot2.workers.values():
            task.cancel()

    asyncio.run(scenario())


def test_coalescing_and_raw() -> None:
    print("\nbot: coalescing and raw-item ordering")

    async def scenario():
        bot, channel, guild = _make_bot()
        turns: list[list[dict]] = []

        async def fake_run_turn(channel_id, items):
            turns.append(list(items))
            await asyncio.sleep(0)

        bot._run_turn = fake_run_turn
        q = bot._queue_for(555)
        for word in ("one", "two", "three"):
            q.put_nowait({"author": "avalon", "text": word, "channel": channel})
        await asyncio.sleep(0.05)
        check("three messages coalesce into one turn", len(turns) == 1, str(turns))
        check("all three lines are present",
              [i["text"] for i in turns[0]] == ["one", "two", "three"])

        # A raw item must run alone, and must not be starved by later traffic.
        turns.clear()
        q.put_nowait({"author": "a", "text": "before", "channel": channel})
        q.put_nowait({"author": "a", "text": "/compact", "channel": channel, "raw": True})
        q.put_nowait({"author": "a", "text": "after", "channel": channel})
        await asyncio.sleep(0.05)
        shapes = [[i["text"] for i in t] for t in turns]
        check("raw item is not absorbed into a batch", ["/compact"] in shapes, str(shapes))
        # Re-queuing it at the tail put it behind everything that arrived during
        # the turn, so under steady traffic !compact never ran.
        check("raw item runs before later messages",
              shapes.index(["/compact"]) < shapes.index(["after"]), str(shapes))
        for task in bot.workers.values():
            task.cancel()

    asyncio.run(scenario())


def test_drain_waits_for_claimed_work() -> None:
    print("\nbot: drain")

    async def scenario():
        bot, channel, guild = _make_bot()
        released = asyncio.Event()

        async def slow_turn(channel_id, items):
            await released.wait()

        bot._run_turn = slow_turn
        bot.close = lambda: asyncio.sleep(0)          # no gateway to close
        bot._queue_for(555).put_nowait({"author": "a", "text": "x", "channel": channel})
        await asyncio.sleep(0.05)
        # The invariant that only a comment guarded: busy is claimed
        # synchronously, so a shutdown in the spawn window cannot see "0 in
        # flight" and close the client out from under a live message.
        check("claimed work marks the channel busy", 555 in bot.busy)

        drain = asyncio.create_task(bot.drain(timeout=5))
        await asyncio.sleep(0.1)
        check("drain waits while a turn is claimed", not drain.done())
        released.set()
        await asyncio.wait_for(drain, timeout=5)
        check("drain returns once the turn finishes", drain.done())
        check("busy is cleared", bot.busy == set())
        for task in bot.workers.values():
            task.cancel()

    asyncio.run(scenario())


def test_commands() -> None:
    print("\ncommands: the ! surface")
    from daemon import commands as cmd_mod

    async def scenario():
        bot, channel, guild = _make_bot()
        config.JOBS_FILE.unlink(missing_ok=True)

        async def handle(text):
            channel.sent.clear()
            await cmd_mod.handle(bot, _msg(bot, channel, guild, text), text)
            return " ".join(channel.sent)

        check("!ping reports load", "up" in await handle("!ping"))
        check("!ctx shows the session", "session" in await handle("!ctx"))

        out = await handle("!model sonnet")
        check("!model switches", "sonnet" in out and bot.state.get(555)["model"] == "sonnet")
        check("!model clears the resolution cache",
              bot.state.get(555).get("model_resolved") is None)
        check("!model rejects an unknown alias", "unknown alias" in await handle("!model nope"))

        old = bot.state.get(555)["session_id"]
        await handle("!reset")
        check("!reset mints a new session", bot.state.get(555)["session_id"] != old)

        check("!abort with nothing running says so", "nothing running" in await handle("!abort"))
        check("unknown command is reported", "unknown command" in await handle("!nope"))

        # !wake, end to end through the real jobs.json
        out = await handle("!wake in 2h check the build")
        check("!wake books a job", "⏰" in out, out)
        jobs = wake_mod.listing(str(555))
        check("the job is keyed by channel id, not name", len(jobs) == 1, str(jobs))
        check("the prompt is stored whole", jobs[0]["prompt"] == "check the build")

        out = await handle("!wake list")
        check("!wake list shows it", jobs[0]["id"] in out)

        out = await handle(f"!wake cancel {jobs[0]['id']}")
        check("!wake cancel removes it", "cancelled" in out and wake_mod.listing(str(555)) == [])
        check("!wake cancel of a missing id says so", "no wake job" in await handle("!wake cancel zzzz"))

        # "6:30 pm" arrives as two tokens; parsing only the first booked 06:30.
        await handle("!wake at 6:30 pm stand up")
        job = wake_mod.listing(str(555))[0]
        hour = int(wake_mod.local(job["at"]).split()[-2].split(":")[0])
        check("'6:30 pm' books the evening, not the morning", hour == 18, str(hour))
        check("the meridiem does not leak into the prompt",
              job["prompt"] == "stand up", job["prompt"])
        check("bad duration is reported", "bad duration" in await handle("!wake in 2huor x"))

    asyncio.run(scenario())


def test_abort_identity() -> None:
    print("\nbot: abort has turn identity")

    class _DeadProc:
        returncode = 0

    class _LiveProc:
        returncode = None
        pid = os.getpid()

    async def scenario():
        bot, channel, guild = _make_bot()
        # Aborting a turn that already finished must not claim success -- doing
        # so also threw away the completed answer in _deliver.
        bot.running[555] = (1, _DeadProc())
        check("abort of a finished turn reports nothing to kill", await bot.abort(555) is False)
        check("a finished turn is not marked aborted", 555 not in bot.aborted)

        bot.running[555] = (2, _LiveProc())
        bot.harness.kill = lambda proc: asyncio.sleep(0)
        check("abort of a live turn succeeds", await bot.abort(555) is True)
        check("the abort records which turn it killed", bot.aborted[555] == 2)

    asyncio.run(scenario())


def test_primed_commits_at_init() -> None:
    print("\nharness: primed commits at init, not at result")
    st = fresh_state()
    cfg = make_cfg(write_stub())
    os.environ["STUB_MODE"] = "die_after_init"
    rec = st.register(930, "died")

    # The stub emits init and then dies with no result. If primed were committed
    # at result instead, the next turn would retry --session-id against a uuid
    # whose transcript already exists and fail forever. The happy-path stub
    # cannot prove this -- it emits both events.
    res, _ = asyncio.run(run(cfg, st, rec, 930))
    check("the turn is reported as failed", res.is_error)
    check("primed was committed at init anyway", st.get(930)["primed"] is True)

    touch_transcript(st.get(930))
    os.environ["STUB_MODE"] = "ok"
    res2, _ = asyncio.run(run(cfg, st, st.get(930), 930))
    check("the next turn resumes rather than erroring", res2.resumed is True)
    check("and succeeds", not res2.is_error, f"{res2.error_kind} {res2.stderr_tail}")


def test_reset_midturn_cannot_brick_the_channel() -> None:
    print("\nharness: !reset racing a live turn")
    st = fresh_state()
    cfg = make_cfg(write_stub())
    os.environ["STUB_MODE"] = "ok"
    rec = st.register(940, "raced")
    old_session = rec["session_id"]

    class _Racing(harness_mod.ClaudeHarness):
        async def _read_stdout(self, proc, channel_id, session_id, result, on_event):
            # Simulate `!reset` landing mid-turn: it mutates the very dict the
            # turn is holding. Reading rec["session_id"] lazily made the guard
            # compare a value to itself, marking a uuid primed whose transcript
            # will never exist -- bricking the channel on the next turn.
            self.state.reset_session(940)
            return await super()._read_stdout(proc, channel_id, session_id, result, on_event)

    async def go():
        h = _Racing(cfg, st)

        async def on_event(kind, payload):
            pass

        return await h.run_turn(channel_id=940, rec=rec, prompt="hi", on_event=on_event)

    asyncio.run(go())
    new_session = st.get(940)["session_id"]
    check("the reset took effect", new_session != old_session)
    check("the new session was NOT marked primed", st.get(940)["primed"] is False)

    # The proof: the next turn starts cleanly instead of dying on the hard gate.
    res, _ = asyncio.run(run(cfg, st, st.get(940), 940))
    check("the channel still works after the race", not res.is_error,
          f"{res.error_kind}: {res.text[:120]}")


def test_oversized_line_is_not_silent() -> None:
    print("\nharness: a line over the stream limit")
    st = fresh_state()
    cfg = make_cfg(write_stub())
    os.environ["STUB_MODE"] = "big"
    rec = st.register(950, "oversized")

    original = harness_mod._STREAM_LIMIT
    harness_mod._STREAM_LIMIT = 4096          # the 200KB result now overruns
    try:
        res, _ = asyncio.run(run(cfg, st, rec, 950))
    finally:
        harness_mod._STREAM_LIMIT = original

    check("the drop is counted", res.dropped_events > 0, str(res.dropped_events))
    # Without this the turn was delivered under a success footer reading
    # "(no text output)" -- work that appears to succeed while doing nothing.
    check("the turn is reported as an error", res.is_error)
    check("classified as truncated", res.error_kind == harness_mod.KIND_TRUNCATED,
          str(res.error_kind))


def test_exit_zero_without_result() -> None:
    print("\nharness: clean exit that never reports a result")
    st = fresh_state()
    cfg = make_cfg(write_stub())
    os.environ["STUB_MODE"] = "silent_success"
    rec = st.register(960, "silent")

    res, _ = asyncio.run(run(cfg, st, rec, 960))
    check("not reported as a success", res.is_error)
    check("classified as no_result", res.error_kind == harness_mod.KIND_NO_RESULT,
          str(res.error_kind))
    check("exit code was actually zero", res.exit_code == 0)


def test_child_env_has_no_secrets() -> None:
    print("\nharness: the child environment")
    cfg = make_cfg(write_stub())
    os.environ["DISCORD_BOT_TOKEN"] = "super-secret-token"
    env = harness_mod.ClaudeHarness(cfg, fresh_state())._child_env()
    # A turn ingests untrusted web pages and repos; one prompt injection running
    # `printenv` would otherwise hand over the bot token.
    for secret in ("DISCORD_BOT_TOKEN", "DISCORD_GUILD_ID", "DISCORD_ALLOWED_USER_IDS"):
        check(f"{secret} is stripped", secret not in env)
    check("TZ is still passed through", env.get("TZ") == cfg.tz)


def test_bash_transcript_encoding_parity() -> None:
    print("\nbin/rig: transcript encoding matches the Python implementation")
    # cmd_backup reimplements the encoding in bash (tr '/.' '--'). If the two
    # ever diverge, --resume keeps working while hourly backups silently contain
    # zero transcripts.
    for path in ("/opt/agent-rig/workdirs", "/opt/agent-rig/workdirs/a.b",
                 "/tmp/rig-test/x.y.z"):
        out = subprocess.run(
            ["bash", "-c", f"printf '%s' {path!r} | tr '/.' '--'"],
            capture_output=True, text=True,
        ).stdout
        check(f"bash and python agree on {path}",
              out == state_mod.encode_workdir(path), f"{out!r} != {state_mod.encode_workdir(path)!r}")


def test_backup_restore_round_trip() -> None:
    print("\nbin/rig: backup and restore actually round-trip")
    # This is the recovery path the daemon's own missing-transcript error points
    # the operator at ("Restore it from `rig backup`"), and it is the only
    # protection against disk loss or a mistaken !reset. A broken restore is
    # discovered at the exact moment it is already too late, so it gets a real
    # round trip rather than trust.
    st = fresh_state()
    rec = st.register(970, "recovery")
    transcript = touch_transcript(rec, '{"marker":"irreplaceable"}\n')
    config.JOBS_FILE.unlink(missing_ok=True)
    wake_mod.add(str(970), "a booked continuation", int(time.time()) + 9999)

    env = {
        **os.environ,
        "RIG_ROOT": str(config.RIG_ROOT),
        "RIG_HOME": os.environ["HOME"],
        "PATH": os.environ.get("PATH", ""),
    }
    run_rig = lambda *a: subprocess.run(
        ["bash", str(ROOT / "bin" / "rig"), *a], env=env, capture_output=True, text=True
    )

    out = run_rig("backup")
    check("backup exits 0", out.returncode == 0, out.stdout + out.stderr)

    archives = sorted((config.RIG_ROOT / "state" / "backups").glob("rig-*.tar.gz"))
    check("an archive was written", len(archives) >= 1)
    if not archives:
        return
    names = subprocess.run(
        ["tar", "tzf", str(archives[-1])], capture_output=True, text=True
    ).stdout
    check("the archive contains state.json", "state/state.json" in names)
    check("the archive contains jobs.json", "state/jobs.json" in names, names)
    # The whole point: an archive with no transcripts is a backup of nothing.
    check("the archive contains the session transcript",
          transcript.name in names, names)

    # Now destroy what the rig cannot rebuild, and get it back.
    transcript.unlink()
    config.STATE_FILE.unlink()
    config.JOBS_FILE.unlink(missing_ok=True)

    out = run_rig("restore", str(archives[-1]))
    check("restore exits 0", out.returncode == 0, out.stdout + out.stderr)
    check("the transcript is back", transcript.exists())
    check("its contents survived intact",
          "irreplaceable" in transcript.read_text() if transcript.exists() else False)
    check("state.json is back", config.STATE_FILE.exists())
    restored = state_mod.State()
    check("the channel's session id survived",
          (restored.get(970) or {}).get("session_id") == rec["session_id"])
    check("the booked wake survived",
          [j["prompt"] for j in wake_mod.listing()] == ["a booked continuation"])


def test_backup_reports_a_degraded_archive() -> None:
    print("\nbin/rig: an incomplete backup must not report success")
    # An hourly job that exits 0 while archiving nothing is how you end up with
    # months of empty backups and no idea.
    empty = TMP / "empty-home"
    (empty / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
    out = subprocess.run(
        ["bash", str(ROOT / "bin" / "rig"), "backup"],
        env={**os.environ, "RIG_ROOT": str(config.RIG_ROOT), "RIG_HOME": str(empty)},
        capture_output=True, text=True,
    )
    check("exits nonzero when no transcripts were found", out.returncode != 0,
          f"rc={out.returncode}")
    check("and says why", "INCOMPLETE" in out.stderr or "no transcript" in out.stderr,
          out.stderr)


def test_compaction_detection() -> None:
    print("\ncompaction detection")
    st = fresh_state()
    rec = st.register(800, "compacted")
    check("no transcript means no compaction",
          harness_mod.detect_compaction(rec["workdir"], rec["session_id"]) is False)
    touch_transcript(rec, '{"type":"user"}\n')
    check("ordinary transcript is not compacted",
          harness_mod.detect_compaction(rec["workdir"], rec["session_id"]) is False)
    touch_transcript(rec, '{"type":"user"}\n{"isCompactSummary":true}\n')
    check("compaction marker detected",
          harness_mod.detect_compaction(rec["workdir"], rec["session_id"]) is True)


def test_config_validation() -> None:
    print("\nconfig validation")
    saved = {k: os.environ.get(k) for k in
             ("DISCORD_BOT_TOKEN", "DISCORD_GUILD_ID", "DISCORD_ALLOWED_USER_IDS", "DEFAULT_MODEL")}
    for k in saved:
        os.environ.pop(k, None)
    problems = config.Config().problems()
    check("empty config is refused", len(problems) >= 3, str(problems))
    check("names the missing token", any("TOKEN" in p for p in problems))
    check("names the missing allowlist", any("ALLOWED_USER_IDS" in p for p in problems))

    os.environ.update({"DISCORD_BOT_TOKEN": "x", "DISCORD_GUILD_ID": "1",
                       "DISCORD_ALLOWED_USER_IDS": "42", "DEFAULT_MODEL": "opus"})
    check("valid config passes", config.Config().problems() == [])
    cfg = config.Config()
    check("allowlist parsed", cfg.allowed_user_ids == {42})

    os.environ["DEFAULT_MODEL"] = "nope"
    check("unknown DEFAULT_MODEL is refused",
          any("not a known alias" in p for p in config.Config().problems()))

    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    os.environ["DEFAULT_MODEL"] = "opus"


def test_env_loading() -> None:
    print("\nagent.env loading")
    p = TMP / "sample.env"
    p.write_text('# comment\nFOO_TEST=bar\nQUOTED_TEST="baz qux"\nEMPTY_TEST=\n', encoding="utf-8")
    os.environ["FOO_TEST"] = "already-set"
    config.load_env(p)
    check("real environment wins over the file", os.environ["FOO_TEST"] == "already-set")
    check("quotes stripped", os.environ["QUOTED_TEST"] == "baz qux")
    check("comment lines create no variables", "# comment" not in os.environ)
    check("empty values are kept as empty, not skipped", os.environ.get("EMPTY_TEST") == "")


def main() -> int:
    print(f"agent-rig tests  (tmp: {TMP})")
    try:
        test_splitter()
        test_state()
        test_env_loading()
        test_config_validation()
        test_happy_path()
        test_session_exists_retry()
        test_model_fallback()
        test_missing_transcript()
        test_timeout()
        test_wake_parsing()
        test_wake_jobs()
        test_wake_concurrent_writers()
        test_inject_spool()
        test_wake_firing()
        test_large_events()
        test_partial_answer_is_an_error()
        test_classifiers_ignore_model_prose()
        test_authorization_gate()
        test_coalescing_and_raw()
        test_drain_waits_for_claimed_work()
        test_commands()
        test_abort_identity()
        test_primed_commits_at_init()
        test_reset_midturn_cannot_brick_the_channel()
        test_oversized_line_is_not_silent()
        test_exit_zero_without_result()
        test_child_env_has_no_secrets()
        test_bash_transcript_encoding_parity()
        test_backup_restore_round_trip()
        test_backup_reports_a_degraded_archive()
        test_compaction_detection()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
