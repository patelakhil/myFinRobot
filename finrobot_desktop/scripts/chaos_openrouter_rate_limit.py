"""Standalone chaos/monkey demo + evidence generator for the "run research"
OpenRouter rate-limit queueing enhancement (BUG-20260615-rl).

Unlike ``pytest tests/chaos/`` (the automated regression gate), this script
is meant to be run BY HAND / pasted into a PR description as cross-checkable
evidence: it drives the real ``Pipeline`` retry/queue engine
(``finrobot.engine.pipelines.runner``) against N randomly generated,
increasingly adversarial simulated OpenRouter 429 scripts, prints a
before/after comparison against the OLD fixed-schedule behaviour for each
case, and exits non-zero if any invariant is violated (so it can also be
wired into CI as a smoke gate, separate from the pytest suite).

No network access, no real API key required — see
``tests/chaos/_openrouter_fakes.py`` for the simulated OpenRouter exception
shapes this reuses.

Usage
-----
    python scripts/chaos_openrouter_rate_limit.py                 # 200 runs, seed=0
    python scripts/chaos_openrouter_rate_limit.py --runs 2000
    python scripts/chaos_openrouter_rate_limit.py --seed 1337 --runs 500
    python scripts/chaos_openrouter_rate_limit.py --verbose        # print every case

Exit code is 0 iff every invariant held for every generated case (printed
summary also states PASS/FAIL per invariant so a human can cross-verify).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Allow running directly via `python scripts/chaos_openrouter_rate_limit.py`
# without installing the package first.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests" / "chaos"))

from _openrouter_fakes import make_openrouter_429, make_text_only_429

from finrobot.engine.pipelines.base import Pipeline, PipelineStep, PipelineStepError, TextValidator
from finrobot.engine.pipelines.runner import (
    _RATE_LIMIT_MAX_BONUS_ATTEMPTS,
    _RATE_LIMIT_MAX_TOTAL_QUEUE_S,
    _RETRY_DELAYS,
)
from finrobot.engine.pipelines.validators import validate_is_non_empty


@dataclass
class CaseResult:
    case_id: int
    script_len: int
    max_hint_s: float
    old_would_have_succeeded: bool
    new_succeeded: bool
    new_total_queued_s: float
    new_attempts: int
    wall_s: float
    violations: list[str] = field(default_factory=list)


def _random_script(rng: random.Random, max_len: int) -> list[float | None]:
    """A list of simulated 429 cool-down hints (seconds); None = header-free."""
    n = rng.randint(0, max_len)
    out: list[float | None] = []
    for _ in range(n):
        kind = rng.random()
        if kind < 0.55:
            out.append(round(rng.uniform(0, 20), 2))  # typical per-minute throttle
        elif kind < 0.8:
            out.append(round(rng.uniform(20, 90), 2))  # slow-but-recoverable
        elif kind < 0.92:
            out.append(round(rng.uniform(3600, 200_000), 1))  # daily-quota-shaped
        else:
            out.append(None)  # no usable hint at all
    return out


def _old_fixed_schedule_would_succeed(script: list[float | None], max_retries: int = 3) -> bool:
    """Reference model of the PRE-fix behaviour: exactly 1 + max_retries
    attempts total, fixed [2, 5, 10]s waits, no regard for any hint — used
    purely to print the before/after contrast, not part of the code under
    test."""
    return len(script) <= max_retries


async def _run_one_case(case_id: int, script: list[float | None], rng: random.Random) -> CaseResult:
    attempts = {"n": 0}

    async def executor(agent, deps, prompt, structured_context, ticker):
        attempts["n"] += 1
        idx = attempts["n"] - 1
        if idx < len(script):
            hint = script[idx]
            if hint is None:
                raise make_openrouter_429("429 Too Many Requests")
            if rng.random() < 0.5:
                raise make_openrouter_429(retry_after_header=str(hint))
            raise make_text_only_429(hint)
        return "chaos-monkey demo: research report body"

    step = PipelineStep(
        name="thesis",
        agent=MagicMock(),
        validator=TextValidator(validate_is_non_empty),
        executor=executor,
        critical=True,
    )
    pipeline = Pipeline(steps=[step], max_retries=3)

    @dataclass
    class _FakeDeps:
        data_layer: object = None
        skill_runtime: object = None
        model_name: str = "openrouter:deepseek/deepseek-chat"

    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    violations: list[str] = []
    t0 = time.monotonic()
    succeeded = False
    with patch.object(asyncio, "sleep", new=AsyncMock(side_effect=_fake_sleep)):
        try:
            result = await asyncio.wait_for(pipeline.execute(_FakeDeps(), "CHAOS"), timeout=5.0)
            succeeded = not result.failed_validations
        except PipelineStepError:
            succeeded = False
        except TimeoutError:
            violations.append("HUNG: did not terminate within 5s wall-clock (mocked sleep)")
    wall_s = time.monotonic() - t0

    total_queued = sum(sleeps)
    if total_queued > _RATE_LIMIT_MAX_TOTAL_QUEUE_S + sum(_RETRY_DELAYS) + 1.0:
        violations.append(
            f"QUEUE BUDGET EXCEEDED: queued {total_queued:.1f}s > cap "
            f"{_RATE_LIMIT_MAX_TOTAL_QUEUE_S + sum(_RETRY_DELAYS):.1f}s"
        )
    max_calls = 1 + 3 + _RATE_LIMIT_MAX_BONUS_ATTEMPTS
    if attempts["n"] > max_calls:
        violations.append(f"UNBOUNDED ATTEMPTS: {attempts['n']} > cap {max_calls}")
    # Core claim: if the script resolves to success within the NEW attempt
    # AND cumulative-queue-time budget, the step must actually succeed. This
    # mirrors the runner's own bookkeeping (attempt cap + running total vs.
    # the queue window) rather than a per-item-only approximation, so it
    # doesn't flag the CORRECT fail-fast behaviour (cumulative hints that
    # blow the 300s window before the script's last item is reached) as a
    # false violation. The authoritative version of this property — with
    # hint magnitudes constrained up front so it can never ambiguously land
    # on the fail-fast edge — is the Hypothesis test
    # ``test_recoverable_throttle_always_eventually_succeeds`` in
    # tests/chaos/test_rate_limit_chaos_monkey.py; this is a looser,
    # illustrative echo of the same claim for the human-readable report.
    running_total = 0.0
    within_budget = len(script) <= 3 + _RATE_LIMIT_MAX_BONUS_ATTEMPTS
    if within_budget:
        for h in script:
            wait = min(h, _RATE_LIMIT_MAX_SINGLE_WAIT_S_FOR_DISPLAY) if h is not None else 30.0
            # A hint past the whole window is handed up to the run-level park
            # immediately (the step never naps on it) → not step-recoverable.
            if (h is not None and h > _RATE_LIMIT_MAX_TOTAL_QUEUE_S) or (
                running_total + wait > _RATE_LIMIT_MAX_TOTAL_QUEUE_S
            ):
                within_budget = False
                break
            running_total += wait
    if within_budget and not succeeded:
        violations.append("EXPECTED SUCCESS BUT STEP DEGRADED/FAILED")

    max_hint = max([h for h in script if h is not None], default=0.0)
    return CaseResult(
        case_id=case_id,
        script_len=len(script),
        max_hint_s=max_hint,
        old_would_have_succeeded=_old_fixed_schedule_would_succeed(script),
        new_succeeded=succeeded,
        new_total_queued_s=total_queued,
        new_attempts=attempts["n"],
        wall_s=wall_s,
        violations=violations,
    )


_RATE_LIMIT_MAX_SINGLE_WAIT_S_FOR_DISPLAY = (
    90.0  # mirror runner.py's cap for the script filter above
)


async def _main_async(args: argparse.Namespace) -> int:
    rng = random.Random(args.seed)
    results: list[CaseResult] = []
    t_start = time.monotonic()
    for case_id in range(args.runs):
        script = _random_script(rng, max_len=args.max_script_len)
        res = await _run_one_case(case_id, script, rng)
        results.append(res)
        if args.verbose or res.violations:
            flag = "FAIL" if res.violations else "ok"
            print(
                f"[{flag}] case={res.case_id:5d} script_len={res.script_len:2d} "
                f"max_hint={res.max_hint_s:10.1f}s old_would_succeed={res.old_would_have_succeeded!s:5} "
                f"new_succeeded={res.new_succeeded!s:5} queued={res.new_total_queued_s:7.1f}s "
                f"attempts={res.new_attempts:2d}"
                + (f"  !! {res.violations}" if res.violations else "")
            )
    elapsed = time.monotonic() - t_start

    total = len(results)
    violated = [r for r in results if r.violations]
    old_would_fail_new_succeeds = [
        r for r in results if (not r.old_would_have_succeeded) and r.new_succeeded
    ]
    never_hung = all("HUNG" not in v for r in results for v in r.violations)
    queue_bounded = all("QUEUE BUDGET" not in v for r in results for v in r.violations)
    attempts_bounded = all("UNBOUNDED" not in v for r in results for v in r.violations)

    print("\n" + "=" * 78)
    print("CHAOS/MONKEY SUMMARY — OpenRouter rate-limit response-driven queueing")
    print("=" * 78)
    print(f"cases run                         : {total}")
    print(f"wall time                         : {elapsed:.2f}s")
    print(
        f"cases where OLD fixed-schedule would\n"
        f"  have failed but NEW queueing saved : "
        f"{len(old_would_fail_new_succeeds)} "
        f"({100 * len(old_would_fail_new_succeeds) / max(total, 1):.1f}%% of all cases)".replace(
            "%%", "%"
        )
    )
    print(f"invariant 'never hangs'            : {'PASS' if never_hung else 'FAIL'}")
    print(f"invariant 'queue time is bounded'  : {'PASS' if queue_bounded else 'FAIL'}")
    print(f"invariant 'attempts are bounded'   : {'PASS' if attempts_bounded else 'FAIL'}")
    print(
        "invariant 'recoverable-within-budget scripts succeed' : "
        f"{'PASS' if not any('EXPECTED SUCCESS' in v for r in results for v in r.violations) else 'FAIL'}"
    )
    print(f"TOTAL VIOLATIONS                   : {len(violated)}")
    print("=" * 78)

    if violated:
        print(f"\n{len(violated)} case(s) violated an invariant — see [FAIL] lines above.")
        return 1
    print("\nAll invariants held for every generated case. ✅")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=200, help="number of randomised cases")
    parser.add_argument("--seed", type=int, default=0, help="RNG seed (reproducible by default)")
    parser.add_argument(
        "--max-script-len",
        type=int,
        default=12,
        dest="max_script_len",
        help="max number of simulated 429s before the case is allowed to recover",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="print every case, not just failures"
    )
    parser.add_argument(
        "--log", action="store_true", help="also emit runner.py's own retry/queue DEBUG logging"
    )
    args = parser.parse_args()
    # runner.py logs every retry/queue decision at WARNING/INFO — useful when
    # diagnosing ONE case (--verbose implies you want to see it inline via
    # the case's own print, not a wall of interleaved logger noise), so it's
    # quiet by default and only surfaced at DEBUG for power users.
    logging.basicConfig(level=logging.DEBUG if args.log else logging.CRITICAL)
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
