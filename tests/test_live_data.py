import io
import bisect
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from onchain_agent.data_tools import POOL, sha256
from onchain_agent.live_data import LiveCollector, USDC, WETH, metrics_from_logs
from onchain_agent.rpc import RpcClient, RpcError, RpcSettings
from onchain_agent.validation import validate_report
from scripts.audit_data import SWAP


def hash_value(number):
    return "0x" + format(number, "064x")


def swap(block, amount, tx=1, index=0):
    data = b"".join(value.to_bytes(32, "big", signed=True) for value in [amount, -1, 1, 1, 1])
    return {"address": POOL, "topics": [SWAP, hash_value(2), hash_value(3)], "data": "0x" + data.hex(),
            "blockNumber": hex(block), "blockHash": hash_value(block + 1), "transactionHash": hash_value(tx),
            "logIndex": hex(index), "transactionIndex": "0x0", "removed": False}


class FakeRpc:
    def __init__(self, logs=(), *, head=100, fail_logs=False, reorg=False, range_limit=10, chain_id=1, decimals=6):
        self.settings = RpcSettings("https://eth-mainnet.g.alchemy.com/v2/test-only", workers=2)
        self.request_count = 0
        self.logs = list(logs)
        self.head = head
        self.base = int(datetime(2026, 11, 10, tzinfo=timezone.utc).timestamp())
        self.fail_logs, self.reorg, self.range_limit = fail_logs, reorg, range_limit
        self.chain_id, self.decimals = chain_id, decimals
        self.lock = threading.Lock()
        self.log_ranges = []

    def call(self, method, params):
        with self.lock:
            self.request_count += 1
        if method == "eth_chainId":
            return hex(self.chain_id)
        if method == "eth_getBlockByNumber":
            tag = params[0]
            number = self.head if tag == "latest" else self.head - 10 if tag == "finalized" else int(tag, 16)
            digest = hash_value(number + 1)
            if self.reorg and tag == hex(self.head):
                digest = hash_value(99999)
            return {"number": hex(number), "timestamp": hex(self.base + number * 12), "hash": digest}
        if method == "eth_call":
            address, selector = params[0]["to"], params[0]["data"]
            value = {"0x0dfe1681": int(USDC, 16), "0xd21220a7": int(WETH, 16), "0xddca3f43": 500,
                     "0x313ce567": self.decimals if address == USDC else 18}[selector]
            return hash_value(value)
        if method == "eth_getLogs":
            first, last = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
            with self.lock:
                self.log_ranges.append((first, last))
            if self.fail_logs:
                raise RpcError("Simulated RPC failure")
            if last - first + 1 > self.range_limit:
                raise RpcError("Simulated provider block limit", range_limit=True)
            return [log for log in self.logs if first <= int(log["blockNumber"], 16) <= last]
        raise AssertionError("Unexpected RPC method: " + method)


class LiveCollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "live"

    def collect(self, rpc, **kwargs):
        return LiveCollector(rpc, self.root).collect(**kwargs)

    def test_recent_window_boundaries_exact_amounts_and_provenance(self):
        # 3-minute interval is [00:17:01, 00:20:01), so block 85 at 00:17:00 is excluded.
        rpc = FakeRpc([swap(85, 999999 * 10**6, tx=9), swap(90, 100000 * 10**6),
                       swap(90, -200000 * 10**6, index=1), swap(99, 1000 * 10**6, tx=2)])
        result = self.collect(rpc, lookback_minutes=3)
        self.assertEqual(result["metrics"]["usdc_volume"], "301000")
        self.assertEqual(result["metrics"]["swap_count"], 3)
        self.assertEqual(result["metrics"]["unique_swap_transactions"], 2)
        self.assertEqual(result["metrics"]["large_trade_count"], 2)
        self.assertEqual(result["largest_swaps"][0]["usdc"], "200000")
        self.assertEqual(result["source_refs"][0]["requested_blocks"], [86, 100])
        folder = next(self.root.iterdir())
        self.assertEqual(sha256(folder / "events.ndjson"), result["source_refs"][0]["sha256"])
        self.assertEqual(sha256(folder / "manifest.json"), result["source_refs"][1]["sha256"])
        self.assertTrue(result["collection_complete"])
        fact = result["facts"][0]
        report = {"answer_markdown": "Live result", "claims": [{"fact_id": fact["id"], "value": fact["value"],
                    "unit": fact["unit"], "evidence_ids": fact["evidence_ids"]}], "limitations": result["limitations"]}
        self.assertTrue(validate_report(report, [{"tool": "get_live_metrics", "result": result}])["passed"])

    def test_estimated_boundary_is_verified_for_irregular_block_times(self):
        rpc = FakeRpc()
        times = [rpc.base]
        for i in range(1, 101):
            times.append(times[-1] + (36 if i % 7 == 0 else 7 if i % 3 == 0 else 12))
        original = rpc.call
        def call(method, params):
            result = original(method, params)
            if method == "eth_getBlockByNumber":
                result["timestamp"] = hex(times[int(result["number"], 16)])
            return result
        rpc.call = call
        collector = LiveCollector(rpc, self.root)
        for target in [times[0] - 1, times[-1] + 1, *times, *[t + 1 for t in times]]:
            self.assertEqual(collector.lower_bound(target, 100), bisect.bisect_left(times, target))

    def test_current_day_is_partial_and_timezone_is_explicit(self):
        result = self.collect(FakeRpc(), day="2026-11-10", day_timezone="Asia/Shanghai")
        self.assertFalse(result["window_complete"])
        self.assertTrue(result["collection_complete"])
        self.assertEqual(result["period_utc"]["start_inclusive"], "2026-11-09T16:00:00+00:00")
        self.assertEqual(result["metrics"]["swap_count"], 0)
        self.assertIsNone(result["metrics"]["mean_swap_usdc"])
        self.assertIsNone(result["metrics"]["large_trade_frequency"])

    def test_completed_day_has_exclusive_end_and_correct_data_cutoff(self):
        rpc = FakeRpc(head=7300)
        result = self.collect(rpc, day="2026-11-10")
        self.assertTrue(result["window_complete"])
        self.assertEqual(result["source_refs"][0]["requested_blocks"], [0, 7199])
        self.assertEqual(result["as_of_block_utc"], "2026-11-10T23:59:48+00:00")

    def test_finalized_query_uses_older_head_and_does_not_claim_latest(self):
        result = self.collect(FakeRpc([swap(99, 1000)]), lookback_minutes=1, finality="finalized")
        self.assertEqual(result["anchor"]["number"], 90)
        self.assertEqual(result["metrics"]["swap_count"], 0)
        self.assertEqual(result["finality"], "finalized")

    def test_duplicate_logs_do_not_double_count_volume(self):
        log = swap(99, 100000 * 10**6)
        result = self.collect(FakeRpc([log, dict(log)]), lookback_minutes=1)
        self.assertEqual(result["metrics"]["swap_count"], 1)
        manifest = json.loads(next(self.root.glob("*/manifest.json")).read_text())
        self.assertEqual(manifest["duplicates_removed"], 1)

    def test_adaptive_range_split_preserves_all_events(self):
        result = self.collect(FakeRpc([swap(99, 100000 * 10**6)], range_limit=2), lookback_minutes=3)
        self.assertEqual(result["metrics"]["swap_count"], 1)

    def test_failed_collection_and_reorg_never_publish_metrics(self):
        for kwargs in ({"fail_logs": True}, {"reorg": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(RpcError):
                self.collect(FakeRpc([swap(99, 100000)], **kwargs), lookback_minutes=1)
        self.assertFalse(list(self.root.glob("*/metrics.json")))
        self.assertFalse(list(self.root.glob("*/events.ndjson")))
        for manifest in self.root.glob("*/manifest.json"):
            data = json.loads(manifest.read_text())
            self.assertEqual(data["status"], "failed")
            self.assertFalse(data["collection_complete"])

    def test_future_date_wrong_chain_and_wrong_decimals_are_rejected(self):
        for rpc, arguments in [(FakeRpc(), {"day": "2026-11-11"}), (FakeRpc(chain_id=10), {}),
                               (FakeRpc(decimals=18), {})]:
            with self.subTest(arguments=arguments), self.assertRaises(RpcError):
                self.collect(rpc, **arguments)
        self.assertFalse(self.root.exists())

    def test_removed_and_conflicting_logs_are_rejected(self):
        log = swap(99, 1000)
        for logs in ([{**log, "removed": True}], [log, {**log, "data": swap(99, 2000)["data"]}]):
            with self.subTest(logs=logs), self.assertRaises(RpcError):
                self.collect(FakeRpc(logs), lookback_minutes=1)

    def test_malformed_swap_cannot_be_counted(self):
        with self.assertRaises(RpcError):
            metrics_from_logs([{**swap(99, 1000), "data": "0x00"}])

    def test_live_incomplete_result_is_not_accepted_as_evidence(self):
        result = {"status": "ok", "data_mode": "on_demand_rpc", "collection_complete": False}
        report = {"answer_markdown": "Partial", "claims": [], "limitations": ["Partial collection"]}
        self.assertFalse(validate_report(report, [{"tool": "get_live_metrics", "result": result}])["passed"])


class RpcTransportTests(unittest.TestCase):
    def test_error_message_never_contains_secret_rpc_url(self):
        rpc = RpcClient(RpcSettings("https://eth-mainnet.g.alchemy.com/v2/secret-key"))
        opener = Mock()
        opener.open.side_effect = URLError(rpc.settings.url)
        with patch("onchain_agent.rpc.build_opener", return_value=opener), patch("onchain_agent.rpc.time.sleep"):
            with self.assertRaises(RpcError) as caught:
                rpc.call("eth_chainId", [])
        self.assertNotIn("secret-key", str(caught.exception))
        self.assertEqual(rpc.request_count, 3)

    def test_only_read_methods_and_request_budget_are_allowed(self):
        rpc = RpcClient(RpcSettings("https://eth-mainnet.g.alchemy.com/v2/test", max_requests=0))
        for method in ("eth_sendRawTransaction", "eth_chainId"):
            with self.subTest(method=method), self.assertRaises(RpcError):
                rpc.call(method, [])

    def test_json_rpc_range_error_can_trigger_splitting_without_exposing_body(self):
        rpc = RpcClient(RpcSettings("https://eth-mainnet.g.alchemy.com/v2/test"))
        response = io.BytesIO(json.dumps({"id": 1, "error": {"code": -32602,
                    "message": "block range exceeds limit; secret-key"}}).encode())
        opener = Mock()
        opener.open.return_value = response
        with patch("onchain_agent.rpc.build_opener", return_value=opener):
            with self.assertRaises(RpcError) as caught:
                rpc.call("eth_getLogs", [])
        self.assertTrue(caught.exception.range_limit)
        self.assertNotIn("secret-key", str(caught.exception))

    def test_http_400_free_tier_range_error_can_trigger_splitting(self):
        rpc = RpcClient(RpcSettings("https://eth-mainnet.g.alchemy.com/v2/test"))
        body = io.BytesIO(json.dumps({"error": {"code": -32600,
            "message": "Free tier block range is limited to 10 blocks: secret-key"}}).encode())
        error = HTTPError(rpc.settings.url, 400, "Bad request", {}, body)
        with patch("onchain_agent.rpc.build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(RpcError) as caught:
                rpc.call("eth_getLogs", [])
        self.assertTrue(caught.exception.range_limit)
        self.assertNotIn("secret-key", str(caught.exception))

    def test_rate_limit_is_distinguished_from_network_failure(self):
        rpc = RpcClient(RpcSettings("https://eth-mainnet.g.alchemy.com/v2/test"))
        with patch("onchain_agent.rpc.build_opener") as opener, patch("onchain_agent.rpc.time.sleep"):
            opener.return_value.open.side_effect = HTTPError(rpc.settings.url, 429, "secret-key", {}, None)
            with self.assertRaisesRegex(RpcError, "provider rate limit"):
                rpc.call("eth_chainId", [])
        self.assertEqual(rpc.request_count, 3)


if __name__ == "__main__":
    unittest.main()
