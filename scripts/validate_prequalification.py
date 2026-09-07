"""Compare prequalification output between Haiku and Sonnet - MANUAL DEV TOOL.

NOT production code. NOT imported by the app. NOT part of the pytest suite
(same category / exclusion as scripts/compare_rubric_models.py).

It exists so a human can decide whether DEFAULT_MODELS["prequalification"]
should stay on Sonnet (the current initial default) or move to the cheaper
Haiku, by looking at two real prequalification results for the same input side
by side — with particular attention to the UNKNOWN-vs-FAIL judgment.

The built-in --sample case is designed around three deliberate boundary cases:
  1. a MANDATORY criterion with BORDERLINE/partial evidence (should be a
     defensible PASS or FAIL with clear reasoning — not a coin toss),
  2. a criterion with ZERO relevant evidence that MUST resolve to UNKNOWN,
     never FAIL ("not mentioned" != "fails"),
  3. a criterion the evidence DIRECTLY CONTRADICTS, which should resolve to FAIL.

Usage
-----
    python scripts/validate_prequalification.py --sample --yes
    python scripts/validate_prequalification.py --application-id <uuid>

Requires a real ANTHROPIC_API_KEY. Makes TWO real, billable Claude API calls;
prints a rough cost estimate and asks for confirmation unless --yes is given.
"""

from __future__ import annotations

import argparse
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
from app.ai.prompts.prequalification import build_prequalification_prompt  # noqa: E402
from app.ai.schemas.prequalification import PrequalificationAssessment  # noqa: E402
from app.services.prequalification_confidence import compute_confidence  # noqa: E402

_HAIKU = "claude-haiku-4-5-20251001"
_SONNET_DEFAULT = DEFAULT_MODELS["prequalification"]

# --- built-in sample: 3 designed boundary criteria -------------------

_SAMPLE_CRITERIA = [
    SimpleNamespace(
        requirement_type="MANDATORY", category="Technical Skill",
        criterion_text="5+ years of professional Python development",
    ),
    SimpleNamespace(
        requirement_type="MANDATORY", category="Distributed Systems",
        criterion_text="Hands-on experience with Apache Spark / PySpark",
    ),
    SimpleNamespace(
        requirement_type="BEHAVIORAL", category="Collaboration",
        criterion_text="Has mentored or coached junior engineers",
    ),
    SimpleNamespace(
        requirement_type="OTHER", category="Logistics",
        criterion_text="Available to work on-site in Berlin",
    ),
    SimpleNamespace(
        requirement_type="PREFERRED", category="Messaging",
        criterion_text="Kafka or an equivalent event-streaming platform",
    ),
]

# Evidence designed so that:
#  - #1 (5+ yrs Python): BORDERLINE — ~3.5 yrs clearly Python + ~1.5 yrs mixed.
#  - #2 (Spark/PySpark): ZERO mention anywhere => must be UNKNOWN, not FAIL.
#  - #3 (mentoring): ZERO mention => must be UNKNOWN, not FAIL.
#  - #4 (on-site Berlin): CONTRADICTED — "remote-only" stated => FAIL.
#  - #5 (Kafka/equivalent): RabbitMQ present => PASS (equivalent allowed).
_SAMPLE_EVIDENCE = {
    "skills": ["Python", "SQL", "Data pipelines", "REST APIs"],
    "technologies": ["Python", "PostgreSQL", "RabbitMQ", "Docker", "AWS"],
    "experience": [
        {
            "role": "Backend Engineer", "organization": "PayStream",
            "dates": "Mar 2021 - Present",
            "description": (
                "Python services for payments; event-driven with RabbitMQ; "
                "owns two microservices. ~3.5 years."
            ),
        },
        {
            "role": "Software Developer", "organization": "Contoso",
            "dates": "2019 - 2021",
            "description": (
                "Mixed stack: some Python scripting, mostly Java Spring; "
                "internal tools."
            ),
        },
    ],
    "projects": [
        {
            "name": "Realtime ledger", "description": "Event-driven ledger.",
            "technologies": ["Python", "RabbitMQ", "PostgreSQL"],
        }
    ],
    "certifications": [],
    "education": [
        {"qualification": "BSc Computer Science", "institution": "TU Munich",
         "dates": "2015 - 2019"}
    ],
    "other_relevant_claims": ["Seeking fully remote roles only."],
}


def _first_active_user_id(db):
    """Dev-only: first active internal user, to satisfy the HR-only auth gate
    on the extraction lookup from a session-less CLI."""
    from sqlalchemy import select

    from app.database.models.user import User

    uid = db.execute(
        select(User.id).where(User.is_active.is_(True)).order_by(User.created_at)
    ).scalars().first()
    if uid is None:
        sys.exit("No active internal user in the DB — create one first.")
    return uid


