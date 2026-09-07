"""PrequalificationResult model — the per-criterion PASS/FAIL/UNKNOWN comparison
of one application's resume evidence against the approved rubric
(CLAUDE.md §§4, 12, 20, 23, B).

One row = one run of the ``prequalification`` AI task for one application,
against one approved rubric version, using one resume extraction. The row stores
the full list of per-criterion results (AI judgment + Python-computed
confidence) plus provenance.

Design decisions
----------------
ID type — **UUID v4**, Python-side ``default=uuid.uuid4`` (project precedent).

Three FKs, all **``ON DELETE RESTRICT``** — same non-cascading convention as
``resume_extractions`` / ``documents`` / ``applications``. A prequalification is
a business/audit record; none of the things it references may vanish underneath
it:
* ``application_id``       — whose application this assessed.
* ``rubric_version_id``    — **which rubric version it was judged against**.
  Rubrics can be regenerated and re-approved; an old prequalification must stay
  interpretable against the exact criteria it actually ran on (§23). This is
  why the version id is stored, not just the job id.
* ``resume_extraction_id`` — which extracted evidence snapshot was used.
  NOTE: because this is RESTRICT, force-re-parsing a resume that already has a
  prequalification will be blocked by this FK until the prequalification is
  removed/replaced. That is the correct conservative behaviour (§23) and a known
  interaction for the deferred re-run/versioning cleanup.

``results`` — **JSONB**, a list of per-criterion objects. Each object carries
the AI's ``result`` / ``evidence_summary`` / ``reasoning``, the
**Python-computed** ``confidence``, and denormalized ``criterion_id`` /
``criterion_index`` / ``requirement_type`` / ``category`` / ``criterion_text``
so the stored assessment is self-contained for display and audit without joins.
There is **no** AI-provided overall recommendation / score / confidence here —
that boundary is enforced in ``app/ai/schemas/prequalification.py`` and the
service.

``ai_model`` — resolved model id that produced the judgments (§23 traceability).

No status / version column — **deliberately**, same as ``resume_extractions``.
``prequalification_service.prequalify_application`` refuses to overwrite unless
called with ``force=True`` (delete + replace). Whether re-runs should be
retained/versioned is an open question left for a later phase (see the memory
note ``resume-reextraction-deferred`` — prequalification follows the same
pattern for now).

No ``updated_at``: a row is write-once.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.database import Base


class PrequalificationResult(Base):
    """Postgres record of one prequalification AI run and its per-criterion results."""

    __tablename__ = "prequalification_results"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("applications.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    rubric_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("rubric_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    resume_extraction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("resume_extractions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # List of per-criterion result objects (AI judgment + Python confidence +
    # denormalized criterion fields). Validated before write. Evidence-and-
    # judgment, never an aggregate recommendation.
    results: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)

    ai_model: Mapped[str] = mapped_column(String(100), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper only
        # No criterion / evidence text (may echo candidate content).
        return (
            f"<PrequalificationResult id={self.id!r} "
            f"application_id={self.application_id!r} "
            f"rubric_version_id={self.rubric_version_id!r} "
            f"ai_model={self.ai_model!r}>"
        )
