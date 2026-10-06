"""Migration round-trip tests for the Phase 4 schema changes.

WHY A THROWAWAY DATABASE
------------------------
Every other test in this suite runs inside a savepoint that is rolled back, so
it never touches schema. A migration test cannot work that way: ``alembic``
commits DDL, and running ``downgrade`` against the shared development database
mid-suite would drop tables out from under any concurrently running test and
leave the developer's database at the wrong revision if it failed halfway.

So these tests create a **temporary database**, run the full migration history
against it, and drop it. ``app.database.database.engine`` is monkeypatched
before each alembic command because ``env.py`` imports that engine by name at
module-execution time (once per command), which is the seam that lets the
migrations point somewhere else.

CREATE DATABASE PRIVILEGE — NOT A SILENT SKIP
--------------------------------------------
This module needs a role that can ``CREATE DATABASE``. If it cannot, the default
behaviour is a hard **failure** (``pytest.fail``) with an explicit message — a
green run must never quietly mean "migration round-trip was not verified".

The single escape hatch is the env var ``MIGRATION_ROUNDTRIP_SKIP_OK=1``. When
set, the missing privilege becomes a skip *plus* a ``warnings.warn`` and a
banner printed to stderr, so a genuinely restricted CI environment can opt out
but the opt-out is loud and deliberate, never the default. The recommended
setup for such environments is a separate, required CI job whose Postgres role
has ``CREATEDB`` and which does NOT set that var — see the module-level comment
in ``.github``/CI config when it lands.
"""

from __future__ import annotations

import os
import sys
import uuid
import warnings
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

import app.database.database as app_db

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"

_PHASE4_REVISIONS = ("c9a4f1d7b208", "a7e35c9d146b")

_SKIP_OK_ENV = "MIGRATION_ROUNDTRIP_SKIP_OK"

_NO_PRIVILEGE_MESSAGE = (
    "round-trip coverage requires CREATE DATABASE privilege — not verified. "
    "The connected Postgres role cannot CREATE DATABASE, so the "
    "upgrade->downgrade->upgrade migration round-trip could not be exercised. "
    f"Grant CREATEDB to the test role, or set {_SKIP_OK_ENV}=1 to convert this "
    "into a (loud) skip for a deliberately restricted environment."
)


def _handle_missing_privilege(exc: Exception) -> None:
    """Fail hard by default; skip loudly only when explicitly opted in."""
    if os.environ.get(_SKIP_OK_ENV) == "1":
        banner = (
            "\n"
            "============================================================\n"
            "MIGRATION ROUND-TRIP TESTS SKIPPED\n"
            f"  reason : cannot CREATE DATABASE ({type(exc).__name__})\n"
            f"  opt-in : {_SKIP_OK_ENV}=1 is set\n"
            "  effect : upgrade/downgrade/upgrade was NOT verified in this run\n"
            "============================================================\n"
        )
        print(banner, file=sys.stderr, flush=True)
        warnings.warn(
            "migration round-trip not verified: no CREATE DATABASE privilege "
            f"and {_SKIP_OK_ENV}=1",
            stacklevel=2,
        )
        pytest.skip("migration round-trip skipped via " + _SKIP_OK_ENV)
    pytest.fail(f"{_NO_PRIVILEGE_MESSAGE}\nunderlying error: {exc}", pytrace=False)


@pytest.fixture(scope="module")
def temp_database_url():
    """Create a scratch database for the module; drop it afterwards."""
    base_url = app_db._URL
    temp_name = f"migrationtest_{uuid.uuid4().hex[:12]}"

    admin_url = base_url.set(database="postgres")
    admin_engine = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")

    try:
        with admin_engine.connect() as conn:
            conn.execute(sa.text(f'CREATE DATABASE "{temp_name}"'))
    except sa.exc.OperationalError as exc:
        # Server unreachable / auth failure — every DB test in the suite is
        # already failing for the same reason; report it, don't skip.
        admin_engine.dispose()
        pytest.fail(
            f"cannot reach Postgres to create a temporary database: {exc}",
            pytrace=False,
        )
    except sa.exc.SQLAlchemyError as exc:
        # Most commonly psycopg2.errors.InsufficientPrivilege (no CREATEDB).
        admin_engine.dispose()
        _handle_missing_privilege(exc)

    try:
        yield base_url.set(database=temp_name)
    finally:
        with admin_engine.connect() as conn:
            conn.execute(
                sa.text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"
                ).bindparams(n=temp_name)
            )
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{temp_name}"'))
        admin_engine.dispose()


@pytest.fixture
def migrate(temp_database_url, monkeypatch):
    """Return ``run("upgrade", "head")`` bound to the temporary database."""
    engine = sa.create_engine(temp_database_url, future=True)
    monkeypatch.setattr(app_db, "engine", engine)

    config = Config(str(_ALEMBIC_INI))

    def run(action: str, target: str) -> None:
        getattr(command, action)(config, target)

    try:
        yield run
    finally:
        engine.dispose()


def _inspect(url):
    engine = sa.create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            yield_data = {
                "tables": set(sa.inspect(conn).get_table_names()),
                "user_roles": {
                    r[0] for r in conn.execute(
                        sa.text("SELECT unnest(enum_range(NULL::user_role))")
                    )
                } if conn.execute(sa.text(
                    "SELECT 1 FROM pg_type WHERE typname = 'user_role'"
                )).first() else set(),
                "stray_enum_types": {
                    r[0] for r in conn.execute(
                        sa.text(
                            "SELECT typname FROM pg_type "
                            "WHERE typname LIKE '%_old'"
                        )
                    )
                },
                # ``role::text`` on purpose: after the enum is narrowed, the
                # literal 'SYSTEM' is no longer a valid label and a direct
                # comparison raises instead of returning 0.
                "application_columns": {
                    c["name"]
                    for c in sa.inspect(conn).get_columns("applications")
                } if "applications" in set(
                    sa.inspect(conn).get_table_names()
                ) else set(),
                "system_users": conn.execute(
                    sa.text(
                        "SELECT count(*) FROM users WHERE role::text = 'SYSTEM'"
                    )
                ).scalar() if "users" in set(
                    sa.inspect(conn).get_table_names()
                ) else 0,
            }
        return yield_data
    finally:
        engine.dispose()


