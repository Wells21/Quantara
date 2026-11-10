"""Quantara_Runbook.docx Revision 43 (2026-11-04): RECERT outcome and the edge-level gate architecture.

Helpers as in edit_runbook_rev33.py.

usage: python edit_runbook_rev43.py IN_DOCX OUT_DOCX
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


ns["replace_once"]("Revision date: 3 November 2026 - Revision 42 (Revision 41: 3 November 2026;",
                   "Revision date: 4 November 2026 - Revision 43 (Revision 42: 3 November 2026; Revision 41: 3 November 2026;")
after_para("Revision 42 (3 November 2026): full review of this runbook after the AFML and MLAM walkthrough.",
           P("Revision 43 (4 November 2026): RECERT outcome and the edge-level gate architecture (owner). All 4 edges "
             "failed the D075 theory check at the edge level and were removed (0 deployable). Edge-level gates never "
             "depend on the allocator: the model-filtered drift test is deleted (D075 amended; both theory checks one-sided "
             "95 %); strategy risk (D055) and Gate E (D023) are deployment-stage gates; Gate C stress is a kill gate "
             "(D079); CV leakage is a stop (D080); the label audit is threshold-free (D081); Gate D becomes v4 = F1 "
             "act/pass + calibrated log loss (D078, two details pending the owner). Section 2, Section 6A Steps 7, 10K, "
             "12B-12E, 13, KEY RULES, STATE, Section 16C, Appendix C; Appendix 60."))

edit_para("Current state: Section 6A STATE",
          "the AFML/MLAM walkthrough is complete since 3 November 2026; next: the RECERT, Section 16C).",
          "the AFML/MLAM walkthrough is complete since 3 November 2026; the RECERT of 4 November 2026 removed all 4 edges, "
          "so config/edge_universe.json has 0 edges; next: the edge search under the Revision 43 gates).")

# Section 2
set_para("Rev 33 - Gate 0 is the screen, promotion is the strict bar",
         "Rev 33 / Rev 43 - the theory checks are Gate 0 (before Gate A, the card's pre-registered cell, cross-asset "
         "pooling) and the drift re-check at the locked geometry after Gate C, both one-sided 95 % (owner 2026-11-02; "
         "2026-11-04: 'lets do 1.645 at both'). The model-filtered promotion check is deleted (Rev 43, D075 amended). "
         "Price-only data: leniency only where it adds power or fit, never a weaker bar against drift.")
after_para("Rev 42 - read an allocator or live script in full before changing it.",
           BUL("Rev 43 - edge level before deployment (owner 2026-11-04): an edge enters config/edge_universe.json only "
               "after every edge-level gate passes (Section 6A KEY RULES); no edge-level gate may depend on the allocator "
               "(its final_live_threshold) or on the Step 04 selected_threshold, which is never the live threshold. "
               "Threshold-dependent checks are deployment-stage gates after the allocator: strategy risk (D055) and Gate "
               "E (D023). A deployment failure means the edge is not deployed and returns to research (removed from "
               "edge_universe)."))

# Step 7 [RULE D050-A]
set_para("Promoting an edge to config/edge_universe.json before its model-filtered drift test passes",
         "Promoting an edge to config/edge_universe.json before every edge-level gate passes (Rev 43). No edge-level "
         "gate uses the allocator's final_live_threshold or the Step 04 threshold; the model-filtered drift test (old "
         "D075 item 4) is deleted (owner 2026-11-04). At every RECERT each edge_universe edge re-runs the counterfactuals "
         "and the drift baseline at its locked geometry (one-sided 95 %), pre-registered, before any gate that removes "
         "edges; failures go to the owner.")
set_para("STRESS: re-run the winner at spread x (1 + level_uncertainty_pct)",
         "STRESS (Rev 43, D079 -- a KILL gate): re-run the winner at spread x (1 + level_uncertainty_pct) with "
         "shared/research/gate_c_stress.py --edge-root <edge> --cell H:E:V:PT:SL --base-stamp <Gate C stamp>; exec mean R "
         "<= 0 there = KILL (exit 1; was 'flagged fragile'). Record the break-even spread for the live per-trade guard.")

# Step 10K strategy risk
edit_para("GATE (D055, owner 2026-10-27)",
          "Snippet 15.5's p(1 - p) shortcut contradicts its own algorithm and is only a flagged diagnostic.",
          "Snippet 15.5's p(1 - p) shortcut contradicts its own algorithm and is only a flagged diagnostic. Rev 43: a "
          "DEPLOYMENT-stage gate, computed by allocator 07c on the bets taken at the live threshold (AFML p.216: the bet "
          "frequency is 'set by the portfolio manager, as she decides what constitutes an opportunity worth betting on'); "
          "not an edge-level gate. A failure means the edge is not deployed and returns to research.")

# Step 12B Gate D
set_para("6A · STEP 12B - GATE D: meta-model discrimination",
         "6A · STEP 12B - GATE D v4: the meta-model must act/pass and size better than no skill (Rev 43, D078)")
set_para("AUC-ROC of raw_probability vs quote_executable_meta_label",
         "Rev 43 (D078, docs/mldp/rules/GATE_D_V4_D078.md; AFML s.3.7 pp.52-53, s.14.8 pp.206-207, s.9.4 p.134): on the "
         "Step 03 OOF rows, no Step 04 or allocator threshold. D1 act or pass: F1 of primary + meta (act when "
         "raw_probability > 0.5) minus the primary model's F1 (2b / (1 + b), b = base rate), ISO-week block bootstrap "
         "10,000 draws, seed 20261104; PASS if the 5th percentile > 0. D2 sizing: log loss of the base rate minus log "
         "loss of cross-fitted calibrated probabilities (Platt on stacked CPCV paths, 10 purged + embargoed time blocks; "
         "the live calibrator is unchanged), week-clustered SE; PASS if mean - 1.645 SE > 0. Gate D = D1 AND D2; exit "
         "code IS the verdict. AUC / Mann-Whitney (v3) is a reported diagnostic. OPEN before Gate D v4 gates an edge "
         "(owner, backlog D078): D2 must weight calibrator, base rate and loss consistently (unweighted vs AFML-weighted "
         "give opposite verdicts on the RECERT edges), and the D1 decision point (p > 0.5 acts on 74-100 % of events "
         "because the return-attribution weights correlate with the label). A Gate D fail is a real finding.")

# Step 12C Gate E
edit_para("AFML s.1.3.1.6 (p.8): the strategy is first run on data observed after the end of its backtest",
          "(Rev 29; a FAIL removes the edge, so the allocator and manifest are re-run without it).",
          "(Rev 29; a FAIL removes the edge, so the allocator and manifest are re-run without it). Rev 43: Gate E is a "
          "DEPLOYMENT-stage gate (AFML p.8: the lifecycle starts 'Once a strategy is deployed'; the embargo is its first "
          "stage) and needs the live threshold, so it stays after the allocator; it is not an edge-level gate.")

# Step 12D label audit
set_para("After Step 04 calibration (and again after the allocator sets final_live_threshold), run the label audit.",
         "Rev 43 (D081): at the edge level the label audit is threshold-free -- label population, PT / SL / vertical "
         "shares, cost per R by Event_Vol quintile (minRet evidence, D027), overlap / uniqueness, Step 02 CPCV F1 against "
         "the approve-all baseline (D028); the Step 04 selected_threshold is used nowhere. The primary vs meta judgement "
         "at the edge level is Gate D v4 D1 (D078). After the allocator, label_audit.py --live-threshold adds Exercise "
         "3.5 at final_live_threshold (report only). It is a diagnostic, not a gate.")

# Step 12E CV leakage
set_para("After Step 03, run the CV leakage audit (diagnostic, not a gate)",
         "After Step 03, run the CV leakage audit -- Rev 43 (D080): a formal STOP before Gate D. 10-fold unshuffled vs "
         "shuffled vs purged, and the k = 5/10/20/40 sweep (p.107: purged performance must not keep rising with k). "
         "LEAK_SUSPECT (exit 1) means the purge / embargo is not stopping leakage on this edge: the edge does not "
         "proceed to Gate D until the leak is found and fixed (a new version).")

# Step 13
set_para("PASS (Gate C + PBO + Steps 01-09 + Gate D",
         "PASS = every EDGE-LEVEL gate in the KEY RULES chain, in order (Rev 43): Gate 0, Gates A / B / C, PBO, stress "
         "(D079), the post-Gate C drift re-check (D075), the row floor, Task 2A exact mean, Steps 01-09, CV leakage "
         "(D080), Gate D v4 (D078) and Level-1 DSR / FWER (D054, Step 10J) -- with the threshold-free label audit "
         "(Step 12D) and the CV leakage verdict quoted in the promotion note. Only then promote to "
         "config/edge_universe.json. Strategy risk (D055, Step 10K) and Gate E (Step 12C) are deployment-stage gates "
         "after the allocator:")

# KEY RULES
set_para("Gate 0 (counterfactuals + month-matched drift, one-sided 95 %, D075) -> A gross >= 0.05",
         "EDGE LEVEL (decides config/edge_universe.json): Gate 0 (counterfactuals + month-matched drift, one-sided 95 %, "
         "D075) -> A gross >= 0.05 -> B all net > 0 (fresh grid) -> C exec_mean_R > 0 + compound velocity -> PBO < 0.5 "
         "AND Prob < 0.5 -> stress KILL if exec R <= 0 at spread x (1 + u) (D079) -> drift re-check at the locked "
         "geometry, one-sided 95 % (D075) -> 4-place lock -> export (>= 2000 rows) -> Task 2A (exact mean) -> 01-09 -> CV "
         "leakage OK (D080) -> Gate D v4: D1 F1 AND D2 log loss (D078) -> DSR > 0.95 AND FWER < 0.05 (D054) -> promote. "
         "DEPLOYMENT (after the allocator): allocator 00a-11 (live threshold, weights) -> strategy risk <= .05 at the "
         "live threshold (D055) -> live manifest -> Gate E holdout (D023) -> Phase C / D -> owner go (Rev 43).")

# STATE
edit_para("STATE 2026-11-03",
          "STATE 2026-11-03 (live stopped since 2026-10-23; the AFML/MLAM walkthrough is complete)",
          "STATE 2026-11-04 (live stopped since 2026-10-23; the RECERT ended with 0 deployable edges)")
set_para("Deployable (config/edge_universe.json, 4)",
         "Deployable (config/edge_universe.json): 0. The RECERT of 3-4 November 2026 (logs/recert_20261103/RECERT_LOG.md) "
         "removed GBPJPY/dailyrangecompletionfade_long, HK/drawdownhazardcontinuation_short, JPN/rangestructure_long and "
         "XAUUSD/rangestructure_long: all 4 failed the D075 theory check at their locked geometry (raw events not "
         "distinguishable from same-month random same-side entries; card counterfactuals not shown); JPN also failed Gate "
         "D (AUC 0.507, p 0.218). Owner 2026-11-04: 'remove all four'. Folders kept in place pending owner review (Step "
         "13 archive rule). Earlier removals: XAUUSD/volatilitycarry_long (Gate E FAIL, 2026-10-23, Appendix 25.2) and "
         "JPN/volatilitycarry_long (full 405-cell PBO, 2026-10-26, Appendix 37).")
set_para("Every model predates the walkthrough decisions",
         "The AFML (Ch.1-22) and MLAM (Ch.1-8, Appendices A-B) walkthrough is complete (3 November 2026) and the RECERT "
         "is done. The live manifest (SHA 786689d7..., 19 October 2026, 6 edges) is stale and must not be used; there is "
         "nothing to deploy. Next: the edge search, every new thesis through the Revision 43 edge-level gates (Gate D v4 "
         "after the owner settles backlog D078). Record: docs/mldp/AFML_MLAM_WALKTHROUGH.md and "
         "logs/recert_20261103/RECERT_LOG.md. Earlier removals: Sections 23.5 and 24.")

# 16C RECERT sequence
set_para("Rev 29 - the RECERT (the full list is config/mldp_implementation_backlog.json)",
         "Rev 29 / Rev 43 - the RECERT (the full list is config/mldp_implementation_backlog.json). EDGE LEVEL: (1) Gate "
         "A-C re-selection of every edge on the 405-cell grid with EWM spans 100,150,200 (D074), stress KILL (D079) and "
         "PBO over that run (D051); the run's cells join the edge's trial record (D050-A); lock in all four places; (2) "
         "re-export every edge (187-entry schema; same-asset exports serially); (3) Task 2A, Steps 01-09 on venv312 (Step "
         "01 with the best-of-20 ONC, D077), the CV leakage audit (STOP on LEAK_SUSPECT, D080) and Gate D v4 (D078); (4) "
         "the threshold-free label audit (D081); (5) D075: the counterfactuals and the drift baseline of every edge at its "
         "locked geometry, one-sided 95 %, pre-registered, failures to the owner; (6) Level-1 DSR / FWER (D054; "
         "dsr_all_trials over all the edge's Gate C trials on the denoised, conditionally detoned trial correlation, "
         "D076) -- an edge failing any edge-level gate is removed from config/edge_universe.json now. DEPLOYMENT: (7) the "
         "venv39 pickle scan (D065); (8) allocator 00a-11 on venv39 including 07b_dsr_psr_validation (on the D076 records) "
         "and 07c (strategy risk at the live threshold, D055, a deployment gate; D052 per-path selection, D059 "
         "mark-to-market streams, D010 denoising, D070 report), with D057 reported, D048 re-tested with --approved-only, "
         "the Ch.16 T1-T4 tests re-run (D060) and label_audit.py --live-threshold (D081 report); (9) live manifest "
         "(Section 8) and python portfolio_live/edge_versions.py; (10) Gate E, one look per new version (Step 12C, a "
         "deployment gate; a FAIL removes the edge and the allocator is re-run); (11) Sections 9-18 with Phase C rebuilt "
         "(Section 11B); (12) live redeploy only on the owner's go. RECERT of 3-4 November 2026: stopped after step 5 -- "
         "all 4 edges failed D075 (JPN also Gate D) and were removed; 0 deployable (logs/recert_20261103/RECERT_LOG.md).")

# Appendix C
after_para("[ ] Rev 42: after the AFML/MLAM walkthrough, the RECERT (Section 16C) is complete",
           P("[ ] Rev 43: every edge in the live manifest passed every edge-level gate (Section 6A KEY RULES) before it "
             "entered config/edge_universe.json, then strategy risk (D055) and Gate E (D023) after the allocator."))

appendix = (
    H1("60. Appendix: 2026-11-04 RECERT Outcome and the Edge-Level Gate Architecture (Revision 43)")
    + BUL("RECERT 3-4 November 2026 (logs/recert_20261103/RECERT_LOG.md):\n"
          "  - Step 1: re-selection on the D074 grid, reusing the 2026-10-31 full replays; GBPJPY corrected for its "
          "feature window. New locks: GBPJPY H8_E200_V168, HK H8_E150_V168, XAUUSD H8_E200_V16; JPN unchanged.\n"
          "  - Steps 2-3: re-export, Task 2A exact, Steps 01-09 complete. Gate D v3: GBPJPY, HK, XAUUSD pass; JPN fails.\n"
          "  - Step 5: D075 fails on all 4.\n"
          "  - Owner: 'remove all four'. config/edge_universe.json has 0 edges.")
    + BUL("Architecture (owner 2026-11-04):\n"
          "  - Edge-level gates decide entry to edge_universe and never depend on the allocator or the Step 04 "
          "threshold.\n"
          "  - D075 amended: the model-filtered drift test is deleted (it gave the raw verdict on all 6 edges on "
          "2026-11-02); Gate 0 and the post-Gate C re-check are both one-sided 1.645.\n"
          "  - Strategy risk (D055, AFML p.216) and Gate E (D023, AFML p.8) stay after the allocator as deployment gates.")
    + BUL("New and changed gates:\n"
          "  - D079: Gate C stress is a KILL gate (shared/research/gate_c_stress.py).\n"
          "  - D080: CV leakage LEAK_SUSPECT is a stop (cv_leakage_audit.py exit 1).\n"
          "  - D081: the label audit is threshold-free at the edge level (--live-threshold report after the allocator).\n"
          "  - D078: Gate D v4 = D1 (F1 act/pass, week-block bootstrap) AND D2 (cross-fitted calibrated log loss), "
          "docs/mldp/rules/GATE_D_V4_D078.md; AUC is a diagnostic.")
    + BUL("Open before Gate D v4 gates an edge (backlog D078, owner):\n"
          "  - D2's weighting must be consistent. All-unweighted fails all 4 RECERT edges; all-weighted passes them; the "
          "first code mixed the two.\n"
          "  - D1's decision point: p > 0.5 acts on 74-100 % of events, because the return-attribution weights correlate "
          "with the label.\n"
          "  - On the 4 removed edges Gate D v4 failed D1 on every edge.")
    + BUL("Updated everywhere:\n"
          "  - docs/mldp/rules/THEORY_CHECK_RULE_D075.md and GATE_D_V4_D078.md.\n"
          "  - config/mldp_deviation_register.json (D075, D023, D055 amended; D078-D081 new), "
          "config/mldp_implementation_backlog.json, config/research_methodology_standing_rules.json.\n"
          "  - Ledger addendum RUNBOOK_v5.3_ADDENDUM_2026-11-04_RECERT_GATE_ARCHITECTURE_REV43.\n"
          "  - shared/edges/gate_d_calibration_monotonicity.py (v4) with tests/test_gate_d_v4.py; label_audit.py, "
          "cv_leakage_audit.py; run_drift_filtered.py marked retired.")
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
