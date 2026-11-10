"""Fixed deterministic reference checks; language-level scoring is a separate task."""

import json

from .data_tools import DataError, Dataset, ROOT


def lookup(value, path):
    for key in path.split("."):
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def evaluate_tools() -> dict:
    cases = json.loads((ROOT / "evaluation/cases.json").read_text(encoding="utf-8"))
    dataset = Dataset()
    results = []
    for case in cases:
        failures = []
        try:
            response = getattr(dataset, case["tool"])(**case["arguments"])
            if "expected_error" in case:
                failures.append("Expected a rejected request.")
            for path, expected in case.get("expected", {}).items():
                if lookup(response, path) != expected:
                    failures.append(f"{path}: expected {expected!r}, got {lookup(response, path)!r}")
        except DataError as exc:
            if case.get("expected_error", "\0") not in str(exc):
                failures.append(str(exc))
        results.append({"id": case["id"], "passed": not failures, "failures": failures})
    return {"passed": all(case["passed"] for case in results), "cases": results,
            "scope": "Deterministic tool outputs only. This is not a model accuracy benchmark; use the question rubrics to review live reports."}
