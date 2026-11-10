"""
Aggregates all drift-dashboard data sources and renders a single
self-contained HTML report (Plotly, client-side charts) to
portfolio_live/drift_dashboard/report/index.html.

Run this, then use serve.py to view it at http://localhost:<port>.
serve.py rebuilds this report on every page load -- no manual re-run needed
to see new trades, just refresh the browser tab.

v1 scope (built 2026-09-24, see docs/dashboards/LIVE_VS_BACKTEST_DRIFT_DASHBOARD_PLAN.md):
- Executive overview: current buffer-above-floor vs. portfolio Monte Carlo /
  historical-bootstrap reference distribution.
- Per-edge drift table: realized_r and hold_hours KS-test verdicts,
  drift-delta, sample sizes -- the actual decision-making core.
- Per-edge hold-time distribution: live vs. backtest histogram overlay.

v2 scope (built 2026-09-24):
- Execution-quality / cost-drag drill-down (execution_quality.py): converts
  real slippage/swap/commission/fee from the closed-trade ledger into R
  units per edge, and for any edge flagged as drifting on realized_r,
  estimates how much of that drift is explained by measured execution cost
  vs. unexplained (genuine behavioral/statistical) drift. Directly answers
  "we accounted for cost/spread at gate time, why would live differ?" --
  Gate A/B/C's cost_bps is a single per-entry spread assumption; it has no
  model for slippage-vs-plan, overnight swap on longer-than-expected holds,
  or commission, all of which are real and visible per-trade in live data.
- Redesigned UI (modern dark dashboard, sticky nav, glass cards).

Still not built (v3 follow-ups): model-quality calibration-curve panel
(live_calibrated_probability vs Step04 OOF), capital-velocity time-series
panel.

DYNAMIC SCOPE: every edge/asset shown is derived from
live_manifest.json.selected_edges at run time -- see data_sources.py.
Rendered output shows ONLY the current live-deployed edges (per user
instruction 2026-09-24) -- no stale/excluded-trades section.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go

_THIS_FILE = Path(__file__).resolve()
_V2_ROOT = _THIS_FILE.parents[2]
if str(_V2_ROOT) not in sys.path:
    sys.path.insert(0, str(_V2_ROOT))

from portfolio_live.drift_dashboard.data_sources import (  # noqa: E402
    load_current_live_edge_ids,
    load_gate_c_reference,
    load_live_signal_audit,
    scope_closed_trades,
)
from portfolio_live.drift_dashboard.deep_dive import build_edge_deep_dive  # noqa: E402
from portfolio_live.drift_dashboard.drift_stats import build_edge_drift_report  # noqa: E402
from portfolio_live.drift_dashboard.edge_reference import (  # noqa: E402
    read_current_edge_geometry,
    resolve_backtest_trade_sample,
)
from portfolio_live.drift_dashboard.execution_quality import (  # noqa: E402
    build_edge_execution_quality,
    explain_drift_vs_cost,
)
from portfolio_live.drift_dashboard.model_quality import build_edge_model_quality  # noqa: E402
from portfolio_live.drift_dashboard.portfolio_reference import (  # noqa: E402
    PortfolioReferenceError,
    build_portfolio_drawdown_reference,
)

REPORT_DIR = _THIS_FILE.parent / "report"
REPORT_PATH = REPORT_DIR / "index.html"

PLOTLY_TEMPLATE = "plotly_dark"
PLOT_FONT = dict(family="Inter, -apple-system, Segoe UI, sans-serif", color="#c9cedb")
PLOT_PAPER_BG = "rgba(0,0,0,0)"
PLOT_GRID = "#232838"

VERDICT_COLOR = {
    "on_distribution": "#34d399",
    "low_power_on_distribution": "#7fd9b9",
    "drift_flagged": "#f87171",
    "low_power_drift_flagged": "#f5a3a3",
    "insufficient_sample": "#5b6272",
}
VERDICT_LABEL = {
    "on_distribution": "On distribution",
    "low_power_on_distribution": "On distribution (low power)",
    "drift_flagged": "DRIFT FLAGGED",
    "low_power_drift_flagged": "Drift flagged (low power)",
    "insufficient_sample": "Insufficient sample",
}


def _style_fig(fig: go.Figure) -> go.Figure:
    fig.update_layout(
        template=PLOTLY_TEMPLATE,
        font=PLOT_FONT,
        paper_bgcolor=PLOT_PAPER_BG,
        plot_bgcolor=PLOT_PAPER_BG,
        margin=dict(t=48, b=40, l=56, r=24),
    )
    fig.update_xaxes(gridcolor=PLOT_GRID, zerolinecolor=PLOT_GRID)
    fig.update_yaxes(gridcolor=PLOT_GRID, zerolinecolor=PLOT_GRID)
    return fig


def _executive_section(portfolio_ref) -> str:
    d = portfolio_ref.to_dict()
    bands = {row["scenario_name"]: row for row in d["bootstrap_reference_rows"]}
    cost00 = bands.get("historical_cost00_cluster_bootstrap")

    fig = go.Figure()
    if cost00:
        fig.add_trace(
            go.Box(
                q1=[cost00["worst_buffer_above_floor_median"]],
                median=[cost00["worst_buffer_above_floor_median"]],
                q3=[cost00["worst_buffer_above_floor_p975"]],
                lowerfence=[cost00["worst_buffer_above_floor_p025"]],
                upperfence=[cost00["worst_buffer_above_floor_p975"]],
                name="Backtest: worst buffer ever reached (historical bootstrap)",
                marker_color="#60a5fa",
            )
        )
    fig.add_trace(
        go.Scatter(
            x=["Backtest: worst buffer ever reached (historical bootstrap)"],
            y=[d["current_buffer_above_floor"]],
            mode="markers+text",
            marker=dict(size=20, color="#fb923c", symbol="diamond", line=dict(width=2, color="#fff7ed")),
            text=["CURRENT LIVE BUFFER"],
            textposition="top center",
            name="Current live buffer-above-floor",
        )
    )
    fig.update_layout(
        title="Current drawdown buffer vs. backtest's own worst-case simulated paths",
        yaxis_title="Buffer above drawdown floor ($)",
        height=380,
        showlegend=True,
        legend=dict(orientation="h", y=-0.15),
    )
    _style_fig(fig)

    warning_html = ""
    if d.get("account_warning"):
        warning_html = (
            f'<div class="alert-banner">⚠ {d["account_warning"]}'
            f' (as of {d.get("account_as_of") or "unknown"})</div>'
        )

    buffer_ratio = None
    if cost00 and cost00.get("worst_buffer_above_floor_median"):
        try:
            buffer_ratio = d["current_buffer_above_floor"] / abs(cost00["worst_buffer_above_floor_median"])
        except (TypeError, ZeroDivisionError):
            buffer_ratio = None

    return f"""
    <section id="overview">
      <div class="section-head"><h2>Executive Overview</h2><span class="section-sub">Live account state vs. backtest's own simulated drawdown distribution</span></div>
      {warning_html}
      <div class="stat-grid">
        <div class="stat-card"><span class="stat-label">Current balance</span><span class="stat-value">${d['current_balance']:,.2f}</span></div>
        <div class="stat-card"><span class="stat-label">Current equity</span><span class="stat-value">${d['current_equity']:,.2f}</span></div>
        <div class="stat-card"><span class="stat-label">High watermark</span><span class="stat-value">${d['high_watermark']:,.2f}</span></div>
        <div class="stat-card"><span class="stat-label">Drawdown floor</span><span class="stat-value">${d['current_floor']:,.2f}</span></div>
        <div class="stat-card accent"><span class="stat-label">Buffer above floor</span><span class="stat-value">${d['current_buffer_above_floor']:,.2f}</span></div>
        <div class="stat-card"><span class="stat-label">Data source</span><span class="stat-value small">{d['account_source']}</span></div>
      </div>
      <div class="chart-card">{fig.to_html(full_html=False, include_plotlyjs=False)}</div>
      <p class="footnote">Backtest reference: start-month cluster-bootstrap resampling of real historical
      return streams (1500 samples), portfolio_allocator Step 11. "Worst buffer ever reached" is the
      minimum buffer-above-floor observed at ANY point along each simulated path.
      {"Your current buffer is running about " + f"{buffer_ratio:.1f}x" + " the median worst-case simulated dip." if buffer_ratio else ""}</p>
      <p class="footnote">Portfolio-level backtest drawdown (R units, Step 07c backtest_statistics_report.csv):
      max_drawdown_r={d['backtest_max_drawdown_r']:.2f}, p95_drawdown_r={d['backtest_p95_drawdown_r']:.2f},
      max_time_under_water={d['backtest_max_time_under_water_years']*365.25:.0f} days,
      p95_time_under_water={d['backtest_p95_time_under_water_years']*365.25:.0f} days.</p>
    </section>
    """


def _drift_table_row(report, exec_quality, deep_dive) -> str:
    if report.gap:
        return f"""
        <tr>
          <td class="edge-cell">{report.edge_id}</td>
          <td colspan="6" class="gap-cell">No backtest reference available ({report.gap})</td>
        </tr>
        """
    cells = []
    for metric_name in ("realized_r", "hold_hours"):
        m = report.metrics.get(metric_name)
        if m is None:
            cells.append("<td colspan='3' class='gap-cell'>n/a</td>")
            continue
        color = VERDICT_COLOR.get(m.verdict, "#5b6272")
        label = VERDICT_LABEL.get(m.verdict, m.verdict)
        p_str = f"{m.p_value:.4f}" if m.p_value is not None else "—"
        delta_str = f"{m.to_dict()['drift_delta']:+.3f}" if m.to_dict()["drift_delta"] is not None else "—"
        cells.append(
            f"<td>n={m.n_live}</td>"
            f"<td class='mono'>Δ {delta_str}</td>"
            f"<td><span class='badge' style='--badge-color:{color};'>{label}</span><span class='p-val'>p={p_str}</span></td>"
        )

    root_cause_rows = []
    rr_metric = report.metrics.get("realized_r")
    if rr_metric is not None and rr_metric.verdict in ("drift_flagged", "low_power_drift_flagged"):
        if exec_quality:
            delta = rr_metric.to_dict()["drift_delta"]
            explanation = explain_drift_vs_cost(delta, exec_quality.mean_total_cost_drag_r)
            if explanation:
                root_cause_rows.append(f"<tr class='root-cause-row'><td></td><td colspan='6' class='root-cause'>🔎 Cost drag: {explanation}</td></tr>")
        if deep_dive and deep_dive.verdict_note:
            root_cause_rows.append(f"<tr class='root-cause-row'><td></td><td colspan='6' class='root-cause'>📊 Streak/noise check: {deep_dive.verdict_note}</td></tr>")

    return f"<tr><td class='edge-cell'>{report.edge_id}</td>{''.join(cells)}</tr>{''.join(root_cause_rows)}"


def _drift_section(edge_ids: list[str], scoped, exec_quality_by_edge: dict, deep_dive_by_edge: dict) -> str:
    rows_html = []
    for eid in sorted(edge_ids):
        live_rows = scoped.in_scope[scoped.in_scope["edge_id"] == eid]
        bt = resolve_backtest_trade_sample(eid)
        report = build_edge_drift_report(eid, live_rows, bt.get("trades"), bt.get("gap"))
        rows_html.append(_drift_table_row(report, exec_quality_by_edge.get(eid), deep_dive_by_edge.get(eid)))

    return f"""
    <section id="drift">
      <div class="section-head"><h2>Per-Edge Drift Engine</h2><span class="section-sub">Two-sample KS test, live realized outcomes vs. each edge's own current-geometry backtest sample</span></div>
      <p class="footnote">Same test family as Gate D's Mann-Whitney U. "Insufficient sample" (n&lt;10 live
      closed trades) is not a pass -- it means there isn't enough data yet. "Low power" (n&lt;30) verdicts
      are directionally informative but should be treated cautiously. Rows flagged for drift on realized_r
      get two automatic root-cause estimates below them: cost drag (Execution Quality panel) and a
      streak/small-sample-noise check (Deep-Dive panel below).</p>
      <div class="table-wrap">
        <table class="drift-table">
          <thead>
            <tr>
              <th>Edge</th>
              <th colspan="3">Realized R</th>
              <th colspan="3">Hold time (hours)</th>
            </tr>
          </thead>
          <tbody>
            {''.join(rows_html)}
          </tbody>
        </table>
      </div>
    </section>
    """


def _deep_dive_section(edge_ids: list[str], scoped, deep_dive_by_edge: dict) -> str:
    cards = []
    for eid in sorted(edge_ids):
        dd = deep_dive_by_edge.get(eid)
        if dd is None or dd.n_live < 2:
            continue
        d = dd.to_dict()
        band_html = ""
        if d["bootstrap_band_lo"] is not None:
            pct = d["bootstrap_percentile"]
            extreme = pct is not None and (pct < 5 or pct > 95)
            pct_color = "#f87171" if extreme else "#34d399"
            band_html = f"""
            <div class="dd-row">
              <span class="dd-label">Small-sample noise band (backtest, 95%)</span>
              <span class="mono">[{d['bootstrap_band_lo']:+.3f}R, {d['bootstrap_band_hi']:+.3f}R]</span>
              <span class="dd-label">Live mean lands at</span>
              <span class="mono" style="color:{pct_color};font-weight:700;">{pct:.0f}th percentile</span>
            </div>
            """
        streak_html = ""
        if d["backtest_max_loss_streak"] is not None:
            streak_html = f"""
            <div class="dd-row">
              <span class="dd-label">Current live losing streak</span>
              <span class="mono">{d['live_current_streak']} trades</span>
              <span class="dd-label">This edge's worst-ever backtest streak</span>
              <span class="mono">{d['backtest_max_loss_streak']} trades</span>
            </div>
            """
        resolve_html = ""
        if d["est_trades_to_resolve"] is not None:
            resolve_html = f"<p class='footnote' style='margin-top:10px;'>⏳ Est. ~{d['est_trades_to_resolve']} total live trades needed to confirm this either way, if the current effect size holds (rough power estimate).</p>"

        cards.append(f"""
        <div class="dd-card">
          <h3>{eid}</h3>
          <div class="dd-row"><span class="dd-label">Live trades so far</span><span class="mono">{d['n_live']}</span>
          <span class="dd-label">Live mean R</span><span class="mono">{d['live_mean_r']:+.4f}</span></div>
          {streak_html}
          {band_html}
          <p class="dd-note">{d['verdict_note'] or 'Not enough data yet for a streak/noise read.'}</p>
          {resolve_html}
        </div>
        """)

    if not cards:
        cards.append("<p class='gap-cell'>No edges with enough live trades yet for a streak/noise drill-down.</p>")

    return f"""
    <section id="deepdive">
      <div class="section-head"><h2>Deep-Dive: Streak & Small-Sample Noise</h2><span class="section-sub">Is this a normal losing streak this edge's own backtest already knows about, or something else?</span></div>
      <p class="footnote">Two checks per edge, both using ONLY that edge's own real backtest sample: (1) compare
      the live book's current consecutive-loss streak against the worst streak that geometry ever produced in
      its full backtest history; (2) resample 3,000 random windows of the same size as the
      live sample from the backtest, to see how often a window this small produces a mean this extreme just from
      noise. Shown for every edge with 2+ live trades, not only flagged ones, so you can watch the picture
      sharpen as more trades fire.</p>
      <div class="dd-grid">
        {''.join(cards)}
      </div>
    </section>
    """


def _model_quality_section(edge_ids: list[str], scoped) -> str:
    try:
        audit = load_live_signal_audit()
    except Exception:
        audit = None

    rows_html = []
    for eid in sorted(edge_ids):
        live_rows = scoped.in_scope[scoped.in_scope["edge_id"] == eid]
        if live_rows.empty:
            continue
        mq = build_edge_model_quality(eid, live_rows, audit)
        if mq.gap:
            rows_html.append(f"<tr><td class='edge-cell'>{eid}</td><td colspan='4' class='gap-cell'>{mq.gap}</td></tr>")
            continue
        ref_auc_str = f"{mq.reference_auc:.4f}" if mq.reference_auc is not None else "—"
        live_auc_str = f"{mq.live_auc:.4f}" if mq.live_auc is not None else "n/a"
        rows_html.append(
            f"""<tr>
              <td class="edge-cell">{eid}</td>
              <td class="mono">{ref_auc_str} (n={mq.reference_n})</td>
              <td class="mono">{live_auc_str}</td>
              <td>{mq.live_n_priced}/{mq.live_n_total_closed} matched</td>
              <td class="note-cell">{mq.note}</td>
            </tr>"""
        )

    if not rows_html:
        rows_html.append("<tr><td colspan='5' class='gap-cell'>No in-scope live trades yet.</td></tr>")

    return f"""
    <section id="modelquality">
      <div class="section-head"><h2>Model Quality — Calibration & AUC</h2><span class="section-sub">Live meta-label discrimination vs. each edge's Step04 OOF reference (same metric family as Gate D)</span></div>
      <p class="footnote">Reference AUC is recomputed here (Mann-Whitney U, same as Gate D) directly from each
      edge's Step04 calibrated_oof_predictions.csv on raw_probability vs. quote_executable_meta_label. Live AUC
      needs each closed trade's signal_id to still be present in live_signal_audit.csv (a rotating log, so older
      trades often drop out — reported honestly as a match rate, never backfilled) and needs both wins and losses
      in the matched sample. Outcome proxy for live trades: realized_r &gt; 0.</p>
      <div class="table-wrap">
        <table class="drift-table">
          <thead><tr><th>Edge</th><th>Reference AUC</th><th>Live AUC</th><th>Signal match</th><th>Note</th></tr></thead>
          <tbody>{''.join(rows_html)}</tbody>
        </table>
      </div>
    </section>
    """


def _capital_velocity_section(edge_ids: list[str], scoped) -> str:
    figs_html = []
    for eid in sorted(edge_ids):
        live_rows = scoped.in_scope[scoped.in_scope["edge_id"] == eid].copy()
        if len(live_rows) < 3 or "hold_hours" not in live_rows.columns:
            continue
        live_rows = live_rows.sort_values("exit_time")
        live_rows["occupied_days"] = live_rows["hold_hours"] / 24.0
        live_rows["cum_r"] = pd.to_numeric(live_rows["realized_r"], errors="coerce").cumsum()
        live_rows["cum_days"] = live_rows["occupied_days"].cumsum()
        live_rows["cum_velocity"] = live_rows["cum_r"] / live_rows["cum_days"].replace(0, np.nan)
        if live_rows["cum_velocity"].dropna().empty:
            continue

        gate_c = load_gate_c_reference(eid)
        geo = read_current_edge_geometry(eid)
        ref_velocity = None
        if gate_c and gate_c.get("rows"):
            matched = [r for r in gate_c["rows"] if geo and r.get("label") == geo.config_label]
            chosen = (matched or gate_c["rows"])[0]
            ref_velocity = chosen.get("capital_time_velocity_R_per_occupied_day")

        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=list(range(1, len(live_rows) + 1)), y=live_rows["cum_velocity"],
            mode="lines+markers", name="Live cumulative R/occupied-day", line=dict(color="#fb923c", width=2),
        ))
        if ref_velocity is not None and not pd.isna(ref_velocity):
            fig.add_hline(y=float(ref_velocity), line_dash="dash", line_color="#60a5fa",
                           annotation_text=f"Gate C reference: {float(ref_velocity):.3f}", annotation_position="top left")
        fig.update_layout(title=f"{eid}", xaxis_title="Live trade #", yaxis_title="Cumulative R / occupied day", height=300, showlegend=False)
        _style_fig(fig)
        figs_html.append(f"<div class='chart-half'>{fig.to_html(full_html=False, include_plotlyjs=False)}</div>")

    if not figs_html:
        figs_html.append("<p class='gap-cell'>No edges with 3+ live trades yet for a capital-velocity read.</p>")

    return f"""
    <section id="velocity">
      <div class="section-head"><h2>Capital Velocity Over Time</h2><span class="section-sub">Live cumulative realized R per occupied day vs. Gate C's reference velocity for this exact geometry</span></div>
      <p class="footnote">Directly tracks the V3 objective: high capital turnover, not just positive expectancy.
      The dashed reference line is capital_time_velocity_R_per_occupied_day from this edge's own Gate C
      execution-velocity summary at its current live geometry.</p>
      <div class="chart-grid">
        {''.join(figs_html)}
      </div>
    </section>
    """


def _execution_quality_section(edge_ids: list[str], scoped) -> tuple[str, dict]:
    rows_html = []
    exec_quality_by_edge = {}
    for eid in sorted(edge_ids):
        live_rows = scoped.in_scope[scoped.in_scope["edge_id"] == eid]
        if live_rows.empty:
            continue
        eq = build_edge_execution_quality(eid, live_rows)
        exec_quality_by_edge[eid] = eq
        d = eq.to_dict()
        drag_color = "#f87171" if d["mean_total_cost_drag_r"] < -0.02 else ("#34d399" if d["mean_total_cost_drag_r"] > 0.005 else "#c9cedb")
        rows_html.append(
            f"""<tr>
              <td class="edge-cell">{eid}</td>
              <td>{d['n_priced']}</td>
              <td>{d['pct_with_slippage']:.0f}%</td>
              <td class="mono">{d['mean_swap_drag_r']:+.4f}</td>
              <td class="mono">{d['mean_commission_drag_r']:+.4f}</td>
              <td class="mono" style="color:{drag_color};font-weight:700;">{d['mean_total_cost_drag_r']:+.4f}</td>
              <td>{d['assumed_cost_bps']:.2f} bps</td>
              <td class="note-cell">{d['note']}</td>
            </tr>"""
        )

    if not rows_html:
        rows_html.append("<tr><td colspan='8' class='gap-cell'>No in-scope live trades yet.</td></tr>")

    section_html = f"""
    <section id="execution">
      <div class="section-head"><h2>Execution Quality — Cost Drag Drill-Down</h2><span class="section-sub">Where real dollars are lost between the Gate A/B/C cost_bps assumption and the live fill</span></div>
      <p class="footnote">Gate A/B/C's cost_bps is a single spread assumption applied once at entry. It has
      no model for (a) real slippage between planned and actual fill price, (b) overnight swap/financing on
      trades held longer than typical, (c) commission. All three are real, per-trade dollars in the closed-trade
      ledger, converted here into R units (÷ planned_risk_usd) so they're directly comparable to the drift
      deltas above. This is expected friction, not necessarily edge decay -- the root-cause line under each
      flagged edge above estimates how much of its drift this friction can and can't explain.</p>
      <div class="table-wrap">
        <table class="drift-table">
          <thead>
            <tr>
              <th>Edge</th><th>N priced</th><th>% w/ slippage</th><th>Swap drag (R)</th>
              <th>Commission drag (R)</th><th>Total cost drag (R)</th><th>Assumed cost</th><th>Note</th>
            </tr>
          </thead>
          <tbody>{''.join(rows_html)}</tbody>
        </table>
      </div>
    </section>
    """
    return section_html, exec_quality_by_edge


def _hold_time_section(edge_ids: list[str], scoped) -> str:
    figs_html = []
    for eid in sorted(edge_ids):
        live_rows = scoped.in_scope[scoped.in_scope["edge_id"] == eid]
        if live_rows.empty:
            continue
        bt = resolve_backtest_trade_sample(eid)
        trades = bt.get("trades")
        if trades is None or "hold_hours" not in trades.columns:
            continue
        live_hours = live_rows["hold_hours"].dropna()
        bt_hours = trades["hold_hours"].dropna()
        if live_hours.empty or bt_hours.empty:
            continue

        fig = go.Figure()
        fig.add_trace(go.Histogram(x=bt_hours, name="Backtest (current geometry)", histnorm="probability", opacity=0.55, marker_color="#60a5fa"))
        fig.add_trace(go.Histogram(x=live_hours, name=f"Live (n={len(live_hours)})", histnorm="probability", opacity=0.7, marker_color="#fb923c"))
        fig.update_layout(
            barmode="overlay",
            title=f"{eid}",
            xaxis_title="Hold time (hours)",
            height=300,
            legend=dict(orientation="h", y=-0.25),
        )
        _style_fig(fig)
        figs_html.append(f"<div class='chart-half'>{fig.to_html(full_html=False, include_plotlyjs=False)}</div>")

    if not figs_html:
        figs_html.append("<p class='gap-cell'>No edges currently have both live trades and a resolvable backtest sample.</p>")

    return f"""
    <section id="holdtime">
      <div class="section-head"><h2>Hold-Time Distributions</h2><span class="section-sub">Live vs. backtest, current geometry</span></div>
      <p class="footnote">Confirmed 2026-09-24: vertical_horizon=1000 wins on net_R_per_mean_hold_bar for every
      currently-deployed edge with a Gate A grid, because trades essentially never reach the vertical barrier.
      These panels show where the real tail sits, to judge whether an intermediate vertical horizon
      (between 20 and 1000, never yet tested) could trim it without cutting off trades that resolve fine on their own.</p>
      <div class="chart-grid">
        {''.join(figs_html)}
      </div>
    </section>
    """


def _decommission_section() -> str:
    """AFML Ch.1 p.8-9 re-allocation / decommission verdicts (portfolio_live/decommission_rule.py)."""
    import importlib.util

    rule_path = _V2_ROOT / "portfolio_live" / "decommission_rule.py"
    spec = importlib.util.spec_from_file_location("_dashboard_decommission_rule", rule_path)
    rule = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = rule
    spec.loader.exec_module(rule)
    cfg = json.loads(rule.CONFIG_PATH.read_text(encoding="utf-8"))
    try:
        verdicts = rule.run(cfg)
    except Exception as exc:  # visible gap, never a silent pass
        return f"<section id='decommission'><h2>Decommission / Shrink</h2><p class='gap-cell'>Rule failed: {exc}</p></section>"
    colors = {"full": "#34d399", "reduced": "#fb923c", "off": "#f87171", "insufficient_sample": "#5b6272", "blocked": "#f87171"}
    fmt = lambda x, f: "—" if x is None else format(x, f)  # noqa: E731
    rows = "".join(
        f"<tr><td class='edge-cell'>{v.edge_id}</td><td>n={v.n_bets}</td>"
        f"<td><span class='badge' style='--badge-color:{colors.get(v.verdict, '#5b6272')};'>{v.verdict}</span></td>"
        f"<td class='mono'>{fmt(v.allocation, '.0%')}</td><td class='mono'>{fmt(v.psr_vs_zero, '.3f')}</td>"
        f"<td class='mono'>{fmt(v.psr_vs_backtest, '.3f')}</td><td class='mono'>{fmt(v.mean_r, '+.3f')}</td>"
        f"<td class='mono'>{fmt(v.backtest_sr_per_bet, '.3f')}</td><td>{v.note}</td></tr>"
        for v in verdicts
    )
    levels = " / ".join(f"&gt;{lv['psr_vs_backtest_above']:.2f} → {lv['allocation']:.0%}" for lv in cfg["shrink_levels"][:-1])
    return f"""
    <section id="decommission">
      <div class="section-head"><h2>Decommission / Shrink</h2><span class="section-sub">AFML Ch.1 p.8-9 re-allocation and decommission, Probabilistic Sharpe Ratio (AFML §14.7.2 p.203)</span></div>
      <p class="footnote">Bets of each edge's current certified version only. Off when PSR[0] ≤ {cfg['off_rule']['off_if_at_or_below']}
      (95% confident the edge loses money). Otherwise allocation by PSR vs the meta-approved backtest Sharpe: {levels} / else
      {cfg['shrink_levels'][-1]['allocation']:.0%}. No verdict below {cfg['min_bets_for_verdict']} bets. Deviation register D019, D021.</p>
      <div class="table-wrap"><table class="drift-table">
        <thead><tr><th>Edge</th><th>Bets</th><th>Verdict</th><th>Allocation</th><th>PSR[0]</th><th>PSR[backtest]</th><th>Live mean R</th><th>Backtest SR/bet</th><th>Note</th></tr></thead>
        <tbody>{rows}</tbody>
      </table></div>
    </section>
    """


def build_report() -> Path:
    edge_ids = load_current_live_edge_ids()
    scoped = scope_closed_trades(live_edge_ids=edge_ids)

    try:
        portfolio_ref = build_portfolio_drawdown_reference()
        exec_overview_html = _executive_section(portfolio_ref)
    except PortfolioReferenceError as exc:
        exec_overview_html = f"<section id='overview'><h2>Executive Overview</h2><p class='gap-cell'>Could not build portfolio reference: {exc}</p></section>"

    deep_dive_by_edge = {}
    for eid in edge_ids:
        live_rows = scoped.in_scope[scoped.in_scope["edge_id"] == eid]
        if live_rows.empty:
            continue
        bt = resolve_backtest_trade_sample(eid)
        geo = read_current_edge_geometry(eid)
        gate_c = load_gate_c_reference(eid)
        dd = build_edge_deep_dive(eid, live_rows, bt.get("trades"), gate_c, geo.config_label if geo else None)
        if dd is not None:
            deep_dive_by_edge[eid] = dd

    exec_quality_html, exec_quality_by_edge = _execution_quality_section(list(edge_ids), scoped)
    drift_html = _drift_section(list(edge_ids), scoped, exec_quality_by_edge, deep_dive_by_edge)
    deep_dive_html = _deep_dive_section(list(edge_ids), scoped, deep_dive_by_edge)
    model_quality_html = _model_quality_section(list(edge_ids), scoped)
    capital_velocity_html = _capital_velocity_section(list(edge_ids), scoped)
    hold_time_html = _hold_time_section(list(edge_ids), scoped)
    decommission_html = _decommission_section()

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Quantara — Live vs Backtest Drift Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  :root {{
    --bg: #0a0b10;
    --bg-elevated: #12141d;
    --card: #161925;
    --card-border: #23273a;
    --text: #e8eaf2;
    --text-dim: #8b91a6;
    --accent: #fb923c;
    --accent-2: #60a5fa;
    --green: #34d399;
    --red: #f87171;
    --radius: 14px;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: 'Inter', -apple-system, 'Segoe UI', sans-serif;
    background:
      radial-gradient(1200px 600px at 10% -10%, rgba(96,165,250,0.08), transparent),
      radial-gradient(1000px 500px at 90% 0%, rgba(251,146,60,0.06), transparent),
      var(--bg);
    color: var(--text);
    margin: 0;
    min-height: 100vh;
  }}
  .topbar {{
    position: sticky; top: 0; z-index: 20;
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 32px;
    background: rgba(10,11,16,0.85);
    backdrop-filter: blur(10px);
    border-bottom: 1px solid var(--card-border);
  }}
  .brand {{ display:flex; align-items:center; gap:10px; font-weight:800; font-size:17px; letter-spacing:-0.02em; }}
  .brand .dot {{ width:9px; height:9px; border-radius:50%; background: var(--green); box-shadow: 0 0 8px var(--green); }}
  nav.tabs {{ display:flex; gap:4px; }}
  nav.tabs a {{
    color: var(--text-dim); text-decoration:none; font-size:13px; font-weight:600;
    padding:7px 14px; border-radius:8px; transition: all .15s;
  }}
  nav.tabs a:hover {{ color: var(--text); background: var(--card); }}
  .refreshed {{ font-size:11px; color: var(--text-dim); font-family:'JetBrains Mono', monospace; }}
  .page {{ max-width: 1280px; margin: 0 auto; padding: 8px 32px 64px; }}
  h1.hero {{ font-size: 28px; font-weight:800; letter-spacing:-0.02em; margin: 28px 0 4px; }}
  .hero-sub {{ color: var(--text-dim); font-size: 14px; margin-bottom: 32px; }}
  section {{ margin-bottom: 48px; scroll-margin-top: 80px; }}
  .section-head {{ display:flex; align-items:baseline; gap:14px; margin-bottom:6px; flex-wrap:wrap; }}
  .section-head h2 {{ font-size: 19px; font-weight:700; margin:0; }}
  .section-sub {{ color: var(--text-dim); font-size: 13px; }}
  .alert-banner {{
    background: rgba(251,146,60,0.1); border:1px solid rgba(251,146,60,0.3);
    color: var(--accent); padding:10px 16px; border-radius:10px; font-size:13px; font-weight:600; margin: 10px 0;
  }}
  .stat-grid {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(160px,1fr)); gap:12px; margin:18px 0; }}
  .stat-card {{
    background: var(--card); border:1px solid var(--card-border); border-radius: var(--radius);
    padding:16px 18px; transition: transform .15s, border-color .15s;
  }}
  .stat-card:hover {{ transform: translateY(-2px); border-color:#33395a; }}
  .stat-card.accent {{ border-color: rgba(251,146,60,0.4); background: linear-gradient(180deg, rgba(251,146,60,0.08), var(--card)); }}
  .stat-label {{ display:block; font-size:11.5px; color: var(--text-dim); font-weight:600; text-transform:uppercase; letter-spacing:0.04em; }}
  .stat-value {{ display:block; font-size:22px; font-weight:700; margin-top:6px; font-family:'JetBrains Mono', monospace; }}
  .stat-value.small {{ font-size:14px; font-family:'Inter',sans-serif; }}
  .chart-card {{ background: var(--card); border:1px solid var(--card-border); border-radius: var(--radius); padding:12px; margin-top:14px; }}
  .footnote {{ font-size:12.5px; color: var(--text-dim); max-width: 980px; line-height:1.6; margin: 10px 0; }}
  .table-wrap {{ overflow-x:auto; border-radius: var(--radius); border:1px solid var(--card-border); margin-top:16px; }}
  table.drift-table {{ border-collapse: collapse; width:100%; font-size:13px; }}
  table.drift-table th, table.drift-table td {{ padding:11px 14px; text-align:left; border-bottom:1px solid var(--card-border); }}
  table.drift-table th {{ background: var(--bg-elevated); color: var(--text-dim); font-weight:600; font-size:11.5px; text-transform:uppercase; letter-spacing:0.03em; }}
  table.drift-table tbody tr:hover {{ background: rgba(255,255,255,0.02); }}
  table.drift-table tbody tr:last-child td {{ border-bottom:none; }}
  .edge-cell {{ font-weight:600; color:#fff; }}
  .mono {{ font-family:'JetBrains Mono', monospace; font-size:12.5px; }}
  .badge {{
    display:inline-block; background: color-mix(in srgb, var(--badge-color) 22%, transparent);
    color: var(--badge-color); border:1px solid color-mix(in srgb, var(--badge-color) 55%, transparent);
    padding:3px 10px; border-radius:20px; font-weight:700; font-size:11px; letter-spacing:0.02em;
  }}
  .p-val {{ color: var(--text-dim); font-size:11.5px; margin-left:8px; font-family:'JetBrains Mono', monospace; }}
  .gap-cell {{ color: var(--text-dim); font-style:italic; }}
  .note-cell {{ color: var(--text-dim); font-size:12px; max-width:340px; }}
  .root-cause-row td {{ background: rgba(96,165,250,0.05); padding-top:8px; padding-bottom:12px; }}
  .root-cause {{ color:#a8c5f5; font-size:12.5px; font-style:italic; }}
  .chart-grid {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(420px,1fr)); gap:16px; margin-top:16px; }}
  .chart-half {{ background: var(--card); border:1px solid var(--card-border); border-radius: var(--radius); padding:8px; }}
  ul.followups {{ font-size:13px; color: var(--text-dim); line-height:1.9; }}
  .dd-grid {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(380px,1fr)); gap:14px; margin-top:16px; }}
  .dd-card {{ background: var(--card); border:1px solid var(--card-border); border-radius: var(--radius); padding:18px 20px; }}
  .dd-card h3 {{ margin:0 0 10px; font-size:14px; font-weight:700; color:#fff; }}
  .dd-row {{ display:flex; flex-wrap:wrap; gap:6px 10px; align-items:baseline; font-size:12.5px; margin-bottom:8px; }}
  .dd-label {{ color: var(--text-dim); }}
  .dd-note {{ font-size:12.5px; color:#a8c5f5; line-height:1.6; margin:10px 0 0; }}
  ::-webkit-scrollbar {{ height:8px; width:8px; }}
  ::-webkit-scrollbar-thumb {{ background:#2a2f42; border-radius:4px; }}
</style>
</head>
<body>
<div class="topbar">
  <div class="brand"><span class="dot"></span> Quantara Drift Dashboard</div>
  <nav class="tabs">
    <a href="#overview">Overview</a>
    <a href="#execution">Execution Quality</a>
    <a href="#decommission">Decommission</a>
    <a href="#drift">Drift Engine</a>
    <a href="#deepdive">Deep-Dive</a>
    <a href="#modelquality">Model Quality</a>
    <a href="#velocity">Capital Velocity</a>
    <a href="#holdtime">Hold Times</a>
  </nav>
  <span class="refreshed">refreshed {now_utc} · auto-refreshes on reload</span>
</div>
<div class="page">
  <h1 class="hero">Live vs. Backtest Drift</h1>
  <p class="hero-sub">Scope: {len(edge_ids)} currently live-deployed edges, read fresh from live_manifest.json every time this page loads. Reload the browser tab any time — no manual rebuild step.</p>
  {exec_overview_html}
  {exec_quality_html}
  {decommission_html}
  {drift_html}
  {deep_dive_html}
  {model_quality_html}
  {capital_velocity_html}
  {hold_time_html}
</div>
</body>
</html>
"""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(html, encoding="utf-8")
    return REPORT_PATH


if __name__ == "__main__":
    path = build_report()
    print(f"Report written to: {path}")
