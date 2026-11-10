import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from onchain_agent.data_tools import DataError, Dataset, summarize
from onchain_agent.evaluate import evaluate_tools
from onchain_agent.memory import Memory
from onchain_agent.validation import parse_report, validate_report


class DataToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = Dataset()

    def test_fixed_reference_cases(self):
        result = evaluate_tools()
        self.assertTrue(result["passed"], result)

    def test_period_frequency_uses_total_counts(self):
        result = self.dataset.period("2026-10-01", "2026-10-04")
        self.assertEqual(Decimal(result["metrics"]["large_trade_frequency"]), Decimal(109) / 14831)
        self.assertEqual(len(result["source_refs"]), 3)
        self.assertTrue(all(len(fact["evidence_ids"]) == 3 for fact in result["facts"]))

    def test_zero_swaps_have_no_undefined_numeric_values(self):
        metrics = summarize([])
        self.assertIsNone(metrics["mean_swap_usdc"])
        self.assertIsNone(metrics["large_trade_frequency"])
        self.assertEqual(metrics["usdc_volume"], "0")

    def test_request_validation(self):
        for day in ("../2026-10-01", "2026-10-32", "20261001"):
            with self.subTest(day=day), self.assertRaises(DataError):
                self.dataset.get_daily_metrics(day)
        with self.assertRaises(DataError):
            self.dataset.get_large_trades("2026-10-09", 6)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.fact = {"id": "2026-10-08/swap_count", "value": "8938", "unit": "events",
                     "evidence_ids": ["snapshot:2026-10-08"]}
        self.observations = [{"tool": "get_daily_metrics", "result": {"status": "ok", "facts": [self.fact],
                              "source_refs": [{"id": "snapshot:2026-10-08", "sha256": "example"}]}}]
        self.report = {"answer_markdown": "Swap 次数为 8938。", "claims": [
            {"fact_id": self.fact["id"], "value": "8938", "unit": "events", "evidence_ids": self.fact["evidence_ids"]}],
            "limitations": ["Only local historical snapshots."]}

    def test_matching_claim_is_accepted(self):
        self.assertTrue(validate_report(parse_report(json.dumps(self.report)), self.observations)["passed"])

    def test_wrong_values_units_and_missing_sources_are_rejected(self):
        for key, value in (("value", "9000"), ("unit", "users"), ("evidence_ids", []), ("fact_id", "invented")):
            report = json.loads(json.dumps(self.report))
            report["claims"][0][key] = value
            with self.subTest(key=key):
                self.assertFalse(validate_report(report, self.observations)["passed"])

    def test_prior_note_without_current_tool_evidence_is_rejected(self):
        self.assertFalse(validate_report(self.report, [{"tool": "read_memory", "result": {"status": "ok"}}])["passed"])

    def test_unavailable_data_can_have_no_claims(self):
        report = {"answer_markdown": "请求日期未覆盖。", "claims": [], "limitations": ["Missing date."]}
        observations = [{"tool": "get_daily_metrics", "result": {"status": "unavailable", "facts": []}}]
        self.assertTrue(validate_report(report, observations)["passed"])

    def test_memory_survives_new_instance_and_keeps_review_status(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "memory.sqlite3"
            self.assertEqual(Memory(path).read()["notes"], [])
            Memory(path).save("test-run", "Swap 次数是多少？", self.report, self.observations[0]["result"]["source_refs"])
            notes = Memory(path).read("Swap")["notes"]
            self.assertEqual(len(notes), 1)
            self.assertIn("interpretation_unreviewed", notes[0]["status"])
            self.assertEqual(Memory(path).read(limit=0)["notes"], [])


if __name__ == "__main__":
    unittest.main()
