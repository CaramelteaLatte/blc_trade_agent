"""Query the audited historical dataset with exact decimal calculations."""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal, localcontext
from pathlib import Path

from scripts.audit_data import audit

ROOT = Path(__file__).resolve().parents[1]
POOL = "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640"
COUNT_FIELDS = ("event_count", "swap_count", "mint_count", "burn_count", "other_event_count",
                "large_trade_count", "unique_swap_transactions")
UNITS = {key: "events" for key in COUNT_FIELDS}
UNITS.update(unique_swap_transactions="transactions", usdc_volume="USDC",
             mean_swap_usdc="USDC/swap", large_trade_frequency="ratio",
             large_trade_percentage="percent")
DEFINITIONS = {
    "swap_count": "Number of Swap events, not unique transactions or users.",
    "usdc_volume": "Sum of abs(Swap.amount0_raw) / 1e6; assumes token0 is USDC with 6 decimals.",
    "mean_swap_usdc": "usdc_volume / swap_count; null if swap_count is zero.",
    "large_trade_count": "Number of swaps with USDC-side amount >= 100000 USDC.",
    "large_trade_frequency": "large_trade_count / swap_count; null for zero swaps.",
    "large_trade_percentage": "100 * large_trade_frequency.",
}
LIMITATIONS = [
    "Historical local snapshots only: 2026-10-01 through 2026-10-10 UTC; no live data.",
    "Local consistency checked; chain origin, completeness and token metadata not independently verified by RPC.",
    "Transaction size does not identify investors or establish causation or future returns.",
]


class DataError(ValueError):
    """A request cannot be answered from verified local inputs."""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise DataError("Dates must use YYYY-MM-DD.") from exc
    if parsed.isoformat() != value:
        raise DataError("Dates must use YYYY-MM-DD.")
    return parsed


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def summarize(rows: list[dict]) -> dict:
    """Aggregate counts before computing ratios; never average daily ratios."""
    with localcontext() as context:
        context.prec = 28
        result = {key: sum(row[key] for row in rows) for key in COUNT_FIELDS}
        volume = sum((Decimal(row["usdc_volume"]) for row in rows), Decimal(0))
        result["usdc_volume"] = decimal_text(volume)
        swaps = result["swap_count"]
        ratio = Decimal(result["large_trade_count"]) / swaps if swaps else None
        result["mean_swap_usdc"] = decimal_text(volume / swaps) if swaps else None
        result["large_trade_frequency"] = decimal_text(ratio) if ratio is not None else None
        result["large_trade_percentage"] = decimal_text(ratio * 100) if ratio is not None else None
        return result


def facts_for(prefix: str, values: dict, evidence_ids: list[str]) -> list[dict]:
    return [{"id": f"{prefix}/{key}", "metric": key,
             "value": str(value) if value is not None else None,
             "unit": UNITS[key], "evidence_ids": evidence_ids}
            for key, value in values.items() if key in UNITS]