def test_full_history_upgrades_downgrades_and_upgrades_again_cleanly(
    migrate, temp_database_url
):
    """upgrade head -> downgrade base -> upgrade head, on a fresh database.

    The second upgrade is the part that catches the classic Postgres enum bug:
    a downgrade that forgets ``DROP TYPE`` leaves an orphan and the re-upgrade
    dies with "type ... already exists".
    """
    migrate("upgrade", "head")
    after_first = _inspect(temp_database_url)
    assert "screening_sessions" in after_first["tables"]
    assert "screening_questions" in after_first["tables"]
    assert "screening_answers" in after_first["tables"]
    assert "screening_evaluations" in after_first["tables"]
    assert "candidate_rankings" in after_first["tables"]
    assert "candidate_shortlist_entries" in after_first["tables"]
    assert "interview_guides" in after_first["tables"]
    assert "interview_questions" in after_first["tables"]
    assert "interview_feedback" in after_first["tables"]
    assert "interview_feedback_ratings" in after_first["tables"]
    assert "resume_retry_token" in after_first["application_columns"]
    assert after_first["user_roles"] == {
        "HR", "HIRING_MANAGER", "ADMIN", "SYSTEM",
    }
    assert after_first["system_users"] == 1
    assert after_first["stray_enum_types"] == set()

    migrate("downgrade", "base")
    after_down = _inspect(temp_database_url)
    assert "screening_sessions" not in after_down["tables"]
    assert "screening_questions" not in after_down["tables"]
    assert "screening_answers" not in after_down["tables"]
    assert "screening_evaluations" not in after_down["tables"]
    assert "candidate_rankings" not in after_down["tables"]
    assert "candidate_shortlist_entries" not in after_down["tables"]
    assert "interview_guides" not in after_down["tables"]
    assert "interview_questions" not in after_down["tables"]
    assert "interview_feedback" not in after_down["tables"]
    assert "interview_feedback_ratings" not in after_down["tables"]
    assert after_down["application_columns"] == set()  # table itself is gone
    assert "users" not in after_down["tables"]
    assert after_down["user_roles"] == set()  # type dropped, not orphaned
    assert after_down["stray_enum_types"] == set()

    # The real assertion: this must not raise.
    migrate("upgrade", "head")
    after_second = _inspect(temp_database_url)
    assert after_second["tables"] == after_first["tables"]
    assert after_second["user_roles"] == after_first["user_roles"]
    assert after_second["system_users"] == 1


def test_phase4_revisions_downgrade_and_reupgrade_in_isolation(
    migrate, temp_database_url
):
    """Step just the two Phase 4 revisions back and forth on top of Phase 3."""
    migrate("upgrade", "head")

    migrate("downgrade", _PHASE4_REVISIONS[0])  # drop screening_sessions only
    mid = _inspect(temp_database_url)
    assert "screening_sessions" not in mid["tables"]
    assert "SYSTEM" in mid["user_roles"]
    assert mid["system_users"] == 1

    migrate("downgrade", "e3c212e580f7")  # undo the enum widening + seed
    before_phase4 = _inspect(temp_database_url)
    assert before_phase4["user_roles"] == {"HR", "HIRING_MANAGER", "ADMIN"}
    assert before_phase4["system_users"] == 0
    assert before_phase4["stray_enum_types"] == set()
    assert "users" in before_phase4["tables"]  # earlier schema intact

    migrate("upgrade", "head")
    restored = _inspect(temp_database_url)
    assert "screening_sessions" in restored["tables"]
    assert "SYSTEM" in restored["user_roles"]
    assert restored["system_users"] == 1


def test_reapplying_the_seed_migration_keeps_exactly_one_system_user(
    migrate, temp_database_url
):
    """Downgrade/upgrade of the seeding revision must not accumulate rows."""
    migrate("upgrade", "head")
    for _ in range(2):
        migrate("downgrade", "e3c212e580f7")
        migrate("upgrade", "head")

    assert _inspect(temp_database_url)["system_users"] == 1


def test_screening_sessions_constraints_are_created_as_specified(
    migrate, temp_database_url
):
    migrate("upgrade", "head")
    engine = sa.create_engine(temp_database_url, future=True)
    try:
        with engine.connect() as conn:
            inspector = sa.inspect(conn)

            fks = inspector.get_foreign_keys("screening_sessions")
            assert len(fks) == 1
            assert fks[0]["referred_table"] == "applications"
            assert fks[0]["options"]["ondelete"] == "RESTRICT"

            uniques = {
                u["name"]: u["column_names"]
                for u in inspector.get_unique_constraints("screening_sessions")
            }
            assert uniques["uq_screening_sessions_application"] == [
                "application_id"
            ]

            token_indexes = [
                i for i in inspector.get_indexes("screening_sessions")
                if i["column_names"] == ["access_token"]
            ]
            assert token_indexes and token_indexes[0]["unique"] is True

            # Step 2: the status server-default was realigned CREATED -> PENDING
            # (migration b3d9f0a12c47).
            status_col = next(
                c for c in inspector.get_columns("screening_sessions")
                if c["name"] == "status"
            )
            assert "PENDING" in str(status_col["default"])
            assert "CREATED" not in str(status_col["default"])
    finally:
        engine.dispose()


def test_screening_questions_and_answers_constraints(migrate, temp_database_url):
    """Step 3 (migration d4c81f6a2e50): RESTRICT FKs, the per-round ordering
    unique constraint, and the one-answer-per-question unique index."""
    migrate("upgrade", "head")
    engine = sa.create_engine(temp_database_url, future=True)
    try:
        with engine.connect() as conn:
            inspector = sa.inspect(conn)

            q_fks = {
                fk["referred_table"]: fk["options"].get("ondelete")
                for fk in inspector.get_foreign_keys("screening_questions")
            }
            assert q_fks == {
                "screening_sessions": "RESTRICT",
                "rubric_criteria": "RESTRICT",
            }
            q_uniques = {
                u["name"]: u["column_names"]
                for u in inspector.get_unique_constraints("screening_questions")
            }
            assert q_uniques["uq_screening_questions_session_round_seq"] == [
                "screening_session_id", "round", "sequence_index",
            ]

            a_fks = inspector.get_foreign_keys("screening_answers")
            assert len(a_fks) == 1
            assert a_fks[0]["referred_table"] == "screening_questions"
            assert a_fks[0]["options"]["ondelete"] == "RESTRICT"
            a_q_index = [
                i for i in inspector.get_indexes("screening_answers")
                if i["column_names"] == ["screening_question_id"]
            ]
            assert a_q_index and a_q_index[0]["unique"] is True
    finally:
        engine.dispose()


