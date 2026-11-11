"""Reproducible, time-separated pilot study; no model API or RPC is needed."""

import csv
import json
import math
import random
import statistics
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from pathlib import Path

from scripts.audit_data import SWAP
from .data_tools import ROOT, POOL, Dataset, DataError, sha256

PLAN = ROOT / "research/study_plan.json"
MODEL_NAMES = ("training_mean", "persistence", "signal_conditional_mean",
               "current_volatility_ols", "current_volatility_plus_signal_ols")


def epoch(value):
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise DataError("Study dates must have an explicit timezone.")
    return int(parsed.timestamp())


def utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def load_plan(path=PLAN):
    plan = json.loads(Path(path).read_text())
    start, split, end = [epoch(plan[k]) for k in ("start_utc", "split_utc", "end_utc_exclusive")]
    if not start < split < end or any(t % 86400 for t in (start, split, end)):
        raise DataError("Study boundaries must be ordered UTC midnights.")
    if plan["pool"] != POOL or plan["price_grid_seconds"] != 300 or plan["feature_seconds"] != 3600 or plan["target_seconds"] != 3600:
        raise DataError("This study supports the configured pool, five-minute prices and one-hour features/labels.")
    if (not 0 < plan["signal_quantile"] < 1 or plan["large_swap_usdc"] <= 0
            or plan["minimum_hour_swaps"] < 1 or plan["minimum_large_swaps"] < 1
            or plan["maximum_price_age_seconds"] < 0
            or not 100 <= plan["bootstrap_repetitions"] <= 10000
            or not 1 <= plan["bootstrap_block_hours"] <= 24):
        raise DataError("Invalid study thresholds or bootstrap budget.")
    return plan


def decode_swap(log):
    """Decode the pool's signed deltas and post-swap USDC-per-WETH spot price."""
    if log.get("address", "").lower() != POOL or log.get("removed") is not False:
        raise DataError("Research input contains an unexpected pool or removed log.")
    try:
        payload = bytes.fromhex(log["data"].removeprefix("0x"))
        if len(payload) != 160 or len(log["topics"]) != 3 or log["topics"][0] != SWAP:
            raise ValueError()
        a0 = int.from_bytes(payload[:32], "big", signed=True)
        a1 = int.from_bytes(payload[32:64], "big", signed=True)
        sqrt = int.from_bytes(payload[64:96], "big")
        liquidity = int.from_bytes(payload[96:128], "big")
        tick = int.from_bytes(payload[128:], "big", signed=True)
        if not 0 < sqrt < 2**160 or liquidity >= 2**128 or not -887272 <= tick <= 887272:
            raise ValueError()
        implied_tick = 2 * math.log(sqrt / 2**96) / math.log1p(0.0001)
        if not tick - 1e-6 <= implied_tick <= tick + 1 + 1e-6 or a0 * a1 > 0:
            raise ValueError()
        with localcontext() as context:
            context.prec = 50
            price = Decimal(2**192) * 10**12 / Decimal(sqrt * sqrt)
        return {"timestamp": int(log["blockTimestamp"], 16),
                "block": int(log["blockNumber"], 16), "tx_index": int(log["transactionIndex"], 16),
                "log_index": int(log["logIndex"], 16), "tx_hash": log["transactionHash"],
                "amount0_raw": a0, "amount1_raw": a1, "sqrt_price_x96": sqrt,
                "price": float(price), "price_exact": format(price, "f")}
    except (KeyError, ValueError, TypeError, OverflowError):
        raise DataError("Invalid Swap ABI, timestamp, signed amounts or tick/price relationship.") from None