class Dataset:
    def __init__(self, data_dir: Path | None = None):
        self.root = (data_dir or ROOT / "data").resolve()
        with localcontext() as context:
            context.prec = 28
            checked = audit(self.root)
        if not checked["all_checks_passed"]:
            raise DataError("Dataset audit failed: " + "; ".join(checked["failures"]))
        self.days = {row["date_utc"]: row for row in checked["days"]}
        self.metadata = {}
        self.file_hashes = {}
        for day in self.days:
            path = self.root / "processed" / f"metrics_{day}.json"
            self.metadata[day] = json.loads(path.read_text(encoding="utf-8"))
            self.file_hashes[path] = sha256(path)
            if self.metadata[day]["pool"].lower() != POOL:
                raise DataError("Unexpected pool in dataset.")

    def _dates(self, start: str, end: str) -> list[str]:
        first, last = parse_date(start), parse_date(end)
        if not first < last:
            raise DataError("Period must be non-empty, with start < end (end is exclusive).")
        if first < parse_date(min(self.days)) or last > parse_date(max(self.days)) + timedelta(days=1):
            raise DataError(f"Available UTC dates: {min(self.days)} through {max(self.days)}; no live data.")
        dates = [(first + timedelta(days=i)).isoformat() for i in range((last - first).days)]
        if any(day not in self.days for day in dates):
            raise DataError("Requested period contains missing dates; partial aggregates are not returned.")
        return dates

    def _evidence(self, days: list[str]) -> list[dict]:
        refs = []
        for day in days:
            raw = self.root / "raw" / f"events_{day}.ndjson"
            metrics = self.root / "processed" / f"metrics_{day}.json"
            digest = self.days[day]["snapshot_sha256"]
            if sha256(raw) != digest or sha256(metrics) != self.file_hashes[metrics]:
                raise DataError("Inputs changed after audit; start a new run and audit again.")
            refs.append({"id": f"snapshot:{day}", "file": f"data/raw/{raw.name}",
                         "sha256": digest, "date_utc": day,
                         "requested_blocks": self.days[day]["requested_blocks"]})
        return refs

    def period(self, start: str, end: str) -> dict:
        days = self._dates(start, end)
        refs = self._evidence(days)
        values = summarize([self.days[day] for day in days])
        prefix = days[0] if len(days) == 1 else f"{start}..{end}"
        return {"status": "ok", "pool": POOL,
                "period_utc": {"start_inclusive": start, "end_exclusive": end},
                "metrics": values, "definitions": DEFINITIONS,
                "facts": facts_for(prefix, values, [ref["id"] for ref in refs]),
                "source_refs": refs, "limitations": LIMITATIONS}

    def get_daily_metrics(self, day: str) -> dict:
        return self.period(day, (parse_date(day) + timedelta(days=1)).isoformat())

    def compare_periods(self, a_start: str, a_end: str, b_start: str, b_end: str) -> dict:
        a, b = self.period(a_start, a_end), self.period(b_start, b_end)
        refs = list({ref["id"]: ref for ref in a["source_refs"] + b["source_refs"]}.values())
        facts = a["facts"] + [fact for fact in b["facts"] if fact["id"] not in {x["id"] for x in a["facts"]}]
        changes = {}
        with localcontext() as context:
            context.prec = 28
            for key in ("swap_count", "usdc_volume", "mean_swap_usdc", "large_trade_frequency"):
                av, bv = a["metrics"][key], b["metrics"][key]
                delta = Decimal(str(bv)) - Decimal(str(av)) if av is not None and bv is not None else None
                pct = delta / Decimal(str(av)) * 100 if delta is not None and Decimal(str(av)) != 0 else None
                changes[key] = {"b_minus_a": decimal_text(delta) if delta is not None else None,
                                "relative_change_pct": decimal_text(pct) if pct is not None else None}
                for suffix, value, unit in (("b_minus_a", delta, UNITS[key]), ("relative_change_pct", pct, "percent")):
                    facts.append({"id": f"compare:{a_start}..{a_end}:{b_start}..{b_end}/{key}/{suffix}",
                                  "metric": key + "/" + suffix,
                                  "value": decimal_text(value) if value is not None else None,
                                  "unit": unit, "evidence_ids": [ref["id"] for ref in refs]})
        return {"status": "ok", "pool": POOL, "period_a": a["period_utc"], "period_b": b["period_utc"],
                "metrics_a": a["metrics"], "metrics_b": b["metrics"], "changes_b_vs_a": changes,
                "facts": facts, "source_refs": refs,
                "definitions": {**DEFINITIONS, "relative_change_pct": "100 * (B - A) / A; null if A is zero."},
                "limitations": LIMITATIONS}

    def get_large_trades(self, day: str, limit: int = 5) -> dict:
        if type(limit) is not int or not 1 <= limit <= 5:
            raise DataError("limit must be an integer between 1 and 5; only audited daily top-5 evidence is available.")
        self._dates(day, (parse_date(day) + timedelta(days=1)).isoformat())
        refs = self._evidence([day])
        trades = sorted(self.metadata[day]["largest_swap_evidence"],
                        key=lambda item: (-Decimal(item["usdc"]), item["block_number"], item["log_index"]))[:limit]
        facts = []
        for trade in trades:
            ref_id = f"log:{trade['transaction_hash']}:{trade['log_index']}"
            refs.append({"id": ref_id, **trade,
                         "transaction_url": f"https://etherscan.io/tx/{trade['transaction_hash']}",
                         "snapshot_id": f"snapshot:{day}"})
            facts.append({"id": f"{ref_id}/usdc", "metric": "swap_usdc", "value": trade["usdc"],
                          "unit": "USDC", "evidence_ids": [f"snapshot:{day}", ref_id]})
        return {"status": "ok", "pool": POOL, "date_utc": day, "trades": trades, "facts": facts,
                "source_refs": refs, "sort": "USDC descending, block ascending, log index ascending",
                "limitations": LIMITATIONS}
