"""Research reports with interchangeable Codex subscription and Harness API backends."""

import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from zoneinfo import ZoneInfo

from .api_config import ApiSettings, HARNESS_PROVIDER, MODEL
from .data_tools import ROOT, sha256
from .memory import Memory
from .validation import parse_report, validate_report

DEMO_QUESTION = "实时查询这个池最近一小时的 USDC 成交量、Swap 次数、平均交易额和大额交易占比，给出最大的三笔 Swap 证据。请明确数据截止时间、区块状态和分析局限。"


def make_harness(run_dir: Path, use_memory: bool = True, reasoning: str = "medium", settings: ApiSettings | None = None, show_reasoning: bool = True):
    from deepseek_harness import DeepSeekHarness

    settings = settings or ApiSettings.from_environment()
    settings.require_key()
    return DeepSeekHarness(
        provider=HARNESS_PROVIDER, model=settings.model, reasoning_effort=reasoning, max_tokens=8192,
        cwd=str(ROOT), runtime_cwd=str(ROOT), profile="sdk-minimal",
        dsh_home=str(ROOT / ".dsh/state/harness"),
        patches=(str(ROOT / ".dsh/onchain.patch.yml"),),
        env={"OPENAI_API_KEY": settings.api_key,
             "ALCHEMY_RPC_URL": os.environ.get("ALCHEMY_RPC_URL", ""),
             "RESEARCH_API_BASE_URL": settings.base_url, "RESEARCH_MODEL": settings.model,
             "RESEARCH_PYTHON": sys.executable, "RESEARCH_PROJECT_ROOT": str(ROOT),
             "RESEARCH_TOOL_LOG": str(run_dir / "tool_results.jsonl"),
             "RESEARCH_PROGRESS_LOG": str(run_dir / "progress.jsonl"),
             "RESEARCH_SHOW_REASONING": "1" if show_reasoning else "0",
             "RESEARCH_MEMORY_DB": str(ROOT / ".dsh/state/research.sqlite3"),
             "RESEARCH_READ_NOTES": "1" if use_memory else "0"},
        initialize_timeout_seconds=60, request_timeout_seconds=300, shutdown_timeout_seconds=5,
    )


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def harness_failure(events: list[dict], finish_reason: str) -> str:
    for event in reversed(events):
        if event.get("type") == "turn/end":
            payload = event.get("data", event.get("payload", event))
            reason = payload.get("reason", {})
            error = reason.get("error", {}) if isinstance(reason, dict) else {}
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])
    return f"Harness did not complete the research turn: {finish_reason}"


