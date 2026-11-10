"""Quantara_Runbook.docx Revision 32 (2026-11-02): THEORY CHECK RULE D075 (MLAM Ch.1) -- Gate 0 before Gate A.

Helpers as in edit_runbook_rev31.py. Appendix 49's result lines are read from the result JSONs in
logs/counterfactual_tests_20261031/.

usage: python edit_runbook_rev32.py IN_DOCX OUT_DOCX
"""
import html
import json
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
LOGS = HERE.parents[1] / "logs" / "counterfactual_tests_20261031"
head = (HERE / "edit_runbook_rev17.py").read_text(encoding="utf-8")
head = head[: head.index('replace_once("Revision date: 24 October 2026 - Revision 16')]
ns: dict = {"__name__": "rev17_head"}
src, dst = Path(sys.argv[1]), Path(sys.argv[2])
sys.argv = [sys.argv[0], str(src), str(dst)]
exec(compile(head, "edit_runbook_rev17.py(head)", "exec"), ns)
P, H1, BUL = ns["P"], ns["H1"], ns["BUL"]

PARA = re.compile(r"<w:p[ >].*?</w:p>", re.S)
TEXT = re.compile(r"(<w:t(?: [^>]*)?>)(.*?)(</w:t>)", re.S)


def H2(t):
    return f'<w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr><w:r><w:t xml:space="preserve">{escape(t)}</w:t></w:r></w:p>'


def _find(key):
    hits = [m for m in PARA.finditer(ns["x"]) if key in html.unescape("".join(t[1] for t in TEXT.findall(m.group(0))))]
    if len(hits) != 1:
        raise SystemExit(f"paragraph count {len(hits)} for: {key[:80]}")
    return hits[0].start(), hits[0].end(), hits[0].group(0)


def after_para(key, xml):
    e = _find(key)[1]
    ns["x"] = ns["x"][:e] + xml + ns["x"][e:]


def before_para(key, xml):
    s = _find(key)[0]
    ns["x"] = ns["x"][:s] + xml + ns["x"][s:]


RULE = "docs/mldp/rules/THEORY_CHECK_RULE_D075.md"

# -- header ----------------------------------------------------------------------------------------------------------
ns["replace_once"]("Revision date: 31 October 2026 - Revision 31 (Revision 30: 31 October 2026;",
                   "Revision date: 2 November 2026 - Revision 32 (Revision 31: 31 October 2026; Revision 30: 31 October 2026;")
after_para("Revision 31 (31 October 2026): the geometry grid's EWM spans become",
           P("Revision 32 (2 November 2026): MLAM Chapter 1. THEORY CHECK RULE D075 (owner approved): every new thesis "
             "passes Gate 0 -- counterfactual tests and a same-month random-entry drift baseline -- before Gate A; the drift "
             "baseline is repeated after Gate C and on the model's trades before promotion; every edge is re-checked at the "
             "RECERT. Section 2; Section 6A Steps 4B, 6B, 7, 12; Appendix 49."))

# -- Section 2: standing rule -----------------------------------------------------------------------------------------
after_para("Rev 29 - commit and push only when the owner says so;",
           BUL("Rev 32 - theory before backtest (THEORY CHECK RULE D075, owner 2026-11-02; MLAM Ch.1 pp.2-4, 8-9): no new "
               "thesis reaches Gate A without passing Gate 0 (Section 6A Step 6B); a Gate 0 failure kills the thesis as "
               "written and is never re-tuned to pass. Canonical text: " + RULE + "."))

# -- Step 4B: testable counterfactuals --------------------------------------------------------------------------------
after_para("Fields: schema_version, written_utc, provenance (post_hoc if written after the edge was found)",
           BUL("Rev 32 (D075): every counterfactual names the trigger clause, its inversion and the prediction, and must be "
               "testable on research data (>= 100 events when inverted). Gate 0 (Step 6B) tests each one."))

# -- Step 6B: Gate 0 --------------------------------------------------------------------------------------------------
before_para("6A · STEP 7 - GATES A / B / C + PBO: GEOMETRY SELECTION",
            H2("6A · STEP 6B - GATE 0: THEORY CHECK (Rev 32, D075; MLAM Ch.1 pp.2-4, 8-9) -- before Gate A")
            + BUL("Why: MLAM s.1.1 p.2 -- without a testable theory 'the odds are that you do not have an edge at all'; "
                  "s.1.2.2 p.4 -- a theory explains positives and negatives (the absence of y is due to the absence of x); "
                  "s.1.4.2 p.9 -- backtests do not tell us why. Gates A-D measure the profit; Gate 0 asks whether the "
                  "card's cause produces it. The book sets no threshold; the rule below is ours (owner approved).")
            + BUL("Applies to every new thesis -- new or ported, every asset, both sides. Pre-register first: "
                  "logs/theory_check/<date>_<ASSET>_<edge>/PREREGISTRATION.md, written before any result.")
            + BUL("Reference geometry, fixed and never searched: event-vol lookback 4 h, EWM 100 bars (AFML Snippet 3.1 "
                  "span0), vertical 24 h, barriers 1:1. The edge's own trigger, extraction, primary side and labeler; "
                  "gross mid R (no costs).")
            + BUL("(a) Drift baseline: the event set vs 20,000 random bars of the same side (same period, geometry, "
                  "labeler). Primary: month-matched (each event replaced by a random bar from the same calendar month, "
                  "10,000 draws). PASS = (edge mean R - month-matched mean R) 95 % CI lower bound > 0, SE = "
                  "sqrt(SE_edge^2 + SE_draws^2), SE_edge week-clustered. All-months and hour-matched are diagnostics.")
            + BUL("(b) Counterfactuals: every card counterfactual, one clause inverted at a time. PASS = (edge mean R - "
                  "inverted mean R) 95 % CI lower bound > 0 (week-clustered SEs). Fewer than 100 inverted events = "
                  "INCONCLUSIVE: make the card testable, else the owner decides.")
            + BUL("Gate 0 passes only when (a) and every counterfactual pass. FAIL kills the thesis as written: no "
                  "re-tuning of trigger thresholds (MLAM p.8, the backtest-tweak-backtest cycle). A revised mechanism is a "
                  "new card and a new attempt, counted in the trial counters and config/cross_asset_thesis_coverage.json.")
            + BUL("Gate 0 tests are theory tests, not strategy trials: nothing is selected on them and they do not enter "
                  "PBO / DSR; every run is logged.")
            + BUL("Tools: shared/research/counterfactual_tests/run_counterfactuals.py, run_drift_baseline.py, "
                  "run_drift_filtered.py (a single Gate 0 tool is backlog D075). Canonical text: " + RULE + "."))

