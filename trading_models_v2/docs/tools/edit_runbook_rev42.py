"""Quantara_Runbook.docx Revision 42 (2026-11-03): full runbook review after the AFML and MLAM walkthrough.

Helpers as in edit_runbook_rev33.py.

usage: python edit_runbook_rev42.py IN_DOCX OUT_DOCX
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


ns["replace_once"]("Revision date: 3 November 2026 - Revision 41 (Revision 40: 3 November 2026;",
                   "Revision date: 3 November 2026 - Revision 42 (Revision 41: 3 November 2026; Revision 40: 3 November 2026;")
after_para("Revision 41 (3 November 2026): MLAM Appendix A (testing on synthetic data) and Appendix B closed",
           P("Revision 42 (3 November 2026): full review of this runbook after the AFML and MLAM walkthrough. Brings the "
             "body up to date with the MLAM-era decisions (D074-D077) and the MLAM Ch.5-8 and Appendix A-B findings: Section 2 "
             "(two owner rules), Section 6A Steps 4, 10B, 10D, 10J, 10L, 13, KEY RULES and STATE, the Section 16C RECERT "
             "sequence (D074 re-selection, D075 re-checks, D076 DSR records, D077 Step 01) and Appendix C; Appendix 59."))

# status line
edit_para("Current state: Section 6A STATE",
          "Current state: Section 6A STATE (live trading stopped since 23 October 2026 for the AFML/MLAM walkthrough).",
          "Current state: Section 6A STATE (live trading stopped since 23 October 2026; the AFML/MLAM walkthrough is "
          "complete since 3 November 2026; next: the RECERT, Section 16C).")

# Section 2: two owner rules
after_para("Rev 34 - denoise before clustering",
           BUL("Rev 42 - do not repeat a test that an earlier chapter review or audit already ran on our data: map the "
               "question to the earlier record first and test only the gaps that can change Quantara (owner, 2026-11-03).")
           + BUL("Rev 42 - read an allocator or live script in full before changing it."))

# Step 4: labels for new theses
after_para("Bar-level mechanisms only",
           BUL("Rev 42 (MLAM Ch.5-6): a new thesis may use trend-scanning labels (MLAM Snippets 5.1-5.3) as its primary "
               "label at Gate 0 -- never as a replacement for the meta-label; feature importance can change with the "
               "labelling method (MLAM s.6.7 p.91), so the card names its label. XAUUSD labels are hour-of-day dependent "
               "(MLAM Ch.5 T2, Cramer's V .13-.14): a known label property, not a defect."))

# Step 10B: D029 closed; retrain timing
edit_para("Time decay (Snippet 4.11) is wired",
          "is wired with TIME_DECAY_C = 1.0 (no decay) until Ch.9 (D029).",
          "is wired with TIME_DECAY_C = 1.0 (no decay); the AFML Ch.9 nested-CV test kept c = 1 (D029, closed).")
edit_para("All 6 current edge models were trained with class balance applied twice",
          "They are retrained once, after the Ch.6/7/9 reviews, as new versions with Gates C/D/E re-run.",
          "They are retrained once, at the RECERT (Section 16C), as new versions with Gates C/D/E re-run.")

# Step 10D: timing + Ch.6 findings
edit_para("Runs in repo mode only (flat Colab retired",
          "~3 minutes per edge with QUANTARA_STEP01_WORKERS=4-6.",
          "the importance stage took 5-8 minutes per edge at 4 workers in the MLAM Ch.6 tests (partition cached), plus "
          "the best-of-20 ONC (D077); re-measure at the RECERT.")
after_para("Deviations in force: D039",
           BUL("Rev 42 (MLAM Ch.6, walkthrough s.4ZB): D041 confirmed on the 6 edges -- with min_weight_fraction_leaf 0 "
               "Step 01 kept fewer clusters on 5 of 6 (none on 3) and had worse out-of-sample log loss on all 6. On the "
               "book's synthetic data D041 keeps only 2 of 5 signal clusters: Step 01 is conservative on clean, "
               "non-overlapping data (known limitation). Log loss stays the MDA score (probability-weighted accuracy "
               "agreed on 4 of 6); D042 stays (residualised selection depends on the cluster order on all 6 edges). VI + "
               "ONC recovered the synthetic cluster truth exactly."))

# Step 10J: App. B caveat
after_para("Trial counts already spent cannot be reset",
           BUL("Rev 42 (MLAM Appendix B): the expected-maximum formula (AFML p.204, MLAM eq. 3-4) holds for K >> 1. At "
               "E[K] = 2 -- every edge's Level 1 record -- it is 7.9 % below the exact expectation; recomputed DSRs change "
               "by at most 0.04 and no D054 verdict changes. The book formula is kept; quote the caveat when a DSR is near "
               "0.95 with E[K] = 2."))

# Step 10L: Ch.7
after_para("Allocation method (D060): HRP kept",
           BUL("Rev 42 (MLAM Ch.7, walkthrough s.4ZC): on the 4 deployable edges (condition number 1.28) no NCO variant -- "
               "min-variance on corr or abs(corr), IVP or long-only CLA inside -- is more stable than HRP-current at nObs "
               "260 or T; HRP stays. The NCO variants are not added to the D060 diagnostic (owner 2026-11-03)."))

# Step 13
edit_para("PASS (Gate C + PBO + Steps 01-09 + Gate D",
          "with the label audit and CV leakage audit",
          "and, for every new thesis, Gate 0 before Gate A, the raw drift re-check after Gate C and the model-filtered "
          "drift test at the allocator's threshold (D075, Steps 6B and 7) -- with the label audit and CV leakage audit")

# KEY RULES chain
edit_para("A gross >= 0.05 -> B all net",
          "A gross >= 0.05 -> B all net > 0 (fresh grid) -> C exec_mean_R > 0 + compound velocity -> PBO < 0.5 AND "
          "Prob < 0.5 -> stress -> 4-place lock",
          "Gate 0 (counterfactuals + month-matched drift, one-sided 95 %, D075) -> A gross >= 0.05 -> B all net > 0 "
          "(fresh grid) -> C exec_mean_R > 0 + compound velocity -> PBO < 0.5 AND Prob < 0.5 -> stress -> raw drift "
          "re-check at the locked geometry (D075) -> 4-place lock")
edit_para("A gross >= 0.05 -> B all net",
          "-> promote -> allocator -> Gate E (Rev 29).",
          "-> promote -> allocator -> model-filtered drift test at final_live_threshold, two-sided 95 % (D075) -> Gate E "
          "(Rev 29).")

# STATE
edit_para("STATE 2026-10-31",
          "STATE 2026-10-31 (live stopped since 2026-10-23 for the AFML/MLAM walkthrough)",
          "STATE 2026-11-03 (live stopped since 2026-10-23; the AFML/MLAM walkthrough is complete)")
edit_para("AFML Ch.1-22 walked through by 2026-10-31",
          "AFML Ch.1-22 walked through by 2026-10-31; MLAM Ch.1, Ch.5 and Appendix A remain.",
          "The AFML (Ch.1-22) and MLAM (Ch.1-8, Appendices A-B) walkthrough is complete (3 November 2026); next is the "
          "RECERT (Section 16C).")

# 16C RECERT sequence
edit_para("Rev 29 - the RECERT (the full list",
          "(1) re-export every edge (187-entry schema; same-asset exports serially); (2) Task 2A, Steps 01-09 on venv312 "
          "and Gate D; (3) label audit and CV leakage audit (Steps 12D / 12E); (4) the RECERT gates: Level-1 DSR / FWER "
          "(D054) and strategy risk on bets (D055)",
          "(1) Rev 42: Gate A-C re-selection of every edge on the 405-cell grid with EWM spans 100,150,200 (D074) and PBO "
          "over that run (D051); the run's cells join the edge's trial record (D050-A); lock in all four places; (2) "
          "re-export every edge (187-entry schema; same-asset exports serially); (3) Task 2A, Steps 01-09 on venv312 "
          "(Step 01 with the best-of-20 ONC, D077) and Gate D; (4) label audit and CV leakage audit (Steps 12D / 12E); "
          "(5) Rev 42 (D075): counterfactuals and the raw drift baseline of every edge at its locked geometry, "
          "pre-registered; failures go to the owner; (6) the RECERT gates: Level-1 DSR / FWER (D054; dsr_all_trials "
          "re-run over all the edge's Gate C trials on the denoised, conditionally detoned trial correlation, D076) and "
          "strategy risk on bets (D055)")
edit_para("Rev 29 - the RECERT (the full list",
          "(5) the venv39 pickle scan (D065); (6) allocator 00a-11 on venv39 including 07b_dsr_psr_validation and 07c "
          "(D052 per-path selection, D059 mark-to-market streams, D010 denoising, D070 report); (7) live manifest "
          "(Section 8) and python portfolio_live/edge_versions.py; (8) Gate E, one look per new version (Step 12C; a "
          "FAIL removes the edge and the allocator is re-run); (9) Sections 9-18 with Phase C rebuilt (Section 11B: the "
          "selected features changed); (10) live redeploy only on the owner's go.",
          "(7) the venv39 pickle scan (D065); (8) allocator 00a-11 on venv39 including 07b_dsr_psr_validation (on the "
          "D076 records) and 07c (D052 per-path selection, D059 mark-to-market streams, D010 denoising, D070 report); (9) "
          "Rev 42 (D075): the model-filtered drift test of every edge at its final_live_threshold (two-sided 95 %), "
          "pre-registered; failures go to the owner; (10) live manifest (Section 8) and python "
          "portfolio_live/edge_versions.py; (11) Gate E, one look per new version (Step 12C; a FAIL removes the edge and "
          "the allocator is re-run); (12) Sections 9-18 with Phase C rebuilt (Section 11B: the selected features "
          "changed); (13) live redeploy only on the owner's go.")

# Appendix C checklist
after_para("Executor started after dashboard verification.",
           P("[ ] Rev 42: after the AFML/MLAM walkthrough, the RECERT (Section 16C) is complete and the owner has given "
             "the go."))

appendix = (
    H1("59. Appendix: 2026-11-03 Revision 42 -- Runbook Review after the AFML and MLAM Walkthrough")
    + BUL("Checked: every section of this runbook against config/mldp_deviation_register.json (D001-D077), "
          "config/mldp_implementation_backlog.json (37 items) and docs/mldp/AFML_MLAM_WALKTHROUGH.md. Sections 3-21 "
          "(account, broker, allocator, manifest, Phase C/D, go-live) needed no change beyond Appendix C; the appendices "
          "are dated records and stay as written.")
    + BUL("Updated: the status line and Section 6A STATE (walkthrough complete; next the RECERT); Section 2 (no repeated "
          "tests; read allocator / live scripts in full before changing them); Step 4 (trend-scanning labels for new "
          "theses, label named on the card; XAUUSD label hour dependence); Step 10B (D029 closed at c = 1; retrain at the "
          "RECERT); Step 10D (Step 01 timing; D041 confirmed on the edges, the synthetic-data limitation, log loss and "
          "D042 kept); Step 10J (Appendix B: the expected-maximum formula at E[K] = 2); Step 10L (MLAM Ch.7: no NCO "
          "variant beats HRP); Step 13 and KEY RULES (the D075 Gate 0 and drift checks in the gate chain).")
    + BUL("Section 16C RECERT sequence, now 13 steps: D074 Gate A-C re-selection on the EWM 100,150,200 grid first; "
          "D077 in Step 01; the D075 counterfactual / raw drift re-checks before the gates and the model-filtered drift "
          "test after the allocator; the D054 DSR records re-run with the D076 trial clustering before 07b. Appendix C "
          "gains the RECERT go-live item.")
    + BUL("Ledger: config/us500_edge_search_ledger.json gains RUNBOOK_v5.3_ADDENDUM_2026-11-03_MLAM_CH2_APPA_REV42 (D076, "
          "D077 and the MLAM Ch.3, Ch.5-8 and Appendix A-B outcomes), which had no ledger addendum yet.")
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
