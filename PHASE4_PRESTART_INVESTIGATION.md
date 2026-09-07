# Phase 4 (AI Screening) — Pre-Start Investigation Report

Read-only inspection of the current codebase. No code was changed. Every answer
below is grounded in the files/lines cited.

---

## 1. `ApplicationStatus` enum values

### Direct answer
`ApplicationStatus` is a **plain Python constants class over a validated
`String(50)` column** (not a native Postgres ENUM), exactly as the "growing
vocabulary" convention predicts. It defines **5 values**. **None** of them cover
"screening in progress", "screening completed", or "screening
incomplete/abandoned" — there is no screening-related status at all yet.

### Supporting evidence

Defined in [`app/database/models/application.py:61`](app/database/models/application.py:61) (class body lines 61–98):

```python
class ApplicationStatus:
    APPLIED = "APPLIED"                                  # line 80
    RESUME_PROCESSING = "RESUME_PROCESSING"              # line 81
    RESUME_PROCESSED = "RESUME_PROCESSED"                # line 82
    RESUME_FAILED = "RESUME_FAILED"                      # line 83
    PREQUALIFICATION_COMPLETED = "PREQUALIFICATION_COMPLETED"  # line 84

    ALL: frozenset[str] = frozenset({ ... })             # lines 86–94
    @classmethod
    def is_valid(cls, value: str) -> bool: ...           # lines 96–98
```

The column itself — [`app/database/models/application.py:138`](app/database/models/application.py:138):

```python
status: Mapped[str] = mapped_column(
    String(50),
    nullable=False,
    server_default=text(f"'{ApplicationStatus.APPLIED}'"),
)
```

Module docstring confirms the design choice — [`app/database/models/application.py:11-16`](app/database/models/application.py:11):

> `status` — **validated String, NOT a native Postgres ENUM**. The application
> lifecycle vocabulary grows every phase (screening, scoring, shortlisting,
> interview, decision...). Same growing-vocabulary rule as `JobStatus` /
> `ApplicationLinkStatus`: a `String` column validated at the service layer
> against `ApplicationStatus`.

Migration confirms no PG ENUM type is created —
[`app/database/migrations/versions/e7617aa2220b_create_candidates_and_applications.py:61-64`](app/database/migrations/versions/e7617aa2220b_create_candidates_and_applications.py:61)
(`status` is `sa.String(length=50)`, server default `'APPLIED'`) and docstring lines 12–16.

Only one definition exists. Grep for `class ApplicationStatus` across `app/`
returns exactly one hit (`app/database/models/application.py:61`); nothing in
`app/utils/`.

Current writers of the field:
- `ApplicationStatus.APPLIED` — [`app/services/application_service.py:153`](app/services/application_service.py:153)
- `ApplicationStatus.RESUME_PROCESSED` — [`app/services/resume_parsing_service.py:277`](app/services/resume_parsing_service.py:277)
- `ApplicationStatus.PREQUALIFICATION_COMPLETED` — [`app/services/prequalification_service.py:359`](app/services/prequalification_service.py:359)

(Note: `RESUME_PROCESSING` and `RESUME_FAILED` are declared but not assigned
anywhere in the current service code — parsing moves `APPLIED` → `RESUME_PROCESSED`
directly.)

### Screening coverage — explicit statement
- **Screening in progress** — NOT PRESENT. No value exists.
- **Screening completed** — NOT PRESENT. No value exists.
- **Screening incomplete / abandoned** — NOT PRESENT. No value exists.

CLAUDE.md §3 requires a distinct `INCOMPLETE` status for an abandoned screening
("Do not automatically mark the candidate as REJECTED"); there is currently no
`ApplicationStatus` member for it, nor for any other screening state.

---

## 2. `UserRole` enum values and existing "system"-style actors

### Direct answer
`UserRole` **is** a native Postgres ENUM (`user_role`), confirmed in both the
model and the migration. It has **3 values: `HR`, `HIRING_MANAGER`, `ADMIN`** —
all explicitly "internal staff". **No** system / service-account / non-human
`User` row is seeded anywhere (no seed data migration, and `app/seed.py` only
creates a human operator from CLI args). A "not a real human" actor for
AI-initiated audit events **does not exist and would need to be created** — or
the existing `user_id=None` (nullable FK) convention would be used, which is what
every current non-authenticated / system-ish audit write already does.

