#!/usr/bin/env python3
"""
Stage 7 — runs the adapted test files ONE TEST FUNCTION AT A TIME and writes a report.

Lives in cf-worker/ next to test_package_system.py and test_usage_protection.py (the adapted copies).

    cd /workspaces/boltane-workers-poc/files12/cf-worker
    source ~/venv-tests/bin/activate
    python stage7_run_suite.py                 # adapted tests + the "before" baseline
    python stage7_run_suite.py --regression    # ...and the summary line of every earlier offline test

Nothing here touches the network, Supabase, Meta or OpenRouter, and no repo file is modified.

Part 0  safety net: the application code (cf-worker/src) is byte-identical to the last git commit —
        nothing in Stage 7 may change it, so a difference here is reported as a failure.
Part 1  the adapted tests, each test function separately (so one failure hides nothing).
Part 2  the "before" baseline: the same two files with the ONE changed line put back
        (`async def execute` -> `def execute` in the Supabase fake), run the same way, in a temp folder.
        This is a reconstruction of the Render-era fake, not a copy of an original file (the originals
        are not in the repo); it shows which tests the single change fixes.
Part 3  (--regression) runs the earlier offline test scripts and prints their own summary line.
        Known recorded results: test_stage5_firewall 29/29, test_stage6_routes 36 checks,
        test_stage6_meta_sim 23 PASS / 31 GAP / 0 FAIL. The others are printed as observed.

The report is saved to stage7_suite_report.txt (no secrets; do not commit it).
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CF = Path(__file__).resolve().parent
REPORT = CF / "stage7_suite_report.txt"
SUPPORT = CF / "tooling" / "local-test-support"
SRC = CF / "src"
ADAPTED = ("test_package_system", "test_usage_protection")
CHANGED_LINE = "    async def execute(self):   # Stage 7: async, because the Workers supabase_client awaits it"
ORIGINAL_LINE = "    def execute(self):"

REGRESSION = [
    # (script, needs PYTHONPATH, note)
    ("test_stage0_ports.py", True, ""),
    ("test_pii_masking_general.py", False, ""),
    ("test_stage1_settings.py", True, ""),
    ("test_stage2_supabase_unit.py", True, ""),
    ("test_stage3_offline.py", False, ""),
    ("test_stage4_offline.py", True, ""),
    ("test_stage5_firewall.py", False, "recorded: 29/29"),
    ("test_stage6_routes.py", True, "recorded: 36 checks"),
    ("test_stage6_meta_sim.py", False, "recorded: 23 PASS / 31 GAP / 0 FAIL"),
]

RUNNER = '''
import asyncio, importlib, inspect, sys
sys.path.insert(0, sys.argv[1])
name = sys.argv[2]
try:
    mod = importlib.import_module(name)
except BaseException as exc:
    print(f"RESULT IMPORT-FAIL {name}: {type(exc).__name__}: {str(exc).splitlines()[0][:160] if str(exc) else ''}")
    sys.exit(0)
tests = [(n, f) for n, f in vars(mod).items()
         if n.startswith("test_") and inspect.iscoroutinefunction(f) and f.__module__ == mod.__name__]
tests.sort(key=lambda pair: pair[1].__code__.co_firstlineno)
for n, f in tests:
    try:
        asyncio.run(f())
        print(f"RESULT PASS {n}")
    except BaseException as exc:
        first = str(exc).splitlines()[0][:160] if str(exc) else ""
        print(f"RESULT FAIL {n}: {type(exc).__name__}: {first}")
'''

_lines: list[str] = []


def out(text: str = "") -> None:
    print(text)
    _lines.append(text)


def header(title: str) -> None:
    out()
    out("=" * 72)
    out(title)
    out("=" * 72)


def env_for(use_path: bool) -> dict:
    env = dict(os.environ)
    env.pop("OPENROUTER_SOCKS5_PROXY", None)
    if use_path:
        env["PYTHONPATH"] = os.pathsep.join([str(SUPPORT), str(SRC)])
    return env


def run_functions(directory: Path, module: str) -> dict:
    """Returns {test_name: ('PASS'|'FAIL'|'IMPORT-FAIL', detail)} for one module, run function by function."""
    with tempfile.TemporaryDirectory(prefix="stage7_runner_") as tmp:
        runner = Path(tmp) / "_runner.py"
        runner.write_text(RUNNER, encoding="utf-8")
        try:
            proc = subprocess.run([sys.executable, str(runner), str(directory), module], cwd=str(CF),
                                  capture_output=True, text=True, timeout=300, env=env_for(True))
        except subprocess.TimeoutExpired:
            return {module: ("FAIL", "timed out after 300s")}
    results: dict = {}
    for line in proc.stdout.splitlines():
        match = re.match(r"RESULT (PASS|FAIL|IMPORT-FAIL) ([^:\s]+):?\s*(.*)", line)
        if match:
            results[match.group(2)] = (match.group(1), match.group(3))
    if not results:
        results[module] = ("FAIL", f"no result lines (exit {proc.returncode}); stderr tail: {proc.stderr.strip()[-300:] or '(empty)'}")
    return results


def show(label: str, results: dict) -> None:
    out(label)
    for name, (verdict, detail) in results.items():
        out(f"  {verdict:<11} {name}" + (f"   -> {detail}" if detail and verdict != "PASS" else ""))
    passed = sum(1 for v, _ in results.values() if v == "PASS")
    out(f"  => {passed}/{len(results)} passed")


def git(args: list, cwd: Path):
    try:
        proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=30)
        return proc.returncode, proc.stdout
    except Exception as exc:  # noqa: BLE001
        return 127, str(exc)


def part_integrity() -> bool:
    header("Part 0) Application code untouched (git)")
    code, top = git(["rev-parse", "--show-toplevel"], CF)
    if code != 0:
        out("git not available here — integrity check skipped")
        return True
    root = Path(top.strip())
    rel = (CF / "src").relative_to(root).as_posix()
    _, changed = git(["diff", "--stat", "HEAD", "--", rel], root)
    _, untracked = git(["status", "--porcelain", "--untracked-files=all", "--", rel], root)
    stray = [ln for ln in untracked.splitlines() if ln.startswith("??") and "__pycache__" not in ln and not ln.endswith(".pyc")]
    ok = not changed.strip() and not stray
    out(f"  {'PASS' if not changed.strip() else 'FAIL'}  tracked files under {rel}/ identical to HEAD"
        + (f"\n{changed.rstrip()}" if changed.strip() else ""))
    out(f"  {'PASS' if not stray else 'FAIL'}  no untracked/stray files under {rel}/"
        + ("".join(f"\n        {ln}" for ln in stray) if stray else ""))
    _, head = git(["log", "--oneline", "-1"], root)
    out(f"  HEAD: {head.strip()}")
    return ok


def part_adapted() -> dict:
    header("Part 1) Adapted tests on the Workers tree, one function at a time")
    summary: dict = {}
    for module in ADAPTED:
        path = CF / f"{module}.py"
        if not path.exists():
            out(f"{module}.py: NOT FOUND in {CF} — drag the adapted file into cf-worker/ first")
            summary[module] = None
            continue
        results = run_functions(CF, module)
        summary[module] = results
        show(f"{module}.py", results)
    return summary


def part_baseline(adapted: dict) -> None:
    header("Part 2) Baseline: the same files with the single changed line put back")
    package_path = CF / "test_package_system.py"
    if not package_path.exists() or not (CF / "test_usage_protection.py").exists():
        out("adapted files missing — baseline not run")
        return
    text = package_path.read_text(encoding="utf-8")
    if text.count(CHANGED_LINE) != 1:
        out("cannot rebuild the baseline: the expected changed line was not found exactly once in test_package_system.py "
            "(was the file edited after delivery?)")
        return
    with tempfile.TemporaryDirectory(prefix="stage7_baseline_") as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "test_package_system.py").write_text(text.replace(CHANGED_LINE, ORIGINAL_LINE), encoding="utf-8")
        (tmp_path / "test_usage_protection.py").write_text((CF / "test_usage_protection.py").read_text(encoding="utf-8"),
                                                          encoding="utf-8")
        before: dict = {}
        for module in ADAPTED:
            before[module] = run_functions(tmp_path, module)
            show(f"{module}.py (BEFORE)", before[module])
    out()
    out("Tests whose verdict differs between BEFORE and the adapted run:")
    changed = 0
    for module in ADAPTED:
        after = adapted.get(module) or {}
        for name, (verdict, detail) in before[module].items():
            now = after.get(name, ("-", ""))[0]
            if now != verdict:
                changed += 1
                out(f"  {module}.{name}: {verdict} -> {now}")
    if not changed:
        out("  (none)")


def part_regression() -> None:
    header("Part 3) Earlier offline tests (summary line of each; exit code 0 = passed)")
    for script, use_path, note in REGRESSION:
        path = CF / script
        if not path.exists():
            out(f"  {script:<34} MISSING")
            continue
        started = time.perf_counter()
        try:
            proc = subprocess.run([sys.executable, str(path)], cwd=str(CF), capture_output=True, text=True,
                                  timeout=900, env=env_for(use_path))
            text = proc.stdout + "\n" + proc.stderr
            summary = ""
            for pattern in (r"\d+/\d+ checks passed", r"\d+ scenarios: [^\n]+", r"ALL PASS", r"\d+ checks? passed"):
                found = re.findall(pattern, text)
                if found:
                    summary = found[-1]
                    break
            if not summary:
                tail = [ln for ln in text.splitlines() if ln.strip()]
                summary = ("last line: " + tail[-1][:100]) if tail else "(no output)"
            out(f"  {script:<34} exit {proc.returncode}  {summary}   ({time.perf_counter() - started:.0f}s)"
                + (f"   [{note}]" if note else ""))
        except subprocess.TimeoutExpired:
            out(f"  {script:<34} TIMED OUT after 900s")


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 7 suite runner")
    parser.add_argument("--regression", action="store_true", help="also run the earlier offline test scripts")
    args = parser.parse_args()

    out("Stage 7 suite — " + time.strftime("%Y-%m-%d %H:%M:%S"))
    out(f"python {sys.version.split()[0]}  cwd {CF}")
    if not (SUPPORT / "workers.py").exists() or not (SRC / "app").is_dir():
        out(f"missing {SUPPORT / 'workers.py'} or {SRC / 'app'} — run from the cf-worker folder of the repo")
        return 2

    integrity_ok = part_integrity()
    adapted = part_adapted()
    part_baseline(adapted)
    if args.regression:
        part_regression()

    failed = (not integrity_ok) or any(r is None or any(v != "PASS" for v, _ in r.values()) for r in adapted.values())
    REPORT.write_text("\n".join(_lines) + "\n", encoding="utf-8")
    out()
    out(f"adapted tests + integrity: {'ALL PASSED' if not failed else 'SOMETHING FAILED (see Parts 0/1)'}")
    out(f"saved: {REPORT}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
