"""Small, durable research memory; interpretations remain explicitly unreviewed."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .data_tools import DEFINITIONS, LIMITATIONS, POOL


class Memory:
    def __init__(self, path: Path):
        self.path = path

    def read(self, query: str = "", limit: int = 3) -> dict:
        if not 0 <= limit <= 10:
            raise ValueError("Memory limit must be between 0 and 10.")
        notes = []
        if self.path.exists() and limit:
            with sqlite3.connect(self.path) as conn:
                rows = conn.execute("SELECT payload FROM notes ORDER BY created_at DESC, run_id DESC").fetchall()
            for (payload,) in rows:
                note = json.loads(payload)
                if query.casefold() in (note["question"] + note["summary"]).casefold():
                    notes.append(note)
                    if len(notes) >= limit:
                        break
        return {"status": "ok", "project": {"pool": POOL, "timezone": "UTC", "definitions": DEFINITIONS,
                 "historical_snapshot_limitations": LIMITATIONS,
                 "research_tool": "get_volatility_study recomputes the historical pilot; it is not a live directional forecast or a trading-return backtest.",
                 "live_tools": "get_live_metrics / get_live_day_metrics query RPC on demand. Report cutoff, finality and coverage from the new result."}, "notes": notes,
                "memory_policy": "Prior notes are leads, not current evidence. Re-query tools for factual claims. Interpretations are unreviewed."}

    def save(self, run_id: str, question: str, report: dict, sources: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        note = {"run_id": run_id, "question": question, "summary": report["answer_markdown"][:6000],
                "claims": report["claims"], "sources": sources, "limitations": report["limitations"],
                "status": "structured_numbers_checked; interpretation_unreviewed",
                "created_at": datetime.now(timezone.utc).isoformat()}
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS notes (run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL)")
            conn.execute("INSERT INTO notes VALUES (?, ?, ?)", (run_id, note["created_at"], json.dumps(note, ensure_ascii=False)))
