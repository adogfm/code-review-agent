"""LLM abstraction and OpenAI implementation."""

from __future__ import annotations

import os
from typing import Protocol

from dotenv import load_dotenv
from openai import APIConnectionError, APIStatusError, AuthenticationError, OpenAI, RateLimitError

from code_checker import CodeCheckFinding
from models import ReviewReport
from planner import ReviewPlan
from prompt import SYSTEM_PROMPT, build_review_input


class ConfigurationError(RuntimeError):
    """Raised when required local configuration is missing."""


class LLMReviewError(RuntimeError):
    """Raised when the remote review cannot be completed."""


class Reviewer(Protocol):
    is_mock: bool

    def review(
        self,
        code: str,
        tool_findings: list[CodeCheckFinding],
        *,
        task: str,
        plan: ReviewPlan,
    ) -> ReviewReport:
        """Review Python source code and return a structured report."""


class MockReviewer:
    """Deterministic offline reviewer used when no API key is configured."""

    is_mock = True

    def review(
        self,
        code: str,
        tool_findings: list[CodeCheckFinding],
        *,
        task: str,
        plan: ReviewPlan,
    ) -> ReviewReport:
        if not code.strip():
            raise ValueError("Python 代码不能为空。")

        return ReviewReport.model_validate(
            {
                "summary": "[MOCK MODE] 离线审查已完成；这是固定测试结果，不代表真实代码分析。",
                "issues": [
                    {
                        "problem": "示例问题：关键逻辑缺少自动化测试",
                        "severity": "low",
                        "reason": "这是用于验证离线审查流程和输出格式的固定 Mock 结果。",
                        "suggestion": "配置真实 OPENAI_API_KEY 后重新运行，以获得针对代码内容的审查。",
                    }
                ],
            }
        )


class OpenAIReviewer:
    """OpenAI Responses API implementation of the reviewer."""

    is_mock = False

    def __init__(self, api_key: str | None = None) -> None:
        load_dotenv()
        resolved_api_key = (api_key or os.getenv("OPENAI_API_KEY", "")).strip()
        if not _has_real_api_key(resolved_api_key):
            raise ConfigurationError("请在 .env 中设置有效的 OPENAI_API_KEY。")

        self._model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
        self._client = OpenAI(api_key=resolved_api_key)

    def review(
        self,
        code: str,
        tool_findings: list[CodeCheckFinding],
        *,
        task: str,
        plan: ReviewPlan,
    ) -> ReviewReport:
        try:
            response = self._client.responses.parse(
                model=self._model,
                input=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": build_review_input(code, tool_findings, task, plan),
                    },
                ],
                text_format=ReviewReport,
            )
            report = response.output_parsed
            if report is None:
                raise LLMReviewError("模型未返回可解析的审查结果。")
            return report
        except AuthenticationError as exc:
            raise LLMReviewError("API Key 无效或无权访问所选模型。") from exc
        except RateLimitError as exc:
            raise LLMReviewError("API 请求达到速率或额度限制，请稍后重试。") from exc
        except APIConnectionError as exc:
            raise LLMReviewError("无法连接 LLM 服务，请检查网络。") from exc
        except APIStatusError as exc:
            raise LLMReviewError(f"LLM 服务返回错误（HTTP {exc.status_code}）。") from exc
        except LLMReviewError:
            raise
        except Exception as exc:
            raise LLMReviewError(f"无法解析模型响应：{exc}") from exc


def _has_real_api_key(api_key: str) -> bool:
    return bool(api_key) and api_key.lower() not in {
        "your_api_key_here",
        "sk-你的_api_key",
        "sk-your_api_key",
    }


def create_reviewer() -> Reviewer:
    """Select the real LLM when configured, otherwise use deterministic Mock mode."""

    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not _has_real_api_key(api_key):
        return MockReviewer()
    return OpenAIReviewer(api_key=api_key)