# -- Step 7: confirmation after Gate C --------------------------------------------------------------------------------
after_para("Thesis first. The thesis card is written (provenance pre_registered) before the first backtest",
           BUL("Rev 32 (D075): Gate 0 (Step 6B) must pass before this step. After Gate C, repeat the drift baseline at the "
               "locked geometry on the raw event set; it must still pass before Gate D."))

# -- promotion: model-filtered drift test -----------------------------------------------------------------------------
after_para("Promoting an edge whose PBO, DSR, FWER or strategy-risk gate fails",
           BUL("Promoting an edge to config/edge_universe.json before its model-filtered drift test passes (Rev 32, D075): "
               "the trades the model takes (calibrated OOF rows with live_calibrated_probability >= the allocator's "
               "final_live_threshold) vs month-matched random same-side entries, CI lower bound > 0. Biased in the "
               "edge's favour (threshold chosen on the same rows) -- report that with the result. At the RECERT every "
               "edge_universe edge is re-checked (counterfactuals, raw and filtered drift baselines); failures go to "
               "the owner."))


# -- Appendix 49 ------------------------------------------------------------------------------------------------------
raw = []
for f in sorted(LOGS.glob("*_drift.json")):
    d = json.loads(f.read_text(encoding="utf-8"))
    m = d["baseline_M_month_matched_PRIMARY"]
    raw.append(f"{d['asset']} {d['edge']} {d['edge_events']['mean_R']:+.3f} vs {m['baseline_mean_R']:+.3f} "
               f"(diff {m['diff']:+.3f}, CI {m['diff_ci_lo']:+.3f}..{m['diff_ci_hi']:+.3f})")
filt = []
for f in sorted(LOGS.glob("*_drift_filtered.json")):
    d = json.loads(f.read_text(encoding="utf-8"))
    m = d["F1_M_month_matched_PRIMARY"]
    filt.append(f"{d['asset']} {d['edge']} (threshold {d['final_live_threshold']}, {d['taken']['n']}/{d['oof_rows']} taken) "
                f"{d['taken']['mean_R']:+.3f} vs {m['baseline_mean_R']:+.3f} (diff {m['diff']:+.3f}, CI "
                f"{m['diff_ci_lo']:+.3f}..{m['diff_ci_hi']:+.3f}) {m['verdict']}")
if len(raw) != 6 or len(filt) != 6:
    raise SystemExit(f"expected 6 raw + 6 filtered results, got {len(raw)} + {len(filt)}")

appendix = (
    H1("49. Appendix: 2026-11-02 MLAM Chapter 1 and the Theory Check Rule (D075)")
    + BUL("MLAM Ch.1 (pp.1-23) audit: walkthrough section 4W. Thesis counterfactual tests on the 6 Gate C edges "
          "(logs/counterfactual_tests_20261031/PREREGISTRATION.md): 3 PASS / 5 FAIL / 4 INCONCLUSIVE; on 4 of 6 cards the "
          "inverted trigger earns about the edge's gross mid R. Only HK's hazard gate behaves as its card says.")
    + BUL("Raw drift baseline (PREREGISTRATION_DRIFT_BASELINE.md): edge vs month-matched random same-side entries -- "
          + "; ".join(raw) + ". 6/6 NOT DISTINGUISHABLE FROM DRIFT; random entries alone earn +0.07..+0.10 R.")
    + BUL("Model-filtered drift test (addendum): " + "; ".join(filt) + ".")
    + BUL("Owner 2026-11-02: 'i agree and approve' -- THEORY CHECK RULE D075 (" + RULE + "): Gate 0 before Gate A "
          "(Step 6B), drift confirmation after Gate C (Step 7), model-filtered drift test before promotion, all edges "
          "re-checked at the RECERT. Counterfactual PASS rule tightened to (edge - inverted) CI lower > 0 after XAU vc C1 "
          "passed the 2026-10-31 rule by 0.004 R. No deployed edge changes now.")
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