def test_screening_evaluations_constraints(migrate, temp_database_url):
    """Step 4 (migration e5b2a9c31f74): both FKs RESTRICT, UNIQUE on
    screening_session_id."""
    migrate("upgrade", "head")
    engine = sa.create_engine(temp_database_url, future=True)
    try:
        with engine.connect() as conn:
            inspector = sa.inspect(conn)

            fks = {
                fk["referred_table"]: fk["options"].get("ondelete")
                for fk in inspector.get_foreign_keys("screening_evaluations")
            }
            assert fks == {
                "screening_sessions": "RESTRICT",
                "rubric_versions": "RESTRICT",
            }
            uniques = {
                u["name"]: u["column_names"]
                for u in inspector.get_unique_constraints("screening_evaluations")
            }
            assert uniques["uq_screening_evaluations_session"] == [
                "screening_session_id"
            ]
            cols = {c["name"] for c in inspector.get_columns("screening_evaluations")}
            for c in (
                "results", "requirements_score", "requirements_coverage",
                "experience_score", "behavioral_score", "strengths", "gaps",
                "unknowns", "overall_confidence", "ai_recommendation", "ai_model",
            ):
                assert c in cols
    finally:
        engine.dispose()


def test_candidate_rankings_constraints(migrate, temp_database_url):
    """Step 5 (migration f1a2b3c4d5e6): all three FKs RESTRICT, and the
    UNIQUE (job_id, rubric_version_id, application_id) partition constraint."""
    migrate("upgrade", "head")
    engine = sa.create_engine(temp_database_url, future=True)
    try:
        with engine.connect() as conn:
            inspector = sa.inspect(conn)

            fks = {
                fk["referred_table"]: fk["options"].get("ondelete")
                for fk in inspector.get_foreign_keys("candidate_rankings")
            }
            assert fks == {
                "jobs": "RESTRICT",
                "rubric_versions": "RESTRICT",
                "applications": "RESTRICT",
            }
            uniques = {
                u["name"]: u["column_names"]
                for u in inspector.get_unique_constraints("candidate_rankings")
            }
            assert uniques["uq_candidate_rankings_partition_application"] == [
                "job_id", "rubric_version_id", "application_id",
            ]
            cols = {c["name"] for c in inspector.get_columns("candidate_rankings")}
            for c in (
                "rank_position", "overall_score", "eligible",
                "mandatory_unknown_flag", "generated_at", "generation_batch_id",
            ):
                assert c in cols
    finally:
        engine.dispose()


def test_candidate_shortlist_entries_constraints(migrate, temp_database_url):
    """Step 6 (migration a2b3c4d5e6f7): all four FKs RESTRICT, and the
    UNIQUE (job_id, application_id) one-row-per-application constraint."""
    migrate("upgrade", "head")
    engine = sa.create_engine(temp_database_url, future=True)
    try:
        with engine.connect() as conn:
            inspector = sa.inspect(conn)

            fks = {
                fk["referred_table"]: fk["options"].get("ondelete")
                for fk in inspector.get_foreign_keys("candidate_shortlist_entries")
            }
            assert fks == {
                "jobs": "RESTRICT",
                "applications": "RESTRICT",
                "rubric_versions": "RESTRICT",
                "users": "RESTRICT",
            }
            uniques = {
                u["name"]: u["column_names"]
                for u in inspector.get_unique_constraints(
                    "candidate_shortlist_entries"
                )
            }
            assert uniques["uq_candidate_shortlist_entries_job_application"] == [
                "job_id", "application_id",
            ]
            cols = {
                c["name"]
                for c in inspector.get_columns("candidate_shortlist_entries")
            }
            for c in (
                "is_shortlisted", "reason", "rank_position_at_decision",
                "decided_by_user_id", "decided_at", "created_at", "updated_at",
            ):
                assert c in cols
    finally:
        engine.dispose()


def test_interview_guides_and_questions_constraints(migrate, temp_database_url):
    """Step 7 (migration b3c4d5e6f7a8): interview_guides has four RESTRICT FKs
    and UNIQUE(application_id); interview_questions has two RESTRICT FKs (one
    nullable)."""
    migrate("upgrade", "head")
    engine = sa.create_engine(temp_database_url, future=True)
    try:
        with engine.connect() as conn:
            inspector = sa.inspect(conn)

            g_fks = {
                fk["referred_table"]: fk["options"].get("ondelete")
                for fk in inspector.get_foreign_keys("interview_guides")
            }
            assert g_fks == {
                "jobs": "RESTRICT",
                "applications": "RESTRICT",
                "candidate_shortlist_entries": "RESTRICT",
                "rubric_versions": "RESTRICT",
            }
            g_uniques = {
                u["name"]: u["column_names"]
                for u in inspector.get_unique_constraints("interview_guides")
            }
            assert g_uniques["uq_interview_guides_application"] == ["application_id"]

            q_fks = {
                fk["referred_table"]: fk["options"].get("ondelete")
                for fk in inspector.get_foreign_keys("interview_questions")
            }
            assert q_fks == {
                "interview_guides": "RESTRICT",
                "rubric_criteria": "RESTRICT",
            }
            q_cols = {
                c["name"]: c for c in inspector.get_columns("interview_questions")
            }
            assert q_cols["rubric_criterion_id"]["nullable"] is True
            for c in (
                "category", "sequence_index", "question_text", "evaluates",
                "generated_reason", "created_at",
            ):
                assert c in q_cols
    finally:
        engine.dispose()


