"""ResumeExtraction model — the structured evidence inventory parsed from one
resume document (CLAUDE.md §§3, 18, 23).

One row = one successful run of the ``resume_parsing`` AI task against one
``documents`` row. The row stores the validated JSON evidence inventory plus
enough provenance to reconstruct how it was produced.

Design decisions
----------------
ID type — **UUID v4**, Python-side ``default=uuid.uuid4`` (project precedent).

``document_id`` — **required FK, ``ON DELETE RESTRICT``**. Same non-cascading
convention as ``documents`` / ``applications``: an AI extraction is a
business/audit record and must not silently vanish because its document row was
deleted. Candidate-data deletion (CLAUDE.md §24), when supported, will be an
explicit audited orchestration that removes extractions first.

``extracted_data`` — **JSONB**. The service writes only a schema-validated
``ResumeExtractionResult.model_dump()`` here (CLAUDE.md §18: validate AI output
before storing). It is an *evidence inventory*, never an assessment — no
PASS/FAIL/UNKNOWN, no scores.

``ai_model`` — the resolved model id that produced this extraction (e.g.
``claude-haiku-4-5-20251001``). Kept for traceability / reproducibility
(CLAUDE.md §23) so a stored result can always be tied back to the exact AI
configuration behind it.

No status / version column — **deliberately**. This step does not version
re-extractions. ``resume_parsing_service.parse_resume`` refuses to overwrite an
existing extraction unless called with ``force=True`` (which deletes + replaces
the row). Whether re-extractions should instead be retained/versioned
(supersession, like ``rubric_versions``) is an open question left for a later
phase.

No ``updated_at``: a row is write-once. A re-extraction with ``force=True`` is a
delete + fresh insert, not an in-place update.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.database import Base


class ResumeExtraction(Base):
    """Postgres record of one resume-parsing AI run and its evidence inventory."""

    __tablename__ = "resume_extractions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # Schema-validated ResumeExtractionResult.model_dump(). Evidence only.
    extracted_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    # Resolved model id that produced this extraction (CLAUDE.md §23).
    ai_model: Mapped[str] = mapped_column(String(100), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper only
        # No extracted content (may echo resume text / PII).
        return (
            f"<ResumeExtraction id={self.id!r} document_id={self.document_id!r} "
            f"ai_model={self.ai_model!r}>"
        )
