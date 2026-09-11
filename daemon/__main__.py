"""Entry point: `python -m daemon`."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from . import config, state as state_mod
from .bot import RigBot
from .history import History


def setup_logging(level: str) -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(config.LOG_DIR / "daemon.log", encoding="utf-8"))
    except OSError:
        pass  # stdout is enough; the service captures it anyway
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
    )
    logging.getLogger("discord").setLevel(logging.WARNING)


async def amain() -> int:
    config.load_env()
    config.ensure_dirs()
    cfg = config.Config()
    setup_logging(cfg.log_level)
    log = logging.getLogger("rig")

    problems = cfg.problems()
    if problems:
        log.error("refusing to start, %d configuration problem(s):", len(problems))
        for p in problems:
            log.error("  - %s", p)
        log.error("edit %s (see docs/SETUP.md)", config.ENV_FILE)
        return 2

    log.info("rig root %s", config.RIG_ROOT)
    st = state_mod.State()
    history = History(config.HISTORY_DB) if cfg.history_enabled else None
    bot = RigBot(cfg, st, history)

    loop = asyncio.get_running_loop()
    stopping = False

    drain_task: asyncio.Task | None = None

    def request_drain(signame: str) -> None:
        nonlocal stopping, drain_task
        if stopping:
            # A second signal means "stop waiting". loop.stop() inside
            # asyncio.run() raises RuntimeError and skips every cleanup path,
            # orphaning live `claude` process groups -- so kill the children
            # first, then exit hard and deliberately.
            log.warning("%s again — killing %d live turn(s) and exiting",
                        signame, len(bot.running))
            for proc in list(bot.running.values()):
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, AttributeError):
                    pass
            os._exit(1)
        stopping = True
        log.info("%s received; draining in-flight turns", signame)
        # Hold a reference: the event loop keeps tasks only weakly.
        drain_task = loop.create_task(bot.drain())

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, request_drain, sig.name)
        except NotImplementedError:
            pass

    try:
        await bot.start(cfg.discord_token)
    except Exception:
        log.exception("bot stopped with an error")
        return 1
    finally:
        if history:
            history.close()
    return 0


def main() -> int:
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
