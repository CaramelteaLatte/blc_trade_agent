"""On-demand, bounded pool snapshots with explicit time coverage and provenance."""

import json
import os
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.audit_data import BURN, MINT, SWAP

from .data_tools import DEFINITIONS, POOL, ROOT, facts_for, parse_date, sha256, summarize
from .rpc import RpcClient, RpcError, RpcSettings, block_header, quantity

USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
LIVE_ROOT = ROOT / "data/live"


def utc_text(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def write_json(path: Path, value) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def contract_word(rpc, address, selector, at_block) -> int:
    value = rpc.call("eth_call", [{"to": address, "data": selector}, hex(at_block)])
    if not isinstance(value, str) or len(value) != 66:
        raise RpcError("Contract metadata call returned a malformed ABI word.")
    return quantity(value, "contract ABI word")


def pool_metadata(rpc, at_block: int) -> dict:
    # These independent reads share the same pinned block and concurrency budget.
    with ThreadPoolExecutor(max_workers=rpc.settings.workers) as executor:
        chain = executor.submit(rpc.call, "eth_chainId", [])
        calls = [executor.submit(contract_word, rpc, address, selector, at_block)
                 for address, selector in [(POOL, "0x0dfe1681"), (POOL, "0xd21220a7"),
                                           (USDC, "0x313ce567"), (WETH, "0x313ce567"),
                                           (POOL, "0xddca3f43")]]
        chain_id = quantity(chain.result())
        token0, token1, decimals0, decimals1, fee = [future.result() for future in calls]
    if chain_id != 1:
        raise RpcError("RPC is not Ethereum Mainnet (chain ID 1).")
    if token0 != int(USDC, 16) or token1 != int(WETH, 16):
        raise RpcError("Pool token addresses do not match the configured USDC/WETH pool.")
    if (decimals0, decimals1) != (6, 18):
        raise RpcError("Unexpected token decimals; refusing to compute incorrectly scaled volume.")
    return {"chain_id": 1, "pool": POOL, "token0": USDC, "token1": WETH,
            "token0_symbol": "USDC", "token1_symbol": "WETH", "token0_decimals": decimals0,
            "token1_decimals": decimals1, "metadata_at_block": at_block,
            "fee": fee}


def metrics_from_logs(logs: list[dict]) -> tuple[dict, list[dict]]:
    topics = Counter(log["topics"][0] for log in logs)
    swaps = []
    for log in logs:
        if log["topics"][0] != SWAP:
            continue
        try:
            data = bytes.fromhex(log["data"].removeprefix("0x"))
        except (ValueError, KeyError, AttributeError):
            raise RpcError("Malformed Swap event data.") from None
        if len(data) != 160 or len(log["topics"]) != 3:
            raise RpcError("Swap event does not match the Uniswap v3 ABI.")
        amount0 = int.from_bytes(data[:32], "big", signed=True)
        swaps.append((abs(amount0), log))
    raw_volume = sum(amount for amount, _ in swaps)
    row = {"event_count": len(logs), "swap_count": topics[SWAP], "mint_count": topics[MINT],
           "burn_count": topics[BURN], "other_event_count": len(logs) - topics[SWAP] - topics[MINT] - topics[BURN],
           "unique_swap_transactions": len({log["transactionHash"] for _, log in swaps}),
           "large_trade_count": sum(amount >= 100000 * 10 ** 6 for amount, _ in swaps),
           "usdc_volume": format(Decimal(raw_volume) / 10 ** 6, "f")}
    top = sorted(swaps, key=lambda item: (-item[0], quantity(item[1]["blockNumber"]), quantity(item[1]["logIndex"])))[:5]
    trades = [{"usdc": format(Decimal(amount) / 10 ** 6, "f"),
               "transaction_hash": log["transactionHash"], "block_number": quantity(log["blockNumber"]),
               "block_hash": log["blockHash"], "log_index": quantity(log["logIndex"])} for amount, log in top]
    return summarize([row]), trades


class LiveCollector:
    def __init__(self, rpc=None, root: Path = LIVE_ROOT, progress=None):
        self.rpc = rpc if rpc is not None else RpcClient(RpcSettings.from_environment())
        self.root = root
        self.progress = progress or (lambda message: None)
        self.blocks = {}

    def block(self, number: int) -> dict:
        if number not in self.blocks:
            self.blocks[number] = block_header(self.rpc, number)
        return self.blocks[number]

    def lower_bound(self, timestamp: int, hi: int) -> int:
        upper_time = self.block(hi)["timestamp"]
        if upper_time < timestamp:
            return hi + 1
        # A 12-second estimate only chooses a probe. Real adjacent timestamps
        # prove the boundary. Exponentially widen around the probe when slots
        # were missed, then bisect only the verified bracket.
        candidate = max(0, hi - (upper_time - timestamp + 11) // 12)
        step = 1
        if self.block(candidate)["timestamp"] >= timestamp:
            hi = candidate
            while True:
                if hi == 0:
                    return 0
                probe = max(0, candidate - step)
                if self.block(probe)["timestamp"] < timestamp:
                    lo = probe + 1
                    break
                hi = probe
                step *= 2
        else:
            lo = candidate + 1
            while True:
                probe = min(hi, candidate + step)
                if self.block(probe)["timestamp"] >= timestamp:
                    hi = probe
                    break
                lo = probe + 1
                step *= 2
        while lo < hi:
            mid = (lo + hi) // 2
            if self.block(mid)["timestamp"] < timestamp:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def fetch_range(self, first: int, last: int) -> list:
        try:
            result = self.rpc.call("eth_getLogs", [{"fromBlock": hex(first), "toBlock": hex(last), "address": POOL}])
        except RpcError as exc:
            if exc.range_limit and first < last:
                middle = (first + last) // 2
                return self.fetch_range(first, middle) + self.fetch_range(middle + 1, last)
            raise
        if not isinstance(result, list):
            raise RpcError("eth_getLogs did not return an array.")
        for log in result:
            if not isinstance(log, dict) or log.get("address", "").lower() != POOL:
                raise RpcError("RPC returned logs for an unexpected pool.")
            if not first <= quantity(log.get("blockNumber")) <= last:
                raise RpcError("RPC returned logs outside the requested block range.")
            if log.get("removed") is not False:
                raise RpcError("RPC returned removed or unconfirmed log metadata; retry the query.")
            for field in ("blockHash", "transactionHash"):
                value = log.get(field)
                if not isinstance(value, str) or len(value) != 66 or not value.startswith("0x"):
                    raise RpcError("RPC returned malformed log hashes.")
            quantity(log.get("logIndex"))
            quantity(log.get("transactionIndex"))
            topics = log.get("topics")
            if not isinstance(topics, list) or not topics or not all(isinstance(t, str) for t in topics):
                raise RpcError("RPC returned malformed log topics.")
        return result

    def collect(self, lookback_minutes=60, finality="latest", *, day=None, day_timezone="UTC") -> dict:
        if finality not in {"latest", "finalized"}:
            raise RpcError("finality must be latest or finalized.")
        if type(lookback_minutes) is not int or not 1 <= lookback_minutes <= 1440:
            raise RpcError("lookback_minutes must be an integer from 1 to 1440.")
        if day_timezone not in {"UTC", "Asia/Shanghai"}:
            raise RpcError("timezone must be UTC or Asia/Shanghai.")
        parsed_day = parse_date(day) if day is not None else None
        self.progress("正在连接 Ethereum RPC，确定采集截止区块…")
        anchor = block_header(self.rpc, finality)
        self.blocks[anchor["number"]] = anchor
        cutoff = anchor["timestamp"] + 1
        if parsed_day:
            start_dt = datetime.combine(parsed_day, datetime.min.time(), ZoneInfo(day_timezone))
            start, requested_end = int(start_dt.timestamp()), int((start_dt + timedelta(days=1)).timestamp())
            if start >= cutoff:
                raise RpcError("Requested date has not started at the selected chain head; no full-day metrics are available.")
        else:
            start, requested_end = cutoff - lookback_minutes * 60, cutoff
        end = min(requested_end, cutoff)
        self.progress("正在按区块时间戳定位查询范围…")
        end_block = self.lower_bound(end, anchor["number"]) - 1
        first = self.lower_bound(start, max(0, end_block))
        if end_block - first + 1 > 20000:
            raise RpcError("Requested interval exceeds 20,000 blocks; choose a shorter window.")
        self.progress("正在并行核对链 ID、池地址和代币精度…")
        metadata = pool_metadata(self.rpc, max(0, end_block))
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
        folder = self.root / run_id
        folder.mkdir(parents=True, exist_ok=False)
        status = {"status": "collecting", "collection_id": run_id, "provider": "Alchemy Ethereum Mainnet",
                  "pool_metadata": metadata, "requested_period_utc": {"start_inclusive": utc_text(start), "end_exclusive": utc_text(requested_end)},
                  "period_utc": {"start_inclusive": utc_text(start), "end_exclusive": utc_text(end)},
                  "day_timezone": day_timezone if day else None, "requested_day": day,
                  "finality": finality, "anchor": anchor, "requested_blocks": [first, end_block],
                  "window_complete": end == requested_end, "collection_complete": False,
                  "started_at_utc": datetime.now(timezone.utc).isoformat(),
                  "rpc_limits": {"block_span": self.rpc.settings.block_span, "workers": self.rpc.settings.workers,
                                 "deadline_seconds": self.rpc.settings.deadline_seconds, "max_requests": self.rpc.settings.max_requests},
                  "source_sha256": {"onchain_agent/live_data.py": sha256(Path(__file__)),
                                    "onchain_agent/rpc.py": sha256(ROOT / "onchain_agent/rpc.py")}}
        write_json(folder / "manifest.json", status)
        logs, seen, duplicates, chunks = [], {}, 0, 0
        partial = folder / "events.ndjson.partial"
        try:
            span = self.rpc.settings.block_span
            ranges = [(n, min(n + span - 1, end_block)) for n in range(first, end_block + 1, span)]
            total_bytes = 0
            with partial.open("wb") as stream, ThreadPoolExecutor(max_workers=self.rpc.settings.workers) as executor:
                last_progress = time.monotonic()
                for offset in range(0, len(ranges), self.rpc.settings.workers):
                    group = ranges[offset:offset + self.rpc.settings.workers]
                    futures = [executor.submit(self.fetch_range, a, b) for a, b in group]
                    for future in futures:
                        for log in future.result():
                            key = (log["transactionHash"], quantity(log["logIndex"]))
                            if key in seen:
                                if seen[key] != log:
                                    raise RpcError("Conflicting duplicate logs; retry after chain reorganization settles.")
                                duplicates += 1
                                continue
                            if "blockTimestamp" in log and not start <= quantity(log["blockTimestamp"]) < end:
                                raise RpcError("Log timestamp is outside the requested interval.")
                            seen[key] = log
                            logs.append(log)
                            line = (json.dumps(log, separators=(",", ":")) + "\n").encode()
                            total_bytes += len(line)
                            if len(logs) > 100000 or total_bytes > 128 * 1024 * 1024:
                                raise RpcError("Snapshot size limit reached; no partial metrics returned. Choose a shorter window.")
                            stream.write(line)
                        chunks += 1
                    if time.monotonic() - last_progress >= 5 or chunks == len(ranges):
                        self.progress(f"采集区块批次 {chunks}/{len(ranges)}，已收到 {len(logs)} 条事件")
                        last_progress = time.monotonic()
            self.progress("日志采集完成，正在核验边界区块与链重组…")
            boundaries = {str(n): self.block(n) for n in {max(0, first - 1), first, end_block, end_block + 1}
                          if 0 <= n <= anchor["number"]}
            # Recheck after all other RPC reads; latest-chain results remain provisional.
            if block_header(self.rpc, anchor["number"])["hash"] != anchor["hash"]:
                raise RpcError("Chain anchor changed during collection; discard this partial snapshot and retry.")
            values, trades = metrics_from_logs(logs)
            os.replace(partial, folder / "events.ndjson")
            digest = sha256(folder / "events.ndjson")
            status.update(status="complete", collection_complete=True, logs=len(logs), duplicates_removed=duplicates,
                          completed_chunks=chunks, snapshot_sha256=digest, boundary_blocks=boundaries,
                          rpc_request_count=self.rpc.request_count, finished_at_utc=datetime.now(timezone.utc).isoformat())
            write_json(folder / "manifest.json", status)
            result = self.result(status, values, trades, folder)
            write_json(folder / "metrics.json", result)
            self.progress(f"采集完成，正在返回 {values['swap_count']} 条 Swap 的指标和证据")
            return result
        except Exception as exc:
            message = str(exc) if isinstance(exc, RpcError) else f"Collection failed ({type(exc).__name__}); no partial metrics returned."
            status.update(status="failed", error=message, logs=len(logs), completed_chunks=chunks,
                          collection_complete=False, rpc_request_count=self.rpc.request_count)
            write_json(folder / "manifest.json", status)
            raise RpcError(message) from None

    def result(self, status, values, trades, folder) -> dict:
        snapshot_id = "live:" + status["collection_id"]
        base = "data/live/" + folder.name
        refs = [{"id": snapshot_id, "file": base + "/events.ndjson", "sha256": status["snapshot_sha256"],
                 "requested_blocks": status["requested_blocks"], "period_utc": status["period_utc"]},
                {"id": snapshot_id + ":manifest", "file": base + "/manifest.json", "sha256": sha256(folder / "manifest.json")}]
        evidence_ids = [ref["id"] for ref in refs]
        facts = facts_for(snapshot_id, values, evidence_ids)
        for trade in trades:
            log_id = f"{snapshot_id}:log:{trade['transaction_hash']}:{trade['log_index']}"
            refs.append({"id": log_id, **trade, "transaction_url": "https://etherscan.io/tx/" + trade["transaction_hash"],
                         "snapshot_id": snapshot_id})
            facts.append({"id": log_id + "/usdc", "metric": "swap_usdc", "value": trade["usdc"], "unit": "USDC",
                          "evidence_ids": evidence_ids + [log_id]})
        limits = ["One Ethereum pool only; USDC-side absolute volume, not total market volume or capital inflow.",
                  "Read from one RPC provider; metadata and local hashes checked, completeness is not independently proven.",
                  "Transaction size cannot establish investor identity, causation or future returns."]
        if status["finality"] == "latest":
            limits.append("Latest blocks are provisional and may reorganize; the anchor hash was rechecked after collection.")
        else:
            limits.append("The selected finalized head lags the latest chain tip; report the actual cutoff time.")
        if not status["window_complete"]:
            limits.append("The requested day is not fully covered yet; these are partial-day metrics, not full-day totals.")
        return {"status": "ok", "data_mode": "on_demand_rpc", "pool": POOL,
                "period_utc": status["period_utc"], "requested_period_utc": status["requested_period_utc"],
                "as_of_block_utc": utc_text(status["boundary_blocks"][str(status["requested_blocks"][1])]["timestamp"]),
                "chain_head_utc": utc_text(status["anchor"]["timestamp"]), "anchor": status["anchor"],
                "collected_at_utc": status["finished_at_utc"], "finality": status["finality"],
                "window_complete": status["window_complete"], "collection_complete": True,
                "pool_metadata": status["pool_metadata"], "metrics": values, "largest_swaps": trades,
                "definitions": {**DEFINITIONS,
                    "usdc_volume": "Sum of abs(Swap.amount0_raw) / 1e6; token0 address and 6 decimals checked via RPC at the recorded block."},
                "facts": facts, "source_refs": refs, "limitations": limits}


def get_live_metrics(lookback_minutes=60, finality="latest", progress=None):
    return LiveCollector(progress=progress).collect(lookback_minutes, finality)


def get_live_day_metrics(day, timezone="UTC", finality="latest", progress=None):
    return LiveCollector(progress=progress).collect(finality=finality, day=day, day_timezone=timezone)
