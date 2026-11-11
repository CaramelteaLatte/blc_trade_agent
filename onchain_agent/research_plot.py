"""Standalone figures for the research report; optional matplotlib dependency."""

from datetime import datetime


def draw_study(folder, hours, samples, analysis, plan):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as dates
    except ImportError:
        raise ValueError("Plotting needs: .venv/bin/python -m pip install -r requirements-research.txt") from None
    plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False})
    fig, axes = plt.subplots(3, 1, figsize=(11, 10), constrained_layout=True)
    split = datetime.fromisoformat(plan["split_utc"])
    x = [datetime.fromisoformat(h["end_utc"]) for h in hours]
    axes[0].plot(x, [h["close_price"] for h in hours], color="#21618c", lw=1.5)
    axes[0].set(ylabel="USDC / WETH", title="One-pool pilot: large swaps and next-hour volatility")
    sx = [datetime.fromisoformat(r["decision_utc"]) for r in samples]
    axes[1].plot(sx, [r["next_rv_bps"] for r in samples], color="#287d8e", lw=1, label="Next-hour RV (observed later)")
    alerts = [r for r in samples if r["signal"]]
    axes[1].scatter([datetime.fromisoformat(r["decision_utc"]) for r in alerts],
                    [r["next_rv_bps"] for r in alerts], s=25, color="#c44e52", label="Large-swap signal", zorder=3)
    axes[1].set(ylabel="Hourly realized volatility (bps)", xlabel="Decision time (UTC)")
    axes[1].legend(loc="upper left")
    for ax in axes[:2]:
        ax.axvline(split, ls="--", color="#666666", lw=1)
        ax.axvspan(split, datetime.fromisoformat(plan["end_utc_exclusive"]), color="#e9b44c", alpha=0.10)
        ax.xaxis.set_major_formatter(dates.DateFormatter("%m-%d", tz=split.tzinfo))
        ax.grid(alpha=0.15)
    scores = analysis["prediction_scores"]
    labels = ["Train mean", "Persistence", "Signal means", "Current RV OLS", "Current RV + signal OLS"]
    bars = axes[2].barh(labels, [v["mae_bps"] for v in scores.values()], color=["#aab7b8"] * 4 + ["#287d8e"])
    axes[2].bar_label(bars, fmt="%.2f", padding=5)
    axes[2].set(xlabel="Held-out MAE (bps; lower is better)", title=f"Same {analysis['test_hours']} test hours for all methods")
    axes[2].invert_yaxis()
    axes[2].set_xlim(0, max(v["mae_bps"] for v in scores.values()) * 1.18)
    fig.savefig(folder / "study.png", dpi=180)
    fig.savefig(folder / "study.svg")
    plt.close(fig)
