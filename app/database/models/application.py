"""Application model — one candidate's application to one job.

Phase 2 scope: the candidate x job link plus a lifecycle ``status``. Resume
upload / parsing / prequalification are later steps; this model only carries the
status values that the schema itself needs now.

Design decisions
----------------
ID type — **UUID v4**, Python-side ``default=uuid.uuid4`` (project precedent).

``status`` — **validated String, NOT a native Postgres ENUM**. The application
lifecycle vocabulary grows every phase (screening, scoring, shortlisting,
interview, decision...). Same growing-vocabulary rule as ``JobStatus`` /
``ApplicationLinkStatus``: a ``String`` column validated at the service layer
against :class:`ApplicationStatus`. Only the resume-processing states plus
``APPLIED`` are reachable in this step.

``application_link_id`` — **required FK**. Every application comes in through a
specific link; recording which one closes the Phase 1.5 tracking gap.

FK delete behaviour — **all three FKs are ``ondelete='RESTRICT'``**. An
``applications`` row is a business-critical record: potentially a candidate's
only trace in the system, and the anchor for every downstream AI/human
assessment. It must never disappear as a silent side effect of deleting a
``candidate``, ``job`` or ``application_link`` row — that would destroy audit
history with no human decision point (CLAUDE.md §§12, 20, 23). Deletion of a
candidate's data, when it is eventually supported (CLAUDE.md §24), will be an
explicit, audited orchestration that removes the applications first — not a
cascade. NB: this is the first ``RESTRICT`` FK in the codebase; the prior
convention was only ``CASCADE`` (child-of-aggregate) or ``SET NULL`` (nullable
actor ref). ``RESTRICT`` is the right third option for "independent business
record that outlives casual parent deletion".

``(candidate_id, job_id)`` — **unique**. One application per person per job; no
re-application in this MVP. The service layer raises a clear error before the
constraint fires, with the constraint as the backstop.

``candidates.email`` / ``candidates.phone`` are personal data — never put them in
this row's audit metadata. Reference ``candidate_id`` only.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.database import Base


class ApplicationStatus:
    """Lifecycle states for an application (validated string column).

    Later phases append members here with no schema migration (same pattern as
    :class:`~app.database.models.job.JobStatus`). Seeded with only what the
    Phase 2 schema needs:

    * ``APPLIED``            — application submitted; resume not yet processed.
    * ``RESUME_PROCESSING``  — resume parsing in progress.
    * ``RESUME_PROCESSED``   — resume parsed; evidence extracted.
    * ``RESUME_FAILED``      — resume could not be parsed (candidate may re-upload).
    * ``PREQUALIFICATION_COMPLETED`` — the prequalification AI task has run for
      this application. This is a **pipeline-stage** marker ("the task ran"),
      NOT a candidate judgment. It is deliberately not called ``PREQUALIFIED``:
      prequalification produces per-criterion PASS/FAIL/UNKNOWN, never an
      overall pass/fail, and the AI is forbidden from deciding qualification
      (CLAUDE.md §§4, 11, B). Same "task ran" semantics as ``RESUME_PROCESSED``.

    Phase 4 adds the three screening states (CLAUDE.md §2A item 3). Same
    "pipeline stage reached", never "candidate judged", semantics as above:

    * ``SCREENING_IN_PROGRESS``  — a ``screening_sessions`` row exists and the
      candidate's AI screening is open. Set by
      ``screening_pipeline_service`` when the session is created.
    * ``SCREENING_COMPLETED``    — the candidate finished the screening
      conversation. (Reached in a later Phase 4 step; declared here so the
      vocabulary is defined in one place.)
    * ``SCREENING_INCOMPLETE``   — the candidate abandoned screening. Per
      CLAUDE.md §3 and §2A item 3 this is NEVER automatically converted to a
      rejection, and it is NOT a FAIL. There is deliberately no ``REJECTED``
      member in this class at all: rejection is a human decision recorded
      separately (CLAUDE.md §11). (Also reached in a later step.)

    Phase 4 Step 4 adds one more "pipeline stage reached" marker:

    * ``SCREENING_EVALUATED``    — the per-candidate screening-evaluation AI
      task has run and a ``screening_evaluations`` row (initial scorecard,
      CLAUDE.md §4) exists for this application. Like ``PREQUALIFICATION_
      COMPLETED`` it means "the task ran", NOT a hire/no-hire judgment — the
      row carries an ``ai_recommendation`` of PROCEED/HOLD only, never REJECT,
      and the final decision stays with a human (§11).
    """

    APPLIED = "APPLIED"
    RESUME_PROCESSING = "RESUME_PROCESSING"
    RESUME_PROCESSED = "RESUME_PROCESSED"
    RESUME_FAILED = "RESUME_FAILED"
    PREQUALIFICATION_COMPLETED = "PREQUALIFICATION_COMPLETED"
    SCREENING_IN_PROGRESS = "SCREENING_IN_PROGRESS"
    SCREENING_COMPLETED = "SCREENING_COMPLETED"
    SCREENING_INCOMPLETE = "SCREENING_INCOMPLETE"
    SCREENING_EVALUATED = "SCREENING_EVALUATED"

    ALL: frozenset[str] = frozenset(
        {
            APPLIED,
            RESUME_PROCESSING,
            RESUME_PROCESSED,
            RESUME_FAILED,
            PREQUALIFICATION_COMPLETED,
            SCREENING_IN_PROGRESS,
            SCREENING_COMPLETED,
            SCREENING_INCOMPLETE,
            SCREENING_EVALUATED,
        }
    )

    @classmethod
    def is_valid(cls, value: str) -> bool:
        return value in cls.ALL


class Application(Base):
    """One candidate's application to one job."""

    __tablename__ = "applications"

    __table_args__ = (
        UniqueConstraint(
            "candidate_id", "job_id", name="uq_applications_candidate_job"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    application_link_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("application_links.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # Validated against ApplicationStatus at the service layer.
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        server_default=text(f"'{ApplicationStatus.APPLIED}'"),
    )

    # Opaque per-application credential for resuming a FAILED résumé upload.
    # Minted lazily by ``candidate_portal_service`` on the first
    # ``RESUME_UPLOAD_FAILED`` outcome and never on any successful path, so it
    # is NULL for the overwhelming majority of rows — hence nullable, unlike
    # ``application_links.token`` / ``screening_sessions.access_token`` which are
    # NOT NULL. Same method and column width as both of those
    # (``secrets.token_urlsafe(32)`` -> 43 chars, 256 bits; ``String(128)``,
    # UNIQUE + indexed). In Postgres a UNIQUE column permits many NULLs, so the
    # constraint costs nothing on rows that never failed.
    #
    # WHY THIS EXISTS
    # A résumé upload failure leaves an application row with no document. Before
    # this column the only route back to that row was ``st.session_state``,
    # which a browser refresh clears — stranding the candidate with an
    # application they could never complete. The three identifiers already in
    # play are all unusable as the recovery credential: ``application_links.
    # token`` is shared by every candidate for the job, ``applications.id`` is a
    # non-secret surrogate PK (CLAUDE.md §2A item 5), and
    # ``screening_sessions.access_token`` does not exist yet — the pipeline that
    # mints it never starts when the upload fails.
    #
    # NEVER log, audit, or put this value in an error message. It is
    # credential-equivalent, exactly like the other two tokens.
    resume_retry_token: Mapped[str | None] = mapped_column(
        String(128), nullable=True, unique=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper only
        return (
            f"<Application id={self.id!r} job_id={self.job_id!r} "
            f"status={self.status!r}>"
        )