def load_swaps(plan, data_dir=None):
    dataset = Dataset(data_dir)
    dates = dataset._dates(plan["start_utc"][:10], plan["end_utc_exclusive"][:10])
    refs = dataset._evidence(dates)
    if data_dir is not None:
        for ref in refs:
            ref["file"] = str(dataset.root / "raw" / Path(ref["file"]).name)
    swaps, source_files = [], []
    for day in dates:
        raw = dataset.root / "raw" / f"events_{day}.ndjson"
        source_files.append((raw, dataset.days[day]["snapshot_sha256"]))
        for line in raw.read_bytes().splitlines():
            if not line.strip():
                continue
            log = json.loads(line)
            if log["topics"][0] == SWAP:
                swaps.append(decode_swap(log))
    swaps.sort(key=lambda s: (s["block"], s["tx_index"], s["log_index"]))
    if any(b["timestamp"] < a["timestamp"] for a, b in zip(swaps, swaps[1:])):
        raise DataError("Block timestamps are not monotone in the supplied logs.")
    # Protect the audit/decode interval, including concurrent local edits.
    if any(sha256(path) != digest for path, digest in source_files):
        raise DataError("Raw snapshots changed while building the research dataset.")
    return swaps, refs


def hourly_features(swaps, plan):
    start, end = epoch(plan["start_utc"]), epoch(plan["end_utc_exclusive"])
    interval, max_age = plan["price_grid_seconds"], plan["maximum_price_age_seconds"]
    if any(not start <= s["timestamp"] < end for s in swaps):
        raise DataError("Swap timestamp lies outside the study interval.")
    # A grid close at t uses strictly earlier logs; observations at t belong
    # to the following interval. Missing/stale starts are never backfilled.
    grid, cursor, last = [], 0, None
    for boundary in range(start, end + 1, interval):
        while cursor < len(swaps) and swaps[cursor]["timestamp"] < boundary:
            last = swaps[cursor]
            cursor += 1
        age = boundary - last["timestamp"] if last else None
        usable = last is not None and age <= max_age
        grid.append({"timestamp": boundary, "time_utc": utc(boundary),
                     "price": last["price"] if usable else None, "age_seconds": age,
                     "source_transaction": last["tx_hash"] if usable else None,
                     "source_log_index": last["log_index"] if usable else None})
    grouped = [[] for _ in range((end - start) // 3600)]
    for swap in swaps:
        grouped[(swap["timestamp"] - start) // 3600].append(swap)
    hours = []
    for index, group in enumerate(grouped):
        points = grid[index * 12:index * 12 + 13]
        prices = [point["price"] for point in points]
        valid = all(p is not None for p in prices)
        rv = math.sqrt(sum(math.log(b / a)**2 for a, b in zip(prices, prices[1:]))) * 10000 if valid else None
        count = len(group)
        large = sum(abs(s["amount0_raw"]) >= plan["large_swap_usdc"] * 10**6 for s in group)
        with localcontext() as context:
            context.prec = 50
            volume = format(Decimal(sum(abs(s["amount0_raw"]) for s in group)) / 10**6, "f")
            net = format(Decimal(sum(s["amount0_raw"] for s in group)) / 10**6, "f")
        hours.append({"start": start + index * 3600, "end": start + (index + 1) * 3600,
                      "start_utc": utc(start + index * 3600), "end_utc": utc(start + (index + 1) * 3600),
                      "swap_count": count, "large_swap_count": large,
                      "large_share": large / count if count else None, "usdc_volume": volume,
                      "swap_net_usdc_to_pool": net,
                      "buy_weth_swaps": sum(s["amount0_raw"] > 0 and s["amount1_raw"] < 0 for s in group),
                      "sell_weth_swaps": sum(s["amount0_raw"] < 0 and s["amount1_raw"] > 0 for s in group),
                      "zero_leg_swaps": sum(s["amount0_raw"] == 0 or s["amount1_raw"] == 0 for s in group),
                      "open_price": prices[0], "close_price": prices[-1], "current_rv_bps": rv,
                      "valid_grid_points": sum(p is not None for p in prices),
                      "first_block": group[0]["block"] if group else None,
                      "last_block": group[-1]["block"] if group else None})
    return hours, grid


def align_samples(hours, plan):
    split = epoch(plan["split_utc"])
    samples, excluded = [], Counter()
    for index, hour in enumerate(hours):
        if index + 1 == len(hours):
            excluded["no_future_hour"] += 1
            continue
        future = hours[index + 1]
        if hour["end"] != future["start"]:
            raise DataError("Missing hour in the research timeline.")
        if hour["swap_count"] < plan["minimum_hour_swaps"]:
            excluded["too_few_swaps"] += 1
            continue
        if hour["current_rv_bps"] is None or future["current_rv_bps"] is None:
            excluded["missing_or_stale_price_grid"] += 1
            continue
        phase = "train" if future["end"] <= split else "test" if hour["end"] >= split else "purged"
        if phase == "purged":
            excluded["label_crosses_split"] += 1
            continue
        samples.append({**hour, "decision_time": hour["end"], "decision_utc": hour["end_utc"],
                        "label_end": future["end"], "label_end_utc": future["end_utc"],
                        "next_rv_bps": future["current_rv_bps"], "phase": phase})
    return samples, dict(excluded)


def quantile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def fit_ols(rows, with_signal=False):
    """Centered one/two-predictor OLS; singular signal falls back to current RV."""
    xs, ys = [r["current_rv_bps"] for r in rows], [r["next_rv_bps"] for r in rows]
    zs = [float(r["signal"]) for r in rows]
    mx, my, mz = statistics.mean(xs), statistics.mean(ys), statistics.mean(zs)
    xx = sum((x - mx)**2 for x in xs)
    xy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    bx, bz, fallback = xy / xx if xx > 1e-12 else 0.0, 0.0, None
    if with_signal:
        zz = sum((z - mz)**2 for z in zs)
        xz = sum((x - mx) * (z - mz) for x, z in zip(xs, zs))
        zy = sum((z - mz) * (y - my) for z, y in zip(zs, ys))
        determinant = xx * zz - xz * xz
        if determinant > 1e-12 * max(1.0, xx * zz):
            bx, bz = (xy * zz - zy * xz) / determinant, (zy * xx - xy * xz) / determinant
        else:
            fallback = "singular_signal_design; use current-volatility-only model"
    return {"intercept": my - bx * mx - bz * mz, "current_rv_coefficient": bx,
            "signal_coefficient": bz, "fallback": fallback, "clip_predictions_at_zero": True}


def fit_and_predict(samples, plan, threshold_rows=None):
    train = [r for r in samples if r["phase"] == "train"]
    if len(train) < 24:
        raise DataError("At least 24 eligible training hours are needed for this pilot.")
    # Threshold calibration does not depend on future-price availability.
    calibration = threshold_rows if threshold_rows is not None else train
    threshold = quantile([r["large_share"] for r in calibration], plan["signal_quantile"])
    if threshold is None:
        raise DataError("No eligible training hours for signal calibration.")
    for row in samples:
        row["signal"] = row["large_swap_count"] >= plan["minimum_large_swaps"] and row["large_share"] >= threshold
    overall = statistics.mean(r["next_rv_bps"] for r in train)
    means = {signal: statistics.mean(group) if (group := [r["next_rv_bps"] for r in train if r["signal"] == signal]) else overall
             for signal in (False, True)}
    base, augmented = fit_ols(train), fit_ols(train, True)
    for row in samples:
        row["prediction_training_mean"] = overall
        row["prediction_persistence"] = row["current_rv_bps"]
        row["prediction_signal_conditional_mean"] = means[row["signal"]]
        for name, fit in (("current_volatility_ols", base), ("current_volatility_plus_signal_ols", augmented)):
            row["prediction_" + name] = max(0.0, fit["intercept"] + fit["current_rv_coefficient"] * row["current_rv_bps"]
                                           + fit["signal_coefficient"] * row["signal"])
    return {"signal_threshold": threshold, "calibration_hours": len(calibration), "training_mean_bps": overall,
            "training_signal_mean_bps": means[True], "training_non_signal_mean_bps": means[False],
            "current_volatility_ols": base, "current_volatility_plus_signal_ols": augmented}


def group_stats(rows):
    on = [r["next_rv_bps"] for r in rows if r["signal"]]
    off = [r["next_rv_bps"] for r in rows if not r["signal"]]
    a, b = statistics.mean(on) if on else None, statistics.mean(off) if off else None
    return {"signal_hours": len(on), "non_signal_hours": len(off), "signal_mean_rv_bps": a,
            "non_signal_mean_rv_bps": b, "difference_bps": a - b if a is not None and b is not None else None,
            "ratio": a / b if a is not None and b else None}


def evaluate_predictions(rows):
    result = {}
    for name in MODEL_NAMES:
        errors = [r["prediction_" + name] - r["next_rv_bps"] for r in rows]
        result[name] = {"mae_bps": statistics.mean(abs(e) for e in errors),
                        "rmse_bps": math.sqrt(statistics.mean(e * e for e in errors)), "hours": len(errors)}
    return result


def block_bootstrap(rows, plan):
    """Resample time slots, not filtered rows, so exclusions do not close gaps."""
    by_time = {r["decision_time"]: r for r in rows}
    timeline = [by_time.get(t) for t in range(epoch(plan["split_utc"]), epoch(plan["end_utc_exclusive"]), 3600)]
    rng, differences, paired_errors = random.Random(plan["random_seed"]), [], []
    n, length = len(timeline), plan["bootstrap_block_hours"]
    for _ in range(plan["bootstrap_repetitions"]):
        draw = []
        while len(draw) < n:
            begin = rng.randrange(n)
            draw.extend(timeline[(begin + j) % n] for j in range(length))
        draw = [r for r in draw[:n] if r is not None]
        if not draw:
            continue
        delta = group_stats(draw)["difference_bps"]
        if delta is not None:
            differences.append(delta)
        paired_errors.append(statistics.mean(
            abs(r["prediction_current_volatility_plus_signal_ols"] - r["next_rv_bps"])
            - abs(r["prediction_current_volatility_ols"] - r["next_rv_bps"]) for r in draw))
    return {"method": "circular moving-block percentile bootstrap on hourly test timeline",
            "block_hours": length, "repetitions": plan["bootstrap_repetitions"],
            "valid_effect_repetitions": len(differences),
            "effect_ci95_bps": [quantile(differences, 0.025), quantile(differences, 0.975)],
            "paired_mae_change_ci95_bps": [quantile(paired_errors, 0.025), quantile(paired_errors, 0.975)]}


def analyze(hours, grid, samples, excluded, plan):
    split = epoch(plan["split_utc"])
    calibration = [r for r in hours if r["end"] + 3600 <= split and r["swap_count"] >= plan["minimum_hour_swaps"]]
    fit = fit_and_predict(samples, plan, calibration)
    train, test = [r for r in samples if r["phase"] == "train"], [r for r in samples if r["phase"] == "test"]
    if len(test) < 12:
        raise DataError("At least 12 eligible held-out hours are needed for this pilot.")
    groups, scores, uncertainty = group_stats(test), evaluate_predictions(test), block_bootstrap(test, plan)
    enough = groups["signal_hours"] >= plan["minimum_test_signal_hours"] and groups["non_signal_hours"] >= plan["minimum_test_signal_hours"]
    low, _ = uncertainty["effect_ci95_bps"]
    _, high_mae = uncertainty["paired_mae_change_ci95_bps"]
    effect_supported = enough and low is not None and low > 0
    incremental_supported = enough and high_mae is not None and high_mae < 0
    conclusion = ("探索性测试段出现正向关联，但仍需更长、独立样本复核。" if effect_supported
                  else "当前测试段不足以支持‘大额 Swap 集中后下一小时波动更高’这一假设。")
    predictive = ("加入大额信号改善了当前波动率基线的测试误差；仍属于探索性结果。" if incremental_supported
                  else "未得到大额信号在当前波动率基线之外提供稳定增量预测能力的证据。")
    return {"study_id": plan["study_id"], "question": plan["question"], "scope": plan["scope"], "pool": plan["pool"],
            "method": {"price_unit": "USDC/WETH", "volatility_unit": "bps (not annualized)",
                       "price_formula": "2^192 * 10^12 / sqrtPriceX96^2",
                       "rv_formula": "10000 * sqrt(sum of twelve five-minute log-return squares)",
                       "signal_rules": {key: plan[key] for key in ("large_swap_usdc", "signal_quantile", "minimum_large_swaps", "minimum_hour_swaps")},
                       "time_policy": plan["time_policy"], "history_policy": plan["history_policy"]},
            "period_utc": {"start_inclusive": plan["start_utc"], "end_exclusive": plan["end_utc_exclusive"]},
            "split_utc": plan["split_utc"], "hour_buckets": len(hours), "train_hours": len(train), "test_hours": len(test),
            "input_swap_count": sum(r["swap_count"] for r in hours), "excluded_hours": excluded,
            "unavailable_price_grid_points": sum(r["price"] is None for r in grid),
            "zero_leg_swaps": sum(r["zero_leg_swaps"] for r in hours),
            "fit": fit, "train_groups": group_stats(train), "test_groups": groups,
            "prediction_scores": scores, "uncertainty": uncertainty,
            "sample_size_gate_passed": enough, "positive_association_supported": effect_supported,
            "incremental_predictive_value_supported": incremental_supported,
            "conclusions": [conclusion, predictive],
            "research_recommendation": "将大额集中信号保留为研究监测标记；当前版本不据此自动下单或宣称盈利。先扩展历史和独立样本，再检验预先固定的风险规则。",
            "limitations": [
                "Only ten historical days in the default plan; small and potentially regime-dependent samples. This is an exploratory temporal holdout, not a prospective experiment.",
                "Historical snapshots pass internal consistency checks, but historical on-chain authenticity and completeness have not been independently verified.",
                "Price is this pool's post-swap spot price in USDC per WETH, not a market-wide price, executable quote or USD peg guarantee.",
                "Realized volatility uses five-minute previous-tick prices, no future backfill, a fifteen-minute staleness cap, and no annualization; missing points exclude the hour.",
                "Large-swap concentration can coincide with volatility or other conditions; group differences do not establish causation or investor identity.",
                "Bootstrap intervals depend on the chosen six-hour blocks and approximate temporal stability; they do not include full model-selection or parameter uncertainty.",
                "Forecast scores measure next-hour volatility, not directional returns. No trade execution, fees/slippage, PnL, Sharpe or drawdown backtest is implemented.",
                "Signed Swap amount0 measures only USDC transfers due to swaps; it excludes liquidity and fee transfers and is not overall capital inflow.",
            ]}


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def study_facts(analysis, prefix, evidence):
    values = {"input_swap_count": (analysis["input_swap_count"], "events"),
              "train_hours": (analysis["train_hours"], "hours"), "test_hours": (analysis["test_hours"], "hours"),
              "signal_threshold": (analysis["fit"]["signal_threshold"], "ratio")}
    for key, value in analysis["test_groups"].items():
        values["test_" + key] = (value, "hours" if key.endswith("hours") else "ratio" if key == "ratio" else "bps")
    for name, score in analysis["prediction_scores"].items():
        for metric in ("mae_bps", "rmse_bps"):
            values[name + "/" + metric] = (score[metric], "bps")
    for key in ("effect_ci95_bps", "paired_mae_change_ci95_bps"):
        for endpoint, value in zip(("low", "high"), analysis["uncertainty"][key]):
            values[key + "/" + endpoint] = (value, "bps")
    return [{"id": prefix + "/" + key, "metric": key,
             "value": None if value is None else str(value) if isinstance(value, int) else format(value, ".8f"),
             "unit": unit, "evidence_ids": evidence} for key, (value, unit) in values.items()]


def render_report(a, plan, include_plot=False):
    g, u, fit = a["test_groups"], a["uncertainty"], a["fit"]
    def number(value):
        return "不可估计" if value is None else f"{value:.4f}"
    lines = ["# 大额 Swap 与下一小时价格波动：探索性研究", "", *a["conclusions"], "",
             f"数据窗口：{plan['start_utc']} 至 {plan['end_utc_exclusive']}（右端不含，UTC）。",
             f"总计 {a['input_swap_count']:,} 条 Swap、{a['hour_buckets']} 个小时；有效训练 {a['train_hours']} 小时，测试 {a['test_hours']} 小时。",
             f"时间切分：{plan['split_utc']}。训练标签必须在切分前结束；测试决策时刻从切分时刻开始。", "",
             "## 定义与数据处理", "",
             "从 Swap ABI 解码 amount0、amount1、sqrtPriceX96、liquidity、tick，并检查价格与 tick 一致性。",
             "池中 token0 为 USDC（6 位）、token1 为 WETH（18 位）；USDC/WETH 价格 = 2^192 × 10^12 / sqrtPriceX96²。",
             "每五分钟取严格早于采样边界的最后一笔池价格；超过十五分钟未更新的价格记为缺失，不用未来价格补齐。",
             "每小时波动率 RV = sqrt(sum(12 个五分钟 log-return²)) × 10000，单位 bps，不年化。整小时需要全部价格点有效。",
             "信号在特征小时结束时产生，目标为紧随其后的完整一小时 RV；只在观测到目标小时结束后进行历史评分。",
             f"大额阈值固定为 {plan['large_swap_usdc']:,} USDC；训练期大额次数占比的第{plan['signal_quantile']*100:g}百分位 = {number(fit['signal_threshold'] * 100)}%。",
             f"大额占比达到阈值，且当小时至少 {plan['minimum_large_swaps']} 笔大额 Swap、{plan['minimum_hour_swaps']} 笔 Swap，才触发信号。",
             "阈值只从训练期特征确定；预测参数只使用训练期的未来标签拟合，未按测试表现反复挑选阈值。",
             f"排除统计：`{json.dumps(a['excluded_hours'], ensure_ascii=False)}`；缺失/过期价格采样点 {a['unavailable_price_grid_points']} 个。",
             f"有 {a['zero_leg_swaps']} 条 Swap 的一侧金额为零，保留其价格与事件记录，不强行标记买卖方向。", "",
             "## 测试段关联", "",
             "| 指标 | 结果 |", "|---|---:|",
             f"| 信号小时 / 非信号小时 | {g['signal_hours']} / {g['non_signal_hours']} |",
             f"| 信号后平均 RV | {number(g['signal_mean_rv_bps'])} bps |",
             f"| 非信号后平均 RV | {number(g['non_signal_mean_rv_bps'])} bps |",
             f"| 平均差值（信号减非信号） | {number(g['difference_bps'])} bps |",
             f"| 差值的探索性95%区间 | [{number(u['effect_ci95_bps'][0])}, {number(u['effect_ci95_bps'][1])}] bps |", "",
             f"区间来自沿测试小时轴重采样的循环移动区块 bootstrap，块长{plan['bootstrap_block_hours']}小时、{plan['bootstrap_repetitions']}次、固定随机种子；保留缺失小时的位置。",
             "这不是因果检验。组内样本少、序列相关、行情阶段变化或区间跨零时，不能声称假设成立。", "",
             "## 测试段预测比较", "", "所有方法在同一组测试小时评分；参数冻结在训练段，越小越好。", "",
             "| 方法 | MAE（bps） | RMSE（bps） |", "|---|---:|---:|"]
    for name, score in a["prediction_scores"].items():
        lines.append(f"| {name} | {number(score['mae_bps'])} | {number(score['rmse_bps'])} |")
    lines += ["", "training_mean：训练标签均值；persistence：下一小时波动等于当前小时；signal_conditional_mean：训练期按信号分组的标签均值。",
              "两个 OLS 模型分别用当前小时波动率，以及当前波动率加大额信号作为特征；负预测截为零，奇异设计回退到仅当前波动率模型。",
              f"增加信号后的配对 MAE 变化95%区间：[{number(u['paired_mae_change_ci95_bps'][0])}, {number(u['paired_mae_change_ci95_bps'][1])}] bps；负值表示改善。", "",
              "## 研究建议与边界", "", a["research_recommendation"], "",
              "这是波动率预测的历史检验，不是价格涨跌预测或可交易收益回测。本版不输出盈利、胜率、Sharpe 或最大回撤。",
              "完整数据见 hourly_features.csv、prices_5m.csv、predictions.csv；数值见 analysis.json，参数见 study_plan.json，输入与输出哈希见 manifest.json。",
              *(["研究图：", "", "![Study results](study.png)", ""] if include_plot else []),
              "## 局限", "", *["- " + item for item in a["limitations"]], "", "## 方法来源", "",
              "- [Uniswap Swap ABI](https://raw.githubusercontent.com/Uniswap/v3-core/main/contracts/interfaces/pool/IUniswapV3PoolEvents.sol)",
              "- [Uniswap 价格与代币精度](https://blog.uniswap.org/uniswap-v3-math-primer)",
              "- [时间序列预测评估与基线](https://otexts.com/fpp3/toolbox.html)", ""]
    return "\n".join(lines)


def run_study(plan_path=PLAN, *, data_dir=None, output_root=None, plot=False, progress=None):
    emit = progress or (lambda message: None)
    plan_path = Path(plan_path)
    plan_digest = sha256(plan_path)
    plan = load_plan(plan_path)
    emit("正在审计历史快照并解码 Swap 价格与方向…")
    swaps, sources = load_swaps(plan, data_dir)
    hours, grid = hourly_features(swaps, plan)
    samples, excluded = align_samples(hours, plan)
    emit("正在使用训练段确定信号与模型，检验后续小时并估计不确定性…")
    analysis = analyze(hours, grid, samples, excluded, plan)
    if sha256(plan_path) != plan_digest:
        raise DataError("Study plan changed during the calculation.")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    folder = (Path(output_root) if output_root else ROOT / "reports/studies") / run_id
    folder.mkdir(parents=True, exist_ok=False)
    write_json(folder / "study_plan.json", plan)
    write_json(folder / "analysis.json", analysis)
    write_csv(folder / "prices_5m.csv", grid)
    write_csv(folder / "hourly_features.csv", hours)
    write_csv(folder / "predictions.csv", samples)
    (folder / "report.md").write_text(render_report(analysis, plan, plot), encoding="utf-8")
    if plot:
        from .research_plot import draw_study
        draw_study(folder, hours, samples, analysis, plan)
    source_paths = [Path(__file__), ROOT / "onchain_agent/research_plot.py", ROOT / "onchain_agent/data_tools.py",
                    ROOT / "scripts/audit_data.py", ROOT / "requirements.txt", ROOT / "requirements-research.txt"]
    manifest = {"status": "complete", "study_id": plan["study_id"], "run_id": run_id,
                "generated_at_utc": datetime.now(timezone.utc).isoformat(), "python": sys.version,
                "data_sources": sources, "plan_sha256": plan_digest,
                "source_sha256": {str(p.relative_to(ROOT)): sha256(p) for p in source_paths},
                "output_sha256": {p.name: sha256(p) for p in sorted(folder.iterdir()) if p.is_file()},
                "scope": "Local file integrity and reproducible calculations; not independent chain verification or on-chain attestation."}
    if plot:
        from importlib.metadata import version
        manifest["plot_versions"] = {name: version(name) for name in ("matplotlib", "numpy")}
    write_json(folder / "manifest.json", manifest)
    prefix = "study:" + run_id
    refs = [{"id": prefix + ":" + name, "file": str((folder / name).resolve().relative_to(ROOT)) if folder.resolve().is_relative_to(ROOT) else str((folder / name).resolve()),
             "sha256": sha256(folder / name)} for name in ("analysis.json", "manifest.json")]
    result = {"status": "ok", "data_mode": "historical_research", "study_complete": True,
              **analysis, "facts": study_facts(analysis, prefix, [r["id"] for r in refs]),
              "source_refs": refs, "artifact_directory": str(folder.resolve())}
    emit(f"研究完成：训练 {analysis['train_hours']} 小时、测试 {analysis['test_hours']} 小时；{folder}")
    return result, folder


def get_volatility_study():
    """Recompute the fixed historical pilot with current inputs and evidence."""
    from .progress import record_progress
    return run_study(progress=record_progress)[0]
