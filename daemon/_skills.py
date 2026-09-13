"""Load a skill module by path, without touching sys.path.

Some skills are both a CLI an agent shells out to and a library the daemon
imports -- `wake` so `!wake` and `wake.py add` cannot drift, `inject` so there
is exactly one implementation of the spool file format.

The obvious way to do that is `sys.path.insert(0, skills/wake)`, and it was how
this worked. It costs more than it looks:

  * That directory then shadows stdlib and site-packages for the entire daemon.
    A file named `json.py` or `time.py` dropped into a skill folder -- by you,
    by an agent writing a helper -- silently breaks unrelated code.
  * The shadowing grows with every skill added.
  * If `skills/` moves, the failure is an ImportError deep inside
    `__main__ -> bot -> spool`, nowhere near the path that actually broke.

importlib loads the file directly: no global state, and a missing skill fails
at the load call with a message naming the path.
"""

from __future__ import annotations

import importlib.util
import sys
from types import ModuleType

from . import config

_loaded: dict[str, ModuleType] = {}


def load(skill: str, module: str | None = None) -> ModuleType:
    """Import `skills/<skill>/<module>.py`. Cached, like a normal import."""
    module = module or skill
    key = f"{skill}/{module}"
    if key in _loaded:
        return _loaded[key]

    path = config.REPO_DIR / "skills" / skill / f"{module}.py"
    if not path.exists():
        raise RuntimeError(
            f"the daemon needs {path}, which does not exist. "
            f"If skills/ moved, the daemon cannot start until REPO_DIR points at it."
        )

    spec = importlib.util.spec_from_file_location(f"rig_skill_{skill}_{module}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    mod = importlib.util.module_from_spec(spec)
    # Registered under a namespaced key so a skill named `json` cannot collide
    # with the real one, while still letting dataclasses/pickle find it.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    _loaded[key] = mod
    return mod
