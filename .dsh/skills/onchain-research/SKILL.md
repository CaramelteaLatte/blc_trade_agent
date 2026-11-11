---
name: onchain-research
description: Research the project's Ethereum Uniswap pool using historical snapshots, fresh RPC data or the large-swap/next-hour-volatility pilot; produce reports with numerical claims linked to evidence. Use for measured activity and historical hypothesis tests, not unsupported market forecasts.
---

# On-chain research

Use the `mcp__onchain__` tools to answer questions about the configured pool. Read project definitions with `read_memory`; treat remembered research as unreviewed context and re-query numerical evidence for the current report.

## Available work

- `get_daily_metrics(day)`: one UTC day, YYYY-MM-DD.
- `compare_periods(a_start, a_end, b_start, b_end)`: two half-open UTC ranges. For October 4 use 2026-10-04 to 2026-10-05. Changes mean B minus A.
- `get_large_trades(day, limit)`: the largest 1–5 audited swaps, with log references.
- `read_memory(query, limit)`: fixed definitions and a bounded list of past reports.
- `get_live_metrics(lookback_minutes, finality)`: collect fresh RPC logs now, for the last 1–1440 minutes ending at the selected head; includes metrics and top 5 swaps. Default finality is `latest` (provisional); `finalized` is delayed.
- `get_live_day_metrics(day, timezone, finality)`: collect a calendar day via RPC, timezone `UTC` or `Asia/Shanghai`; an unfinished day is explicitly partial.
- `get_volatility_study()`: recompute the fixed historical research case on October 1–10 snapshots: calibrated large-swap signal, next-hour realized volatility, time-separated forecast benchmarks and exploratory bootstrap intervals. Writes reproducible study artifacts; does not call RPC.

## Historical research case

Use `get_volatility_study` when asked whether concentrated large swaps predict subsequent volatility, for the project's complete research case, or for its historical forecasting evaluation. The supplied snapshots cover ten days; do not call this a live prediction or describe it as production-ready. Read the returned train/test periods, exclusions, sample sizes, signal rule, baseline errors, confidence intervals and conclusions before interpreting the result. A group difference is not causation; improved error against one baseline does not imply beating every baseline. Report negative or inconclusive findings as returned. No profitability or directional price forecast has been tested.

The study's `facts` contain rounded-to-eight-decimal metric strings for reports; copy those exact values and their analysis/manifest evidence IDs into claims. The artifact manifest links raw snapshot hashes, code, plan and derived CSVs. Definitions, configured thresholds, date boundaries and method settings are metadata, not additional observed metrics. Explain that bps measure non-annualized realized volatility, not investment return. If asked for present/future forecasts, explain the historical scope and the need for longer, independently checked data; fresh one-hour metrics alone cannot train or validate a forecast.

## Choose the data source

For "now", "recent", "last hour", "today", "refresh" or explicit real-time requests, use a live tool. Last hour is 60 minutes and last 24 hours is 1440 minutes. "Today/yesterday" uses the request clock supplied by the host and defaults to Asia/Shanghai unless the user specifies a timezone. If no clock is supplied, ask for an explicit day or use a relative window; never invent today's date.

Local historical tools cover 2026-10-01 through 2026-10-10 UTC. Use them for those historical dates unless the user requests fresh collection or source verification. For other dates, use the live day tool. If the user explicitly restricts the answer to local snapshots, query the historical tool and report any unavailable date without switching sources.

Always state the returned period, actual cutoff, finality, and whether the requested day is complete. `collection_complete=true` means all selected blocks were fetched; it does not imply a full calendar day. Do not call finalized data instantaneous or latest blocks irreversible. Recent windows end at the selected chain head, not necessarily the wall-clock present.

If RPC is unconfigured, fails, or hits a budget, report the tool's unavailable reason and return no unsupported claims. Never silently replace requested fresh data with old snapshots or memory. Every report needs a current dataset tool observation. A tool result marked `unavailable` supplies no measured facts. Do not invent predictions.

## Interpretation

Swap events differ from distinct transactions and users. Volume counts only the USDC side. The large-trade threshold is at least 100,000 USDC. A frequency is a ratio; percentage = ratio × 100. Aggregate numerators and denominators before computing a multi-day ratio. Let the tools calculate comparisons rather than doing mental arithmetic.

Different trade sizes support statements about size distribution. They do not identify whales, institutions, bots or motives. A transaction reference supports that transaction; an aggregate requires all snapshot/manifest references and its definition. Local audit success does not establish independent mainnet authenticity or completeness; live collection checks metadata through one provider. Keep hypotheses separate from observations, and do not present untested investment rules as effective strategies.

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

For each measured number used in the answer, copy a supporting entry from the current tool results into `claims`. Keep exact numeric strings in claims; human-readable text may round them and must say when it does. Include the returned evidence IDs (historical `snapshot:`, fresh `live:`, or research `study:`) in the prose and transaction URLs when discussing swaps. Research analysis/manifest references are sufficient when those are the IDs returned by the study tool. Source IDs, block numbers, cutoff times and pool metadata are references, not extra measured metric claims. Copy every fact's evidence IDs, including live manifests.

If no tool can supply evidence, use an empty claims list and explain why the requested conclusion cannot be reached. Do not invent fact IDs. The application checks claim values, units and source links against this run's tool results and only stores reports that pass; prose interpretation still needs human review.
