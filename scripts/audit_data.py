#!/usr/bin/env python3
"""Recheck the supplied snapshots and metrics offline; this is not an RPC audit."""

import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from pathlib import Path


# Uniswap v3 event signatures; see the official IUniswapV3PoolEvents interface.
SWAP = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
MINT = "0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde"
BURN = "0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c"
TOKEN0_SCALE = 10**6  # Supplied metrics assume token0 is USDC with 6 decimals.


def audit(data_dir):
    raw_dir, processed_dir = data_dir / "raw", data_dir / "processed"
    paths = sorted(raw_dir.glob("events_*.ndjson"))
    if not paths:
        raise ValueError(f"No events_*.ndjson files in {raw_dir}")
    with (processed_dir / "daily_metrics_2026-10-01_to_10.csv").open() as stream:
        csv_list = list(csv.DictReader(stream))
    csv_rows = {row["date_utc"]: row for row in csv_list}
    days, failures, notes = [], [], []
    seen, swap_transactions = set(), set()
    totals, topic_counts = Counter(), Counter()
    previous_end = None

    for path in paths:
        date = path.stem.removeprefix("events_")
        raw = path.read_bytes()
        logs = [json.loads(line) for line in raw.splitlines() if line.strip()]
        if not logs:
            raise ValueError(f"Empty snapshot: {path}")
        metrics = json.loads((processed_dir / f"metrics_{date}.json").read_text())
        status = json.loads((raw_dir / f"fetch_status_{date}.json").read_text())
        topics = Counter(log["topics"][0] for log in logs)
        topic_counts.update(topics)
        blocks = [int(log["blockNumber"], 16) for log in logs]
        times = [int(log["blockTimestamp"], 16) for log in logs]
        order, swaps = [], []
        duplicates = 0
        swap_shape_ok = True

        for log in logs:
            key = (log["address"].lower(), log["transactionHash"], int(log["logIndex"], 16))
            duplicates += key in seen
            seen.add(key)
            order.append((int(log["blockNumber"], 16), int(log["transactionIndex"], 16), key[2]))
            if log["topics"][0] != SWAP:
                continue
            payload = bytes.fromhex(log["data"][2:])
            swap_shape_ok &= len(payload) == 160 and len(log["topics"]) == 3
            amount0_raw = int.from_bytes(payload[:32], "big", signed=True)
            swaps.append((abs(amount0_raw), log))
            swap_transactions.add(log["transactionHash"])

        volume_raw = sum(value for value, _ in swaps)
        volume = Decimal(volume_raw) / TOKEN0_SCALE
        threshold = Decimal(metrics["large_trade_threshold_usdc"])
        large_count = sum(value >= threshold * TOKEN0_SCALE for value, _ in swaps)
        frequency = Decimal(large_count) / len(swaps) if swaps else Decimal(0)
        counts = {
            "event_count": len(logs),
            "swap_count": len(swaps),
            "mint_count": topics[MINT],
            "burn_count": topics[BURN],
            "other_event_count": len(logs) - topics[SWAP] - topics[MINT] - topics[BURN],
        }
        lookup = {(log["transactionHash"], int(log["logIndex"], 16)):
                  (value, int(log["blockNumber"], 16)) for value, log in swaps}
        evidence = metrics["largest_swap_evidence"]
        evidence_keys = [(item["transaction_hash"], item["log_index"]) for item in evidence]
        evidence_valid = all(
            lookup.get(key) == (Decimal(item["usdc"]) * TOKEN0_SCALE, item["block_number"])
            for item, key in zip(evidence, evidence_keys)
        )
        top = sorted(swaps, key=lambda pair: (-pair[0], int(pair[1]["blockNumber"], 16),
                                              int(pair[1]["logIndex"], 16)))[:5]
        top_values = [Decimal(value) / TOKEN0_SCALE for value, _ in top]
        top_keys = [(log["transactionHash"], int(log["logIndex"], 16)) for _, log in top]
        checks = {
            "snapshot_sha256": hashlib.sha256(raw).hexdigest() == metrics["snapshot_sha256"],
            "source_file": metrics["source_file"] == path.name,
            "date_metadata": metrics["date_utc"] == status["date_utc"] == date,
            "pool_address": all(log["address"].lower() == metrics["pool"].lower()
                                == status["pool"].lower() for log in logs),
            "utc_date": all(datetime.fromtimestamp(t, timezone.utc).date().isoformat() == date
                            for t in times),
            "no_duplicate_logs": duplicates == 0,
            "no_removed_logs": all(log.get("removed") is False for log in logs),
            "event_order": order == sorted(order),
            "swap_abi_shape": swap_shape_ok,
            "fetch_log_count": status["logs"] == len(logs),
            "fetch_complete_flag": status["complete"] is True,
            "fetch_end": status["last_completed_block"] == status["last_block"],
            "block_bounds": status["first_block"] <= min(blocks) <= max(blocks) <= status["last_block"],
            "metrics_block_bounds": all(metrics[key] == status[key] for key in ("first_block", "last_block")),
            "consecutive_requested_ranges": previous_end is None or status["first_block"] == previous_end + 1,
            "volume": volume == Decimal(metrics["usdc_volume"]),
            "large_trade_threshold": threshold == 100000,
            "large_trade_count": large_count == metrics["large_trade_count"],
            "large_trade_frequency": frequency == Decimal(metrics["large_trade_frequency"]),
            "top5_evidence": evidence_valid and len(set(evidence_keys)) == len(evidence_keys)
                             and [Decimal(item["usdc"]) for item in evidence] == top_values,
            "csv_row": date in csv_rows and all(str(metrics.get(key)) == value
                                                for key, value in csv_rows[date].items()),
        }
        checks.update({name: value == metrics[name] for name, value in counts.items()})
        normalized_size = len(raw.replace(b"\r\n", b"\n"))
        checks["fetch_bytes_explained"] = status["bytes"] in (len(raw), normalized_size)
        if status["bytes"] != len(raw) and status["bytes"] == normalized_size:
            notes.append(f"{date}: fetch bytes match LF-normalized size; snapshot uses CRLF. Hash uses original bytes.")
        if checks["top5_evidence"] and evidence_keys != top_keys:
            notes.append(f"{date}: valid top-5 evidence; equal-size trades use a different tie order.")
        failures.extend(f"{date}: {name}" for name, passed in checks.items() if not passed)
        totals.update(counts)
        totals.update({"large_trade_count": large_count, "volume_raw": volume_raw})
        days.append({
            "date_utc": date, **counts,
            "unique_swap_transactions": len({log["transactionHash"] for _, log in swaps}),
            "usdc_volume": str(volume), "large_trade_count": large_count,
            "large_trade_frequency": str(frequency),
            "mean_swap_usdc": str(volume / len(swaps)) if swaps else None,
            "snapshot_sha256": hashlib.sha256(raw).hexdigest(),
            "actual_file_bytes": len(raw), "fetch_status_bytes": status["bytes"],
            "requested_blocks": [status["first_block"], status["last_block"]],
            "observed_blocks": [min(blocks), max(blocks)],
            "observed_timestamps_utc": [datetime.fromtimestamp(t, timezone.utc).isoformat()
                                        for t in (min(times), max(times))],
            "checks": checks,
        })
        previous_end = status["last_block"]

    if set(csv_rows) != {day["date_utc"] for day in days} or len(csv_rows) != len(csv_list):
        failures.append("CSV dates do not uniquely match snapshot dates")
    total_volume = str(Decimal(totals.pop("volume_raw")) / TOKEN0_SCALE)
    return {
        "scope": "Offline internal consistency only; no independent RPC/source-completeness verification.",
        "assumptions": {"token0": "USDC", "token0_decimals": 6,
                        "metadata_source": "Supplied metrics; contract metadata not independently fetched.",
                        "large_trade_comparison": ">=", "decimal_precision": 28},
        "all_checks_passed": not failures,
        "totals": {"days": len(days), **totals, "unique_swap_transactions": len(swap_transactions),
                   "usdc_volume": total_volume},
        "topic_counts": dict(topic_counts), "days": days, "notes": notes, "failures": failures,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parents[1] / "data")
    parser.add_argument("--output", type=Path, help="Optional JSON report path; original inputs are never rewritten")
    args = parser.parse_args()
    with localcontext() as context:
        context.prec = 28
        result = audit(args.data_dir)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("all_checks_passed", "totals", "failures")},
                     ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["all_checks_passed"] else 1)


if __name__ == "__main__":
    main()
