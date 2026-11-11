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
    run = commands.add_parser("run", help="Run a research question (default: DeepSeek Harness + configured API gateway)")
    run.add_argument("question", nargs="?")
    run.add_argument("--no-memory", action="store_true", help="Do not read or write previous research notes")
    run.add_argument("--reasoning", choices=["low", "medium", "high"], default="medium")
    run.add_argument("--no-thinking", action="store_true", help="Hide public reasoning summaries (Harness backend); keep tool progress")
    run.add_argument("--backend", choices=["codex", "harness"], default="harness",
                     help="codex: saved ChatGPT login; harness: OPENAI_API_KEY / Responses API")
    doctor = commands.add_parser("doctor", help="Check API configuration; does not call the model")
    doctor.add_argument("--harness", action="store_true", help="Also start and close the actual Harness runtime")
    doctor.add_argument("--catalog", action="store_true", help="Check API credentials and model ID using the gateway /models endpoint")
    doctor.add_argument("--codex", action="store_true", help="Also check the optional Codex subscription backend")
    query = commands.add_parser("query", help="Run a deterministic data tool without a model")
    query.add_argument("tool", choices=["get_daily_metrics", "compare_periods", "get_large_trades"])
    query.add_argument("arguments", help="Tool arguments as a JSON object")
    commands.add_parser("evaluate", help="Run the fixed offline tool cases (no model API)")
    study = commands.add_parser("research", help="Run the historical large-swap / next-hour volatility study without an LLM")
    study.add_argument("--plot", action="store_true", help="Also export PNG/SVG figures (requirements-research.txt)")
    live = commands.add_parser("live", help="Collect fresh pool data via RPC without calling a model")
    window = live.add_mutually_exclusive_group()
    window.add_argument("--minutes", type=int, default=60, help="Look back 1–1440 minutes from chain head")
    window.add_argument("--day", help="YYYY-MM-DD, defaults to a UTC calendar day")
    live.add_argument("--timezone", choices=["UTC", "Asia/Shanghai"], default="UTC")
    live.add_argument("--finality", choices=["latest", "finalized"], default="latest")
    args = parser.parse_args()
    try:
        if args.command == "query":
            arguments = json.loads(args.arguments)
            if not isinstance(arguments, dict):
                raise ValueError("Tool arguments must be a JSON object.")
            print(json.dumps(getattr(Dataset(), args.tool)(**arguments), ensure_ascii=False, indent=2))
        elif args.command == "doctor":
            from .api_config import ApiSettings, check_model_catalog
            from .runtime import make_harness
            settings = ApiSettings.from_environment()
            result = {**settings.public_dict(), "default_backend": "harness",
                      "harness_sdk_version": version("deepseek-harness-sdk"), "python": sys.version.split()[0]}
            from .rpc import rpc_configuration_status
            result["ethereum_rpc"] = rpc_configuration_status()
            if args.codex:
                from .codex_backend import codex_status
                result["codex"] = codex_status()
            if args.catalog:
                result["catalog"] = check_model_catalog(settings)
            if args.harness:
                with tempfile.TemporaryDirectory(prefix="onchain-runtime-check-") as folder:
                    with make_harness(Path(folder), use_memory=False, settings=settings):
                        result["harness_startup"] = "passed"
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if settings.api_key and result.get("catalog", {}).get("model_available", True) else 2
        elif args.command == "evaluate":
            from .evaluate import evaluate_tools
            result = evaluate_tools()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["passed"] else 1
        elif args.command == "research":
            from .research import run_study
            from .progress import ConsoleProgress
            with ConsoleProgress() as progress:
                result, directory = run_study(plot=args.plot, progress=progress.emit)
            print("\n".join(result["conclusions"]))
            print(json.dumps({"train_hours": result["train_hours"], "test_hours": result["test_hours"],
                              "test_groups": result["test_groups"], "prediction_scores": result["prediction_scores"],
                              "uncertainty": result["uncertainty"]}, ensure_ascii=False, indent=2))
            print(f"\nResearch report and evidence: {directory}")
        elif args.command == "live":
            from .live_data import get_live_metrics, get_live_day_metrics
            from .progress import ConsoleProgress
            with ConsoleProgress() as progress:
                result = (get_live_day_metrics(args.day, args.timezone, args.finality, progress.emit) if args.day
                          else get_live_metrics(args.minutes, args.finality, progress.emit))
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            from .runtime import DEMO_QUESTION, run_research
            from .progress import ConsoleProgress
            with ConsoleProgress() as progress:
                report, directory = run_research(args.question or DEMO_QUESTION, not args.no_memory, args.reasoning, args.backend, progress.emit, not args.no_thinking)
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
