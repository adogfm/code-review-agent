"""Structured input/output models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


Severity = Literal["critical", "high", "medium", "low"]


class ReviewIssue(BaseModel):
    problem: str = Field(description="发现的具体问题")
    severity: Severity = Field(description="问题严重程度")
    reason: str = Field(description="该问题为何重要")
    suggestion: str = Field(description="具体、可执行的修改建议")


class ReviewReport(BaseModel):
    summary: str = Field(description="整体审查结论")
    issues: list[ReviewIssue] = Field(description="发现的问题；没有问题时为空列表")

    def to_text(self) -> str:
        lines = ["\n=== Python 代码审查结果 ===", self.summary]
        if not self.issues:
            lines.append("\n未发现明确问题。")
            return "\n".join(lines)

        for index, issue in enumerate(self.issues, start=1):
            lines.extend(
                [
                    f"\n{index}. [{issue.severity.upper()}] {issue.problem}",
                    f"   原因：{issue.reason}",
                    f"   建议：{issue.suggestion}",
                ]
            )
        return "\n".join(lines)
