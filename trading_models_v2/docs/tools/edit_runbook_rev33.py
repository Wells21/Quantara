"""Quantara_Runbook.docx Revision 33 (2026-11-02): D075 amended -- joint-clustered counterfactual rule and Gate 0
leniency A + B + C (owner approved); Gate 0 geometry panel result.

Helpers as in edit_runbook_rev31.py.

usage: python edit_runbook_rev33.py IN_DOCX OUT_DOCX
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


RULE = "docs/mldp/rules/THEORY_CHECK_RULE_D075.md"

# -- header ----------------------------------------------------------------------------------------------------------
ns["replace_once"]("Revision date: 2 November 2026 - Revision 32 (Revision 31: 31 October 2026;",
                   "Revision date: 2 November 2026 - Revision 33 (Revision 32: 2 November 2026; Revision 31: 31 October 2026;")
after_para("Revision 32 (2 November 2026): MLAM Chapter 1.",
           P("Revision 33 (2 November 2026): D075 amended (owner approved): counterfactuals use one week-clustered SE over "
             "both sets; Gate 0 leniency A (cell pre-registered on the card), B (one-sided 95 %), C (pooled across assets); "
             "the model-filtered promotion check stays two-sided 95 %. Section 6A Steps 4B, 6B, 7; Appendix 50."))

# -- Section 2 --------------------------------------------------------------------------------------------------------
after_para("Rev 32 - theory before backtest (THEORY CHECK RULE D075",
           BUL("Rev 33 - Gate 0 is the screen, promotion is the strict bar: Gate 0 one-sided 95 % with the card's "
               "pre-registered cell and cross-asset pooling; the model-filtered promotion check two-sided 95 % (owner "
               "2026-11-02, price-only data: leniency only where it adds power or fit, never a weaker bar against drift)."))

# -- Step 4B ----------------------------------------------------------------------------------------------------------
after_para("Rev 32 (D075): every counterfactual names the trigger clause",
           BUL("Rev 33 (D075 A): the card also pre-registers its Gate 0 cell -- 1:1, EWM 100, lookback 1, 4 or 8 h x "
               "vertical 4, 24 or 96 h, matched to the thesis's stated horizon (default H4_V24) -- before any Gate 0 "
               "result; never changed afterwards."))

# -- Step 6B ----------------------------------------------------------------------------------------------------------
set_para("Reference geometry, fixed and never searched: event-vol lookback 4 h",
         "Gate 0 cell (Rev 33, D075 A): the cell pre-registered on the card -- barriers 1:1 (AFML s.3.5 p.48: learning "
         "the side requires symmetric barriers), EWM 100 bars (AFML Snippet 3.1 span0), lookback 1, 4 or 8 h x vertical "
         "4, 24 or 96 h matched to the thesis horizon; card silent -> H4_V24; never searched or changed after a result. "
         "The 2026-11-02 panel found the verdict identical in all 9 cells (Appendix 50). The edge's own trigger, "
         "extraction, primary side and labeler; gross mid R (no costs).")
set_para("(a) Drift baseline: the event set vs 20,000 random bars of the same side",
         "(a) Drift baseline: the event set vs 20,000 random bars of the same side (same period, cell, labeler). "
         "Primary: month-matched (each event replaced by a random bar from the same calendar month, 10,000 draws). "
         "diff = edge mean R - month-matched mean R, SE = sqrt(SE_edge^2 + SE_draws^2), SE_edge week-clustered. PASS = "
         "diff - 1.645 SE > 0 (one-sided 95 %, Rev 33 D075 B: the claim is directional). All-months and hour-matched "
         "are diagnostics.")
set_para("(b) Counterfactuals: every card counterfactual, one clause inverted at a time.",
         "(b) Counterfactuals: every card counterfactual, one clause inverted at a time. diff = edge mean R - inverted "
         "mean R with ONE week-clustered SE over both sets (joint clustering: per ISO week, the sum of (r - m_E)/n_E over "
         "edge events minus the sum of (r - m_I)/n_I over inverted events; SE = root of the sum of squares). PASS = diff "
         "- 1.645 SE > 0 (one-sided 95 %). Fewer than 100 inverted events = INCONCLUSIVE: make the card testable, else "
         "the owner decides.")
after_para("(b) Counterfactuals: every card counterfactual, one clause inverted at a time.",
           BUL("(c) Pooled across assets (Rev 33, D075 C): Gate 0 runs the thesis on every asset at once, per side "
               "(the coverage rule requires all of them). Pooled diff over all assets' events (each event's comparison "
               "from its own asset), SE week-clustered over ISO weeks shared across assets; PASS = diff - 1.645 SE > 0 "
               "for (a) and each counterfactual. An asset proceeds only if its own diff is > 0 (point estimate, assets "
               "with >= 100 events); per-asset results are always reported. A thesis firing on one asset is tested alone."))
set_para("Gate 0 passes only when (a) and every counterfactual pass.",
         "Gate 0 passes on an asset only when (a) and every counterfactual pass (pooled where (c) applies) and that "
         "asset's own diffs are > 0. FAIL kills the thesis as written: no re-tuning of trigger thresholds (MLAM p.8, the "
         "backtest-tweak-backtest cycle). A revised mechanism is a new card and a new attempt, counted in the trial "
         "counters and config/cross_asset_thesis_coverage.json.")

# -- Step 7 / promotion -----------------------------------------------------------------------------------------------
set_para("Rev 32 (D075): Gate 0 (Step 6B) must pass before this step.",
         "Rev 32/33 (D075): Gate 0 (Step 6B) must pass before this step. After Gate C, repeat the drift baseline at the "
         "locked geometry on the raw event set (one-sided 95 %); it must still pass before Gate D.")
set_para("Promoting an edge to config/edge_universe.json before its model-filtered drift test passes",
         "Promoting an edge to config/edge_universe.json before its model-filtered drift test passes (Rev 32/33, D075): "
         "the trades the model takes (calibrated OOF rows with live_calibrated_probability >= the allocator's "
         "final_live_threshold) vs month-matched random same-side entries; PASS = diff - 1.96 SE > 0 (two-sided 95 %, "
         "the strict final bar, stricter than Gate 0 because the threshold was chosen on the same rows -- report that "
         "with the result). At the RECERT every edge_universe edge is re-checked (counterfactuals, raw and filtered "
         "drift baselines); failures go to the owner.")

# -- Appendix 50 ------------------------------------------------------------------------------------------------------
appendix = (
    H1("50. Appendix: 2026-11-02 Gate 0 Geometry Panel and Leniency A + B + C (D075 amended)")
    + BUL("Owner 2026-11-02: counterfactual PASS = edge minus inverted with one week-clustered SE over both sets "
          "('Approved'); keep both the raw Gate 0 and the model-filtered promotion check; run the geometry panel ('yes'); "
          "price-only data, so be lenient where possible.")
    + BUL("Geometry panel (logs/gate0_geometry_panel_20261102/): 6 Gate C edges x 9 cells (1:1, EWM 100, lookback 1/4/8 h "
          "x vertical 4/24/96 h) + each locked cell (reproduces the stored labels exactly). Every edge fails every test "
          "in every cell; edge minus same-month random -0.004..+0.046 R. Edge and random R both rise with lookback and "
          "vertical; the locked 2:1 cells inflate drift (random +0.07..+0.10 R vs +0.00..+0.06 at 1:1); short cells have "
          "the tightest CIs.")
    + BUL("Owner: 'Approved A + B + C, save it everywhere'. A: Gate 0 cell pre-registered on the card, matched to the "
          "thesis horizon (default H4_V24). B: Gate 0 one-sided 95 % (diff - 1.645 SE > 0). C: pooled across assets per "
          "side with shared week clusters and per-asset consistency. Promotion check stays two-sided 95 %. Canonical "
          "text: " + RULE + "; register D075; walkthrough section 4W.5-4W.6.")
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
