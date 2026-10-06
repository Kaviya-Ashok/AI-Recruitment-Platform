"""Jobs page — create a job with its Job Description, and list existing jobs.

Phase 1.1: creation + listing only. No JD viewing/expand, no edit, no delete,
no AI. Rendered via ``st.navigation`` from ``app/main.py`` (never as a Streamlit
auto-discovered page — see the main module docstring).
"""

from __future__ import annotations

import os
import uuid

import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from app.database.database import session_scope
from app.database.models.job import JdInputMethod, JobStatus
from app.services.application_link_service import (
    ApplicationLinkError,
    close_job,
    generate_link,
    get_active_link,
    revoke_link,
)
from app.services.job_service import (
    JdAnalysisError,
    JobNotFoundError,
    JobSort,
    JobValidationError,
    analyze_jd,
    count_jobs_by_status,
    create_job,
    get_current_requirements,
    list_jobs,
)
from app.services.rubric_service import (
    RubricError,
    RubricGenerationError,
    RubricNotFoundError,
    RubricStateError,
    add_criterion,
    approve_rubric,
    delete_criterion,
    generate_rubric,
    get_approved_rubric,
    get_current_draft,
    list_criteria,
    update_criterion,
)
from app.utils.parsing import DocumentParsingError
from app.utils.session import get_current_user
from app.utils.ui import entity_status_badge, label_for
from app.utils.ui_widgets import (
    confirmed,
    load_error,
    page_header,
    success_toast,
)
from app.utils.validation import FileValidationError

_PASTE = "Paste text"
_UPLOAD = "Upload file"

_REQUIREMENT_TYPE_ORDER = ["MANDATORY", "PREFERRED", "EXPERIENCE", "BEHAVIORAL", "OTHER"]

_ANALYSIS_DB_ERROR = "Couldn't save the analysis — please try again."
_ANALYSIS_UNEXPECTED = "Something went wrong during analysis. Please try again."

_RUBRIC_DB_ERROR = "Couldn't save that rubric change — please try again."
_RUBRIC_UNEXPECTED = "Something went wrong with the rubric. Please try again."

_LINK_DB_ERROR = "Couldn't save that link change — please try again."
_LINK_UNEXPECTED = "Something went wrong with the application link. Please try again."

_LINK_STATUSES = {
    JobStatus.RUBRIC_APPROVED,
    JobStatus.OPEN,
    JobStatus.CLOSED,
}

# --- Jobs list: tabs, search/sort, paging ---------------------------------

#: Jobs loaded per "Show more" click (and on first paint) in each tab.
_PAGE_SIZE = 10

#: A tab showing more than this many cards defaults them all to collapsed.
_EXPAND_THRESHOLD = 5

# Three HR-facing tabs over the SEVEN JobStatus values. These are display
# *groups*, not new statuses: nothing here changes what a status means or how a
# job moves between them. Each card still shows its precise status via
# ``status_badge`` (e.g. "Rubric approved" inside the Draft tab).
#
#   (session key, status used for the tab's label, statuses in the group)
_TAB_GROUPS: tuple[tuple[str, str, frozenset[str]], ...] = (
    ("open", JobStatus.OPEN, frozenset({JobStatus.OPEN})),
    (
        "draft",
        JobStatus.DRAFT,
        frozenset({
            JobStatus.DRAFT,
            JobStatus.JD_ANALYZED,
            JobStatus.RUBRIC_PENDING,
            JobStatus.RUBRIC_APPROVED,
        }),
    ),
    ("closed", JobStatus.CLOSED, frozenset({JobStatus.CLOSED, JobStatus.ARCHIVED})),
)

# Safety net: any JobStatus added in a later phase and not listed above lands in
# the Draft ("still being set up") tab rather than silently disappearing from
# the page. Computed, so it can never drift out of date.
_UNGROUPED: frozenset[str] = JobStatus.ALL - frozenset().union(
    *(statuses for _, _, statuses in _TAB_GROUPS)
)

#: Resolved tabs — the Draft group absorbs any ungrouped status.
_TABS: tuple[tuple[str, str, frozenset[str]], ...] = tuple(
    (key, label_status, statuses | _UNGROUPED if key == "draft" else statuses)
    for key, label_status, statuses in _TAB_GROUPS
)

#: Human sort label -> JobSort value. Order here is the selectbox order.
_SORT_OPTIONS: dict[str, str] = {
    "Newest": JobSort.NEWEST,
    "Oldest": JobSort.OLDEST,
    "Recently updated": JobSort.RECENTLY_UPDATED,
}


