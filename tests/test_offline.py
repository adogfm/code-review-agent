"""Offline tests that never call the OpenAI API."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

from agent import CodeReviewAgent
from code_checker import CodeCheckFinding, CodeChecker
from file_editor import FileEditError, FileEditor
from llm import MockReviewer, OpenAIReviewer, create_reviewer
from models import ReviewReport
from planner import ReviewPlan, TaskPlanner


class RecordingReviewer:
    is_mock = True

    def __init__(self) -> None:
        self.received_findings: list[CodeCheckFinding] = []
        self.received_task = ""
        self.received_plan: ReviewPlan | None = None

    def review(
        self,
        code: str,
        tool_findings: list[CodeCheckFinding],
        *,
        task: str,
        plan: ReviewPlan,
    ) -> ReviewReport:
        self.received_findings = tool_findings
        self.received_task = task
        self.received_plan = plan
        return ReviewReport(summary="Reviewer 已运行。", issues=[])


class OfflineReviewerTests(unittest.TestCase):
    def test_missing_api_key_selects_mock_reviewer(self) -> None:
        with patch("llm.load_dotenv"), patch.dict(os.environ, {}, clear=True):
            reviewer = create_reviewer()

        self.assertIsInstance(reviewer, MockReviewer)
        self.assertTrue(reviewer.is_mock)

    def test_mock_review_returns_fixed_structured_result(self) -> None:
        report = CodeReviewAgent(MockReviewer()).review("print('hello')")

        self.assertIn("[MOCK MODE]", report.summary)
        self.assertIn("CodeChecker 检出 0 个基础问题", report.summary)
        self.assertEqual(len(report.issues), 1)
        self.assertEqual(report.issues[0].severity, "low")
        self.assertTrue(report.issues[0].problem)
        self.assertTrue(report.issues[0].reason)
        self.assertTrue(report.issues[0].suggestion)

    def test_api_key_keeps_real_reviewer_available(self) -> None:
        with patch("llm.load_dotenv"), patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "test-api-key", "OPENAI_MODEL": "gpt-4o-mini"},
            clear=True,
        ):
            reviewer = create_reviewer()

        self.assertIsInstance(reviewer, OpenAIReviewer)
        self.assertFalse(reviewer.is_mock)


class CodeCheckerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.checker = CodeChecker()

    def test_reports_syntax_error_without_raising(self) -> None:
        findings = self.checker.check("def broken(:\n    pass")

        self.assertEqual([finding.rule_id for finding in findings], ["syntax-error"])
        self.assertEqual(findings[0].line, 1)

    def test_reports_bare_except(self) -> None:
        findings = self.checker.check("try:\n    work()\nexcept:\n    pass")

        self.assertEqual([finding.rule_id for finding in findings], ["bare-except"])
        self.assertEqual(findings[0].line, 3)

    def test_reports_literal_division_floor_division_and_modulo_by_zero(self) -> None:
        findings = self.checker.check("a = 1 / 0\nb = 2 // -0.0\nc = 3 % +0")

        self.assertEqual(
            [finding.rule_id for finding in findings],
            ["division-by-zero", "division-by-zero", "division-by-zero"],
        )

    def test_ignores_nonzero_or_dynamic_denominator(self) -> None:
        findings = self.checker.check("a = 1 / 2\nb = 2 / value")

        self.assertEqual(findings, [])

    def test_agent_merges_tool_findings_into_mock_report(self) -> None:
        report = CodeReviewAgent(MockReviewer()).review("value = 10 / 0")

        self.assertIn("CodeChecker 检出 1 个基础问题", report.summary)
        self.assertEqual(len(report.issues), 2)
        self.assertIn("第 1 行", report.issues[0].problem)
        self.assertIn("常量零", report.issues[0].problem)

    def test_agent_passes_tool_findings_to_reviewer(self) -> None:
        reviewer = RecordingReviewer()

        CodeReviewAgent(reviewer).review("try:\n    work()\nexcept:\n    pass")

        self.assertEqual(len(reviewer.received_findings), 1)
        self.assertEqual(reviewer.received_findings[0].rule_id, "bare-except")
        self.assertIsNotNone(reviewer.received_plan)
        self.assertTrue(reviewer.received_plan.use_code_checker)


class TaskPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.planner = TaskPlanner()

    def test_comprehensive_review_uses_code_checker(self) -> None:
        plan = self.planner.plan("全面审查代码正确性和安全性")

        self.assertTrue(plan.use_code_checker)

    def test_style_only_task_skips_code_checker(self) -> None:
        plan = self.planner.plan("只检查代码风格和命名")

        self.assertFalse(plan.use_code_checker)

    def test_static_intent_overrides_style_intent(self) -> None:
        plan = self.planner.plan("检查代码风格，同时查找语法错误")

        self.assertTrue(plan.use_code_checker)

    def test_unknown_task_defaults_to_code_checker(self) -> None:
        plan = self.planner.plan("给我一些改进建议")

        self.assertTrue(plan.use_code_checker)

    def test_agent_does_not_execute_checker_when_plan_skips_it(self) -> None:
        reviewer = RecordingReviewer()
        checker = Mock(spec=CodeChecker)

        report = CodeReviewAgent(reviewer, checker=checker).review(
            "value = 10 / 0",
            task="只检查代码风格和命名",
        )

        checker.check.assert_not_called()
        self.assertEqual(reviewer.received_findings, [])
        self.assertIsNotNone(reviewer.received_plan)
        self.assertFalse(reviewer.received_plan.use_code_checker)
        self.assertIn("Task Planner 决定跳过 CodeChecker", report.summary)


class CountingCodeChecker(CodeChecker):
    def __init__(self) -> None:
        self.call_count = 0

    def check(self, code: str) -> list[CodeCheckFinding]:
        self.call_count += 1
        return super().check(code)


class FileEditorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path.cwd() / f".file_editor_test_{uuid4().hex}.py"

    def tearDown(self) -> None:
        self.path.unlink(missing_ok=True)

    def test_review_and_fix_applies_patch_then_rechecks(self) -> None:
        code = "try:\n    work()\nexcept:\n    pass\n"
        checker = CountingCodeChecker()
        self.path.write_text(code, encoding="utf-8")

        run = CodeReviewAgent(MockReviewer(), checker=checker).review_and_fix(self.path, code)

        self.assertTrue(run.edit.applied)
        self.assertIn("except Exception:", self.path.read_text(encoding="utf-8"))
        self.assertIn("-except:", run.edit.diff)
        self.assertIn("+except Exception:", run.edit.diff)
        self.assertEqual(run.verification_findings, [])
        self.assertEqual(checker.call_count, 2)

    def test_division_by_zero_is_not_unsafely_rewritten(self) -> None:
        code = "value = 10 / 0\n"
        self.path.write_text(code, encoding="utf-8")

        run = CodeReviewAgent(MockReviewer()).review_and_fix(self.path, code)

        self.assertFalse(run.edit.applied)
        self.assertEqual(self.path.read_text(encoding="utf-8"), code)
        self.assertTrue(any("除零问题" in reason for reason in run.edit.skipped_reasons))

    def test_rejects_stale_file_before_applying_patch(self) -> None:
        original = "try:\n    work()\nexcept:\n    pass\n"
        changed = "print('changed')\n"
        findings = CodeChecker().check(original)
        report = CodeReviewAgent(MockReviewer()).review(original)
        self.path.write_text(changed, encoding="utf-8")

        with self.assertRaisesRegex(FileEditError, "过期补丁"):
            FileEditor().apply(self.path, original, report, findings)


if __name__ == "__main__":
    unittest.main()
