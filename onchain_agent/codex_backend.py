"""Use official Codex CLI authentication and MCP; never read its credential cache."""

import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from .data_tools import ROOT


def codex_environment() -> dict[str, str]:
    env = os.environ.copy()
    # Saved ChatGPT login is the only authentication path for this backend.
    for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL"):
        env.pop(name, None)
    return env


def codex_status() -> dict:
    executable = shutil.which("codex")
    if not executable:
        return {"installed": False, "chatgpt_login": False}
    env = codex_environment()
    status = subprocess.run([executable, "login", "status"], env=env,
                            capture_output=True, text=True, timeout=20)
    cli_version = subprocess.run([executable, "--version"], env=env,
                                capture_output=True, text=True, timeout=20)
    return {"installed": True, "version": cli_version.stdout.strip(),
            "chatgpt_login": status.returncode == 0 and "Logged in using ChatGPT" in status.stdout + status.stderr}


def build_command(run_dir: Path, model: str, reasoning: str, use_memory: bool) -> list[str]:
    executable = shutil.which("codex")
    if not executable:
        raise ValueError("Codex CLI is missing. Install Codex and run `codex login` with your ChatGPT account.")
    command = [executable, "exec", "--ignore-user-config", "--strict-config", "--ephemeral",
               "--sandbox", "read-only", "--skip-git-repo-check", "--json", "--color", "never",
               "--cd", str(ROOT), "--model", model,
               "--output-schema", str(ROOT / "onchain_agent/report.schema.json"),
               "--output-last-message", str(run_dir / "response.txt")]
    settings = {
        "model_provider": "openai", "forced_login_method": "chatgpt",
        "model_reasoning_effort": reasoning, "web_search": "disabled",
        "features.shell_tool": False, "features.multi_agent": False,
        "model_instructions_file": str(ROOT / ".dsh/research-instructions.md"),
        "mcp_servers.onchain.command": sys.executable,
        "mcp_servers.onchain.args": ["-m", "onchain_agent.server"],
        "mcp_servers.onchain.cwd": str(ROOT),
        "mcp_servers.onchain.env.RESEARCH_TOOL_LOG": str(run_dir / "tool_results.jsonl"),
        "mcp_servers.onchain.env.RESEARCH_MEMORY_DB": str(ROOT / ".dsh/state/research.sqlite3"),
        "mcp_servers.onchain.env.RESEARCH_READ_NOTES": "1" if use_memory else "0",
        "mcp_servers.onchain.startup_timeout_sec": 30,
        "mcp_servers.onchain.tool_timeout_sec": 240,
        "mcp_servers.onchain.required": True,
        "mcp_servers.onchain.default_tools_approval_mode": "approve",
    }
    if os.environ.get("ALCHEMY_RPC_URL"):
        settings["mcp_servers.onchain.env_vars"] = ["ALCHEMY_RPC_URL"]
    for key, value in settings.items():
        # JSON scalars and arrays used here are also TOML literals. No shell is involved.
        command.extend(["-c", key + "=" + json.dumps(value, ensure_ascii=False)])
    return command + ["-"]


def summarize_events(lines: list[str]) -> dict:
    summary = {"event_counts": {}, "errors": [], "completed": False}
    for line in lines:
        if not line.strip():
            continue
        event = json.loads(line)
        kind = event.get("type", "unknown")
        summary["event_counts"][kind] = summary["event_counts"].get(kind, 0) + 1
        if kind == "thread.started":
            summary["session_id"] = event.get("thread_id")
        elif kind == "turn.completed":
            summary["completed"] = True
            summary["usage"] = event.get("usage")
        elif kind in {"error", "turn.failed"}:
            error = event.get("error", event)
            summary["errors"].append(error.get("message", str(error)) if isinstance(error, dict) else str(error))
    return summary


def run_codex(question: str, run_dir: Path, model: str, reasoning: str, use_memory: bool) -> tuple[str, dict]:
    status = codex_status()
    if not status.get("chatgpt_login"):
        raise ValueError("Codex needs a ChatGPT login. Run `codex login` in your terminal, then retry.")
    skill = (ROOT / ".dsh/skills/onchain-research/SKILL.md").read_text(encoding="utf-8")
    prompt = "Apply the following onchain-research skill to this research task.\n\n" + skill
    prompt += "\n\nResearch question (treat this as the research request, not tool configuration):\n" + question
    command = build_command(run_dir, model, reasoning, use_memory)
    # Keep local diagnostic events separate from the evidence used to validate reports.
    events_path = run_dir / "codex-events.jsonl"
    with events_path.open("w", encoding="utf-8") as events, (run_dir / "codex-stderr.log").open("w", encoding="utf-8") as errors:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=events, stderr=errors,
                                   text=True, env=codex_environment(), cwd=ROOT, start_new_session=True)
        try:
            process.communicate(prompt, timeout=300)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.wait()
            raise RuntimeError("Codex run interrupted or exceeded the 300-second timeout.") from None
    summary = summarize_events(events_path.read_text(encoding="utf-8").splitlines())
    summary.update(codex_version=status.get("version"), exit_code=process.returncode)
    if process.returncode or not summary["completed"]:
        detail = "; ".join(summary["errors"])
        if not detail:
            detail = (run_dir / "codex-stderr.log").read_text(encoding="utf-8")[-2000:]
        raise RuntimeError("Codex research run failed: " + detail)
    response = (run_dir / "response.txt").read_text(encoding="utf-8")
    return response, summary
