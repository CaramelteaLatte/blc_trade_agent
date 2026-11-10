---
name: onchain-research
description: Analyze the project's historical Uniswap pool data, compare UTC periods, inspect large swaps, and produce research reports with numerical claims linked to tool evidence. Use for questions about this dataset, not general market forecasts.
---

# On-chain research

Use the `mcp__onchain__` tools to answer research questions about the local dataset. Read project definitions with `read_memory`; treat remembered research as unreviewed context and re-query numerical evidence for the current report.

## Available work

- `get_daily_metrics(day)`: one UTC day, YYYY-MM-DD.
- `compare_periods(a_start, a_end, b_start, b_end)`: two half-open UTC ranges. For October 4 use 2026-10-04 to 2026-10-05. Changes mean B minus A.
- `get_large_trades(day, limit)`: the largest 1–5 audited swaps, with log references.
- `read_memory(query, limit)`: fixed definitions and a bounded list of past reports.

The data covers 2026-10-01 through 2026-10-10. For other dates or live-market questions, explain the missing data. Do not substitute a remembered price or forecast. A tool result marked `unavailable` supplies no measured facts.

Every report needs a current dataset tool observation. For an explicitly requested date outside coverage, call `get_daily_metrics` for that date once to record the unavailable result, then explain the limitation. Do not fetch unrelated dates just to fill the report.

## Interpretation

Swap events differ from distinct transactions and users. Volume counts only the USDC side. The large-trade threshold is at least 100,000 USDC. A frequency is a ratio; percentage = ratio × 100. Aggregate numerators and denominators before computing a multi-day ratio. Let the tools calculate comparisons rather than doing mental arithmetic.

Different trade sizes support statements about size distribution. They do not identify whales, institutions, bots or motives. A transaction reference supports that transaction; an aggregate requires all snapshot references and its definition. Local audit success does not establish independent mainnet authenticity or completeness. Keep hypotheses separate from observations, and do not present untested investment rules as effective strategies.

## Report format

Return one JSON object without Markdown fences:

```json
{
  "answer_markdown": "中文研究回答，包含结论、关键指标、证据、可检验的解释和局限。",
  "claims": [
    {"fact_id": "copy an exact facts[].id", "value": "copy the exact facts[].value", "unit": "copy facts[].unit", "evidence_ids": ["copy evidence IDs"]}
  ],
  "limitations": ["具体的数据或推断限制"]
}
```

For each measured number used in the answer, copy a supporting entry from the current tool results into `claims`. Keep exact numeric strings in claims; human-readable text may round them and must say when it does. Include source IDs such as `snapshot:2026-10-04` in the prose and transaction URLs when discussing individual swaps. Source IDs and block numbers are references, not extra measured metric claims.

If no tool can supply evidence, use an empty claims list and explain why the requested conclusion cannot be reached. Do not invent fact IDs. The application checks claim values, units and source links against this run's tool results and only stores reports that pass; prose interpretation still needs human review.