def _public_apply_url(token: str) -> str:
    # Streamlit (1.62) has no URL path params, so the public candidate app
    # (app/public_main.py) reads the token from ?token=... — see that module.
    base = os.getenv("APP_PUBLIC_BASE_URL", "http://localhost:8502").rstrip("/")
    return f"{base}/?token={token}"

_FILE_UNREADABLE = (
    "Couldn't read this file — try re-uploading or paste the text instead."
)
_DB_ERROR = "We couldn't save the job right now. Please try again in a moment."
_UNEXPECTED = "Something went wrong creating the job. Please try again."


def _handle_submit(
    *,
    title: str,
    department: str,
    method_label: str,
    pasted_text: str,
    uploaded_file,
    created_by_user_id,
) -> None:
    if method_label == _UPLOAD:
        method = JdInputMethod.FILE_UPLOAD
        file_bytes = uploaded_file.getvalue() if uploaded_file is not None else None
        file_name = uploaded_file.name if uploaded_file is not None else None
        text_arg = None
    else:
        method = JdInputMethod.TEXT_PASTE
        file_bytes = None
        file_name = None
        text_arg = pasted_text

    try:
        with session_scope() as db:
            job = create_job(
                db,
                title=title,
                department=department,
                jd_input_method=method,
                jd_source_text=text_arg,
                uploaded_file_bytes=file_bytes,
                uploaded_filename=file_name,
                created_by_user_id=created_by_user_id,
            )
    except JobValidationError as exc:
        st.error(str(exc))
        return
    except FileValidationError as exc:
        st.error(str(exc))
        return
    except DocumentParsingError:
        st.error(_FILE_UNREADABLE)
        return
    except SQLAlchemyError:
        st.error(_DB_ERROR)
        return
    except Exception:  # noqa: BLE001 - never surface a traceback
        st.error(_UNEXPECTED)
        return

    st.success(f'Created job "{job.title}".')


def _render_create_form(created_by_user_id) -> None:
    st.subheader("Create a job")

    method_label = st.radio(
        "Job Description input",
        options=[_PASTE, _UPLOAD],
        horizontal=True,
        key="jd_method",
    )

    with st.form("create_job_form", clear_on_submit=False):
        title = st.text_input("Title", key="job_title")
        department = st.text_input("Department (optional)", key="job_department")

        pasted_text = ""
        uploaded_file = None
        if method_label == _PASTE:
            pasted_text = st.text_area(
                "Paste the Job Description", height=220, key="jd_text"
            )
        else:
            uploaded_file = st.file_uploader(
                "Upload the Job Description (PDF or DOCX, max 10 MB)",
                type=["pdf", "docx"],
                accept_multiple_files=False,
                key="jd_file",
            )

        submitted = st.form_submit_button("Create job")

    if submitted:
        with st.spinner("Creating job…"):
            _handle_submit(
                title=title,
                department=department,
                method_label=method_label,
                pasted_text=pasted_text,
                uploaded_file=uploaded_file,
                created_by_user_id=created_by_user_id,
            )


def _run_analysis(job_id, requested_by_user_id) -> None:
    """Call analyze_jd (button-triggered only) and report the outcome."""
    try:
        with st.spinner("Analyzing JD with AI — this can take a few seconds…"):
            with session_scope() as db:
                job = analyze_jd(
                    db,
                    job_id=job_id,
                    requested_by_user_id=requested_by_user_id,
                )
                counts: dict[str, int] = {}
                for req in get_current_requirements(db, job_id):
                    counts[req.requirement_type] = counts.get(req.requirement_type, 0) + 1
    except JobNotFoundError:
        st.error("That job no longer exists.")
        return
    except JdAnalysisError as exc:
        st.error(str(exc))
        return
    except SQLAlchemyError:
        st.error(_ANALYSIS_DB_ERROR)
        return
    except Exception:  # noqa: BLE001 - never surface a traceback
        st.error(_ANALYSIS_UNEXPECTED)
        return

    total = sum(counts.values())
    breakdown = ", ".join(
        f"{counts[t]} {t.lower()}" for t in _REQUIREMENT_TYPE_ORDER if counts.get(t)
    )
    success_toast(f'Analyzed "{job.title}": {total} requirements ({breakdown}).')
    st.rerun()


