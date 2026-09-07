"""Compare rubric-generation output between Haiku and Sonnet - MANUAL DEV TOOL.

NOT production code. NOT imported by the app. NOT part of the pytest suite.

It exists so a human can decide whether DEFAULT_MODELS["rubric_generation"]
should stay on Sonnet or move to the cheaper Haiku, by looking at two real
rubric proposals for the same input side by side.

Usage
-----
    python scripts/compare_rubric_models.py --job-id <uuid>
    python scripts/compare_rubric_models.py --sample --yes --out-dir ./out

Requires a real ANTHROPIC_API_KEY. Makes TWO real, billable Claude API calls;
prints a rough cost estimate and asks for confirmation unless --yes is given.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.ai.claude_client import (  # noqa: E402
    DEFAULT_MODELS,
    _estimate_cost_usd,
    get_structured_response,
)
from app.ai.prompts.rubric_generation import build_rubric_generation_prompt  # noqa: E402
from app.ai.schemas.rubric_generation import RubricGenerationResult  # noqa: E402

_HAIKU = "claude-haiku-4-5-20251001"
_SONNET_DEFAULT = DEFAULT_MODELS["rubric_generation"]

_SAMPLE_REQUIREMENTS = [
    SimpleNamespace(requirement_type="MANDATORY", category="Technical Skill",
                    requirement_text="5+ years of professional Python development"),
    SimpleNamespace(requirement_type="MANDATORY", category="Technical Skill",
                    requirement_text="Production experience operating PostgreSQL"),
    SimpleNamespace(requirement_type="PREFERRED", category="Technical Skill",
                    requirement_text="Experience with Apache Spark or similar"),
    SimpleNamespace(requirement_type="EXPERIENCE", category=None,
                    requirement_text="Has designed and owned data pipelines end to end"),
    SimpleNamespace(requirement_type="BEHAVIORAL", category="Collaboration",
                    requirement_text="Mentors and reviews the work of junior engineers"),
    SimpleNamespace(requirement_type="OTHER", category="Logistics",
                    requirement_text="Available to overlap with US Eastern hours"),
]


def _load_requirements_for_job(job_id: str):
    from app.database.database import SessionLocal
    from app.services.job_service import get_current_requirements

    with SessionLocal() as db:
        reqs = get_current_requirements(db, job_id)
        if not reqs:
            sys.exit(f"Job {job_id} has no current requirements — analyse its JD first.")
        # detach into plain objects so the session can close
        return [
            SimpleNamespace(
                requirement_type=r.requirement_type,
                category=r.category,
                requirement_text=r.requirement_text,
            )
            for r in reqs
        ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--job-id", help="Analysed job to pull current requirements from")
    src.add_argument("--sample", action="store_true", help="Use a built-in requirement set")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    parser.add_argument("--out-dir", default=".", help="Where to write result files")
    args = parser.parse_args(argv)

    import logging
    import os

    # Surface claude_client's per-call token/cost INFO logging (_log_usage).
    # Without a root handler these records are silently dropped.
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not os.getenv("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set — this tool makes real API calls.")

    requirements = (
        _SAMPLE_REQUIREMENTS if args.sample else _load_requirements_for_job(args.job_id)
    )
    prompt = build_rubric_generation_prompt(requirements)

    # Rough estimate: ~4 chars/token in, assume ~2500 output tokens per call.
    approx_in = max(1, len(prompt) // 4)
    approx_out = 2500
    for label, model in (("Haiku", _HAIKU), ("Sonnet default", _SONNET_DEFAULT)):
        est = _estimate_cost_usd(model, approx_in, approx_out)
        print(f"  ~{label:14s} ({model}): ~${est} per call")
    print(f"  (~{approx_in} input tokens, assuming ~{approx_out} output tokens)")

    if not args.yes:
        if input("\nMake 2 real Claude API calls now? type 'yes': ").strip() != "yes":
            print("Aborted.")
            return 1

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, RubricGenerationResult | str] = {}

    for label, model in (("haiku", _HAIKU), ("sonnet", None)):  # None -> DEFAULT_MODELS
        used = model or _SONNET_DEFAULT
        print(f"\n=== {label} ({used}) ===")
        try:
            res = get_structured_response(
                prompt, RubricGenerationResult,
                task_name="rubric_generation", model=model,
            )
            results[label] = res
            (out_dir / f"rubric_{label}.json").write_text(
                res.model_dump_json(indent=2), encoding="utf-8"
            )
            for c in res.criteria:
                print(f"  [{c.requirement_type:10s}] {c.criterion_text}")
        except Exception as exc:  # noqa: BLE001 - dev tool, surface everything
            results[label] = f"FAILED: {type(exc).__name__}: {exc}"
            print(f"  {results[label]}")

    lines = ["Rubric model comparison\n=======================\n"]
    for label in ("haiku", "sonnet"):
        r = results.get(label)
        lines.append(f"\n--- {label} ---")
        if isinstance(r, RubricGenerationResult):
            for c in r.criteria:
                lines.append(f"[{c.requirement_type}] ({c.category}) {c.criterion_text}")
        else:
            lines.append(str(r))
    (out_dir / "rubric_comparison.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWrote results to {out_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