def _load_from_application(application_id: str):
    from app.database.database import SessionLocal
    from app.database.models.application import Application
    from app.services.prequalification_service import (
        _resume_extraction_for_application,
    )
    from app.services.rubric_service import get_approved_rubric, list_criteria

    with SessionLocal() as db:
        app_row = db.get(Application, application_id)
        if app_row is None:
            sys.exit(f"No application with id {application_id!r}")
        extraction = _resume_extraction_for_application(
            db, app_row.id, acting_user_id=_first_active_user_id(db)
        )
        if extraction is None:
            sys.exit("That application has no resume extraction — parse it first.")
        rubric = get_approved_rubric(db, app_row.job_id)
        if rubric is None:
            sys.exit("That application's job has no approved rubric.")
        criteria = [
            SimpleNamespace(
                requirement_type=c.requirement_type,
                category=c.category,
                criterion_text=c.criterion_text,
            )
            for c in list_criteria(db, rubric.id)
        ]
        return criteria, dict(extraction.extracted_data)


def _print_result(label: str, model: str, res: PrequalificationAssessment,
                  criteria) -> None:
    print(f"\n=== {label} ({model}) ===")
    by_index = {a.criterion_index: a for a in res.assessments}
    for i, crit in enumerate(criteria, start=1):
        a = by_index.get(i)
        if a is None:
            print(f"  {i}. [{crit.requirement_type}] {crit.criterion_text}")
            print("      !! NO ASSESSMENT RETURNED FOR THIS CRITERION")
            continue
        conf = compute_confidence(
            result=a.result, evidence_summary=a.evidence_summary,
            reasoning=a.reasoning,
        )
        print(f"  {i}. [{crit.requirement_type}] {crit.criterion_text}")
        print(f"      -> {a.result}  (confidence: {conf})")
        print(f"      evidence:  {a.evidence_summary}")
        print(f"      reasoning: {a.reasoning}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--sample", action="store_true",
                     help="Use the built-in 3-boundary-case sample")
    src.add_argument("--application-id",
                     help="Prequalify a real application (uuid) both ways")
    parser.add_argument("--yes", action="store_true",
                        help="Skip the confirmation prompt")
    args = parser.parse_args(argv)

    import logging
    import os

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not os.getenv("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set — this tool makes real API calls.")

    if args.sample:
        criteria = _SAMPLE_CRITERIA
        evidence = _SAMPLE_EVIDENCE
    else:
        criteria, evidence = _load_from_application(args.application_id)

    prompt = build_prequalification_prompt(criteria, evidence)

    approx_in = max(1, len(prompt) // 4)
    approx_out = 1800
    for label, model in (("Haiku", _HAIKU), ("Sonnet default", _SONNET_DEFAULT)):
        est = _estimate_cost_usd(model, approx_in, approx_out)
        print(f"  ~{label:14s} ({model}): ~${est} per call")
    print(f"  (~{approx_in} input tokens, assuming ~{approx_out} output tokens)")

    if not args.yes:
        if input("\nMake 2 real Claude API calls now? type 'yes': ").strip() != "yes":
            print("Aborted.")
            return 1

    for label, model in (("haiku", _HAIKU), ("sonnet", None)):
        used = model or _SONNET_DEFAULT
        try:
            res = get_structured_response(
                prompt, PrequalificationAssessment,
                task_name="prequalification", model=model,
            )
            _print_result(label, used, res, criteria)
        except Exception as exc:  # noqa: BLE001 - dev tool, surface everything
            print(f"\n=== {label} ({used}) ===\n  FAILED: {type(exc).__name__}: {exc}")

    print(
        "\nWHAT TO COMPARE (the point of this script):\n"
        "  * Criterion 2 (Spark/PySpark) and 3 (mentoring): the evidence says\n"
        "    NOTHING about either. Both MUST be UNKNOWN. Any model that returns\n"
        "    FAIL here has the 'no evidence = FAIL' bug and should NOT be the\n"
        "    default.\n"
        "  * Criterion 4 (on-site Berlin): evidence says 'fully remote only'.\n"
        "    This should be FAIL (contradiction), with reasoning that cites the\n"
        "    stated remote-only preference.\n"
        "  * Criterion 1 (5+ yrs Python): borderline. Either PASS or FAIL is\n"
        "    acceptable IF the reasoning is concrete about the ~3.5 clear years\n"
        "    plus ~1.5 mixed years. Reject vague hand-waving.\n"
        "  * Criterion 5 (Kafka/equivalent): RabbitMQ present -> PASS.\n"
        "  * Check every reasoning string for name/gender/age/location — there\n"
        "    should be none.\n"
        "  Pick the model whose UNKNOWN-vs-FAIL calls are reliably correct.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
