"""test_conv_rules - one violating tree per rule; each must report its rule, clean trees nothing.

Level:   L1
Stage:   V2-00B
"""

import unittest

from conv_config import MODULE_FIELDS, RULES
from conv_testkit import module_pair, run_tree, suite_text

CORE = "tests/v2/core/test_sample.py"
V1_HOOK = "src/app/routers/hook.py"
PARAMS = "def many(a, b, c, d, e, f, g):\n    return a\n"
BRANCHES = "def many(x):\n" + "    if x:\n        pass\n" * 11
SECRET = "KEY = '" + "sk" + "-" + "a" * 30 + "'\n"
STORES = 'def f(c):\n    return c.table("stores").select("*").execute()\n'
LOG_TEXT = "def f(log, text):\n    log.info(text)\n"
FLAGS = "FLAGS = [Flag('new_gate', False, 'gate', 'V2-05')]\n"
USE_FLAG = "def f():\n    return is_enabled('new_gate')\n"
ALLOW_FILE = "# conv: allow file-lines - generated table (V2-00B)\n"
ALLOW_FUNC = "# conv: allow func-lines - long on purpose (V2-00B)\n"


def big(lines):
    return "def big():\n" + "    x = 1\n" * lines + "    return x\n"


def nested(levels):
    """A function with `levels` nested if-blocks."""
    lines = ["def deep(a):"]
    lines += ["    " * (i + 1) + "if a:" for i in range(levels)]
    lines.append("    " * (levels + 1) + "return 1")
    return "\n".join(lines) + "\n"


def sample(body, **kw):
    return module_pair("core", "sample", body, **kw)


def handler(kind):
    return f"def f():\n    try:\n        pass\n    except {kind}:\n        pass\n"


VIOLATING = [
    ("no-docstring", {"src/app/v2/core/sample.py": "VALUE = 1\n"}, "header"),
    ("layer-mismatch", sample("", Layer="ports"), "header"),
    ("bad-stage", sample("", Stage="v2-1"), "header"),
    ("tests-file-missing", {**sample(""), CORE: None}, "header"),
    ("core-imports-ports", sample("from app.v2.ports import x\n"), "layer-import"),
    ("domain-imports-domain", module_pair("gate", "sample", "from app.v2.inbound import x\n"),
     "layer-import"),
    ("adapters-top-level-httpx", module_pair("adapters", "client", "import httpx\n"),
     "layer-import"),
    ("domain-imports-v1", module_pair("gate", "sample", "from app.services import x\n"),
     "layer-import"),
    ("v1-imports-v2-domain", {V1_HOOK: "from app.v2.gate import x\n"}, "layer-import"),
    ("file-lines", sample("X = 1\n" * 400), "file-lines"),
    ("func-lines", sample(big(50)), "func-lines"),
    ("nesting", sample(nested(5)), "nesting"),
    ("params", sample(PARAMS), "params"),
    ("branches", sample(BRANCHES), "branches"),
    ("uuid-at-import", sample("import uuid\nVALUE = uuid.uuid4()\n"), "import-time"),
    ("now-at-import", sample("from datetime import datetime\nNOW = datetime.now()\n"),
     "import-time"),
    ("environ-at-import", sample("import os\nTOKEN = os.environ.get('T')\n"), "import-time"),
    ("print", sample("def f():\n    print(1)\n"), "print"),
    ("bare-except", sample(handler("")), "bare-except"),
    ("broad-except", sample(handler("Exception")), "broad-except"),
    ("todo", sample("# TODO fix this\n"), "todo"),
    ("secret", sample(SECRET), "secret"),
    ("select-star", sample(STORES), "select-star"),
    ("log-text", sample(LOG_TEXT), "log-text"),
    ("free-error", sample("def f():\n    raise ValueError('bad')\n"), "free-error"),
    ("unregistered-flag", sample(USE_FLAG), "flag"),
    ("header-flag-unregistered", sample("", Flags="ghost_flag"), "flag"),
    ("flag-default-true", module_pair("core", "flags", "FLAGS = [Flag('x', True, 'd', 'V2-01')]\n"),
     "flag"),
    ("test-name", {CORE: suite_text("test_sample", "def test_badname():\n    pass\n")}, "naming"),
    ("sql-bad-name", {"sql/migrations/bad.sql": "-- x\n"}, "naming"),
    ("folder-map", module_pair("weird", "sample", ""), "folder-map"),
    ("sql-without-rollback", {"sql/migrations/001_add.sql": "-- x\n"}, "sql-pair"),
    ("flat-name-clash", {"docs/adr/x.md": "a\n", "docs/stages/x.md": "b\n"}, "flat-name"),
    ("syntax", {"src/app/v2/core/broken.py": "def (:\n"}, "syntax"),
    ("allow-not-a-limit", sample("# conv: allow print - because (V2-00B)\n"), "allow-syntax"),
    ("allow-unused", sample("# conv: allow file-lines - not needed (V2-00B)\n"), "allow-unused"),
    *[(f"missing-{name}", sample("", **{name: None}), "header") for name in MODULE_FIELDS],
]

