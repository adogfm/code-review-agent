"""LLM abstraction and Qwen OpenAI-compatible implementation."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from json import JSONDecodeError
from typing import Protocol

from dotenv import load_dotenv
from openai import APIConnectionError, APIStatusError, AuthenticationError, OpenAI, RateLimitError
from pydantic import ValidationError

from code_checker import CodeCheckFinding
from models import ReviewReport
from planner import ReviewPlan
from prompt import SYSTEM_PROMPT, build_review_input


class ConfigurationError(RuntimeError):
    """Raised when required local configuration is missing."""


class LLMReviewError(RuntimeError):
    """Raised when the remote review cannot be completed."""


DEFAULT_QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_QWEN_MODEL = "qwen-plus"


@dataclass(frozen=True)
class LLMSettings:
    api_key: str
    base_url: str
    model: str


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
                        "suggestion": "配置真实 DASHSCOPE_API_KEY 后重新运行，以获得针对代码内容的审查。",
                    }
                ],
            }
        )


class OpenAIReviewer:
    """Qwen reviewer implemented through its OpenAI-compatible Chat API."""

    is_mock = False

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        model: str | None = None,
        client: OpenAI | None = None,
    ) -> None:
        load_dotenv()
        settings = _load_settings(api_key=api_key, base_url=base_url, model=model)
        self._model = settings.model
        self._base_url = settings.base_url
        self._client = client or OpenAI(api_key=settings.api_key, base_url=settings.base_url)

    def review(
        self,
        code: str,
        tool_findings: list[CodeCheckFinding],
        *,
        task: str,
        plan: ReviewPlan,
    ) -> ReviewReport:
        try:
            completion = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": build_review_input(code, tool_findings, task, plan),
                    },
                ],
                response_format={"type": "json_object"},
                extra_body={"enable_thinking": False},
            )
            if not completion.choices:
                raise LLMReviewError("模型未返回任何候选结果。")

            choice = completion.choices[0]
            if choice.finish_reason == "length":
                raise LLMReviewError("模型响应因长度限制被截断。")
            if choice.finish_reason == "content_filter":
                raise LLMReviewError("模型响应被内容过滤器中断。")

            refusal = getattr(choice.message, "refusal", None)
            if refusal:
                raise LLMReviewError(f"模型拒绝执行审查：{refusal}")
            return parse_review_report(choice.message.content)
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


def parse_review_report(content: str | None) -> ReviewReport:
    """Parse Chat Completions text into a validated ReviewReport."""

    if not content or not content.strip():
        raise LLMReviewError("模型返回了空内容。")

    candidate = _strip_json_fence(content.strip())
    try:
        return ReviewReport.model_validate_json(candidate)
    except (JSONDecodeError, ValidationError):
        pass

    object_start = candidate.find("{")
    if object_start < 0:
        raise LLMReviewError("模型响应不包含 JSON 对象。")
    try:
        payload, _ = json.JSONDecoder().raw_decode(candidate[object_start:])
        return ReviewReport.model_validate(payload)
    except (JSONDecodeError, ValidationError, TypeError) as exc:
        raise LLMReviewError(f"模型 JSON 不符合 ReviewReport：{exc}") from exc


def _strip_json_fence(content: str) -> str:
    if not content.startswith("```"):
        return content
    lines = content.splitlines()
    if lines and lines[0].strip().lower() in {"```", "```json"}:
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _first_env(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _load_settings(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> LLMSettings:
    resolved_api_key = (api_key or _first_env("DASHSCOPE_API_KEY", "OPENAI_API_KEY")).strip()
    if not _has_real_api_key(resolved_api_key):
        raise ConfigurationError(
            "请在 .env 中设置有效的 DASHSCOPE_API_KEY（或兼容的 OPENAI_API_KEY）。"
        )
    resolved_base_url = (
        base_url
        or _first_env("DASHSCOPE_BASE_URL", "OPENAI_BASE_URL")
        or DEFAULT_QWEN_BASE_URL
    ).rstrip("/")
    resolved_model = (
        model or _first_env("QWEN_MODEL", "OPENAI_MODEL") or DEFAULT_QWEN_MODEL
    )
    return LLMSettings(
        api_key=resolved_api_key,
        base_url=resolved_base_url,
        model=resolved_model,
    )


def _has_real_api_key(api_key: str) -> bool:
    return bool(api_key) and api_key.lower() not in {
        "your_api_key_here",
        "sk-你的_api_key",
        "sk-your_api_key",
    }


def create_reviewer() -> Reviewer:
    """Select the real LLM when configured, otherwise use deterministic Mock mode."""

    load_dotenv()
    api_key = _first_env("DASHSCOPE_API_KEY", "OPENAI_API_KEY")
    if not _has_real_api_key(api_key):
        return MockReviewer()
    return OpenAIReviewer(api_key=api_key)