def _render_requirements(job_id) -> None:
    try:
        with session_scope() as db:
            grouped: dict[str, list[tuple[str, str | None]]] = {}
            for req in get_current_requirements(db, job_id):
                grouped.setdefault(req.requirement_type, []).append(
                    (req.requirement_text, req.category)
                )
    except SQLAlchemyError:
        load_error("Couldn't load requirements right now.")
        return

    if not grouped:
        return

    for rtype in _REQUIREMENT_TYPE_ORDER:
        items = grouped.get(rtype, [])
        if not items:
            continue
        st.markdown(f"**{rtype.title()}** ({len(items)})")
        for text, category in items:
            # Native nested markdown bullet — no &nbsp; padding (H22).
            suffix = f"\n  - *{category}*" if category else ""
            st.markdown(f"- {text}{suffix}")


def _run_generate_rubric(job_id, requested_by_user_id) -> None:
    """Call generate_rubric (button-triggered only) and report the outcome."""
    try:
        with st.spinner("Generating rubric with AI — this can take a few seconds…"):
            with session_scope() as db:
                version = generate_rubric(
                    db, job_id=job_id, requested_by_user_id=requested_by_user_id
                )
                count = len(list_criteria(db, version.id))
                version_number = version.version_number
    except RubricNotFoundError:
        st.error("That job no longer exists.")
        return
    except (RubricStateError, RubricGenerationError) as exc:
        st.error(str(exc))
        return
    except SQLAlchemyError:
        st.error(_RUBRIC_DB_ERROR)
        return
    except Exception:  # noqa: BLE001
        st.error(_RUBRIC_UNEXPECTED)
        return

    success_toast(f"Generated rubric v{version_number} with {count} criteria.")
    st.rerun()


def _rubric_action(fn, /, **kwargs) -> bool:
    """Run a rubric mutating action inside session_scope; show errors. Returns
    True on success (caller reruns)."""
    try:
        with session_scope() as db:
            fn(db, **kwargs)
    except RubricNotFoundError:
        st.error("That rubric or criterion no longer exists.")
        return False
    except RubricError as exc:  # RubricStateError etc.
        st.error(str(exc))
        return False
    except SQLAlchemyError:
        st.error(_RUBRIC_DB_ERROR)
        return False
    except Exception:  # noqa: BLE001
        st.error(_RUBRIC_UNEXPECTED)
        return False
    return True


def _render_approved_rubric(version_view: dict) -> None:
    approved_at = version_view["approved_at"]
    when = approved_at.strftime("%Y-%m-%d %H:%M") if approved_at else "—"
    st.success(
        f"✅ Approved rubric — version {version_view['version_number']} "
        f"(approved {when})"
    )
    _render_criteria_readonly(version_view["criteria"])


def _render_criteria_readonly(criteria: list[dict]) -> None:
    grouped: dict[str, list[dict]] = {}
    for c in criteria:
        grouped.setdefault(c["requirement_type"], []).append(c)
    for rtype in _REQUIREMENT_TYPE_ORDER:
        items = grouped.get(rtype, [])
        if not items:
            continue
        st.markdown(f"**{rtype.title()}** ({len(items)})")
        for c in items:
            suffix = f"\n  - *{c['category']}*" if c["category"] else ""
            st.markdown(f"- {c['criterion_text']}{suffix}")


