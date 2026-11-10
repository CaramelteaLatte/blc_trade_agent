"""CLI for local tools, a runtime check, and live research runs."""

import argparse
import json
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path

from .data_tools import Dataset


def main() -> int:
    parser = argparse.ArgumentParser(description="On-chain research agent using GPT-6.1 Sol and local evidence tools")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Run a research question (default: Codex ChatGPT subscription)")
    run.add_argument("question", nargs="?")
    run.add_argument("--no-memory", action="store_true", help="Do not read or write previous research notes")
    run.add_argument("--reasoning", choices=["low", "medium", "high"], default="medium")
    run.add_argument("--backend", choices=["codex", "harness"], default="codex",
                     help="codex: saved ChatGPT login; harness: OPENAI_API_KEY / Responses API")
    doctor = commands.add_parser("doctor", help="Check local configuration; does not call the model")
    doctor.add_argument("--harness", action="store_true", help="Also start and close the actual Harness runtime")
    query = commands.add_parser("query", help="Run a deterministic data tool without a model")
    query.add_argument("tool", choices=["get_daily_metrics", "compare_periods", "get_large_trades"])
    query.add_argument("arguments", help="Tool arguments as a JSON object")
    commands.add_parser("evaluate", help="Run the fixed offline tool cases (no model API)")
    args = parser.parse_args()
    try:
        if args.command == "query":
            arguments = json.loads(args.arguments)
            if not isinstance(arguments, dict):
                raise ValueError("Tool arguments must be a JSON object.")
            print(json.dumps(getattr(Dataset(), args.tool)(**arguments), ensure_ascii=False, indent=2))
        elif args.command == "doctor":
            from .runtime import MODEL, load_credentials, make_harness
            configured = load_credentials()
            from .codex_backend import codex_status
            codex = codex_status()
            result = {"model": MODEL, "default_backend": "codex", "codex": codex, "api_key_configured": configured,
                      "harness_sdk_version": version("deepseek-harness-sdk"), "python": sys.version.split()[0]}
            if args.harness:
                with tempfile.TemporaryDirectory(prefix="onchain-runtime-check-") as folder:
                    with make_harness(Path(folder), use_memory=False):
                        result["harness_startup"] = "passed"
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if codex.get("chatgpt_login") else 2
        elif args.command == "evaluate":
            from .evaluate import evaluate_tools
            result = evaluate_tools()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["passed"] else 1
        else:
            from .runtime import DEMO_QUESTION, run_research
            report, directory = run_research(args.question or DEMO_QUESTION, not args.no_memory, args.reasoning, args.backend)
            print(report["answer_markdown"])
            print(f"\nReport and tool evidence: {directory}")
        return 0
    except Exception as exc:
        import os
        message = str(exc)
        key = os.environ.get("OPENAI_API_KEY", "")
        if key:
            message = message.replace(key, "[REDACTED]")
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
