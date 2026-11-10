"""Quantara_Runbook.docx Revision 47 (2026-11-09): Gate 0 tests the theory per asset (D090); gate leniency + history
extension (D087); full-history era-scaled Gate C (D088); firing rate >= 3/week (D089); STATE.

Helpers as in edit_runbook_rev46.py.

usage: python edit_runbook_rev47.py IN_DOCX OUT_DOCX
"""
import html
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
head = (HERE / "edit_runbook_rev17.py").read_text(encoding="utf-8")
head = head[: head.index('replace_once("Revision date: 24 October 2026 - Revision 16')]
ns: dict = {"__name__": "rev17_head"}
src, dst = Path(sys.argv[1]), Path(sys.argv[2])
sys.argv = [sys.argv[0], str(src), str(dst)]
exec(compile(head, "edit_runbook_rev17.py(head)", "exec"), ns)
P, H1, BUL = ns["P"], ns["H1"], ns["BUL"]

PARA = re.compile(r"<w:p[ >].*?</w:p>", re.S)
TEXT = re.compile(r"(<w:t(?: [^>]*)?>)(.*?)(</w:t>)", re.S)


def _find(key):
    hits = [m for m in PARA.finditer(ns["x"]) if key in html.unescape("".join(t[1] for t in TEXT.findall(m.group(0))))]
    if len(hits) != 1:
        raise SystemExit(f"paragraph count {len(hits)} for: {key[:80]}")
    m = hits[0]
    if len(TEXT.findall(m.group(0))) != 1:
        raise SystemExit(f"paragraph is not a single run: {key[:80]}")
    return m.start(), m.end(), m.group(0)


def set_para(key, new_text):
    s, e, p = _find(key)
    p2 = TEXT.sub(lambda t: t.group(1) + escape(new_text) + t.group(3), p, count=1)
    ns["x"] = ns["x"][:s] + p2 + ns["x"][e:]


def after_para(key, xml):
    e = _find(key)[1]
    ns["x"] = ns["x"][:e] + xml + ns["x"][e:]


def para_text(key):
    return html.unescape("".join(t[1] for t in TEXT.findall(_find(key)[2])))


def edit_para(key, old, new):
    t = para_text(key)
    if t.count(old) != 1:
        raise SystemExit(f"substring count {t.count(old)} in paragraph: {key[:60]} / {old[:60]}")
    set_para(key, t.replace(old, new))


# ---------------------------------------------------------------- status line + revision note
ns["replace_once"]("Revision date: 4 November 2026 - Revision 46 (Revision 45: 4 November 2026;",
                   "Revision date: 9 November 2026 - Revision 47 (Revision 46: 4 November 2026; Revision 45: 4 November 2026;")
after_para("Revision 46 (4 November 2026): the >= 2000 model-row floor is removed",
           P("Revision 47 (9 November 2026): Gate 0 tests the THEORY on each asset's own events -- the drift-neutral "
             "main counterfactual decides, the month-matched drift baseline is a reported diagnostic and other assets "
             "never veto an asset (owner, D090); the 5 gate changes of 5 November and the bars-only history extension "
             "(D087); Gate C on the full history with an era-scaled broker spread (D088); every edge fires >= 3 times a "
             "week on 2022+ (D089). Section 6A Steps 6B, 7, KEY RULES, STATE; Appendix 64."))

# ---------------------------------------------------------------- Step 6B: Gate 0 (D090)
edit_para("(a) Drift baseline: the event set vs 20,000 random bars",
          "All-months and hour-matched are diagnostics.",
          "All-months and hour-matched are diagnostics. Rev 47 (D090, owner 2026-11-09): the month-matched drift "
          "baseline is now a DIAGNOSTIC as well -- always computed and reported, it no longer decides. Why: it is our "
          "addition (2026-11-02), not the book's; it tests the raw trigger's average, which AFML s.3.6 (high-recall "
          "primary, meta-labeling raises precision) does not require; and it cannot credit any effect that works at "
          "the monthly scale (T22).")