def _render_draft_editor(
    job_id, draft_view: dict, requested_by_user_id
) -> None:
    version_id = draft_view["id"]
    st.info(
        f"Draft rubric — version {draft_view['version_number']} "
        f"({len(draft_view['criteria'])} criteria). Edit below, then approve."
    )

    for c in draft_view["criteria"]:
        cid = c["id"]
        with st.expander(f"[{c['requirement_type']}] {c['criterion_text'][:70]}"):
            new_text = st.text_area(
                "Criterion text", value=c["criterion_text"], key=f"ct_{cid}"
            )
            cols = st.columns([2, 2])
            new_type = cols[0].selectbox(
                "Type",
                _REQUIREMENT_TYPE_ORDER,
                index=_REQUIREMENT_TYPE_ORDER.index(c["requirement_type"]),
                key=f"cty_{cid}",
            )
            new_cat = cols[1].text_input(
                "Category (optional)", value=c["category"] or "", key=f"cc_{cid}"
            )
            b = st.columns([1, 1])
            if b[0].button("Save changes", key=f"save_{cid}"):
                if _rubric_action(
                    update_criterion,
                    criterion_id=cid,
                    requested_by_user_id=requested_by_user_id,
                    requirement_type=new_type,
                    category=new_cat,
                    criterion_text=new_text,
                ):
                    st.rerun()
            if b[1].button("Delete", key=f"del_{cid}"):
                if _rubric_action(
                    delete_criterion,
                    criterion_id=cid,
                    requested_by_user_id=requested_by_user_id,
                ):
                    st.rerun()

    with st.expander("Add a criterion"):
        with st.form(f"add_crit_{version_id}", clear_on_submit=True):
            a_text = st.text_area("Criterion text", key=f"add_text_{version_id}")
            a_cols = st.columns([2, 2])
            a_type = a_cols[0].selectbox(
                "Type", _REQUIREMENT_TYPE_ORDER, key=f"add_type_{version_id}"
            )
            a_cat = a_cols[1].text_input(
                "Category (optional)", key=f"add_cat_{version_id}"
            )
            if st.form_submit_button("Add criterion"):
                if _rubric_action(
                    add_criterion,
                    rubric_version_id=version_id,
                    requested_by_user_id=requested_by_user_id,
                    requirement_type=a_type,
                    category=a_cat,
                    criterion_text=a_text,
                ):
                    st.rerun()

    mandatory = sum(
        1 for c in draft_view["criteria"] if c["requirement_type"] == "MANDATORY"
    )
    if mandatory == 0:
        st.warning("Add at least one MANDATORY criterion before approving.")
    if st.button(
        "Approve rubric", key=f"approve_{version_id}", disabled=mandatory == 0
    ):
        try:
            with st.spinner("Approving…"):
                with session_scope() as db:
                    approve_rubric(
                        db,
                        rubric_version_id=version_id,
                        requested_by_user_id=requested_by_user_id,
                    )
        except RubricError as exc:
            st.error(str(exc))
        except SQLAlchemyError:
            st.error(_RUBRIC_DB_ERROR)
        except Exception:  # noqa: BLE001
            st.error(_RUBRIC_UNEXPECTED)
        else:
            success_toast("Rubric approved and locked.")
            st.rerun()


def _load_rubric_views(job_id) -> dict:
    """Read the approved + draft rubric versions (with criteria) into primitives."""
    out: dict = {"approved": None, "draft": None}
    with session_scope() as db:
        approved = get_approved_rubric(db, job_id)
        draft = get_current_draft(db, job_id)
        for key, version in (("approved", approved), ("draft", draft)):
            if version is None:
                continue
            out[key] = {
                "id": version.id,
                "version_number": version.version_number,
                "approved_at": version.approved_at,
                "criteria": [
                    {
                        "id": c.id,
                        "requirement_type": c.requirement_type,
                        "category": c.category,
                        "criterion_text": c.criterion_text,
                    }
                    for c in list_criteria(db, version.id)
                ],
            }
    return out


def _render_rubric_section(job_id, requested_by_user_id) -> None:
    try:
        views = _load_rubric_views(job_id)
    except SQLAlchemyError:
        load_error("Couldn't load the rubric right now.")
        return

    st.markdown("#### Evaluation rubric")

    has_any = views["approved"] is not None or views["draft"] is not None
    if views["approved"] is not None:
        gen_label = "Regenerate rubric"
        st.caption(
            "Regenerating creates a **new draft** for review. Your approved "
            f"rubric v{views['approved']['version_number']} stays in effect "
            "until you approve the new one."
        )
    elif views["draft"] is not None:
        gen_label = "Regenerate rubric"
        st.caption("Regenerating replaces the current draft (history is kept).")
    else:
        gen_label = "Generate rubric"

    if st.button(gen_label, key=f"genrubric_{job_id}"):
        _run_generate_rubric(job_id, requested_by_user_id)

    if views["approved"] is not None:
        _render_approved_rubric(views["approved"])
    if views["draft"] is not None:
        _render_draft_editor(job_id, views["draft"], requested_by_user_id)
    if not has_any:
        st.caption("No rubric yet — generate one from the requirements above.")


def _link_action(fn, /, success_msg: str, **kwargs) -> None:
    """Run a link mutating action inside session_scope; show result. Reruns on
    success."""
    try:
        with session_scope() as db:
            fn(db, **kwargs)
    except ApplicationLinkError as exc:
        st.error(str(exc))
        return
    except SQLAlchemyError:
        st.error(_LINK_DB_ERROR)
        return
    except Exception:  # noqa: BLE001
        st.error(_LINK_UNEXPECTED)
        return
    success_toast(success_msg)
    st.rerun()