def test_interview_feedback_constraints(migrate, temp_database_url):
    """Step 8 (migration c4d5e6f7a8b9): ``interview_feedback`` has three
    RESTRICT FKs and UNIQUE(application_id, interview_round);
    ``interview_feedback_ratings`` is the one CASCADE child. Also pins the three
    CHECK constraints — the first DB-level CHECKs in this schema."""
    migrate("upgrade", "head")
    engine = sa.create_engine(temp_database_url, future=True)
    try:
        with engine.connect() as conn:
            inspector = sa.inspect(conn)

            f_fks = {
                fk["referred_table"]: fk["options"].get("ondelete")
                for fk in inspector.get_foreign_keys("interview_feedback")
            }
            assert f_fks == {
                "applications": "RESTRICT",
                "interview_guides": "RESTRICT",
                "users": "RESTRICT",
            }
            f_uniques = {
                u["name"]: u["column_names"]
                for u in inspector.get_unique_constraints("interview_feedback")
            }
            assert f_uniques["uq_interview_feedback_application_round"] == [
                "application_id", "interview_round",
            ]
            # No uniqueness on application_id alone — rounds accumulate.
            assert not any(
                cols == ["application_id"] for cols in f_uniques.values()
            )
            f_cols = {c["name"]: c for c in inspector.get_columns("interview_feedback")}
            for c in (
                "application_id", "interview_guide_id", "submitted_by_user_id",
                "interview_round", "notes", "recommendation", "created_at",
            ):
                assert c in f_cols
            assert f_cols["notes"]["nullable"] is True
            assert f_cols["interview_round"]["nullable"] is False
            # Deliberately absent: no denormalised display names, no
            # is_current flag, no updated_at (rows are immutable).
            for absent in (
                "is_current", "updated_at", "candidate_name", "application_name",
                "interview_guide_name", "submitted_by_user_name", "ai_model",
            ):
                assert absent not in f_cols

            # The composite history index (application_id, created_at DESC).
            f_index_cols = [
                i["column_names"] for i in inspector.get_indexes("interview_feedback")
            ]
            assert ["application_id"] in f_index_cols
            assert any(
                cols[:1] == ["application_id"] and len(cols) == 2
                for cols in f_index_cols
                if cols
            )

            r_fks = inspector.get_foreign_keys("interview_feedback_ratings")
            assert len(r_fks) == 1
            assert r_fks[0]["referred_table"] == "interview_feedback"
            assert r_fks[0]["options"]["ondelete"] == "CASCADE"
            r_cols = {
                c["name"]: c
                for c in inspector.get_columns("interview_feedback_ratings")
            }
            for c in ("interview_feedback_id", "competency_label", "rating", "comment"):
                assert c in r_cols
            assert r_cols["comment"]["nullable"] is True
            # No rubric linkage and no competency taxonomy (CLAUDE.md §7).
            assert "rubric_criterion_id" not in r_cols

            checks = {
                c["name"]
                for c in inspector.get_check_constraints("interview_feedback")
            } | {
                c["name"]
                for c in inspector.get_check_constraints("interview_feedback_ratings")
            }
            assert "ck_interview_feedback_round_positive" in checks
            assert "ck_interview_feedback_ratings_rating_range" in checks
            assert "ck_interview_feedback_ratings_label_not_blank" in checks
    finally:
        engine.dispose()


def test_post_interview_analyses_constraints(migrate, temp_database_url):
    """Step 9 (migration e6f7a8b9c0d1): ``post_interview_analyses`` has five
    RESTRICT FKs, the (application_id, status) composite index, and
    deliberately NO uniqueness on application_id — superseded rows accumulate.
    Also pins the absence of any disagreement or transcript column."""
    migrate("upgrade", "head")
    engine = sa.create_engine(temp_database_url, future=True)
    try:
        with engine.connect() as conn:
            inspector = sa.inspect(conn)

            fks = {
                fk["referred_table"]: fk["options"].get("ondelete")
                for fk in inspector.get_foreign_keys("post_interview_analyses")
            }
            assert fks == {
                "applications": "RESTRICT",
                "interview_feedback": "RESTRICT",
                "interview_guides": "RESTRICT",
                "rubric_versions": "RESTRICT",
                "users": "RESTRICT",
            }

            cols = {
                c["name"]: c
                for c in inspector.get_columns("post_interview_analyses")
            }
            for c in (
                "application_id", "interview_feedback_id", "interview_guide_id",
                "rubric_version_id", "requested_by_user_id", "summary",
                "strengths", "gaps", "unknowns", "evidence_consistency_notes",
                "confidence", "ai_recommendation",
                "human_recommendation_snapshot", "analyzed_only_latest_feedback",
                "ai_model", "status", "superseded_at", "created_at",
            ):
                assert c in cols
            # Only a non-current row carries a supersession stamp.
            assert cols["superseded_at"]["nullable"] is True
            assert cols["summary"]["nullable"] is False
            assert cols["status"]["nullable"] is False

            # §8 is a later step: nothing here can hold a disagreement
            # verdict, and no interview transcript is stored.
            for absent in (
                "disagreement", "disagreement_flag", "has_disagreement",
                "ai_human_disagreement", "agrees_with_human",
                "interview_transcript", "transcript", "updated_at",
                "candidate_name", "overall_score",
            ):
                assert absent not in cols

            # Regeneration is non-destructive, so multiple rows per
            # application are normal — no UNIQUE(application_id).
            uniques = {
                u["name"]: u["column_names"]
                for u in inspector.get_unique_constraints(
                    "post_interview_analyses"
                )
            }
            assert not any(
                cols_ == ["application_id"] for cols_ in uniques.values()
            )

            index_cols = [
                i["column_names"]
                for i in inspector.get_indexes("post_interview_analyses")
            ]
            assert ["application_id", "status"] in index_cols
            assert ["application_id"] in index_cols

            # status is a validated String, never a native Postgres enum.
            assert cols["status"]["type"].python_type is str
            assert not hasattr(cols["status"]["type"], "enums")
    finally:
        engine.dispose()

def test_applications_resume_retry_token_column(migrate, temp_database_url):
    """Phase D Fix 3 (migration d5e6f7a8b9c0): ``applications`` gains ONE
    nullable, UNIQUE, indexed credential column — and nothing else on that
    table changes."""
    migrate("upgrade", "head")
    engine = sa.create_engine(temp_database_url, future=True)
    try:
        with engine.connect() as conn:
            inspector = sa.inspect(conn)

            cols = {c["name"]: c for c in inspector.get_columns("applications")}
            assert "resume_retry_token" in cols
            token_col = cols["resume_retry_token"]
            # NULLABLE, unlike application_links.token /
            # screening_sessions.access_token: minted lazily, only on failure.
            assert token_col["nullable"] is True
            assert token_col["type"].length == 128

            token_indexes = [
                i for i in inspector.get_indexes("applications")
                if i["column_names"] == ["resume_retry_token"]
            ]
            assert token_indexes, "resume_retry_token must be indexed"
            assert token_indexes[0]["unique"] is True

            # the rest of the table is untouched by this revision
            for untouched in (
                "id", "candidate_id", "job_id", "application_link_id",
                "status", "created_at", "updated_at",
            ):
                assert untouched in cols
            uniques = {
                u["name"]: u["column_names"]
                for u in inspector.get_unique_constraints("applications")
            }
            assert uniques["uq_applications_candidate_job"] == [
                "candidate_id", "job_id",
            ]
    finally:
        engine.dispose()


