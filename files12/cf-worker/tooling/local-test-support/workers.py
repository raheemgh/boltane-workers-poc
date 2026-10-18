"""
LOCAL TEST-ONLY STUB for the `workers` module — never vendored, never
deployed. Not on the real dependency list in pyproject.toml.

`from workers import env` only works for real inside a running Worker
(workerd — either deployed, or local via `pywrangler dev`, which also
runs a real workerd instance, not plain CPython). Rahim's existing
test suite (test_package_system.py, test_usage_protection.py) runs as
plain `python test_x.py`, though — no workerd involved — and one test
(test_every_openrouter_call_sets_max_output_tokens) calls call_ai()
directly, for real, which now calls get_settings() -> `from workers
import env`. Without SOME stand-in for that import, that one test
can't even be collected in a plain interpreter.

This file is that stand-in, for local test runs only:
`PYTHONPATH=tooling/local-test-support:src python test_x.py` (put it
BEFORE src on PYTHONPATH so `import workers` resolves here rather
than failing, or resolving to whatever workers-runtime-sdk's own
package puts on disk for editor/type-checking purposes — untested by
me, and not something a plain CPython process needs to work either
way).

Backed by os.environ specifically because both existing test files
already do `os.environ.setdefault("SUPABASE_URL", ...)` etc. before
importing anything under `app` — this stub means those same lines,
UNCHANGED, are what feeds get_settings() its values in a local test
run, exactly standing in for what `.dev.vars` feeds it under a real
`pywrangler dev`.
"""

import os


class _Env:
    def __getattr__(self, name):
        # Real attribute-access behaviour against a Workers env
        # binding: a set var/secret returns its string value; an
        # unset one raises (which is what lets getattr(env, "X", None)
        # — used for the one optional field, OPENROUTER_SOCKS5_PROXY —
        # correctly fall back to the default instead of silently
        # returning something truthy).
        try:
            return os.environ[name]
        except KeyError:
            raise AttributeError(name) from None


env = _Env()
