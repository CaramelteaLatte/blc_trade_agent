"""Check structured report claims against the current run's tool observations."""

import json


def parse_report(text: str) -> dict:
    clean = text.strip()
    if clean.startswith("```json\n") and clean.endswith("```"):
        clean = clean[8:-3].strip()
    report = json.loads(clean)
    if not isinstance(report, dict):
        raise ValueError("Expected a JSON report object.")
    if not isinstance(report.get("answer_markdown"), str) or not report["answer_markdown"].strip():
        raise ValueError("Report has no readable answer.")
    if not isinstance(report.get("claims"), list):
        raise ValueError("Report claims must be a list.")
    limitations = report.get("limitations")
    if not isinstance(limitations, list) or not limitations or not all(isinstance(x, str) and x.strip() for x in limitations):
        raise ValueError("Report must state concrete limitations.")
    return report


def validate_report(report: dict, observations: list[dict]) -> dict:
    facts, refs, errors = {}, {}, []
    dataset_tools = {"get_daily_metrics", "compare_periods", "get_large_trades"}
    if not any(item["tool"] in dataset_tools for item in observations):
        errors.append("No dataset tool was called in this run.")
    for item in observations:
        result = item["result"]
        if result.get("status") != "ok":
            continue
        for fact in result.get("facts", []):
            if fact["id"] in facts and fact != facts[fact["id"]]:
                errors.append(f"Conflicting tool fact: {fact['id']}")
            facts[fact["id"]] = fact
        for ref in result.get("source_refs", []):
            if ref["id"] in refs and refs[ref["id"]] != ref:
                errors.append(f"Conflicting evidence: {ref['id']}")
            refs[ref["id"]] = ref
    if facts and not report["claims"]:
        errors.append("Data was retrieved, but the report has no structured numerical claims.")
    used = set()
    for claim in report["claims"]:
        if not isinstance(claim, dict) or not isinstance(claim.get("fact_id"), str):
            errors.append("Malformed claim.")
            continue
        key = claim["fact_id"]
        if key in used:
            errors.append(f"Duplicate claim: {key}")
        used.add(key)
        fact = facts.get(key)
        if fact is None:
            errors.append(f"Unknown fact: {key}")
            continue
        if claim.get("value") != fact["value"] or claim.get("unit") != fact["unit"]:
            errors.append(f"Value or unit does not match: {key}")
        ids = claim.get("evidence_ids")
        if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
            errors.append(f"Invalid evidence list: {key}")
        elif set(ids) != set(fact["evidence_ids"]) or any(x not in refs for x in ids):
            errors.append(f"Evidence does not fully support fact: {key}")
    return {"passed": not errors, "checked_claim_count": len(report["claims"]), "errors": errors,
            "scope": "Structured values, units and evidence only. Prose coverage, interpretations and causal claims require human review.",
            "source_refs": list(refs.values())}