def test_resume_retry_token_column_is_dropped_on_downgrade(
    migrate, temp_database_url
):
    """Step the one revision back and forward again — the column and its unique
    index must disappear and reappear cleanly."""
    migrate("upgrade", "head")
    migrate("downgrade", "c4d5e6f7a8b9")

    engine = sa.create_engine(temp_database_url, future=True)
    try:
        with engine.connect() as conn:
            inspector = sa.inspect(conn)
            cols = {c["name"] for c in inspector.get_columns("applications")}
            assert "resume_retry_token" not in cols
            assert not [
                i for i in inspector.get_indexes("applications")
                if i["column_names"] == ["resume_retry_token"]
            ]
            # everything else on the table survived the downgrade
            assert {"id", "candidate_id", "job_id", "status"} <= cols
    finally:
        engine.dispose()

    migrate("upgrade", "head")
    engine = sa.create_engine(temp_database_url, future=True)
    try:
        with engine.connect() as conn:
            cols = {
                c["name"] for c in sa.inspect(conn).get_columns("applications")
            }
            assert "resume_retry_token" in cols
    finally:
        engine.dispose()


# --- jobs.job_code (migration f7a8b9c0d1e2) ---------------------------------

_PRE_JOB_CODE = "e6f7a8b9c0d1"     # the revision this migration sits on
_JOB_CODE_REV = "f7a8b9c0d1e2"

_INSERT_JOB = sa.text(
    "INSERT INTO jobs (id, title, jd_source_text, jd_input_method, created_at) "
    "VALUES (:id, :title, 'x', 'TEXT_PASTE', :created_at)"
)


def _fresh_at(migrate, revision):
    """Reset the module-scoped scratch database to ``revision``."""
    migrate("downgrade", "base")
    migrate("upgrade", revision)


def test_job_code_backfill_is_deterministic_and_advances_the_sequence(
    migrate, temp_database_url
):
    """Existing jobs are numbered in (created_at, id) order. Two jobs share a
    timestamp, so the id tiebreak is what makes the result deterministic."""
    _fresh_at(migrate, _PRE_JOB_CODE)
    engine = sa.create_engine(temp_database_url, future=True)
    try:
        with engine.begin() as conn:
            # Inserted in an order that differs from the expected numbering.
            for jid, title, ts in (
                ("00000000-0000-0000-0000-0000000000dd", "late", "2026-03-01 00:00:00+00"),
                ("00000000-0000-0000-0000-0000000000bb", "tie-b", "2026-02-01 00:00:00+00"),
                ("00000000-0000-0000-0000-0000000000ee", "early", "2026-01-01 00:00:00+00"),
                ("00000000-0000-0000-0000-0000000000aa", "tie-a", "2026-02-01 00:00:00+00"),
            ):
                conn.execute(
                    _INSERT_JOB, {"id": jid, "title": title, "created_at": ts}
                )
    finally:
        engine.dispose()

    migrate("upgrade", _JOB_CODE_REV)

    engine = sa.create_engine(temp_database_url, future=True)
    try:
        with engine.connect() as conn:
            got = dict(conn.execute(
                sa.text("SELECT title, job_code FROM jobs")
            ).all())
            assert got == {
                "early": "V_001",
                "tie-a": "V_002",    # same timestamp as tie-b; lower id first
                "tie-b": "V_003",
                "late": "V_004",
            }
            # The sequence continues from the backfill: next new job is N+1.
            assert conn.execute(sa.text("SELECT next_job_code()")).scalar_one() == "V_005"
    finally:
        engine.dispose()


def test_job_code_backfill_pads_correctly_past_999(migrate, temp_database_url):
    """``lpad`` truncates anything longer than its width; the migration widens
    the pad instead, in BOTH the backfill and the column default."""
    _fresh_at(migrate, _PRE_JOB_CODE)
    engine = sa.create_engine(temp_database_url, future=True)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO jobs (id, title, jd_source_text, jd_input_method, created_at) "
                "SELECT md5(random()::text || g::text)::uuid, 'T' || g, 'x', "
                "'TEXT_PASTE', timestamptz '2026-01-01 00:00:00+00' "
                "+ (g || ' seconds')::interval "
                "FROM generate_series(1, 1001) AS g"
            ))
    finally:
        engine.dispose()

    migrate("upgrade", _JOB_CODE_REV)

    engine = sa.create_engine(temp_database_url, future=True)
    try:
        with engine.connect() as conn:
            by_title = dict(conn.execute(
                sa.text("SELECT title, job_code FROM jobs")
            ).all())
            assert len(by_title) == 1001
            assert len(set(by_title.values())) == 1001            # all unique
            assert by_title["T1"] == "V_001"
            assert by_title["T999"] == "V_999"
            assert by_title["T1000"] == "V_1000"                  # widened
            assert by_title["T1001"] == "V_1001"
            assert conn.execute(sa.text("SELECT next_job_code()")).scalar_one() == "V_1002"
    finally:
        engine.dispose()


def test_job_code_migration_on_an_empty_jobs_table(migrate, temp_database_url):
    """setval(seq, 0) is out of range, so the empty case must skip it: the first
    job created afterwards is V_001, not an error."""
    _fresh_at(migrate, _PRE_JOB_CODE)
    migrate("upgrade", _JOB_CODE_REV)

    engine = sa.create_engine(temp_database_url, future=True)
    try:
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT next_job_code()")).scalar_one() == "V_001"
            assert conn.execute(sa.text("SELECT next_job_code()")).scalar_one() == "V_002"
    finally:
        engine.dispose()


def test_job_code_column_constraints_and_default(migrate, temp_database_url):
    migrate("downgrade", "base")
    migrate("upgrade", "head")
    engine = sa.create_engine(temp_database_url, future=True)
    try:
        with engine.connect() as conn:
            inspector = sa.inspect(conn)
            cols = {c["name"]: c for c in inspector.get_columns("jobs")}
            assert cols["job_code"]["nullable"] is False
            assert "next_job_code()" in (cols["job_code"]["default"] or "")

            uniques = {
                u["name"]: u["column_names"]
                for u in inspector.get_unique_constraints("jobs")
            }
            assert uniques["uq_jobs_job_code"] == ["job_code"]

            assert conn.execute(sa.text(
                "SELECT count(*) FROM pg_proc WHERE proname = 'next_job_code'"
            )).scalar_one() == 1
            assert conn.execute(sa.text(
                "SELECT count(*) FROM pg_class "
                "WHERE relkind = 'S' AND relname = 'job_code_seq'"
            )).scalar_one() == 1
    finally:
        engine.dispose()


