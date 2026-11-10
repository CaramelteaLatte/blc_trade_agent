"""Research reports with interchangeable Codex subscription and Harness API backends."""

import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

from dotenv import load_dotenv

from .data_tools import ROOT, sha256
from .memory import Memory
from .validation import parse_report, validate_report

MODEL = "gpt-6.1-sol"
DEMO_QUESTION = "比较 2026-10-04 和 2026-10-08：为什么前者 Swap 次数更多，成交量却更低？请给出交易量、Swap 次数、平均交易额和大额交易次数占比，提供证据，并说明哪些原因目前无法确定。"


def load_credentials() -> bool:
    load_dotenv(ROOT / ".env", override=False)
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())


def make_harness(run_dir: Path, use_memory: bool = True, reasoning: str = "medium"):
    from deepseek_harness import DeepSeekHarness

    return DeepSeekHarness(
        provider="openai", model=MODEL, reasoning_effort=reasoning, max_tokens=8192,
        cwd=str(ROOT), runtime_cwd=str(ROOT), profile="sdk-minimal",
        dsh_home=str(ROOT / ".dsh/state/harness"),
        patches=(str(ROOT / ".dsh/onchain.patch.yml"),),
        env={"RESEARCH_PYTHON": sys.executable, "RESEARCH_PROJECT_ROOT": str(ROOT),
             "RESEARCH_TOOL_LOG": str(run_dir / "tool_results.jsonl"),
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


def run_research(question: str, use_memory: bool = True, reasoning: str = "medium", backend: str = "codex") -> tuple[dict, Path]:
    if backend not in {"codex", "harness"}:
        raise ValueError("Unknown agent backend: " + backend)
    if backend == "harness" and not load_credentials():
        raise ValueError("OPENAI_API_KEY is missing. Configure it in the project .env file or environment.")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    run_dir = ROOT / "reports" / run_id
    run_dir.mkdir(parents=True)
    source_files = [ROOT / ".dsh/onchain.patch.yml", ROOT / ".dsh/skills/onchain-research/SKILL.md",
                    ROOT / ".dsh/research-instructions.md", ROOT / "onchain_agent/report.schema.json",
                    ROOT / "scripts/audit_data.py", *sorted((ROOT / "onchain_agent").glob("*.py"))]
    metadata = {"run_id": run_id, "model": MODEL, "provider": "openai", "backend": backend,
                "authentication": "chatgpt_subscription" if backend == "codex" else "api_key",
                "question": question, "reasoning_effort": reasoning, "memory_enabled": use_memory,
                "harness_sdk_version": version("deepseek-harness-sdk"),
                "source_sha256": {str(path.relative_to(ROOT)): sha256(path) for path in source_files},
                "started_at_utc": datetime.now(timezone.utc).isoformat(), "status": "running"}
    write_json(run_dir / "run.json", metadata)
    started = time.monotonic()
    try:
        if backend == "codex":
            from .codex_backend import run_codex
            response, details = run_codex(question, run_dir, MODEL, reasoning, use_memory)
            metadata.update(details, finish_reason="completed")
        else:
            with make_harness(run_dir, use_memory, reasoning) as harness:
                result = harness.run(question, session_id="research-" + run_id)
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
        return report, run_dir
    except Exception as exc:
        message = str(exc)
        key = os.environ.get("OPENAI_API_KEY", "")
        if key:
            message = message.replace(key, "[REDACTED]")
        metadata.update(status="failed", error=message, elapsed_seconds=round(time.monotonic() - started, 3))
        write_json(run_dir / "run.json", metadata)
        raise RuntimeError(f"{message}\nRun artifacts: {run_dir}") from None
