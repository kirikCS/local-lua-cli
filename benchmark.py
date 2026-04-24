"""Benchmark runner for ICEQ agent — compares models on test scenarios."""

import os
import sys
import time
import tempfile
import argparse

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from localscript import agent, config
from localscript.context import Context
from localscript.tools import run_sandbox

from tests.test_scenarios import SCENARIOS


def _setup_workdir(tmpdir: str, setup_files: dict):
    """Create setup files in the temporary workdir."""
    for path, content in setup_files.items():
        full = os.path.join(tmpdir, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)


def _check_results(tmpdir: str, expected_files: list[str]) -> tuple[bool, list[str]]:
    """Check that expected files exist and pass lua54. Returns (all_ok, created_files)."""
    created = []
    all_ok = True
    for fpath in expected_files:
        full = os.path.join(tmpdir, fpath)
        if not os.path.isfile(full):
            all_ok = False
            continue
        created.append(fpath)
        if fpath.endswith(".lua"):
            result = run_sandbox(fpath)
            if "FAILED" in result or "ERRORS" in result:
                all_ok = False
    return all_ok, created


def run_scenario(scenario: dict, model: str | None = None) -> dict:
    """Run a single scenario. Returns result dict."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _setup_workdir(tmpdir, scenario["setup_files"])

        old_cwd = os.getcwd()
        old_max = config.MAX_ITERATIONS
        old_model = config.LLM_MODEL
        os.chdir(tmpdir)
        try:
            config.MAX_ITERATIONS = scenario.get("max_iterations", 10)
            if model:
                config.LLM_MODEL = model

            ctx = Context(scan=True)
            t0 = time.time()
            summary = agent.run(scenario["description"], ctx=ctx)
            elapsed = time.time() - t0

            iters = ctx.history[-1]["iterations"] if ctx.history else 0
            files_ok, created = _check_results(tmpdir, scenario["expected_files"])

            return {
                "id": scenario["id"],
                "success": summary is not None and files_ok,
                "iterations": iters,
                "time": elapsed,
                "files": created,
            }
        except Exception as e:
            return {
                "id": scenario["id"],
                "success": False,
                "iterations": 0,
                "time": time.time() - t0,
                "files": [],
                "error": str(e),
            }
        finally:
            os.chdir(old_cwd)
            config.MAX_ITERATIONS = old_max
            config.LLM_MODEL = old_model


def _print_table(results: list[dict], scenarios: list[dict]):
    """Print results as a formatted table."""
    # Header
    print()
    header = f"{'Scenario':<20} | {'Iters':>5} | {'Time':>7} | {'OK':>3} | Files"
    print(header)
    print("-" * len(header) + "-" * 20)

    # Rows
    total = len(results)
    passed = 0
    total_iters = 0
    total_time = 0.0

    for res, scn in zip(results, scenarios):
        mark = "+" if res["success"] else "x"
        files_str = ", ".join(res["files"]) if res["files"] else "-"
        error = res.get("error", "")
        if error:
            files_str = f"ERROR: {error[:40]}"

        print(
            f"{scn['id'] + ' ' + scn['category']:<20} | "
            f"{res['iterations']:>5} | "
            f"{res['time']:>6.1f}s | "
            f"  {mark} | "
            f"{files_str}"
        )

        if res["success"]:
            passed += 1
        total_iters += res["iterations"]
        total_time += res["time"]

    # Summary
    print("-" * len(header) + "-" * 20)
    avg_iters = total_iters / total if total else 0
    avg_time = total_time / total if total else 0
    print(
        f"{'TOTAL':<20} | {avg_iters:>5.1f} | {avg_time:>6.1f}s | "
        f"{passed}/{total} | "
        f"Success rate: {passed/total*100:.0f}%" if total else "N/A"
    )
    print()


def main():
    parser = argparse.ArgumentParser(description="ICEQ Agent Benchmark")
    parser.add_argument("--model", default=None, help="Model to test (default: from config)")
    parser.add_argument("--scenarios", nargs="*", default=None,
                        help="Scenario IDs to run (e.g. A1 B2 C3). Default: all")
    parser.add_argument("--category", choices=["fix", "multifile", "octapi"],
                        help="Run only scenarios of this category")
    args = parser.parse_args()

    # Filter scenarios
    to_run = SCENARIOS
    if args.scenarios:
        ids = set(args.scenarios)
        to_run = [s for s in SCENARIOS if s["id"] in ids]
    elif args.category:
        to_run = [s for s in SCENARIOS if s["category"] == args.category]

    if not to_run:
        print("No scenarios to run.")
        return

    model_name = args.model or config.LLM_MODEL
    print(f"\n=== ICEQ Benchmark ===")
    print(f"Model: {model_name}")
    print(f"Scenarios: {len(to_run)}")
    print()

    results = []
    for i, scenario in enumerate(to_run, 1):
        print(f"[{i}/{len(to_run)}] {scenario['id']}: {scenario['description'][:60]}...")
        res = run_scenario(scenario, model=args.model)
        mark = "+" if res["success"] else "x"
        print(f"  -> {mark} {res['iterations']} iters, {res['time']:.1f}s")
        results.append(res)

    _print_table(results, to_run)


if __name__ == "__main__":
    main()