def test_job_code_downgrade_leaves_nothing_behind_and_reupgrades(
    migrate, temp_database_url
):
    """upgrade -> downgrade -> upgrade. The downgrade must drop the column, the
    constraint, the function AND the sequence, or the re-upgrade dies with
    "already exists"."""
    _fresh_at(migrate, _PRE_JOB_CODE)
    engine = sa.create_engine(temp_database_url, future=True)
    try:
        with engine.begin() as conn:
            conn.execute(_INSERT_JOB, {
                "id": "00000000-0000-0000-0000-0000000000a1",
                "title": "only", "created_at": "2026-01-01 00:00:00+00",
            })
    finally:
        engine.dispose()

    migrate("upgrade", _JOB_CODE_REV)
    migrate("downgrade", _PRE_JOB_CODE)

    engine = sa.create_engine(temp_database_url, future=True)
    try:
        with engine.connect() as conn:
            inspector = sa.inspect(conn)
            assert "job_code" not in {c["name"] for c in inspector.get_columns("jobs")}
            assert "uq_jobs_job_code" not in {
                u["name"] for u in inspector.get_unique_constraints("jobs")
            }
            assert conn.execute(sa.text(
                "SELECT count(*) FROM pg_proc WHERE proname = 'next_job_code'"
            )).scalar_one() == 0
            assert conn.execute(sa.text(
                "SELECT count(*) FROM pg_class "
                "WHERE relkind = 'S' AND relname = 'job_code_seq'"
            )).scalar_one() == 0
            # the job row itself survives the downgrade
            assert conn.execute(sa.text("SELECT count(*) FROM jobs")).scalar_one() == 1
    finally:
        engine.dispose()

    migrate("upgrade", _JOB_CODE_REV)          # must not raise

    engine = sa.create_engine(temp_database_url, future=True)
    try:
        with engine.connect() as conn:
            assert conn.execute(
                sa.text("SELECT job_code FROM jobs")
            ).scalar_one() == "V_001"
    finally:
        engine.dispose()


# --- interview_transcripts (migration a8b9c0d1e2f3) --------------------------

_PRE_TRANSCRIPTS = "f7a8b9c0d1e2"
_TRANSCRIPTS_REV = "a8b9c0d1e2f3"
_ONE_CURRENT_INDEX = "uq_interview_transcripts_one_current_per_feedback"


def test_interview_transcripts_table_and_partial_unique_index_exist_after_upgrade(
    migrate, temp_database_url
):
    migrate("downgrade", "base")
    migrate("upgrade", "head")
    engine = sa.create_engine(temp_database_url, future=True)
    try:
        with engine.connect() as conn:
            inspector = sa.inspect(conn)
            cols = {c["name"]: c for c in inspector.get_columns("interview_transcripts")}
            assert set(cols) == {
                "id", "interview_feedback_id", "uploaded_by_user_id",
                "drive_file_id", "drive_folder_id", "file_name", "mime_type",
                "file_size_bytes", "text_extractable", "status",
                "superseded_at", "created_at",
            }
            assert cols["superseded_at"]["nullable"] is True
            assert all(
                not c["nullable"] for n, c in cols.items() if n != "superseded_at"
            )

            fks = {
                fk["constrained_columns"][0]: (
                    fk["referred_table"], fk["options"].get("ondelete")
                )
                for fk in inspector.get_foreign_keys("interview_transcripts")
            }
            assert fks == {
                "interview_feedback_id": ("interview_feedback", "RESTRICT"),
                "uploaded_by_user_id": ("users", "RESTRICT"),
            }

            definition = conn.execute(sa.text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'interview_transcripts' AND indexname = :n"
            ), {"n": _ONE_CURRENT_INDEX}).scalar_one()
            assert "UNIQUE" in definition
            assert "(interview_feedback_id)" in definition
            assert "status" in definition and "CURRENT" in definition  # the predicate

            # drive_file_id is unique too
            uniques = [
                u["column_names"]
                for u in inspector.get_unique_constraints("interview_transcripts")
            ]
            assert ["drive_file_id"] in uniques
    finally:
        engine.dispose()


def test_interview_transcripts_upgrade_downgrade_upgrade_roundtrip(
    migrate, temp_database_url
):
    migrate("downgrade", "base")
    migrate("upgrade", _TRANSCRIPTS_REV)
    migrate("downgrade", _PRE_TRANSCRIPTS)

    engine = sa.create_engine(temp_database_url, future=True)
    try:
        with engine.connect() as conn:
            assert "interview_transcripts" not in sa.inspect(conn).get_table_names()
            assert conn.execute(sa.text(
                "SELECT count(*) FROM pg_indexes WHERE indexname = :n"
            ), {"n": _ONE_CURRENT_INDEX}).scalar_one() == 0
            # the table this one points at is untouched
            assert "interview_feedback" in sa.inspect(conn).get_table_names()
    finally:
        engine.dispose()

    migrate("upgrade", _TRANSCRIPTS_REV)        # must not raise

    engine = sa.create_engine(temp_database_url, future=True)
    try:
        with engine.connect() as conn:
            assert "interview_transcripts" in sa.inspect(conn).get_table_names()
            assert conn.execute(sa.text(
                "SELECT count(*) FROM pg_indexes WHERE indexname = :n"
            ), {"n": _ONE_CURRENT_INDEX}).scalar_one() == 1
    finally:
        engine.dispose()


# --- post-interview analysis provenance (migration b9c0d1e2f3a4) -------------

_PRE_PIA = "a8b9c0d1e2f3"
_PIA_REV = "b9c0d1e2f3a4"

_U = "00000000-0000-0000-0000-0000000000a1"
_J = "00000000-0000-0000-0000-0000000000a2"
_L = "00000000-0000-0000-0000-0000000000a3"
_C = "00000000-0000-0000-0000-0000000000a4"
_A = "00000000-0000-0000-0000-0000000000a5"
_RV = "00000000-0000-0000-0000-0000000000a6"
_SE = "00000000-0000-0000-0000-0000000000a7"
_G = "00000000-0000-0000-0000-0000000000a8"
_F1 = "00000000-0000-0000-0000-0000000000b1"
_F2 = "00000000-0000-0000-0000-0000000000b2"
_PIA_OLD = "00000000-0000-0000-0000-0000000000c1"
_PIA_NEW = "00000000-0000-0000-0000-0000000000c2"


