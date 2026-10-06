"""ORM models package.

Every model module must be imported here so that importing
``app.database.models`` registers all tables on
``app.database.database.Base.metadata``. Alembic's ``env.py`` relies on this for
autogenerate.
"""

from app.database.models.application import Application, ApplicationStatus
from app.database.models.application_link import (
    ApplicationLink,
    ApplicationLinkStatus,
)
from app.database.models.audit_event import AuditEvent, AuditEventType
from app.database.models.candidate import Candidate
from app.database.models.candidate_ranking import CandidateRanking
from app.database.models.candidate_shortlist_entry import CandidateShortlistEntry
from app.database.models.final_decision import (
    FinalDecision,
    FinalDecisionStatus,
)
from app.database.models.final_ranking import (
    FinalRanking,
    FinalRankingEntry,
    FinalRankingEntryStatus,
    FinalRankingStatus,
)
from app.database.models.interview_feedback import (
    MIN_INTERVIEW_ROUND,
    RATING_MAX,
    RATING_MIN,
    InterviewFeedback,
    InterviewFeedbackRating,
)
from app.database.models.interview_transcript import (
    InterviewTranscript,
    InterviewTranscriptStatus,
)
from app.database.models.interview_guide import (
    InterviewGuide,
    InterviewQuestion,
    InterviewQuestionCategory,
)
from app.database.models.document import Document
from app.database.models.job import Job, JdInputMethod, JobStatus
from app.database.models.job_requirement import JobRequirement, RequirementType
from app.database.models.post_interview_analysis import (
    PostInterviewAnalysis,
    PostInterviewAnalysisFeedback,
    PostInterviewAnalysisStatus,
    PostInterviewAnalysisTranscript,
)
from app.database.models.prequalification_result import PrequalificationResult
from app.database.models.resume_extraction import ResumeExtraction
from app.database.models.rubric import (
    RubricCriterion,
    RubricVersion,
    RubricVersionStatus,
)
from app.database.models.screening_answer import ScreeningAnswer
from app.database.models.screening_evaluation import (
    ScreeningEvaluation,
    ScreeningEvaluationBucket,
    ScreeningRecommendation,
)
from app.database.models.screening_question import (
    ScreeningQuestion,
    ScreeningQuestionCategory,
    ScreeningQuestionRound,
)
from app.database.models.screening_session import (
    ScreeningSession,
    ScreeningSessionStatus,
)
from app.database.models.user import (
    SYSTEM_USER_EMAIL,
    SYSTEM_USER_ID,
    User,
    UserRole,
)

__all__ = [
    "MIN_INTERVIEW_ROUND",
    "RATING_MAX",
    "RATING_MIN",
    "SYSTEM_USER_EMAIL",
    "SYSTEM_USER_ID",
    "Application",
    "ApplicationLink",
    "ApplicationLinkStatus",
    "ApplicationStatus",
    "AuditEvent",
    "AuditEventType",
    "Candidate",
    "CandidateRanking",
    "CandidateShortlistEntry",
    "FinalDecision",
    "FinalDecisionStatus",
    "FinalRanking",
    "FinalRankingEntry",
    "FinalRankingEntryStatus",
    "FinalRankingStatus",
    "InterviewFeedback",
    "InterviewFeedbackRating",
    "InterviewGuide",
    "InterviewQuestion",
    "InterviewQuestionCategory",
    "InterviewTranscript",
    "InterviewTranscriptStatus",
    "Document",
    "JdInputMethod",
    "Job",
    "JobRequirement",
    "JobStatus",
    "PostInterviewAnalysis",
    "PostInterviewAnalysisFeedback",
    "PostInterviewAnalysisStatus",
    "PostInterviewAnalysisTranscript",
    "PrequalificationResult",
    "RequirementType",
    "ResumeExtraction",
    "RubricCriterion",
    "RubricVersion",
    "RubricVersionStatus",
    "ScreeningAnswer",
    "ScreeningEvaluation",
    "ScreeningEvaluationBucket",
    "ScreeningQuestion",
    "ScreeningQuestionCategory",
    "ScreeningQuestionRound",
    "ScreeningRecommendation",
    "ScreeningSession",
    "ScreeningSessionStatus",
    "User",
    "UserRole",
]
