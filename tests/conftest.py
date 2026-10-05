"""Shared pytest fixtures.

Tests for the persistence/auth layer run against the **real local Postgres**
database (the same one in ``.env``), not SQLite or a mock. Reasons:

* This layer's job *is* the database — native ``ENUM``/``JSONB`` columns, the
  ``UNIQUE`` constraint on ``users.email``, server-side defaults, and the audit
  row written in the same transaction. SQLite would not exercise any of that
  faithfully, and mocking the session would test almost nothing.
* CLAUDE.md §21 requires mocking the *Claude API* in unit tests. It does not
  ask for the database to be mocked, and this code has no AI calls.

Isolation: each test gets a ``db`` session joined to an outer transaction that
is **rolled back** in teardown (``join_transaction_mode="create_savepoint"`` so
the service's own ``commit()`` only releases a savepoint). Nothing the tests
write is ever persisted, so there is no cleanup step and no leftover test users.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.database.database import engine

_AI_RESPONSES_DIR = Path(__file__).parent / "fixtures" / "ai_responses"

_SAMPLE_JD_TEXT = (
    "Senior Data Engineer\n"
    "We need 5+ years building data pipelines with PySpark on AWS.\n"
    "Strong communication skills required."
)


@pytest.fixture
def db():
    connection = engine.connect()
    outer_transaction = connection.begin()
    session = Session(
        bind=connection,
        join_transaction_mode="create_savepoint",
    )
    try:
        yield session
    finally:
        session.close()
        outer_transaction.rollback()
        connection.close()


@pytest.fixture(autouse=True)
def _no_real_screening_evaluation_call(mocker):
    """Phase 4 Step 4: reaching ``SCREENING_COMPLETE`` fires an automatic
    ``evaluate_screening`` (best-effort, ``_trigger_screening_evaluation``).
    Its AI call must never hit the network in the suite. By default it raises
    — the trigger swallows that and safely no-ops — so tests that only care
    about the round lifecycle need no extra setup. Tests that DO want the
    evaluation just ``mocker.patch.object`` the same seam themselves (that
    patch wins over this one).
    """
    from app.ai.claude_client import AIRequestError
    from app.services import screening_evaluation_service

    mocker.patch.object(
        screening_evaluation_service,
        "get_structured_response",
        side_effect=AIRequestError("screening_evaluation not mocked in this test"),
    )


@pytest.fixture(autouse=True)
def _drive_folder_usable_defaults_to_true(mocker):
    """Job-folder resolution first verifies a stored ``drive_folder_id`` via the
    ``_drive_folder_usable`` seam (job codes / Drive folder naming). No test may
    reach the real Drive SDK, and several suites carry their own four-seam
    ``FakeDrive`` that predates this seam. By default a stored folder id is
    treated as usable — i.e. "reuse the folder this job already has", which is
    exactly the behaviour those suites expect on a second upload. Tests of the
    verification itself ``mocker.patch.object`` the same seam (that patch wins
    over this one), exactly as with the fixture above.
    """
    from app.services import storage_service

    mocker.patch.object(
        storage_service, "_drive_folder_usable", return_value=True
    )


# --- Real document fixtures (built with the actual libraries, never mocked) ---


@pytest.fixture(scope="session")
def sample_pdf_bytes() -> bytes:
    """A tiny but genuinely valid single-page PDF containing JD-like text."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), _SAMPLE_JD_TEXT)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture(scope="session")
def sample_docx_bytes() -> bytes:
    """A tiny but genuinely valid .docx containing JD-like text."""
    from docx import Document

    document = Document()
    for line in _SAMPLE_JD_TEXT.split("\n"):
        document.add_paragraph(line)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


@pytest.fixture(scope="session")
def corrupt_document_bytes() -> bytes:
    """Plain text — not a valid PDF or DOCX — for failure-path tests."""
    return b"this is definitely not a real office document"


# --- Canned Claude responses (Phase 1.2+; the Claude API is always mocked) ---


@pytest.fixture(scope="session")
def ai_response_json():
    """Return a loader: ``ai_response_json("jd_analysis_valid.json") -> str``.

    Returns the raw JSON *text* of a fixture under tests/fixtures/ai_responses/,
    i.e. exactly what Claude's response text would contain.
    """

    def _load(name: str) -> str:
        return (_AI_RESPONSES_DIR / name).read_text(encoding="utf-8")

    return _load


@pytest.fixture(scope="session")
def ai_response_dict(ai_response_json):
    """Return a loader: ``ai_response_dict("jd_analysis_valid.json") -> dict``."""

    def _load(name: str) -> dict:
        return json.loads(ai_response_json(name))

    return _load