### Supporting evidence

Model — [`app/database/models/user.py:52`](app/database/models/user.py:52):

```python
class UserRole(str, enum.Enum):
    HR = "HR"                          # line 60
    HIRING_MANAGER = "HIRING_MANAGER"  # line 61
    ADMIN = "ADMIN"                    # line 62
```

Column is a native enum — [`app/database/models/user.py:89-92`](app/database/models/user.py:89):

```python
role: Mapped[UserRole] = mapped_column(
    Enum(UserRole, name="user_role", native_enum=True, validate_strings=True),
    nullable=False,
)
```

Model docstring lines 29–35 confirm the deliberate choice ("**enum, not free
text**... A DB-backed enum... The cost — needing a migration to add a value — is
fine here").

Migration — [`app/database/migrations/versions/df02835dc658_create_users_and_audit_events.py:35-37`](app/database/migrations/versions/df02835dc658_create_users_and_audit_events.py:35):

```python
user_role_enum = postgresql.ENUM(
    'HR', 'HIRING_MANAGER', 'ADMIN', name='user_role', create_type=False
)
```

with explicit `user_role_enum.create(bind, checkfirst=True)` (line 43) and
`.drop(...)` in downgrade (line 125).

**Seeded system user — not found.**
- [`app/seed.py`](app/seed.py) is a one-off CLI script (`python -m app.seed`). It
  takes `--email`, `--full-name`, `--role` (default `ADMIN`, choices restricted
  to `UserRole` values — lines 47–54) and a password from `SEED_ADMIN_PASSWORD`
  env or interactive prompt. It creates exactly one human user via
  `create_user(...)` (line 60). No hard-coded email, no `system@`, no service
  account.
- No migration inserts any `users` row. The only migration touching `users` is
  `df02835dc658` (schema only, no `op.bulk_insert` / data).
- Grep across `app/` for `system@` / `service_account` / `SYSTEM_USER` /
  `system_user` / `seed_user` returns only `create_user(` at
  `app/services/auth_service.py:87` and `app/seed.py:60`. No system actor.

**Existing convention for non-human / unauthenticated actors:** the audit FK
`audit_events.user_id` is **nullable with `ON DELETE SET NULL`**
([`app/database/models/audit_event.py:125-130`](app/database/models/audit_event.py:125)),
and every current write from an unauthenticated or system-ish context passes
`user_id=None`:
- [`app/services/application_service.py:170`](app/services/application_service.py:170) — `user_id=None,  # public, unauthenticated flow`
- [`app/services/storage_service.py:469`](app/services/storage_service.py:469) — `user_id=None,  # reached from the public, unauthenticated flow`
- [`app/services/candidate_portal_service.py:183`](app/services/candidate_portal_service.py:183) — `user_id=None`
- `audit_service.record_event` signature default — [`app/services/audit_service.py:30`](app/services/audit_service.py:30) — `user_id: uuid.UUID | None = None`

### Ambiguity flagged
Whether a Phase 4 AI-initiated screening event should use a **new dedicated
"system" `User` row** (requires either a new `UserRole` value + enum migration,
or reusing an existing role) **or** the **existing `user_id=None` convention** is
a genuine open design choice — the codebase currently only demonstrates the
`user_id=None` path, but every one of those is a *candidate-initiated* action,
not an *AI-initiated* one. This report does not resolve it.

---

## 3. `AuditEventType` enum values

### Direct answer
`AuditEventType` is a `str`-Enum used as an **application-layer validation list
over a `String(100)` column** (deliberately NOT a DB enum). Screening event types
**already exist, verbatim**: **`AI_SCREENING_STARTED`** and
**`AI_SCREENING_COMPLETED`**. These are exact matches, not near-misses.

### Supporting evidence

Defined in [`app/database/models/audit_event.py:60`](app/database/models/audit_event.py:60) (members, lines 70–101):

```python
class AuditEventType(str, enum.Enum):
    USER_CREATED = "USER_CREATED"                              # 70
    JOB_CREATED = "JOB_CREATED"                                # 72
    JOB_OPENED = "JOB_OPENED"                                  # 73
    JOB_CLOSED = "JOB_CLOSED"                                  # 74
    JD_ANALYZED = "JD_ANALYZED"                                # 75
    RUBRIC_GENERATED = "RUBRIC_GENERATED"                      # 76
    RUBRIC_EDITED = "RUBRIC_EDITED"                            # 77
    RUBRIC_APPROVED = "RUBRIC_APPROVED"                        # 78
    RUBRIC_VERSION_CREATED = "RUBRIC_VERSION_CREATED"          # 79
    APPLICATION_LINK_GENERATED = "APPLICATION_LINK_GENERATED"  # 82
    APPLICATION_LINK_REVOKED = "APPLICATION_LINK_REVOKED"      # 83
    APPLICATION_LINK_VIEWED = "APPLICATION_LINK_VIEWED"        # 85
    CANDIDATE_APPLIED = "CANDIDATE_APPLIED"                    # 86
    RESUME_UPLOADED = "RESUME_UPLOADED"                        # 87
    RESUME_PROCESSED = "RESUME_PROCESSED"                      # 88
    PREQUALIFICATION_COMPLETED = "PREQUALIFICATION_COMPLETED"  # 89
    AI_SCREENING_STARTED = "AI_SCREENING_STARTED"              # 90
    AI_SCREENING_COMPLETED = "AI_SCREENING_COMPLETED"          # 91
    SCORE_GENERATED = "SCORE_GENERATED"                        # 92
    CANDIDATE_SHORTLISTED = "CANDIDATE_SHORTLISTED"            # 93
    INTERVIEW_GUIDE_GENERATED = "INTERVIEW_GUIDE_GENERATED"    # 94
    HUMAN_INTERVIEW_COMPLETED = "HUMAN_INTERVIEW_COMPLETED"    # 95
    HUMAN_FEEDBACK_SUBMITTED = "HUMAN_FEEDBACK_SUBMITTED"      # 96
    HUMAN_RECOMMENDATION_SUBMITTED = "HUMAN_RECOMMENDATION_SUBMITTED"  # 97
    AI_HUMAN_DISAGREEMENT_DETECTED = "AI_HUMAN_DISAGREEMENT_DETECTED"  # 98
    POST_INTERVIEW_ANALYSIS_COMPLETED = "POST_INTERVIEW_ANALYSIS_COMPLETED"  # 99
    RANKING_GENERATED = "RANKING_GENERATED"                    # 100
    FINAL_DECISION_SUBMITTED = "FINAL_DECISION_SUBMITTED"      # 101
```

Storage as validated string, not DB enum — column at
[`app/database/models/audit_event.py:134-138`](app/database/models/audit_event.py:134)
(`String(100)`, indexed), rationale in module docstring lines 18–30 ("**validated
string, not a DB enum**... every later phase will add more").

### "screening started" / "screening completed" — explicit statement
- **`AI_SCREENING_STARTED`** — EXISTS VERBATIM ([`audit_event.py:90`](app/database/models/audit_event.py:90)). No current code emits it (grep shows the definition only).
- **`AI_SCREENING_COMPLETED`** — EXISTS VERBATIM ([`audit_event.py:91`](app/database/models/audit_event.py:91)). No current code emits it.

Both are declared but unused today — they were added up-front per the "later
phases append members" note. Phase 4 can reference them directly; no new enum
member and no migration is needed for start/complete. (There is **no** existing
member for screening *abandoned* / *incomplete* — the closest existing pattern is
that `AI_SCREENING_COMPLETED` would not be written.)

---

## 4. Candidate re-entry / secure access mechanism

### Direct answer
The application-link token is **one token per link, and a link belongs to a job —
it is shared by every candidate who applies to that job**. It is NOT
per-candidate or per-application. There is currently **no column on `applications`
(or any other table) that could serve as a unique, secure, per-application
re-entry credential** — `applications` has only `id`, `candidate_id`, `job_id`,
`application_link_id`, `status`, `created_at`, `updated_at`. (`applications.id` is
a UUID v4 primary key, but it is a plain surrogate PK — not a
cryptographically-generated, secret-grade credential, and it is exposed as a
non-secret in HR-side flows: `application_id` is deliberately stashed in public
`st.session_state` and described as "Not sensitive" —
[`candidate_portal_service.py:270-272`](app/services/candidate_portal_service.py:270).)

### Supporting evidence

**Token is per-link / per-job.**
`ApplicationLink` model — [`app/database/models/application_link.py:53`](app/database/models/application_link.py:53):

```python
class ApplicationLink(Base):
    __tablename__ = "application_links"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)   # 58-60

    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )                                                                                                 # 62-67

    token: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, index=True
    )                                                                                                 # 71-73

    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)  # "1, 2, 3... per job"      # 76

    status: Mapped[str] = mapped_column(String(50), nullable=False,
        server_default=text(f"'{ApplicationLinkStatus.ACTIVE}'"))                                      # 79-83
```

The link has **`job_id` but no `candidate_id` / `application_id`** — it exists
before any candidate applies. Module docstring line 4: *"one row per generated
link for a job"*. Service enforces **at most one `ACTIVE` link per job**
([`application_link_service.py:121-130`](app/services/application_link_service.py:121),
`get_active_link`) and regeneration marks the prior one `SUPERSEDED`
([`application_link_service.py:225-229`](app/services/application_link_service.py:225)).

Token generation — [`application_link_service.py:46`](app/services/application_link_service.py:46)
and `_generate_token` (line 108–109): `secrets.token_urlsafe(32)` → 43 URL-safe
chars, 256 bits. Nothing candidate-specific encoded.

Resolution is purely token → link → job, with **no candidate identity involved** —
[`application_link_service.py:144-173`](app/services/application_link_service.py:144)
(`resolve_link`): looks up `ApplicationLink` by `token`, loads `Job` by
`link.job_id`, checks `link.status == ACTIVE` and `job.status == OPEN`. Returns
`LinkResolution(outcome, link, job)` — no candidate.

The public portal reads the token from the URL query string only —
[`app/public_main.py:200-205`](app/public_main.py:200) (`_token_from_url`,
`st.query_params.get("token", "")`) — and every candidate hitting that job's link
sees the same `?token=<token>` URL.

**`applications` table columns (authoritative — the model):**
[`app/database/models/application.py:101-152`](app/database/models/application.py:101):

| column | type | note |
|---|---|---|
| `id` | `UUID(as_uuid=True)` PK, `default=uuid.uuid4` | plain surrogate PK (line 112–114) |
| `candidate_id` | UUID, FK `candidates.id` `ondelete=RESTRICT`, indexed | line 116–121 |
| `job_id` | UUID, FK `jobs.id` `ondelete=RESTRICT`, indexed | line 123–128 |
| `application_link_id` | UUID, FK `application_links.id` `ondelete=RESTRICT`, indexed | line 130–135 |
| `status` | `String(50)` | line 138–142 |
| `created_at` | `DateTime(timezone=True)` | line 144–146 |
| `updated_at` | `DateTime(timezone=True)` | line 147–152 |

Unique constraint `uq_applications_candidate_job` on `(candidate_id, job_id)` —
one application per person per job (lines 106–110).

Migration matches (with `4f6f59712443_restrict_delete_on_applications_fks`
tightening the FKs from the original `CASCADE` in
[`e7617aa2220b`](app/database/migrations/versions/e7617aa2220b_create_candidates_and_applications.py:73-81)
to `RESTRICT`).

**No per-application secret / re-entry token column exists** on `applications`,
`candidates`, or `documents`. Grep for a screening/session/re-entry token model
returns nothing (no `screening_sessions` model or migration exists yet — see Q5).

### Ambiguity flagged
Whether Phase 4 should (a) reuse the existing shared job link token and re-derive
the candidate from submitted email, (b) add a new per-application UUID/secret
column, or (c) add a `screening_sessions` table with its own token, is an open
schema decision. The facts: no per-candidate credential exists today, and
`applications.id`, while a UUID, is treated as non-secret in existing code.

---

## 5. Existing "processing / interstitial" UI patterns in `public_main.py`

### Direct answer
There is **no multi-stage "please wait / processing" interstitial page** anywhere
in the public app (or the HR pages). The only patterns that exist are:

1. **Blocking spinner + single `st.rerun()`** on task completion — used for both
   the candidate submission and the HR-side AI tasks.
2. **A `st.session_state` primitive flag that switches which sub-form renders**,
   with `st.rerun()` as the transition — the résumé-retry flow. This is the
   closest thing to a "staged" flow, but it is a two-state
   (form ↔ retry-form ↔ done) branch, not a progress/polling pipeline.

### Supporting evidence

**Public app — `app/public_main.py`:**

- Submission is fully synchronous inside one spinner —
  [`app/public_main.py:252-263`](app/public_main.py:252):
  ```python
  with st.spinner("Submitting your application…"):
      with session_scope() as db:
          result = submit_application_via_token(db, token=token, ...)
  ```
  then `_apply_result(...)` at line 273.

- `_apply_result` — [`app/public_main.py:310-330`](app/public_main.py:310) — is
  the whole "state machine": on `SUCCESS`/`DUPLICATE` it calls `mark_form_done(...)`
  (writes `_SS_DONE_JOB` / `_SS_DONE_KIND` into `st.session_state`) then
  `st.rerun()`; on `RESUME_UPLOAD_FAILED` it calls
  `remember_pending_application(...)` (writes `_SS_PENDING_APP`) then `st.rerun()`;
  on `LINK_INVALID` just `st.rerun()`; on `ERROR` shows an inline message and
  leaves the form.

- Session-state keys (all primitive strings, per CLAUDE.md §16) —
  [`app/public_main.py:95-97`](app/public_main.py:95):
  ```python
  _SS_PENDING_APP = "pending_application_id"  # str UUID — résumé retry in progress
  _SS_DONE_JOB = "form_done_for_job"          # str UUID — form already completed
  _SS_DONE_KIND = "form_done_kind"            # "SUCCESS" | "DUPLICATE"
  ```

- The branch that selects which screen to show on each rerun —
  [`app/public_main.py:380-397`](app/public_main.py:380) (`_render_apply_section`):
  checks `form_done_kind(...)` → success/duplicate message; else
  `get_pending_application_id(...)` → `_render_resume_retry_form`; else
  `_render_application_form`. This is a rerun-driven branch on session-state
  flags — the reusable precedent — but there is **no** "step 1 of 3 / working…"
  progress UI, no `st.status`, no polling loop, no auto-advancing stages.

- Résumé-retry form itself — [`app/public_main.py:360-377`](app/public_main.py:360)
  (`_render_resume_retry_form`) + `_handle_retry_submit`
  ([lines 276-307](app/public_main.py:276)) — again one blocking
  `st.spinner("Uploading your résumé…")` then `_apply_result`.

**Related public files under `app/pages/`:** none. `app/public_main.py`
deliberately calls `st.navigation([...], position="hidden")` with a single page
so Streamlit does **not** auto-discover `app/pages/` (docstring
[lines 22-27](app/public_main.py:22) and [`main()` line 421-425](app/public_main.py:421)).
`app/pages/jobs.py` and `app/pages/candidates.py` belong to the HR app only.

**HR side — `app/pages/candidates.py`** (for completeness, since Phase 4's
trigger may live here): the Parse Resume / Prequalify buttons use the identical
pattern — [`app/pages/candidates.py:118-141`](app/pages/candidates.py:118)
(`_run_parse`) and [lines 144-167](app/pages/candidates.py:144)
(`_run_prequalify`): one blocking `st.spinner(...)`, synchronous service call
inside `session_scope()`, `st.success(...)` + `st.rerun()` on success, inline
`st.error(...)` on each failure class. No staged pipeline, no `st.status`
container, no progress bar.

### Explicit statement
A multi-stage "please wait / processing" pattern (progress steps, `st.status`,
polling, auto-advance) **does not exist yet** in `public_main.py` or elsewhere.
The reusable precedents are: (1) blocking `st.spinner` + `st.rerun()` on
completion, and (2) session-state string flags (`_SS_PENDING_APP` etc.) that
select which sub-form `_render_apply_section` renders after each `st.rerun()`.

---

## 6. Round-1 screening question generation — existing evidence-shape precedent

### Direct answer
`prequalification_results` stores, per run, a **JSONB list `results`** where each
element is one **per-criterion object** with a clean, self-contained shape:
`criterion_id`, `criterion_index`, `requirement_type`, `category`,
`criterion_text`, `result` (`PASS`/`FAIL`/`UNKNOWN`), `evidence_summary`,
`reasoning`, and `confidence` (`HIGH`/`MEDIUM`/`LOW`, Python-computed). It **is**
a clean per-criterion structure a screening prompt could consume directly, and it
is fetched **by `application_id`** via
**`prequalification_service.get_prequalification_for_application(db, application_id, *, acting_user_id=...)`**,
which returns a single `PrequalificationResult` ORM row (or `None`).

### Supporting evidence

**The stored row shape.** `PrequalificationResult` model —
[`app/database/models/prequalification_result.py:63`](app/database/models/prequalification_result.py:63):

```python
class PrequalificationResult(Base):
    __tablename__ = "prequalification_results"

    id: Mapped[uuid.UUID]                  # PK, uuid4                       # 68-70
    application_id: Mapped[uuid.UUID]      # FK applications.id, RESTRICT, indexed   # 72-77
    rubric_version_id: Mapped[uuid.UUID]   # FK rubric_versions.id, RESTRICT, indexed # 79-84
    resume_extraction_id: Mapped[uuid.UUID]# FK resume_extractions.id, RESTRICT, indexed # 86-91
    results: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)   # 96
    ai_model: Mapped[str] = mapped_column(String(100), nullable=False)            # 98
    created_at: Mapped[datetime]                                                  # 100-102
```

One row = one run for one application, against one rubric version + one resume
extraction (model docstring lines 5–8). **No** `updated_at`, **no** status/version
column — write-once; re-runs require `force=True` which deletes + replaces
(docstring lines 40–47; service [`prequalification_service.py:342-346`](app/services/prequalification_service.py:342)).

**Per-criterion object shape** — built by `_build_result_rows` in
[`app/services/prequalification_service.py:181-202`](app/services/prequalification_service.py:181):

```python
rows.append({
    "criterion_id": str(criterion.id),
    "criterion_index": i,                 # 1-based, matches display_order
    "requirement_type": criterion.requirement_type,   # MANDATORY / PREFERRED / EXPERIENCE / BEHAVIORAL / OTHER
    "category": criterion.category,
    "criterion_text": criterion.criterion_text,
    "result": a.result,                   # "PASS" | "FAIL" | "UNKNOWN"  (AI)
    "evidence_summary": a.evidence_summary,# AI: what evidence was found / why insufficient
    "reasoning": a.reasoning,             # AI: why the result follows
    "confidence": confidence,             # "HIGH"|"MEDIUM"|"LOW" — Python-computed, NOT from AI
})
```

The docstring on the model spells the same list out —
[`prequalification_result.py:29-36`](app/database/models/prequalification_result.py:29):

> `results` — **JSONB**, a list of per-criterion objects. Each object carries the
> AI's `result` / `evidence_summary` / `reasoning`, the **Python-computed**
> `confidence`, and denormalized `criterion_id` / `criterion_index` /
> `requirement_type` / `category` / `criterion_text` so the stored assessment is
> self-contained for display and audit without joins.

The AI's contribution is validated against the Pydantic schema
`CriterionAssessment` — [`app/ai/schemas/prequalification.py:40-71`](app/ai/schemas/prequalification.py:40):
fields `criterion_index: int`, `result: Literal["PASS","FAIL","UNKNOWN"]`,
`evidence_summary: str` (non-empty), `reasoning: str` (non-empty). Wrapper
`PrequalificationAssessment.assessments: list[CriterionAssessment]`
([lines 74-92](app/ai/schemas/prequalification.py:74)). The schema deliberately
has **no aggregate/overall/score/confidence field** (module docstring lines 11–19)
— confidence is added in Python by
[`app/services/prequalification_confidence.py`](app/services/prequalification_confidence.py)
(`compute_confidence`, called at
[`prequalification_service.py:184-188`](app/services/prequalification_service.py:184)).

Service also enforces **exactly one assessment per criterion, no silent
back-fill** — [`prequalification_service.py:171-179`](app/services/prequalification_service.py:171)
(duplicate index → error; `set(by_index) != expected` → error).

**The prompt precedent** ("AI reasons over rubric + evidence, Python enforces the
boundary") — [`app/ai/prompts/prequalification.py`](app/ai/prompts/prequalification.py):
`build_prequalification_prompt(criteria, extracted_data)` (lines 188–213) emits a
`<rubric_criteria>` block (TRUSTED, numbered 1-based in `display_order`) and a
`<candidate_evidence>` block (UNTRUSTED, rendered from the resume-extraction dict
by `_render_evidence`, lines 122–185). Trust model / prompt-injection framing at
lines 43–52; UNKNOWN-vs-FAIL anchoring with worked examples at lines 54–94.

### Fetch call for Phase 4 — exact reference
[`app/services/prequalification_service.py:118-133`](app/services/prequalification_service.py:118):

```python
def get_prequalification_for_application(
    db: Session, application_id: uuid.UUID | str, *, acting_user_id: uuid.UUID | str
) -> PrequalificationResult | None:
    require_internal_user(db, acting_user_id)
    return db.execute(
        select(PrequalificationResult).where(
            PrequalificationResult.application_id == application_id
        )
    ).scalar_one_or_none()
```

- Keyed by `application_id`. Returns the single `PrequalificationResult` row or
  `None` (there is at most one — `force=True` replaces in place).
- **HR/internal-only**: `require_internal_user(db, acting_user_id)` is the first
  line; a caller must pass a valid active-internal-user id or it raises
  `UnauthorizedError` ([`app/utils/authorization.py:68-101`](app/utils/authorization.py:68)).
- There is **no repository layer** — `app/database/repositories/` contains only
  `__init__.py`. Data access is `db.execute(select(...))` inside service
  functions. So the "repository call" a Phase 4 screening service would use is
  this service function directly (same as
  [`app/pages/candidates.py:90-92`](app/pages/candidates.py:90) already does).
- The per-criterion list is then `PrequalificationResult.results` (the JSONB
  list above); rows for a given requirement type are grouped by
  `row["requirement_type"]` (precedent:
  [`app/pages/candidates.py:262-264`](app/pages/candidates.py:262)).

### Confirmation
Yes — `prequalification_results.results` retains a clean, flat, per-criterion
structure (one object per rubric criterion, each carrying its own
`criterion_id` + `criterion_text` + `requirement_type` + `result` +
`evidence_summary` + `reasoning` + `confidence`). A Phase 4 round-1 prompt can
fetch it by `application_id` through
`get_prequalification_for_application(db, application_id, acting_user_id=<hr_user_id>)`
and filter to `result in {"UNKNOWN", "FAIL"}` without any join or reshaping.

Related note: the exact resume-evidence dict the prequalification prompt consumed
is reachable too — `PrequalificationResult.resume_extraction_id` →
`ResumeExtraction.extracted_data`, fetched via
`resume_parsing_service.get_extraction_for_document(db, document_id, acting_user_id=...)`
([`app/services/resume_parsing_service.py:120-135`](app/services/resume_parsing_service.py:120)).

---

## Cross-cutting facts a Phase 4 lead will need

- **No `screening_sessions` / `screening_questions` / `screening_answers` /
  `screening_evaluations` model or migration exists.** Model dir
  ([`app/database/models/`](app/database/models)) has: `user`, `job`,
  `job_requirement`, `rubric`, `application_link`, `audit_event`, `candidate`,
  `document`, `resume_extraction`, `prequalification_result`, `application`.
  Migrations chain ends at
  [`e3c212e580f7_create_prequalification_results`](app/database/migrations/versions/e3c212e580f7_create_prequalification_results.py).
- **Per-task model config already reserves a `"screening_question"` slot** —
  [`app/ai/claude_client.py:83`](app/ai/claude_client.py:83):
  `"screening_question": "claude-sonnet-5"` (a placeholder; docstring lines 63–65
  say the non-`jd_analysis` entries are placeholders). No prompt file
  (`app/ai/prompts/`) or schema (`app/ai/schemas/`) for screening exists yet.
- **AI-task service pattern to mirror** (transaction model, auth gate, audit,
  single commit): `prequalification_service.prequalify_application`
  ([`app/services/prequalification_service.py:231-395`](app/services/prequalification_service.py:231))
  and `resume_parsing_service.parse_resume`
  ([`app/services/resume_parsing_service.py:157+`](app/services/resume_parsing_service.py:157)).
- **Test count claim**: not verified in this task (no test run performed).
