"""Python code review agent command-line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agent import CodeReviewAgent
from file_editor import FileEditError
from llm import ConfigurationError, LLMReviewError, create_reviewer
from planner import DEFAULT_REVIEW_TASK


def read_code_from_stdin() -> str:
    print("请粘贴 Python 代码；单独输入 END 后回车开始审查：")
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "END":
            break
        lines.append(line)
    return "\n".join(lines)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="使用 Agent 审查 Python 代码。")
    parser.add_argument("path", nargs="?", help="可选的 .py 文件；省略时从终端粘贴代码")
    parser.add_argument(
        "--task",
        default=DEFAULT_REVIEW_TASK,
        help="审查任务；Task Planner 会据此决定是否调用 CodeChecker",
    )
    parser.add_argument(
        "--apply-fixes",
        action="store_true",
        help="生成并应用安全的本地修复补丁，然后重新运行 CodeChecker",
    )
    return parser.parse_args()


def load_code(path_value: str | None) -> str:
    if path_value is None:
        return read_code_from_stdin()

    path = Path(path_value)
    if path.suffix.lower() != ".py":
        raise ValueError("仅支持审查 .py 文件。")
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ValueError(f"文件不存在：{path}") from exc
    except OSError as exc:
        raise ValueError(f"无法读取文件：{exc}") from exc


def main() -> int:
    try:
        arguments = parse_arguments()
        code = load_code(arguments.path)
        if not code.strip():
            raise ValueError("没有收到可审查的 Python 代码。")

        reviewer = create_reviewer()
        if reviewer.is_mock:
            print("[MOCK MODE] 未检测到有效 DASHSCOPE_API_KEY，使用本地固定审查结果。")

        agent = CodeReviewAgent(reviewer)
        if arguments.apply_fixes:
            if arguments.path is None:
                raise ValueError("--apply-fixes 只能用于传入的 .py 文件。")
            run = agent.review_and_fix(Path(arguments.path), code, task=arguments.task)
            print(run.review.report.to_text())
            print("\n=== FileEditor 结果 ===")
            if run.edit.applied:
                print(run.edit.diff.rstrip())
                print(f"补丁已应用到：{run.edit.path}")
                print(f"已修复：{', '.join(run.edit.fixed_rules)}")
                print("\n=== CodeChecker 修改后验证 ===")
                if run.verification_findings:
                    for finding in run.verification_findings:
                        print(f"- [{finding.severity.upper()}] {finding.problem}")
                else:
                    print("验证通过：未发现基础问题。")
            else:
                print("没有可安全自动应用的修改。")
            for reason in run.edit.skipped_reasons:
                print(f"未自动修复：{reason}")
        else:
            report = agent.review(code, task=arguments.task)
            print(report.to_text())
        return 0
    except (ValueError, ConfigurationError, FileEditError) as exc:
        print(f"配置或输入错误：{exc}", file=sys.stderr)
        return 2
    except LLMReviewError as exc:
        print(f"LLM 审查失败：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n操作已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