edit_para("(b) Counterfactuals: every card counterfactual, one clause inverted at a time.",
          "Fewer than 100 inverted events = INCONCLUSIVE: make the card testable, else the owner decides.",
          "Fewer than 100 inverted events = INCONCLUSIVE: make the card testable, else the owner decides. Rev 47 "
          "(D090): the counterfactuals are the gate (MLAM s.1.2.2 p.4, s.1.4.2 p.9: a theory is tested for facts and "
          "counterfacts). The MAIN counterfactual C1 must be DRIFT-NEUTRAL: it removes only the stated cause and keeps "
          "the same asset, side, period and a comparable event frequency, so both sets carry the same market drift and "
          "a drift-riding edge earns the same as its C1 and fails. A card whose C1 differs from the edge in drift "
          "exposure for any other reason is rewritten before any run.")
edit_para("(c) Pooled across assets (Rev 33, D075 C):",
          "every asset is still computed and reported (coverage rule).",
          "every asset is still computed and reported (coverage rule). Rev 47 (D090, owner 2026-11-09: 'all assets "
          "must not agree for an edge in a particular asset to work'): NO pooling requirement -- each asset is judged "
          "on its own events; the pooled result is a diagnostic; assets outside the card's universe are reported as "
          "PLACEBO with no verdict.")
set_para("Gate 0 passes on an asset only when",
         "Gate 0 passes on an asset and side (Rev 47, D090, owner 2026-11-09) only when, on that asset's own events: "
         "(1) the main counterfactual C1 diff - 1.28 SE > 0 on the full history (one-sided 90 %, joint week-clustered "
         "SE); (2) every other card counterfactual diff > 0; (3) recency: C1 diff > 0 on 2022-01-01 onward; (4) >= 100 "
         "edge events and >= 100 events in every counterfactual, else INCONCLUSIVE; (5) >= 3 edge events per week on "
         "2022+ (D089); (6) the asset is in the card's pre-registered universe. Tools: gate0_tool.py (window_tests_d090) "
         "then shared/research/gate0/gate0_verdict_d090.py. D090 is prospective: stored results were re-read under it "
         "read-only, and those passes go to Gate A only by owner order, logged as post-hoc. Where a card's LONG edge is "
         "the SHORT's C1 (T18, T20, T21) the two sides are one comparison, not two pieces of evidence. FAIL kills the "
         "thesis as written: no re-tuning of trigger thresholds (MLAM p.8, the backtest-tweak-backtest cycle). A revised "
         "mechanism is a new card and a new attempt, counted in the trial counters and "
         "config/cross_asset_thesis_coverage.json.")

# ---------------------------------------------------------------- Step 7
set_para("Rev 32/33 (D075): Gate 0 (Step 6B) must pass before this step.",
         "Rev 32/33 (D075): Gate 0 (Step 6B) must pass before this step. Rev 47 (D090): after Gate C, the theory "
         "re-check repeats the Gate 0 test at the LOCKED cell on the edge's asset -- C1 diff - 1.28 SE > 0 on the full "
         "history, the other counterfactuals > 0, C1 diff > 0 on 2022+ (GATE0_CELL / GATE0_ASSETS in gate0_tool.py, "
         "then gate0_verdict_d090.py). The drift baseline is reported, not decisive. It must pass before Gate D. "
         "RECERT uses the same test.")
edit_para("Pay -- all mandatory, nothing optional:",
          "DSR > 0.95 AND FWER alpha_K < 0.05 (D054);",
          "DSR > 0.95 (D054; Rev 47, D087: FWER alpha_K is report-only);")
edit_para("Pay -- all mandatory, nothing optional:",
          "- Strategy risk on bets P[p < p_theta] <= .05 (theta 0, k 2; D055).",
          "- Strategy risk on bets P[p < p_theta] <= .10 (theta 0, k 2; D055; Rev 47, D087: was .05; a deployment "
          "gate). Rev 47 (D088, owner 2026-11-09): Gate C, PBO / CSCV and DSR run on the FULL tick history with the "
          "broker half-spread x the monthly era factor max(1, min(Dukascopy spread ratio vs the cost-model fit window "
          "in the same vol state, cost-model ref_price / month median mid)) -- the cost level is always the broker's, "
          "Dukascopy only scales older months; the locked cell's 2022+ exec mean on the plain broker model must be > 0; "
          "a Gate C that failed PBO stays killed.")

