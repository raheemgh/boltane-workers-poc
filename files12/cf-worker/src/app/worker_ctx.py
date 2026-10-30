"""
Carries the Cloudflare request's own `ctx` (the object with waitUntil) from the Worker entrypoint to the route handlers.

Why this exists (Stage 6, measured on the deployed probe Worker, not assumed):
  * FastAPI BackgroundTasks keep the HTTP response waiting until the task ends (5.7 s / 40.9 s / 70.0 s for tasks of
    5 / 40 / 70 s), so Meta's webhook would wait for the whole AI pipeline.
  * ctx.waitUntil returns the response at once and keeps the work alive, but only up to about 30 s after the response
    (a 28 s task finished, a 35 s task was cancelled).
  * asgi.fetch(app, request, env) puts only `env` into the ASGI scope, so the ctx has to travel inside it. The wrapper
    below is the variant of the probe that needs no module-level state: 19 of 20 overlapping requests were verified
    complete and none was lost or errored (the 20th ran in an isolate that never answered again, so it has no verdict).

Pure Python on purpose: no `workers` import here, so the offline tests can load it.
"""


class EnvWithCtx:
    """The Worker `env` plus the current request's ctx. Every other attribute is forwarded to the real env."""

    def __init__(self, env, ctx):
        self._env = env
        self.worker_ctx = ctx

    def __getattr__(self, name):
        # Only called for names not set in __init__ (i.e. everything except worker_ctx and _env).
        if name == "_env":
            raise AttributeError(name)
        return getattr(self._env, name)


def get_worker_ctx(scope):
    """The request's ctx from an ASGI scope, or None when the app is not running inside a Worker (tests, Render)."""
    env = (scope or {}).get("env")
    try:
        return getattr(env, "worker_ctx", None)
    except Exception:  # noqa: BLE001 - a foreign env object that raises on unknown names simply means "no ctx"
        return None