def run_research(question: str, use_memory: bool = True, reasoning: str = "medium", backend: str = "harness", progress=None, show_reasoning: bool = True) -> tuple[dict, Path]:
    emit = progress or (lambda message: None)
    if backend not in {"codex", "harness"}:
        raise ValueError("Unknown agent backend: " + backend)
    settings = ApiSettings.from_environment() if backend == "harness" else None
    if settings:
        settings.require_key()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    run_dir = ROOT / "reports" / run_id
    run_dir.mkdir(parents=True)
    source_files = [ROOT / "research/study_plan.json", ROOT / ".dsh/onchain.patch.yml", ROOT / ".dsh/progress-bridge.mjs", ROOT / ".dsh/skills/onchain-research/SKILL.md",
                    ROOT / ".dsh/research-instructions.md", ROOT / "onchain_agent/report.schema.json",
                    ROOT / "scripts/audit_data.py", *sorted((ROOT / "onchain_agent").glob("*.py"))]
    metadata = {"run_id": run_id, "model": settings.model if settings else MODEL,
                "provider": HARNESS_PROVIDER if settings else "openai", "backend": backend,
                "authentication": "chatgpt_subscription" if backend == "codex" else "api_key",
                "question": question, "reasoning_effort": reasoning, "memory_enabled": use_memory,
                "reasoning_summary_display": show_reasoning and backend == "harness",
                "harness_sdk_version": version("deepseek-harness-sdk"),
                "source_sha256": {str(path.relative_to(ROOT)): sha256(path) for path in source_files},
                "started_at_utc": datetime.now(timezone.utc).isoformat(), "status": "running"}
    if settings:
        metadata.update(settings.public_dict())
    write_json(run_dir / "run.json", metadata)
    started = time.monotonic()
    now = datetime.now(timezone.utc)
    timed_question = (f"请求时间（UTC）：{now.isoformat()}；北京时间：{now.astimezone(ZoneInfo('Asia/Shanghai')).isoformat()}。"
                      "相对日期按用户指定时区解释，未指定的‘今天/昨天’默认北京时间；报告须列出工具实际覆盖时间。\n研究问题：" + question)
    emit(f"启动 {backend}，模型 {metadata['model']}；报告目录：{run_dir}")
    try:
        if backend == "codex":
            from .codex_backend import run_codex
            response, details = run_codex(timed_question, run_dir, MODEL, reasoning, use_memory)
            metadata.update(details, finish_reason="completed")
        else:
            from .progress import ProgressFeed
            with ProgressFeed(run_dir / "progress.jsonl", emit), make_harness(run_dir, use_memory, reasoning, settings, show_reasoning) as harness:
                emit("Harness 已启动，正在请求模型…")
                def notify(notification):
                    if notification.method != "session.event":
                        return
                    event = notification.payload.get("event", {})
                    if event.get("type") == "tool/call":
                        emit("调用工具：" + event.get("data", {}).get("name", "research tool"))
                    elif event.get("type") == "tool/result":
                        emit("工具已返回，模型继续分析…")
                    elif event.get("type") == "llm/retry":
                        emit("模型请求遇到暂时错误，Harness 正在等待重试…")
                result = harness.run(timed_question, session_id="research-" + run_id, on_notification=notify)
            metadata.update(session_id=result.session_id, finish_reason=result.finish_reason, api="responses")
            metadata["event_counts"] = {}
            for event in result.events:
                event_type = event.get("type", "unknown")
                metadata["event_counts"][event_type] = metadata["event_counts"].get(event_type, 0) + 1
            response = result.final_response
            (run_dir / "response.txt").write_text(response + "\n", encoding="utf-8")
            if result.finish_reason != "completed":
                raise ValueError(harness_failure(result.events, result.finish_reason))
        metadata["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report = parse_report(response)
        emit("正在核验报告数值与证据…")
        log_path = run_dir / "tool_results.jsonl"
        observations = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()] if log_path.exists() else []
        validation = validate_report(report, observations)
        write_json(run_dir / "validation.json", validation)
        write_json(run_dir / "report.json", report)
        if not validation["passed"]:
            raise ValueError("Report numerical validation failed: " + "; ".join(validation["errors"]))
        source_lines = []
        for ref in validation["source_refs"]:
            link = ref.get("transaction_url") or "../../" + ref["file"]
            source_lines.append(f"- [{ref['id']}]({link})" + (f" — SHA-256 `{ref['sha256']}`" if "sha256" in ref else ""))
        rendered = report["answer_markdown"] + "\n\n## 数据与推断限制\n\n"
        rendered += "\n".join("- " + item for item in report["limitations"])
        rendered += "\n\n## 证据索引\n\n" + ("\n".join(source_lines) or "本次没有可用的数值证据；数据可用性响应见 tool_results.jsonl。")
        rendered += (f"\n\n已核验 {validation['checked_claim_count']} 条结构化数值；文字解释和因果判断仍需人工审阅。\n"
                     if report["claims"] else "\n\n本报告没有可核验的结构化数值，已记录工具返回的数据不可用状态。\n")
        (run_dir / "report.md").write_text(rendered, encoding="utf-8")
        if use_memory:
            Memory(ROOT / ".dsh/state/research.sqlite3").save(run_id, question, report, validation["source_refs"])
        metadata.update(status="completed", checked_claim_count=validation["checked_claim_count"])
        write_json(run_dir / "run.json", metadata)
        emit(f"完成，{validation['checked_claim_count']} 条结构化数值核验通过。")
        return report, run_dir
    except Exception as exc:
        message = str(exc)
        key = os.environ.get("OPENAI_API_KEY", "")
        if key:
            message = message.replace(key, "[REDACTED]")
        metadata.update(status="failed", error=message, elapsed_seconds=round(time.monotonic() - started, 3))
        write_json(run_dir / "run.json", metadata)
        raise RuntimeError(f"{message}\nRun artifacts: {run_dir}") from None