# ---------------------------------------------------------------- KEY RULES + STATE
edit_para("EDGE LEVEL (decides config/edge_universe.json)",
          "Gate 0 (counterfactuals one-sided 95 % + month-matched drift one-sided 95 %, or 90 % when every "
          "counterfactual passes; pooled over the card's universe; D075)",
          "Gate 0 per asset (drift-neutral C1 one-sided 90 % + other counterfactuals > 0 + C1 > 0 on 2022+ + >= 3 "
          "events/week on 2022+; drift and pooled results are diagnostics; D090)")
edit_para("EDGE LEVEL (decides config/edge_universe.json)",
          "C exec_mean_R > 0 + compound velocity",
          "C on the full history, era-scaled broker spread (D088): exec_mean_R > 0 + compound velocity, 2022+ exec "
          "mean > 0")
edit_para("EDGE LEVEL (decides config/edge_universe.json)",
          "drift re-check at the locked geometry, same bar as Gate 0 (D075)",
          "theory re-check at the locked geometry, same test as Gate 0 (D090)")
edit_para("EDGE LEVEL (decides config/edge_universe.json)",
          "DSR > 0.95 AND FWER < 0.05 (D054)",
          "DSR > 0.95 (D054; FWER report-only, D087)")
edit_para("EDGE LEVEL (decides config/edge_universe.json)",
          "strategy risk <= .05 at the live threshold (D055)",
          "strategy risk <= .10 at the live threshold (D055, D087)")
set_para("Calendar-time hours; EWM bar-count. No model-row floor (Rev 46, D085).",
         "Calendar-time hours; EWM bar-count. No model-row floor (Rev 46, D085). Test long AND short on every asset. "
         "Every edge fires >= 3 times a week on 2022+ (Rev 47, D089). Trials accumulate, never reset (D050-A, D087).")
edit_para("The AFML (Ch.1-22) and MLAM (Ch.1-8, Appendices A-B) walkthrough is complete",
          "T3 needs its source confirmed",
          "T3 needs its source confirmed. Rev 47 (9 November 2026): history extended and bars rebuilt (D087); 35 Gate 0 "
          "tests under the old rule, 2 passes (T13 round-level stop cascade, T18 overnight drift); "
          "XAUUSD/roundlevelcascade_long passed the deciding full-history Gate C (PBO 0.140, DSR 0.992, lock "
          "H8_E150_V4_PT1_SL1) and is in Steps 01-09 / Gate D; the T18 index longs died at Gate C (US500, JPN, HK no "
          "profit; DAX PBO; NASDAQ DSR). Gate 0 is now D090; owner goal: >= 1 live long and 1 live short per asset, "
          "each firing >= 3 times a week")

