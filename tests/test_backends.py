import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from onchain_agent.codex_backend import codex_environment, summarize_events
from onchain_agent.data_tools import ROOT
from onchain_agent.runtime import harness_failure, make_harness
from onchain_agent.api_config import ApiSettings


class BackendTests(unittest.TestCase):
    def test_installed_runtime_loads_project_progress_plugin_without_model_call(self):
        settings = ApiSettings("https://invalid.example/v1", "https://invalid.example/v1/models", api_key="offline-test")
        with tempfile.TemporaryDirectory() as folder:
            with make_harness(Path(folder), use_memory=False, settings=settings):
                path = Path(folder) / "progress.jsonl"
                self.assertTrue(path.exists(), "Harness silently disabled the progress plugin")
                self.assertIn("模型摘要进度通道已连接", path.read_text())

    def test_subscription_process_cannot_inherit_api_credentials(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "CODEX_API_KEY": "test-codex-key",
                                     "OPENAI_BASE_URL": "https://invalid.example"}):
            child = codex_environment()
            self.assertNotIn("OPENAI_API_KEY", child)
            self.assertNotIn("CODEX_API_KEY", child)
            self.assertNotIn("OPENAI_BASE_URL", child)
            self.assertEqual(os.environ["OPENAI_API_KEY"], "test-key")

    def test_failed_turn_preserves_actionable_error(self):
        summary = summarize_events([json.dumps({"type": "turn.failed", "error": {"message": "Usage limit reached"}})])
        self.assertFalse(summary["completed"])
        self.assertEqual(summary["errors"], ["Usage limit reached"])
        message = harness_failure([{"type": "turn/end", "data": {"reason": {
            "kind": "error", "error": {"message": "You have no credits remaining."}}}}], "error")
        self.assertEqual(message, "You have no credits remaining.")

    def test_success_records_session_usage_without_making_tool_output_the_final_answer(self):
        summary = summarize_events([json.dumps(x) for x in [
            {"type": "thread.started", "thread_id": "test-thread"},
            {"type": "item.completed", "item": {"type": "mcp_tool_call"}},
            {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 20}},
        ]])
        self.assertTrue(summary["completed"])
        self.assertEqual(summary["session_id"], "test-thread")
        self.assertEqual(summary["usage"]["output_tokens"], 20)


class MCPIntegrationTests(unittest.TestCase):
    def test_stdio_tools_and_per_run_evidence_log(self):
        async def check(folder):
            log = Path(folder) / "tools.jsonl"
            params = StdioServerParameters(command=sys.executable, args=["-m", "onchain_agent.server"], cwd=ROOT,
                       env={"RESEARCH_TOOL_LOG": str(log), "RESEARCH_MEMORY_DB": str(Path(folder) / "memory.sqlite3"),
                            "RESEARCH_READ_NOTES": "0"})
            async with Client(params, read_timeout_seconds=30) as client:
                listing = await client.list_tools()
                self.assertEqual({tool.name for tool in listing.tools},
                                 {"get_daily_metrics", "compare_periods", "get_large_trades", "read_memory",
                                  "get_live_metrics", "get_live_day_metrics", "get_volatility_study"})
                for name, arguments in [
                    ("read_memory", {}),
                    ("get_daily_metrics", {"day": "2026-10-08"}),
                    ("get_large_trades", {"day": "2026-10-09", "limit": 3}),
                    ("get_daily_metrics", {"day": "2026-10-11"}),
                    ("get_volatility_study", {}),
                ]:
                    result = await client.call_tool(name, arguments)
                    wire = result.model_dump(by_alias=True)
                    self.assertFalse(wire.get("isError"), wire)
            rows = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(rows[0]["result"]["notes"], [])
            self.assertEqual(rows[1]["result"]["metrics"]["swap_count"], 8938)
            self.assertEqual(len(rows[2]["result"]["trades"]), 3)
            self.assertEqual(rows[3]["result"]["status"], "unavailable")
            self.assertEqual(rows[3]["result"]["facts"], [])
            self.assertEqual(rows[4]["result"]["data_mode"], "historical_research")
            self.assertTrue(rows[4]["result"]["study_complete"])
            self.assertEqual(rows[4]["result"]["test_hours"], 96)
        with tempfile.TemporaryDirectory() as folder:
            asyncio.run(check(folder))


if __name__ == "__main__":
    unittest.main()
