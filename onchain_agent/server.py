"""Local MCP tools. No RPC or model credentials are needed in this process."""

import json
import os
import threading
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from .data_tools import Dataset, ROOT
from .memory import Memory

mcp = MCPServer("onchain", instructions="Use the onchain-research skill. Tools return verified local numbers, stable fact IDs and evidence. No live market access.")
_log_lock = threading.Lock()


@lru_cache(maxsize=1)
def dataset() -> Dataset:
    return Dataset()


def record(name: str, arguments: dict, result: dict) -> dict:
    target = os.environ.get("RESEARCH_TOOL_LOG")
    if target:
        line = json.dumps({"tool": name, "arguments": arguments, "result": result,
                           "at_utc": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False) + "\n"
        with _log_lock:
            with Path(target).open("a", encoding="utf-8") as stream:
                stream.write(line)
    return result


def invoke(name: str, arguments: dict) -> dict:
    try:
        result = getattr(dataset(), name)(**arguments)
    except ValueError as exc:
        result = {"status": "unavailable", "reason": str(exc), "facts": [], "source_refs": []}
    return record(name, arguments, result)


@mcp.tool()
def get_daily_metrics(day: str) -> dict:
    """Get audited daily pool metrics. day is YYYY-MM-DD UTC, available 2026-10-01 through 2026-10-10."""
    return invoke("get_daily_metrics", {"day": day})


@mcp.tool()
def compare_periods(a_start: str, a_end: str, b_start: str, b_end: str) -> dict:
    """Compare two UTC periods [start, end), dates YYYY-MM-DD. Ratios use aggregate counts. Changes are B minus A."""
    return invoke("compare_periods", {"a_start": a_start, "a_end": a_end, "b_start": b_start, "b_end": b_end})


@mcp.tool()
def get_large_trades(day: str, limit: int = 5) -> dict:
    """Get 1–5 largest audited swaps with transaction/log references. Equal amounts sort by block and log index."""
    return invoke("get_large_trades", {"day": day, "limit": limit})


@mcp.tool()
def read_memory(query: str = "", limit: int = 3) -> dict:
    """Read project definitions and prior research notes. Re-query data tools before using past numerical claims."""
    if os.environ.get("RESEARCH_READ_NOTES", "1") == "0":
        limit = 0
    memory = Memory(Path(os.environ.get("RESEARCH_MEMORY_DB", str(ROOT / ".dsh/state/research.sqlite3"))))
    return record("read_memory", {"query": query, "limit": limit}, memory.read(query, limit))


if __name__ == "__main__":
    mcp.run(transport="stdio")