# ---------------------------------------------------------------- Appendix 64
appendix = (
    H1("64. Appendix: 2026-11-09 Gate 0 Tests the Theory per Asset (D090), Gate Leniency and History Extension "
       "(D087), Full-History Gate C (D088), Firing Rate (D089) (Revision 47)")
    + BUL("D090 -- what the books say. MLAM s.1.1 p.2: an edge needs a testable theory. MLAM s.1.2.2 p.4 and s.1.4.2 "
          "p.9: test the theory for facts and counterfacts; a backtest is not a controlled experiment. MLAM s.1.4.2 "
          "p.8: no backtest-tweak-backtest. AFML s.3.6: a high-recall primary whose low precision the meta-model "
          "corrects. The books set no Gate 0, no baseline and no threshold: those are ours.")
    + BUL("D090 -- what changed (owner 2026-11-09: 'i approve this'; 'all assets must not agree for an edge in a "
          "particular asset to work'). Gate 0 is decided per asset and side on that asset's own events by the "
          "drift-neutral main counterfactual (one-sided 90 %), the other counterfactuals (> 0), C1 recency on 2022+ "
          "and the firing rate. The month-matched drift baseline (2026-11-02) and the pooled result are reported "
          "diagnostics. The post-Gate C re-check and RECERT repeat the test at the locked cell.")
    + BUL("D090 -- why. Under the old rule 35 Gate 0 tests gave 2 passes. The drift baseline tested the raw trigger's "
          "average and could not credit monthly-scale effects: T22 (weekly time-series momentum) beat its "
          "against-trend counterfactual on both sides (z 2.3 / 2.2) but lost to random entries of the same month "
          "(z -3.3 / -3.1). Pooling plus every-asset agreement let other assets veto an asset's own evidence.")
    + BUL("D090 -- the guard against drift riding is the drift-neutral C1, not Gate D or strategy risk: Gate D tests "
          "the meta-model's precision and calibration, strategy risk the payout / precision economics; neither tests "
          "drift. On 2026-11-02 the inverted trigger earned as much as the edge on 4 of 6 old cards; a drift-neutral "
          "C1 catches exactly that.")
    + BUL("D090 -- prospective. Stored Gate 0 results were re-read read-only (gate0_*_verdict_d090.json and "
          "d090_retro_*.log beside each card in docs/mldp/campaign/NASDAQ_2026-11/); those passes go to Gate A only "
          "by owner order, logged as post-hoc.")
    + BUL("D087 (owner 2026-11-05, 'approve all 5'): (1) history extended, raw ticks kept in full (gold from 2005, "
          "indices 2013-15, USOIL from March 2020), bars rebuilt 2026-11-07, tick-timestamp parser fixed "
          "(shared/io/timeparse.py: whole-second ticks had been dropped in every earlier bar build); (2) Gate 0 at "
          "one-sided 90 % with recency (the Rev 47 rule of 5-9 November, now replaced by D090); (3) a Gate D v4.1 fail "
          "may deploy PRIMARY-ONLY (fixed size, no meta filter) -- design pending, shown to the owner first; (4) FWER "
          "report-only, DSR > 0.95 stays; (5) strategy-risk threshold .05 -> .10. Trials were NOT reset after the "
          "extension (owner 2026-11-07; AFML s.11.5 p.153, MLAM s.8.1 p.105).")
    + BUL("D088 (owner 2026-11-09): Gate C, PBO and DSR on the full history with an era-scaled broker spread; the "
          "era factor is capped by the points ratio (owner 'i approve your recommendation') so Dukascopy's own "
          "pricing changes (NASDAQ 2025-26, BTCUSD 2018-20) do not inflate costs; tables "
          "portfolio_allocator/config/broker_profiles/moneta_cost_models/era_scale/, builder "
          "docs/tools/build_era_spread_scale.py. The cost level is always the broker model. Gate C speed: one 405-cell "
          "batch with parallel contiguous chunks (identical rows; tests/test_gate_c_speed_exactness.py) -- gold full "
          "history 59 min instead of about 6-7 h.")
    + BUL("D089 (owner 2026-11-09): every edge fires >= 3 times a week on 2022+; each card states events/week before "
          "Gate 0.")
    + BUL("Updated: Section 6A Steps 6B, 7, KEY RULES and STATE; docs/mldp/rules/THEORY_CHECK_RULE_D075.md (D090 "
          "section); config/mldp_deviation_register.json (D087-D090 new; D075 status); "
          "config/research_methodology_standing_rules.json; shared/research/gate0/gate0_tool.py, "
          "gate0_verdict_d090.py; docs/mldp/campaign/NASDAQ_2026-11/CAMPAIGN.md.")
)

x = ns["x"]
i = x.rfind("<w:sectPr")
ns["x"] = x[:i] + appendix + x[i:]

zin = ns["zin"]
tmp = Path(tempfile.mkdtemp()) / "out.docx"
with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
    for item in zin.infolist():
        data = ns["x"].encode("utf-8") if item.filename == "word/document.xml" else zin.read(item.filename)
        zout.writestr(item, data)
shutil.move(str(tmp), dst)
print(f"written {dst}")