def _seed_two_analyses(conn):
    """Raw rows at the pre-migration schema: one application with feedback rounds
    1 and 2 and TWO analyses (one superseded, anchored at round 1 with snapshot
    REJECT; one current, anchored at round 2 with snapshot HOLD)."""
    t = sa.text
    conn.execute(t(
        "INSERT INTO users (id, email, hashed_password, full_name, role) "
        "VALUES (:u, 'hr-mig@x.test', 'x', 'HR Mig', 'HR')"), {"u": _U})
    conn.execute(t(
        "INSERT INTO jobs (id, title, jd_source_text, jd_input_method) "
        "VALUES (:j, 'T', 'x', 'TEXT_PASTE')"), {"j": _J})
    conn.execute(t(
        "INSERT INTO application_links (id, job_id, token, sequence_number) "
        "VALUES (:l, :j, 'tok-mig', 1)"), {"l": _L, "j": _J})
    conn.execute(t(
        "INSERT INTO candidates (id, email, full_name) "
        "VALUES (:c, 'c-mig@x.test', 'Cand Mig')"), {"c": _C})
    conn.execute(t(
        "INSERT INTO applications (id, candidate_id, job_id, application_link_id) "
        "VALUES (:a, :c, :j, :l)"), {"a": _A, "c": _C, "j": _J, "l": _L})
    conn.execute(t(
        "INSERT INTO rubric_versions (id, job_id, version_number, "
        "generated_from_requirements_version) VALUES (:rv, :j, 1, 1)"),
        {"rv": _RV, "j": _J})
    conn.execute(t(
        "INSERT INTO candidate_shortlist_entries (id, job_id, application_id, "
        "rubric_version_id, is_shortlisted, decided_by_user_id, decided_at) "
        "VALUES (:se, :j, :a, :rv, true, :u, now())"),
        {"se": _SE, "j": _J, "a": _A, "rv": _RV, "u": _U})
    conn.execute(t(
        "INSERT INTO interview_guides (id, job_id, application_id, "
        "shortlist_entry_id, rubric_version_id, ai_model, generated_at) "
        "VALUES (:g, :j, :a, :se, :rv, 'm', now())"),
        {"g": _G, "j": _J, "a": _A, "se": _SE, "rv": _RV})
    for fid, rnd, rec in ((_F1, 1, "PROCEED"), (_F2, 2, "HOLD")):
        conn.execute(t(
            "INSERT INTO interview_feedback (id, application_id, "
            "interview_guide_id, submitted_by_user_id, interview_round, "
            "recommendation) VALUES (:f, :a, :g, :u, :r, :rec)"),
            {"f": fid, "a": _A, "g": _G, "u": _U, "r": rnd, "rec": rec})
    for pid, fid, snap, status in (
        (_PIA_OLD, _F1, "REJECT", "SUPERSEDED"),
        (_PIA_NEW, _F2, "HOLD", "CURRENT"),
    ):
        conn.execute(t(
            "INSERT INTO post_interview_analyses (id, application_id, "
            "interview_feedback_id, interview_guide_id, rubric_version_id, "
            "requested_by_user_id, summary, evidence_consistency_notes, "
            "confidence, ai_recommendation, human_recommendation_snapshot, "
            "ai_model, status) VALUES (:p, :a, :f, :g, :rv, :u, 's', 'n', "
            "'MEDIUM', 'HOLD', :snap, 'm', :st)"),
            {"p": pid, "a": _A, "f": fid, "g": _G, "rv": _RV, "u": _U,
             "snap": snap, "st": status})


def test_pia_migration_backfills_exactly_one_feedback_row_per_existing_analysis(
    migrate, temp_database_url
):
    _fresh_at(migrate, _PRE_PIA)
    engine = sa.create_engine(temp_database_url, future=True)
    try:
        with engine.begin() as conn:
            _seed_two_analyses(conn)
    finally:
        engine.dispose()

    migrate("upgrade", _PIA_REV)

    engine = sa.create_engine(temp_database_url, future=True)
    try:
        with engine.connect() as conn:
            rows = conn.execute(sa.text(
                "SELECT analysis_id::text, interview_feedback_id::text, "
                "interview_round, recommendation_snapshot "
                "FROM post_interview_analysis_feedback ORDER BY interview_round"
            )).all()
            # exactly one per analysis; round from the feedback row, snapshot from
            # the analysis row (NOT the feedback row's own recommendation)
            assert rows == [
                (_PIA_OLD, _F1, 1, "REJECT"),
                (_PIA_NEW, _F2, 2, "HOLD"),
            ]
            # no transcripts were read by pre-existing analyses
            assert conn.execute(sa.text(
                "SELECT count(*) FROM post_interview_analysis_transcripts"
            )).scalar_one() == 0
            # existing analyses survive untouched and keep the legacy flag
            analyses = conn.execute(sa.text(
                "SELECT id::text, analyzed_only_latest_feedback, "
                "transcript_evidence_notes, transcript_unreadable_rounds::text, "
                "status FROM post_interview_analyses ORDER BY status"
            )).all()
            assert analyses == [
                (_PIA_NEW, True, "", "[]", "CURRENT"),
                (_PIA_OLD, True, "", "[]", "SUPERSEDED"),
            ]
    finally:
        engine.dispose()


def test_pia_migration_keys_foreign_keys_and_columns(migrate, temp_database_url):
    migrate("downgrade", "base")
    migrate("upgrade", "head")
    engine = sa.create_engine(temp_database_url, future=True)
    try:
        with engine.connect() as conn:
            inspector = sa.inspect(conn)

            def fks(table):
                return {
                    fk["constrained_columns"][0]: (
                        fk["referred_table"], fk["options"].get("ondelete")
                    )
                    for fk in inspector.get_foreign_keys(table)
                }

            assert fks("post_interview_analysis_feedback") == {
                "analysis_id": ("post_interview_analyses", "RESTRICT"),
                "interview_feedback_id": ("interview_feedback", "RESTRICT"),
            }
            assert fks("post_interview_analysis_transcripts") == {
                "analysis_id": ("post_interview_analyses", "RESTRICT"),
                "interview_transcript_id": ("interview_transcripts", "RESTRICT"),
            }
            assert inspector.get_pk_constraint("post_interview_analysis_feedback")[
                "constrained_columns"
            ] == ["analysis_id", "interview_feedback_id"]
            assert inspector.get_pk_constraint(
                "post_interview_analysis_transcripts"
            )["constrained_columns"] == ["analysis_id", "interview_transcript_id"]

            # no text is stored in either table
            for table in (
                "post_interview_analysis_feedback",
                "post_interview_analysis_transcripts",
            ):
                types = {
                    c["name"]: str(c["type"]).upper()
                    for c in inspector.get_columns(table)
                }
                assert not any("TEXT" in t for t in types.values()), (table, types)
            fb_cols = {
                c["name"]: c for c in inspector.get_columns(
                    "post_interview_analysis_feedback"
                )
            }
            assert set(fb_cols) == {
                "analysis_id", "interview_feedback_id", "interview_round",
                "recommendation_snapshot",
            }
            assert all(not c["nullable"] for c in fb_cols.values())

            analyses = {
                c["name"]: c for c in inspector.get_columns("post_interview_analyses")
            }
            for name in ("transcript_evidence_notes", "transcript_unreadable_rounds"):
                assert analyses[name]["nullable"] is False
            assert "''" in analyses["transcript_evidence_notes"]["default"]
            assert "'[]'" in analyses["transcript_unreadable_rounds"]["default"]
            # the anchor column keeps its NOT NULL
            assert analyses["interview_feedback_id"]["nullable"] is False
    finally:
        engine.dispose()


