"""conv_config - constants of the conventions checker: layers, limits, vocabularies.

Layer:   tooling
Stage:   V2-00B
Flags:   none
Raises:  never
Budget:  none
Tests:   tests/v2/conventions/test_conv_rules.py
"""

STAGE_PATTERN = r"V2-\d\d[A-Z]?"
V2_ROOT = "src/app/v2/"
LAYER_DIRS = frozenset({"core", "ports", "adapters", "jobs", "pipeline"})
DOMAIN_DIRS = frozenset(
    {"inbound", "gate", "response", "llm", "outbound", "ledger",
     "memory", "store_data", "handoff", "alerts", "integrations"}
)
MODULE_FIELDS = ("Layer", "Stage", "Flags", "Raises", "Budget", "Tests")
TEST_FIELDS = ("Level", "Stage")
TEST_LEVELS = frozenset({"L1", "L2", "L2b", "L3", "L4", "L5"})

# Section 4 limits. Only these five rules may carry a "# conv: allow" exception.
LIMITS = {"file-lines": 400, "func-lines": 50, "nesting": 4, "params": 6, "branches": 10}

# Files the checker governs. Everything else (V1 code, legacy tests) is left alone.
GOVERNED_PREFIXES = (
    V2_ROOT, "tests/v2/", "tooling/conventions/", "tooling/installers/",
    "tooling/local-test-support/v2_",
)

# Layer -> V2 layers it may import. A domain may also import its own folder.
ALLOWED_IMPORTS = {
    "core": frozenset({"core"}),
    "ports": frozenset({"core", "ports"}),
    "adapters": frozenset({"core", "ports", "adapters"}),
    "domain": frozenset({"core", "ports"}),
    "jobs": frozenset({"core", "ports", "domain", "jobs"}),
    "pipeline": frozenset({"core", "ports", "domain", "jobs", "pipeline"}),
}

# Folders allowed directly under each parent (the repo map, section 1).
ALLOWED_SUBDIRS = {
    "src/app/v2": LAYER_DIRS | DOMAIN_DIRS,
    "docs": frozenset({"adr", "stages", "runbooks", "reference", "archive"}),
    "sql": frozenset({"migrations", "rollbacks", "tests"}),
    "tooling": frozenset({"local-test-support", "conventions", "installers"}),
}

# Section 5 vocabularies.
IMPORT_TIME_CALLS = frozenset({
    "time.time", "time.monotonic", "time.perf_counter", "os.urandom", "os.getenv",
    "os.environ", "os.system", "os.popen", "open", "input",
    "datetime.datetime.now", "datetime.datetime.utcnow", "datetime.datetime.today",
    "datetime.date.today",
})
IMPORT_TIME_PREFIXES = (
    "uuid.", "random.", "secrets.", "subprocess.", "socket.", "urllib.request.",
    "httpx.", "requests.",
)
IMPORT_TIME_ALLOWED = frozenset({"uuid.UUID"})
LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)
COUNTER_CALLS = frozenset({"incr", "inc", "increment", "count", "bump"})
SENSITIVE_NAMES = frozenset(
    {"text", "body", "message", "msg", "payload", "content", "customer_text", "raw_body"}
)
BUILTIN_ERRORS = frozenset({
    "Exception", "RuntimeError", "ValueError", "TypeError", "KeyError", "OSError",
    "LookupError", "AssertionError",
})
FLAG_CALLS = frozenset({"is_enabled", "flag_enabled", "is_flag_enabled"})

# Frozen V1 files and their flat Knowledge names (v2-essential-files.md, section 2).
V1_FLAT_NAMES = {
    "src/entry.py": "cfw_entry.py",
    "src/app/main.py": "cfw_main.py",
    "src/app/config.py": "cfw_config.py",
    "src/app/worker_ctx.py": "worker_ctx.py",
    "src/app/routers/webhook.py": "cfw_routers_webhook.py",
    "src/app/routers/health.py": "cfw_routers_health.py",
    "src/app/services/ai_client.py": "cfw_services_ai_client.py",
    "src/app/services/supabase_client.py": "cfw_services_supabase_client.py",
    "src/app/services/pii_masking.py": "cfw_services_pii_masking.py",
    "src/app/services/token_limits.py": "cfw_services_token_limits.py",
    "src/app/services/meta_client.py": "cfw_services_meta_client.py",
    "wrangler.jsonc": "cfw_wrangler.jsonc",
    "pyproject.toml": "cfw_pyproject.toml",
    "stores_table.sql": "stores_table.sql",
}

RULES = {
    "syntax": "the file must parse",
    "header": "module docstring header with the seven fields (section 2)",
    "layer-import": "imports must follow the layer table (section 3)",
    "file-lines": "file <= 400 lines (section 4, allow-able)",
    "func-lines": "function <= 50 lines (section 4, allow-able)",
    "nesting": "nesting <= 4 levels (section 4, allow-able)",
    "params": "parameters <= 6 (section 4, allow-able)",
    "branches": "decision points per function <= 10 (section 4, allow-able)",
    "import-time": "no clock, randomness, uuid or I/O when a module is imported (5.1)",
    "print": "no print(); use the structured logger (5.2, not in tooling)",
    "bare-except": "no bare 'except:' (5.2)",
    "broad-except": "'except Exception' must re-raise, or log and count (5.2)",
    "todo": "TODO needs a stage id: TODO(V2-xx) (5.3)",
    "secret": "no hard-coded secret patterns (5.4)",
    "select-star": "no select('*') on the stores table in V2 code (5.5)",
    "log-text": "warning: customer text or payload passed to a logger (5.6)",
    "flag": "flags must be registered in core/flags.py, default False (5.7)",
    "free-error": "no free-text builtin errors in V2 code; use core/errors.py (5.8)",
    "naming": "snake_case modules; test_<unit>__<behavior> tests (section 6)",
    "folder-map": "folders and modules must sit inside the repo map (section 1)",
    "sql-pair": "every migration NNN_name.sql has a rollback with the same name (section 8)",
    "flat-name": "flat Knowledge names must be unique (section 8)",
    "stale-docs": "docs/INDEX.md and docs/KNOWLEDGE_MAP.md must be fresh (section 0)",
    "allow-syntax": "# conv: allow <limit-rule> - <reason> (V2-xx), limit rules only (section 4)",
    "allow-unused": "warning: an allow comment that matches no violation",
    "commit-msg": "commit message starts with 'V2-xx: ' (section 6)",
}
