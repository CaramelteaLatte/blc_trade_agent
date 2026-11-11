import copy
import importlib.util
import json
import math
import tempfile
import unittest
from pathlib import Path

from onchain_agent.data_tools import POOL, ROOT, DataError, sha256
from onchain_agent.research import (PLAN, align_samples, block_bootstrap, decode_swap, epoch,
                                    fit_and_predict, fit_ols, hourly_features, load_plan,
                                    quantile, run_study)
from onchain_agent.validation import validate_report
from scripts.audit_data import SWAP


def abi_log(sqrt=2**96, tick=0, a0=10**6, a1=-10**18):
    words = [a0.to_bytes(32, "big", signed=True), a1.to_bytes(32, "big", signed=True),
             sqrt.to_bytes(32, "big"), (10**12).to_bytes(32, "big"), tick.to_bytes(32, "big", signed=True)]
    return {"address": POOL, "removed": False, "topics": [SWAP, "0x" + "0"*64, "0x" + "0"*64],
            "data": "0x" + b"".join(words).hex(), "blockTimestamp": "0x100", "blockNumber": "0x2",
            "transactionIndex": "0x0", "logIndex": "0x1", "transactionHash": "0x" + "1"*64}


class ResearchMathTests(unittest.TestCase):
    def test_price_orientation_decimals_and_signed_pool_amounts(self):
        swap = decode_swap(abi_log())
        self.assertEqual(swap["price"], 10**12)
        self.assertEqual(swap["amount0_raw"], 10**6)
        self.assertEqual(swap["amount1_raw"], -10**18)
        tick = math.floor(math.log(4) / math.log1p(0.0001))
        self.assertEqual(decode_swap(abi_log(sqrt=2**97, tick=tick))["price"], 10**12 / 4)
        self.assertEqual(decode_swap(abi_log(a0=0, a1=100))["amount0_raw"], 0)

    def test_corrupt_tick_price_or_amount_signs_are_rejected(self):
        for log in (abi_log(sqrt=0), abi_log(tick=100), abi_log(a0=1, a1=1)):
            with self.subTest(log=log["data"]), self.assertRaises(DataError):
                decode_swap(log)

    def test_grid_excludes_boundary_events_and_never_backfills_from_future(self):
        plan = load_plan()
        start = epoch(plan["start_utc"])
        plan["end_utc_exclusive"] = "2026-10-01T02:00:00+00:00"
        swaps = [{"timestamp": start + offset, "price": price, "tx_hash": str(offset), "log_index": 0,
                  "block": offset, "amount0_raw": 10**6, "amount1_raw": -1}
                 for offset, price in ((299, 100.0), (300, 110.0))]
        hours, grid = hourly_features(swaps, plan)
        self.assertIsNone(grid[0]["price"])
        self.assertEqual(grid[1]["price"], 100.0)
        self.assertEqual(grid[2]["price"], 110.0)
        self.assertIsNone(grid[5]["price"])
        self.assertIsNone(hours[0]["current_rv_bps"])

    def test_realized_volatility_uses_twelve_known_returns_without_annualization(self):
        plan = load_plan()
        start = epoch(plan["start_utc"])
        plan["end_utc_exclusive"] = "2026-10-01T03:00:00+00:00"
        swaps = [{"timestamp": start + i*300 - 1, "price": 100*math.exp(i*0.001),
                  "tx_hash": str(i), "log_index": 0, "block": i,
                  "amount0_raw": 10**6, "amount1_raw": -1} for i in range(1, 37)]
        hours, _ = hourly_features(swaps, plan)
        self.assertIsNone(hours[0]["current_rv_bps"])
        self.assertAlmostEqual(hours[1]["current_rv_bps"], math.sqrt(12)*10, places=9)
        self.assertEqual(hours[1]["usdc_volume"], "12")
        self.assertEqual(hours[1]["buy_weth_swaps"], 12)

    def test_future_labels_and_train_test_boundary_are_separate(self):
        plan = load_plan()
        start = epoch(plan["start_utc"])
        hours = [{"start": start + i*3600, "end": start + (i+1)*3600, "end_utc": str(i),
                  "swap_count": 20, "current_rv_bps": i + 1} for i in range(240)]
        rows, excluded = align_samples(hours, plan)
        train, test = [r for r in rows if r["phase"] == "train"], [r for r in rows if r["phase"] == "test"]
        self.assertEqual((len(train), len(test)), (143, 96))
        self.assertEqual(rows[0]["next_rv_bps"], 2)
        self.assertEqual(max(r["label_end"] for r in train), epoch(plan["split_utc"]))
        self.assertEqual(min(r["decision_time"] for r in test), epoch(plan["split_utc"]))
        self.assertEqual(excluded, {"no_future_hour": 1})

    def test_holdout_values_cannot_change_fitted_threshold_or_parameters(self):
        plan = load_plan()
        rows = [{"phase": "train" if i < 40 else "test", "large_share": (i % 9) / 100,
                 "large_swap_count": i % 5, "current_rv_bps": float(i + 1),
                 "next_rv_bps": float(2*i + 3)} for i in range(60)]
        other = copy.deepcopy(rows)
        for row in other[40:]:
            row.update(next_rv_bps=10**8, current_rv_bps=999, large_share=1, large_swap_count=999)
        self.assertEqual(fit_and_predict(rows, plan), fit_and_predict(other, plan))

    def test_ols_recovers_known_coefficients_and_handles_singular_signal(self):
        rows = [{"current_rv_bps": i, "signal": bool(i % 2), "next_rv_bps": 4 + 3*i + 2*(i % 2)} for i in range(1, 40)]
        fit = fit_ols(rows, True)
        for key, value in (("intercept", 4), ("current_rv_coefficient", 3), ("signal_coefficient", 2)):
            self.assertAlmostEqual(fit[key], value)
        for row in rows:
            row["signal"] = False
        self.assertIsNotNone(fit_ols(rows, True)["fallback"])
        self.assertEqual(fit_ols(rows, True)["signal_coefficient"], 0)

    def test_quantile_interpolation_and_empty_group_bootstrap(self):
        self.assertEqual(quantile([0, 10], 0.9), 9)
        plan = load_plan()
        plan["bootstrap_repetitions"] = 100
        split = epoch(plan["split_utc"])
        rows = [{"decision_time": split+i*3600, "signal": False, "next_rv_bps": 2,
                 "prediction_current_volatility_ols": 2, "prediction_current_volatility_plus_signal_ols": 3}
                for i in range(96) if i != 8]
        result = block_bootstrap(rows, plan)
        self.assertEqual(result, block_bootstrap(rows, plan))
        self.assertEqual(result["effect_ci95_bps"], [None, None])
        self.assertEqual(result["paired_mae_change_ci95_bps"], [1, 1])


class ResearchArtifactTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("numpy"), "Install requirements-research.txt for independent NumPy verification")
    def test_independent_numpy_sampling_volatility_and_regression(self):
        import numpy as np
        from onchain_agent.research import load_swaps
        plan = load_plan()
        # Independent vectorized implementation directly from raw bytes.
        times, prices = [], []
        for path in sorted((ROOT / "data/raw").glob("events_*.ndjson")):
            for line in path.read_bytes().splitlines():
                log = json.loads(line)
                if log["topics"][0] == SWAP:
                    sqrt = int(log["data"][130:194], 16)
                    times.append(int(log["blockTimestamp"], 16))
                    prices.append(1 / ((sqrt / 2**96)**2 * 10**-12))
        stamps, spot = np.asarray(times), np.asarray(prices)
        boundaries = np.arange(epoch(plan["start_utc"]), epoch(plan["end_utc_exclusive"]) + 1, 300)
        positions = np.searchsorted(stamps, boundaries, side="left") - 1
        selected = spot[np.maximum(positions, 0)].copy()
        selected[(positions < 0) | (boundaries - stamps[np.maximum(positions, 0)] > 900)] = np.nan
        reference_rv = np.sqrt(np.sum(np.diff(np.log(selected)).reshape(-1, 12)**2, axis=1)) * 10000
        hours, _ = hourly_features(load_swaps(plan)[0], plan)
        actual = np.asarray([float("nan") if h["current_rv_bps"] is None else h["current_rv_bps"] for h in hours])
        np.testing.assert_allclose(actual, reference_rv, rtol=1e-9, atol=1e-8, equal_nan=True)
        rows, _ = align_samples(hours, plan)
        calibration = [h for h in hours if h["end"] + 3600 <= epoch(plan["split_utc"]) and h["swap_count"] >= plan["minimum_hour_swaps"]]
        fit = fit_and_predict(rows, plan, calibration)
        self.assertAlmostEqual(fit["signal_threshold"], np.quantile([h["large_share"] for h in calibration], 0.9))
        train = [r for r in rows if r["phase"] == "train"]
        x = np.array([[1, r["current_rv_bps"], float(r["signal"])] for r in train])
        y = np.array([r["next_rv_bps"] for r in train])
        reference = np.linalg.lstsq(x, y, rcond=None)[0]
        actual_fit = fit["current_volatility_plus_signal_ols"]
        np.testing.assert_allclose([actual_fit[k] for k in ("intercept", "current_rv_coefficient", "signal_coefficient")], reference, rtol=1e-10, atol=1e-10)

    def test_real_dataset_study_has_traceable_artifacts_and_valid_claims(self):
        with tempfile.TemporaryDirectory() as directory:
            result, folder = run_study(output_root=directory)
            self.assertEqual(result["input_swap_count"], 73017)
            self.assertEqual(result["train_hours"], 142)
            self.assertEqual(result["test_hours"], 96)
            manifest = json.loads((folder / "manifest.json").read_text())
            self.assertEqual(len(manifest["data_sources"]), 10)
            for name, digest in manifest["output_sha256"].items():
                self.assertEqual(sha256(folder / name), digest)
            for source in result["source_refs"]:
                self.assertEqual(sha256(Path(source["file"])), source["sha256"])
            claims = [{"fact_id": f["id"], "value": f["value"], "unit": f["unit"], "evidence_ids": f["evidence_ids"]}
                      for f in result["facts"]]
            report = {"answer_markdown": "Research", "claims": claims, "limitations": result["limitations"]}
            observation = {"tool": "get_volatility_study", "result": result}
            self.assertTrue(validate_report(report, [observation])["passed"])
            result["study_complete"] = False
            self.assertFalse(validate_report(report, [observation])["passed"])


if __name__ == "__main__":
    unittest.main()