def test_pia_migration_upgrade_downgrade_upgrade_roundtrip(
    migrate, temp_database_url
):
    _fresh_at(migrate, _PRE_PIA)
    engine = sa.create_engine(temp_database_url, future=True)
    try:
        with engine.begin() as conn:
            _seed_two_analyses(conn)
    finally:
        engine.dispose()

    migrate("upgrade", _PIA_REV)
    migrate("downgrade", _PRE_PIA)

    engine = sa.create_engine(temp_database_url, future=True)
    try:
        with engine.connect() as conn:
            inspector = sa.inspect(conn)
            tables = set(inspector.get_table_names())
            assert "post_interview_analysis_feedback" not in tables
            assert "post_interview_analysis_transcripts" not in tables
            cols = {c["name"] for c in inspector.get_columns("post_interview_analyses")}
            assert "transcript_evidence_notes" not in cols
            assert "transcript_unreadable_rounds" not in cols
            # the analyses themselves survive the downgrade
            assert conn.execute(sa.text(
                "SELECT count(*) FROM post_interview_analyses"
            )).scalar_one() == 2
    finally:
        engine.dispose()

    migrate("upgrade", _PIA_REV)          # must not raise, and backfills again

    engine = sa.create_engine(temp_database_url, future=True)
    try:
        with engine.connect() as conn:
            assert conn.execute(sa.text(
                "SELECT count(*) FROM post_interview_analysis_feedback"
            )).scalar_one() == 2
    finally:
        engine.dispose()


# --- final rankings (migration c0d1e2f3a4b5) ----------------------------------

_PRE_FR = "b9c0d1e2f3a4"
_FR_REV = "c0d1e2f3a4b5"
_ONE_CURRENT_RUN = "uq_final_rankings_one_current_per_partition"


def test_final_rankings_tables_columns_keys_and_partial_unique_index(
    migrate, temp_database_url
):
    migrate("downgrade", "base")
    migrate("upgrade", "head")
    engine = sa.create_engine(temp_database_url, future=True)
    try:
        with engine.connect() as conn:
            inspector = sa.inspect(conn)

            runs = {c["name"]: c for c in inspector.get_columns("final_rankings")}
            assert set(runs) == {
                "id", "job_id", "rubric_version_id", "requested_by_user_id",
                "screening_weight", "interview_weight", "status",
                "superseded_at", "created_at",
            }
            assert runs["superseded_at"]["nullable"] is True

            entries = {
                c["name"]: c for c in inspector.get_columns("final_ranking_entries")
            }
            assert set(entries) == {
                "id", "final_ranking_id", "application_id", "screening_score",
                "interview_score", "final_score", "rank", "eligible",
                "mandatory_unknown", "entry_status", "status_reason",
                "final_confidence", "screening_confidence",
                "screening_generation_batch_id", "screening_generated_at",
                "screening_rank", "post_interview_analysis_id", "rounds_used",
                "round_means", "feedback_ids",
            }
            for name in ("screening_score", "interview_score", "final_score", "rank"):
                assert entries[name]["nullable"] is True
            # No name / notes / evidence / transcript column of any kind.
            assert not any(
                k in n for n in entries for k in ("name", "email", "notes", "evidence", "transcript")
            )

            def fks(table):
                return {
                    fk["constrained_columns"][0]: (
                        fk["referred_table"], fk["options"].get("ondelete")
                    )
                    for fk in inspector.get_foreign_keys(table)
                }

            assert fks("final_rankings") == {
                "job_id": ("jobs", "RESTRICT"),
                "rubric_version_id": ("rubric_versions", "RESTRICT"),
                "requested_by_user_id": ("users", "RESTRICT"),
            }
            assert fks("final_ranking_entries") == {
                "final_ranking_id": ("final_rankings", "RESTRICT"),
                "application_id": ("applications", "RESTRICT"),
                "post_interview_analysis_id": ("post_interview_analyses", "RESTRICT"),
            }
            # deliberately NO foreign key into Step 5 / the evaluations
            referred = {
                fk["referred_table"]
                for fk in inspector.get_foreign_keys("final_ranking_entries")
            }
            assert "candidate_rankings" not in referred
            assert "screening_evaluations" not in referred

            uniques = {
                u["name"]: u["column_names"]
                for u in inspector.get_unique_constraints("final_ranking_entries")
            }
            assert uniques["uq_final_ranking_entries_run_application"] == [
                "final_ranking_id", "application_id",
            ]

            definition = conn.execute(sa.text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'final_rankings' AND indexname = :n"
            ), {"n": _ONE_CURRENT_RUN}).scalar_one()
            assert "UNIQUE" in definition
            assert "(job_id, rubric_version_id)" in definition
            assert "status" in definition and "CURRENT" in definition
    finally:
        engine.dispose()


def test_final_rankings_upgrade_downgrade_upgrade_roundtrip(migrate, temp_database_url):
    migrate("downgrade", "base")
    migrate("upgrade", _FR_REV)
    migrate("downgrade", _PRE_FR)

    engine = sa.create_engine(temp_database_url, future=True)
    try:
        with engine.connect() as conn:
            tables = set(sa.inspect(conn).get_table_names())
            assert "final_rankings" not in tables
            assert "final_ranking_entries" not in tables
            assert conn.execute(sa.text(
                "SELECT count(*) FROM pg_indexes WHERE indexname = :n"
            ), {"n": _ONE_CURRENT_RUN}).scalar_one() == 0
            # the tables it pointed at are untouched
            assert "post_interview_analyses" in tables and "applications" in tables
    finally:
        engine.dispose()

    migrate("upgrade", _FR_REV)          # must not raise

    engine = sa.create_engine(temp_database_url, future=True)
    try:
        with engine.connect() as conn:
            assert {"final_rankings", "final_ranking_entries"} <= set(
                sa.inspect(conn).get_table_names()
            )
            assert conn.execute(sa.text(
                "SELECT count(*) FROM pg_indexes WHERE indexname = :n"
            ), {"n": _ONE_CURRENT_RUN}).scalar_one() == 1
    finally:
        engine.dispose()