CLEAN = [
    ("baseline", {}),
    ("adapters-import-inside-function",
     module_pair("adapters", "client", "def f():\n    import httpx\n    return httpx\n")),
    ("uuid-inside-function", sample("import uuid\n\n\ndef f():\n    return uuid.uuid4()\n")),
    ("todo-with-stage", sample("# TODO(V2-05) later\n")),
    ("except-with-raise", sample("def f():\n    try:\n        pass\n    except Exception:\n"
                                 "        raise\n")),
    ("except-with-log-and-counter",
     sample("def f(log, metrics):\n    try:\n        pass\n    except Exception:\n"
            "        log.error('x')\n        metrics.incr('y')\n")),
    ("domain-imports-core-and-ports",
     module_pair("gate", "sample", "from app.v2.core import x\nfrom app.v2.ports import y\n")),
    ("domain-imports-its-own-folder",
     module_pair("gate", "sample", "from app.v2.gate import other\n")),
    ("pipeline-build-imports-adapters",
     module_pair("pipeline", "build", "from app.v2.adapters import client\n")),
    ("v1-imports-pipeline", {V1_HOOK: "from app.v2.pipeline import build\n"}),
    ("nesting-at-the-limit", sample(nested(4))),
    ("docs-archive-is-ignored",
     {"docs/archive/knowledge-2026-11/a.md": "a\n", "docs/stages/a.md": "b\n"}),
    ("allow-file-lines", sample("X = 1\n" * 400, prefix=ALLOW_FILE)),
    ("allow-func-lines", sample(ALLOW_FUNC + big(50))),
    ("registered-flag", {**module_pair("core", "flags", FLAGS),
                         **module_pair("gate", "session", USE_FLAG, Flags="new_gate")}),
]


class RuleCases(unittest.TestCase):
    def test_rules__violating_trees_report_their_rule(self):
        for label, files, rule in VIOLATING:
            with self.subTest(label):
                rules = {v.rule for v in run_tree(files).violations}
                self.assertIn(rule, rules)

    def test_rules__clean_trees_report_nothing(self):
        for label, files in CLEAN:
            with self.subTest(label):
                self.assertEqual([], run_tree(files).violations)

    def test_rules__every_rule_has_a_violating_case(self):
        covered = {rule for _label, _files, rule in VIOLATING} | {"stale-docs", "commit-msg"}
        self.assertEqual(set(RULES), covered)

    def test_rules__stale_docs_when_not_generated(self):
        rules = {v.rule for v in run_tree({}, regenerate=False).violations}
        self.assertEqual({"stale-docs"}, rules)

    def test_rules__log_text_is_a_warning_not_an_error(self):
        report = run_tree(sample(LOG_TEXT))
        self.assertEqual(["warn"], [v.severity for v in report.violations])
        self.assertEqual([], report.errors)

    def test_rules__allow_comments_are_reported_not_hidden(self):
        report = run_tree(sample(ALLOW_FUNC + big(50)))
        self.assertEqual(["func-lines"], [v.rule for v, _allow in report.allowed])
        self.assertEqual([], report.errors)

    def test_rules__print_is_allowed_in_tooling(self):
        files = {"tooling/conventions/hello.py": (
            '"""hello - prints\n\nLayer:   tooling\nStage:   V2-00B\nFlags:   none\n'
            'Raises:  never\nBudget:  none\nTests:   tests/v2/core/test_sample.py\n"""\n\n'
            "def run():\n    print('hi')\n")}
        self.assertEqual([], run_tree(files).violations)


if __name__ == "__main__":
    unittest.main()