def _load_link_view(job_id) -> dict:
    with session_scope() as db:
        active = get_active_link(db, job_id)
        return {
            "active": (
                None
                if active is None
                else {
                    "id": active.id,
                    "token": active.token,
                    "sequence_number": active.sequence_number,
                }
            )
        }


def _render_link_section(job_id, job_status: str, requested_by_user_id) -> None:
    st.markdown("#### Application link")

    if job_status == JobStatus.CLOSED:
        st.error("🔒 This job is **closed** — not accepting applications. (Reopening "
                 "a closed job isn't supported yet.)")
        return

    try:
        view = _load_link_view(job_id)
    except SQLAlchemyError:
        load_error("Couldn't load the application link right now.")
        return
    active = view["active"]

    if job_status == JobStatus.RUBRIC_APPROVED and active is None:
        st.caption("The rubric is approved. Generate a link to start accepting "
                   "applications (this opens the job).")
        if st.button("Generate application link", key=f"genlink_{job_id}"):
            _link_action(
                generate_link,
                success_msg="Application link generated — the job is now open.",
                job_id=job_id,
                requested_by_user_id=requested_by_user_id,
            )
        return

    # job_status == OPEN from here on.
    if active is None:
        st.warning("**No active application link** — candidates cannot currently "
                   "apply. A previous link was revoked.")
        if st.button("Generate new link", key=f"newlink_{job_id}"):
            _link_action(
                generate_link,
                success_msg="New application link generated.",
                job_id=job_id,
                requested_by_user_id=requested_by_user_id,
            )
    else:
        st.markdown(f"Active link **#{active['sequence_number']}** — share this URL:")
        st.code(_public_apply_url(active["token"]), language=None)

        with st.expander("Regenerate link"):
            st.caption("Creates a new link and **immediately invalidates the "
                       "current one**. Anyone who saved the old URL can no longer "
                       "apply.")
            if confirmed("I understand the current link will stop working",
                         key=f"regen_ok_{job_id}"):
                if st.button("Regenerate now", key=f"regen_{job_id}"):
                    _link_action(
                        generate_link,
                        success_msg="Link regenerated — the old URL no longer works.",
                        job_id=job_id,
                        requested_by_user_id=requested_by_user_id,
                    )

        with st.expander("Revoke link"):
            st.caption("Disables the current link. The job stays open but "
                       "candidates cannot apply until you generate a new link.")
            if confirmed("I understand candidates will not be able to apply",
                         key=f"revoke_ok_{job_id}"):
                if st.button("Revoke now", key=f"revoke_{job_id}"):
                    _link_action(
                        revoke_link,
                        success_msg="Link revoked — no active application link.",
                        link_id=active["id"],
                        requested_by_user_id=requested_by_user_id,
                    )

    with st.expander("Close job"):
        st.caption("Stops all applications for this job. **This cannot be undone "
                   "in the current version.**")
        if confirmed("I understand this is not reversible", key=f"close_ok_{job_id}"):
            if st.button("Close job now", key=f"close_{job_id}"):
                _link_action(
                    close_job,
                    success_msg="Job closed.",
                    job_id=job_id,
                    requested_by_user_id=requested_by_user_id,
                )


def _card_label(job_view: dict) -> str:
    """The always-visible summary line on a collapsed card: title, department,
    precise status badge, and the created date."""
    department = job_view["department"] or "No department"
    created = job_view["created_at"]
    when = f" · created {created:%Y-%m-%d}" if created else ""
    return (
        f"**{job_view['title']}** — {department}  "
        f"{entity_status_badge('job', job_view['status'])}{when}"
    )


def _render_job_card(
    job_view: dict, requested_by_user_id, *, default_expanded: bool
) -> None:
    """One job as a collapsible card.

    ``on_change="rerun"`` + the ``.open`` check make the body **lazy**: a
    collapsed card runs none of the requirement / rubric / link queries below.
    That is what actually bounds this page's cost, not the visual collapse.
    """
    job_id = job_view["id"]
    analyzed = job_view["status"] != JobStatus.DRAFT

    card = st.expander(
        _card_label(job_view),
        expanded=default_expanded,
        key=f"jobcard_{job_id}",
        on_change="rerun",
    )
    if not card.open:
        return

    with card:
        st.caption(f"Job code: {job_view['job_code']}")
        label = "Re-analyze JD" if analyzed else "Analyze JD"
        clicked = st.button(
            label,
            key=f"analyze_{job_id}",
            disabled=not job_view["has_jd"],
            help=None if job_view["has_jd"] else "This job has no JD text.",
        )
        if clicked:
            _run_analysis(job_id, requested_by_user_id)

        if analyzed:
            _render_requirements(job_id)
            st.divider()
            _render_rubric_section(job_id, requested_by_user_id)

        if job_view["status"] in _LINK_STATUSES:
            st.divider()
            _render_link_section(job_id, job_view["status"], requested_by_user_id)


