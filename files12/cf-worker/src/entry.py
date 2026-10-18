"""
Worker entry module — the file wrangler.jsonc's "main" points at.

Deliberately a one-line shim. The real code lives in the `app` package,
laid out exactly like the Render version (app/main.py, app/routers/,
app/services/), so imports such as `app.services.token_limits` are
unchanged. Wrangler treats src/ (the directory of this file) as the
module root, which is why this file sits at src/ and not inside app/.

The Workers runtime looks for the entrypoint class named `Default` in
this module's namespace.
"""

from app.main import Default  # noqa: F401
