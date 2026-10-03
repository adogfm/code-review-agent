"""Agent orchestration layer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from code_checker import CodeCheckFinding, CodeChecker
from file_editor import FileEditResult, FileEditor
from llm import Reviewer
from models import ReviewReport
from planner import DEFAULT_REVIEW_TASK, ReviewPlan, TaskPlanner


@dataclass(frozen=True)
class ReviewRun:
    report: ReviewReport
    plan: ReviewPlan
    tool_findings: list[CodeCheckFinding]


@dataclass(frozen=True)
class ReviewAndFixRun:
    review: ReviewRun
    edit: FileEditResult
    verification_findings: list[CodeCheckFinding]


class CodeReviewAgent:
    """Coordinates a code review without depending on a concrete LLM vendor."""

    def __init__(
        self,
        reviewer: Reviewer,
        checker: CodeChecker | None = None,
        planner: TaskPlanner | None = None,
        editor: FileEditor | None = None,
    ) -> None:
        self._reviewer = reviewer
        self._checker = checker or CodeChecker()
        self._planner = planner or TaskPlanner()
        self._editor = editor or FileEditor()

    def review(self, code: str, task: str = DEFAULT_REVIEW_TASK) -> ReviewReport:
        return self.review_detailed(code, task).report

    def review_detailed(self, code: str, task: str = DEFAULT_REVIEW_TASK) -> ReviewRun:
        if not code.strip():
            raise ValueError("Python 代码不能为空。")

        normalized_task = task.strip() or DEFAULT_REVIEW_TASK
        plan = self._planner.plan(normalized_task)
        tool_findings = self._checker.check(code) if plan.use_code_checker else []
        reviewer_report = self._reviewer.review(
            code,
            tool_findings,
            task=normalized_task,
            plan=plan,
        )
        tool_issues = [finding.to_review_issue() for finding in tool_findings]
        if plan.use_code_checker:
            planner_summary = (
                f"Task Planner 决定调用 CodeChecker（{plan.reason}）。"
                f"CodeChecker 检出 {len(tool_findings)} 个基础问题。"
            )
        else:
            planner_summary = f"Task Planner 决定跳过 CodeChecker（{plan.reason}）。"

        return ReviewRun(
            report=ReviewReport(
                summary=f"{planner_summary} {reviewer_report.summary}",
                issues=[*tool_issues, *reviewer_report.issues],
            ),
            plan=plan,
            tool_findings=tool_findings,
        )

    def review_and_fix(
        self,
        path: Path,
        code: str,
        task: str = DEFAULT_REVIEW_TASK,
    ) -> ReviewAndFixRun:
        review = self.review_detailed(code, task)
        edit = self._editor.apply(path, code, review.report, review.tool_findings)
        verification_findings = self._checker.check(edit.updated_code) if edit.applied else []
        return ReviewAndFixRun(
            review=review,
            edit=edit,
            verification_findings=verification_findings,
        )