def _job_view(job) -> dict:
    return {
        "id": job.id,
        "job_code": job.job_code,
        "title": job.title,
        "department": job.department,
        "status": job.status,
        "created_at": job.created_at,
        "has_jd": bool(job.jd_source_text and job.jd_source_text.strip()),
    }


def _shown_count(tab_key: str, search: str, sort_value: str) -> int:
    """How many cards this tab currently shows, held in session state so a
    "Show more" click survives the rerun.

    The count resets to one page whenever the tab's search text or sort order
    changes — otherwise a large offset carried over from a previous query would
    silently skip the first results of the new one.
    """
    count_key = f"jobs_shown_{tab_key}"
    query_key = f"jobs_query_{tab_key}"
    query = (search.strip().lower(), sort_value)

    if st.session_state.get(query_key) != query:
        st.session_state[query_key] = query
        st.session_state[count_key] = _PAGE_SIZE

    return int(st.session_state.get(count_key, _PAGE_SIZE))


def _render_tab(
    tab_key: str, label_status: str, statuses: frozenset[str], requested_by_user_id
) -> None:
    controls = st.columns([3, 2])
    search = controls[0].text_input(
        "Search",
        key=f"jobs_search_{tab_key}",
        placeholder="Search title or department…",
        label_visibility="collapsed",
    )
    sort_label = controls[1].selectbox(
        "Sort",
        list(_SORT_OPTIONS),
        key=f"jobs_sort_{tab_key}",
        label_visibility="collapsed",
        help="Sort order for this tab.",
    )
    sort_value = _SORT_OPTIONS[sort_label]

    shown = _shown_count(tab_key, search, sort_value)

    try:
        with session_scope() as db:
            # One extra row tells us whether a "Show more" is warranted without
            # a second COUNT query.
            page = list_jobs(
                db,
                statuses=statuses,
                search=search,
                sort=sort_value,
                limit=shown + 1,
            )
            job_views = [_job_view(j) for j in page]
    except SQLAlchemyError:
        load_error("Couldn't load the job list right now.")
        return

    has_more = len(job_views) > shown
    job_views = job_views[:shown]

    if not job_views:
        if search.strip():
            st.caption("No jobs match your search.")
        else:
            st.caption(f"No {label_for(label_status).lower()} jobs.")
        return

    # The collapse default is evaluated on what is ACTUALLY rendered right now
    # (after search and paging), not on the tab's total. If a search narrows 40
    # jobs to 2, those 2 open — the user has already said what they want; making
    # them click twice more would defeat the search.
    default_expanded = len(job_views) <= _EXPAND_THRESHOLD

    for job_view in job_views:
        _render_job_card(
            job_view, requested_by_user_id, default_expanded=default_expanded
        )

    if has_more:
        if st.button(
            f"Show more ({_PAGE_SIZE} at a time)", key=f"jobs_more_{tab_key}"
        ):
            st.session_state[f"jobs_shown_{tab_key}"] = shown + _PAGE_SIZE
            st.rerun()


def _render_job_list(requested_by_user_id) -> None:
    st.subheader("Jobs")

    try:
        with session_scope() as db:
            counts = count_jobs_by_status(db)
    except SQLAlchemyError:
        load_error("Couldn't load the job list right now.")
        return

    # Counts are the whole pipeline per tab — deliberately NOT narrowed by the
    # search box, so the tab bar stays a stable overview while typing.
    labels = [
        f"{label_for(label_status)} ({sum(counts.get(s, 0) for s in statuses)})"
        for _, label_status, statuses in _TABS
    ]

    for tab, (tab_key, label_status, statuses) in zip(st.tabs(labels), _TABS):
        with tab:
            _render_tab(tab_key, label_status, statuses, requested_by_user_id)


def render_jobs_page() -> None:
    current = get_current_user(st.session_state)
    if current is None:  # defensive: gate is in main.py, but never render ungated
        st.error("Please sign in.")
        return

    try:
        created_by_user_id = uuid.UUID(current["id"])
    except (ValueError, KeyError, TypeError):
        created_by_user_id = None

    page_header("Jobs")
    _render_create_form(created_by_user_id)
    st.divider()
    _render_job_list(created_by_user_id)
