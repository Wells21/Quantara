"""Quantara_Runbook.docx Revision 29 (2026-10-31): full review after AFML Ch.1-22 (Ch.20-22 closed with no change).

Helpers are the Revision 17 editor's (docs/tools/edit_runbook_rev17.py head), as in edit_runbook_rev18.py - rev28.py, plus
para_text / set_para / sub_para, which find ONE paragraph by its plain (unescaped) text and rewrite its single text run.

usage: python edit_runbook_rev29.py IN_DOCX OUT_DOCX
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
    """The one paragraph whose plain text contains key -> (start, end, xml)."""
    hits = [m for m in PARA.finditer(ns["x"]) if key in html.unescape("".join(t[1] for t in TEXT.findall(m.group(0))))]
    if len(hits) != 1:
        raise SystemExit(f"paragraph count {len(hits)} for: {key[:80]}")
    m = hits[0]
    if len(TEXT.findall(m.group(0))) != 1:
        raise SystemExit(f"paragraph is not a single run: {key[:80]}")
    return m.start(), m.end(), m.group(0)


def para_text(key):
    return html.unescape(TEXT.search(_find(key)[2]).group(2))


def set_para(key, new_text):
    s, e, p = _find(key)
    p2 = TEXT.sub(lambda t: t.group(1) + escape(new_text) + t.group(3), p, count=1)
    ns["x"] = ns["x"][:s] + p2 + ns["x"][e:]


def sub_para(key, old, new):
    t = para_text(key)
    if t.count(old) != 1:
        raise SystemExit(f"'{old[:60]}' occurs {t.count(old)} times in: {key[:60]}")
    set_para(key, t.replace(old, new))


def after_para(key, xml):
    e = _find(key)[1]
    ns["x"] = ns["x"][:e] + xml + ns["x"][e:]


# -- header ----------------------------------------------------------------------------------------------------------
ns["replace_once"]("Revision date: 31 October 2026 - Revision 28 (Revision 27: 31 October 2026;",
                   "Revision date: 31 October 2026 - Revision 29 (Revision 28: 31 October 2026; Revision 27: 31 October 2026;")
sub_para("Repository basis: Wells21/trading_models, branch v2", "branch v2,",
         "branch v3 (the current working branch; master is the main branch),")
sub_para("Status: corrected against the current repository implementation",
         "required new-account runtime-state archive procedure.",
         "required new-account runtime-state archive procedure. Current state: Section 6A STATE (live trading stopped since "
         "23 October 2026 for the AFML/MLAM walkthrough).")
after_para("Revision 28 (31 October 2026): AFML Chapter 19",
           P("Revision 29 (31 October 2026): full review of this runbook after AFML Chapters 1-22 (Chapters 20-22 closed with "
             "no change). Adds the owner's standing rules (Section 2), the allocator scripts 07b_dsr_psr_validation.py and "
             "07c_backtest_statistics_validation.py that were missing from Section 7, the venv39 pickle scan before Phase C "
             "(11B.2), the known pre-RECERT test failures (12.3), the RECERT sequence (16C) and Appendix 46; corrects the "
             "branch, the Gate E order (it needs the live manifest), Steps 7B / 10I after D050-A, the feature-schema counts, "
             "the Step 6 schema-validator note, the lifecycle driver path and timing, the Step 13 and KEY RULES gates and the "
             "Section 6A STATE."))

# -- Section 2: owner standing rules ---------------------------------------------------------------------------------
after_para("Treat the exact full unittest discovery used by C5 as a mandatory pre-C5 gate.",
           BUL("Rev 29 - book by default: AFML and MLAM are followed as printed; where MLAM explicitly revises an AFML method, "
               "MLAM is followed in full (owner, 2026-10-24). A deviation is proposed first (book page, what changes, evidence "
               "from our data) and built only after the owner approves; it is registered in config/mldp_deviation_register.json.")
           + BUL("Rev 29 - nothing approved is forgotten: approved work that is not yet in effect stays in "
                 "config/mldp_implementation_backlog.json until done (tests/test_mldp_backlog.py fails if an open register item "
                 "is missing from it).")
           + BUL("Rev 29 - live_settings.py, local_live_settings.json and .gitignore are changed only with the owner's prior "
                 "approval (say exactly what would change).")
           + BUL("Rev 29 - never touch, kill or reprioritise the live bot / dashboard processes; research runs at BelowNormal "
                 "priority, independent jobs in parallel when CPU allows; stale research processes may be stopped.")
           + BUL("Rev 29 - commit and push only when the owner says so; never copy credentials into Git; archive, never "
                 "hard-delete; no package version pins. The sealed holdout (assets/<ASSET>/holdout) is read only by Gate E."))

# -- Section 6A ------------------------------------------------------------------------------------------------------
sub_para("It replaces the separate Google Keep note. The identical text is stored in",
         "The identical text is stored in config/us500_edge_search_ledger.json -> RUNBOOK_v5.3_2026-10-22 and "
         "trading_models_v2/docs/RUNBOOK_v5.3_keep_note.txt (19,510 characters, fits one Keep note).",
         "Its 2026-10-22 base text is also stored in config/us500_edge_search_ledger.json -> RUNBOOK_v5.3_2026-10-22 and "
         "trading_models_v2/docs/RUNBOOK_v5.3_keep_note.txt; every later change is in this section and in the ledger's "
         "RUNBOOK_v5.3_ADDENDUM_* entries (Rev 29). This section is authoritative.")
ns["replace_once"]("pandas 3 breaks allocator 04bb.", "pandas 3 breaks allocator 04bb and 07 (clean_corr).")
set_para("model_input_schema.json missing = EXPECTED (Step 04B creates it).",
         "model_input_schema.json missing = EXPECTED (Step 04B creates it). Rev 29: validate_edge_feature_schema.py exits 1 on "
         "every edge and prints only its first failure (SADF_ct_50): 24 schema entries (SADF, QADF, CADF, CSW, SMT, the 3 "
         "entropy columns, Parkinson_Vol_20, Becker_Parkinson_Vol_1) declare computed_by feature_expansion.py::"
         "add_v1_feature_expansion while the validator requires runtime.py::build_features (5 such entries since 2026-09-11, "
         "19 added in the Ch.17-19 walkthrough with the same convention). That line is known; any other failure is real. "
         "Until the RECERT retrain, edge_contracts.py also fails on HK dhc, JPN rs, XAU rs and XAU vc (their models select "
         "columns removed in Ch.18-19).")
sub_para("PBO/CSCV (AFML Ch.11 s.11.6 pp.155-157): cscv_pbo()",
         "(JPN volatilitycarry PBO 0.507 -> Gate D PASS, live).",
         "(JPN volatilitycarry PBO 0.507 -> Gate D PASS, live; later KILLED by the full 405-cell PBO, Prob[OOS<=0] 0.527, "
         "2026-10-26, Appendix 37).")
sub_para("Driver: bash run_edge_lifecycle_01_09.sh", "(repo root)", "(in trading_models_v2)")
sub_para("Driver: bash run_edge_lifecycle_01_09.sh", "~20-25 min/edge",
         "~20-25 min/edge as measured 2026-09-25, before D034 / D044 / D046 / D052 (1,000 trees, the MLAM Step 01, "
         "CPCV(10, 2)): re-measure at the RECERT")
sub_para("Backtesting is not a research tool; feature importance is",
         "a theory-first redesign is decided after Ch.12-14.",
         "the redesign was decided on 2026-10-29: GEOMETRY RULE D050-A (Step 7) -- search the 405 cells and pay for every trial.")
sub_para("AFML s.13.4-13.5: fit a discrete O-U process",
         "Result: NO OTR; PT/SL/vertical are NOT tuned on history; the rule is fixed by thesis / risk and pre-registered "
         "(feeds D050 after Ch.14);",
         "Result: NO OTR. D053 also proposed not tuning PT/SL/vertical on history and fixing the rule by thesis / risk; the "
         "geometry decision was then taken by D050-A on 2026-10-29 (Step 7, Rev 29): the 405-cell grid IS searched and every "
         "trial is paid for. D053 itself stands for 'no optimal trading rule from synthetic data';")
sub_para("All edges carry the same 186-entry feature_schema", "186-entry feature_schema",
         "186-entry feature_schema at Revision 26 (187 now, Step 10O)")
sub_para("All edges carry the same 188-entry feature_schema", "188-entry feature_schema",
         "188-entry feature_schema at Revision 27 (187 now, Step 10O)")
sub_para("Until the RECERT retrain, models that select a removed column (Entropy_Kontoyiannis_100, Corwin_Schultz_Spread_1",
         "retrain them before any Phase C regeneration.",
         "retrain them before any Phase C regeneration. Until then Step 6's edge_contracts.py also fails on those edges and the "
         "full unittest discovery (Section 12.3) has 8 failures / errors for the same reason.")
gate_e = para_text("AFML s.1.3.1.6 (p.8): the strategy is first run on data observed after the end of its backtest")
old_e = "Gate E runs after Gate D + PBO and before the allocator, for every new edge and for every re-certified version."
if gate_e.count(old_e) != 1:
    raise SystemExit("Gate E sentence not found")
set_para("AFML s.1.3.1.6 (p.8): the strategy is first run on data observed after the end of its backtest",
         gate_e.replace(old_e, "Gate E runs after Gate D + PBO and after the allocator run and live-manifest build "
                        "(Sections 7-8) -- holdout_replay.py reads each edge's final_live_threshold from "
                        "portfolio_live/live_manifest.json and stops if the edge is not there -- and before Phase C / go-live, "
                        "for every new edge and every re-certified version (Rev 29; a FAIL removes the edge, so the allocator "
                        "and manifest are re-run without it)."))
set_para("PASS (Gate C + PBO + Steps 01-09 + Gate D):",
         "PASS (Gate C + PBO + Steps 01-09 + Gate D; Rev 29: then Level-1 DSR / FWER (D054, Step 10J) and strategy risk on "
         "bets (D055, Step 10K) -- gates at the RECERT and for every new edge -- with the label audit and CV leakage audit "
         "(Steps 12D / 12E) quoted in the promotion note; Gate E follows the allocator, Step 12C):")
sub_para("A gross >= 0.05 -> B all net > 0 (fresh grid)", "-> D (AUC, MWU p < 0.05) -> promote.",
         "-> D (AUC, MWU p < 0.05) -> DSR > 0.95 AND FWER < 0.05 (D054) -> strategy risk <= .05 (D055) -> promote -> "
         "allocator -> Gate E (Rev 29).")
after_para("Live code uses live_calibrated_probability, never the fold-averaged calibrated_probability (D015).",
           BUL("Book by default; deviations proposed with evidence and approved first; MLAM over AFML where it revises; "
               "approved work tracked in config/mldp_implementation_backlog.json (Section 2, Rev 29)."))
set_para("STATE 2026-10-22 (Moneta, live since 2026-10-19)",
         f"6A {ns['DOT']} STATE 2026-10-31 (live stopped since 2026-10-23 for the AFML/MLAM walkthrough)")
set_para("Live (6): XAUUSD/rangestructure_long, XAUUSD/volatilitycarry_long",
         "Deployable (config/edge_universe.json, 4): GBPJPY/dailyrangecompletionfade_long, JPN/rangestructure_long, "
         "HK/drawdownhazardcontinuation_short, XAUUSD/rangestructure_long. Removed: XAUUSD/volatilitycarry_long (Gate E FAIL, "
         "2026-10-23, Appendix 25.2) and JPN/volatilitycarry_long (full 405-cell PBO, Prob[OOS<=0] 0.527, 2026-10-26, "
         "Appendix 37); both folders kept.")
set_para("Removed edges and reasons: docx Sections 23.5 and 24.",
         "Every model predates the walkthrough decisions: all edges are re-exported (187-entry schema) and retrained at the "
         "RECERT before any go-live (Section 16C); until then HK dhc, JPN rs, XAU rs and XAU vc cannot be compiled into Phase "
         "C.1, and the live manifest (SHA 786689d7..., 19 October 2026, 6 edges) is stale. AFML Ch.1-22 walked through by "
         "2026-10-31; MLAM Ch.1, Ch.5 and Appendix A remain. Record: docs/mldp/AFML_MLAM_WALKTHROUGH.md. Earlier removals: "
         "Sections 23.5 and 24.")

# -- Section 7: missing allocator scripts ----------------------------------------------------------------------------
line = r'python .\portfolio_allocator\07b_validate_geometry_handoff.py</w:t>'
ns["replace_once"](line, line + '<w:br/><w:t xml:space="preserve">python .\\portfolio_allocator\\07b_dsr_psr_validation.py</w:t>'
                   '<w:br/><w:t xml:space="preserve">python .\\portfolio_allocator\\07c_backtest_statistics_validation.py</w:t>')
x = ns["x"]
i = x.index("07c_backtest_statistics_validation.py</w:t>")
e = x.index("</w:tbl>", i) + len("</w:tbl>")  # the first table end after the 07c line closes the Section 7 code block
ns["x"] = x[:e] + P(
    "Rev 29: 07b_dsr_psr_validation.py (Step 10J: all-trials PSR / DSR / FWER; reads every edge's "
    "artifacts/feature_importance_audit/ch14_dsr_all_trials.json and fails closed without it) and "
    "07c_backtest_statistics_validation.py (Steps 10J-10K: AFML Ch.14-15 statistics and strategy risk) were missing from "
    "this list; both run after 07b_validate_geometry_handoff.py with their defaults. 00c_capture_broker_spread_profile.py is "
    "the superseded 2026-10-16 spread-profile step: nothing reads its output; use 00c_build_broker_cost_model.py.") + x[e:]

# -- Sections 11B.2, 12.3 --------------------------------------------------------------------------------------------
after_para("Every stage must PASS. C1/C2/C3 are current-universe numerical/consumer authorities",
           P("Rev 29 (D065): after any venv312 run that wrote pickles (the retrain), run "
             "..\\venv39\\Scripts\\python.exe docs\\tools\\scan_pickles_loadable.py before phase_c_01; every artifact must "
             "load on venv39."))
after_para('python -m unittest discover -s .\\tests -p "test_*.py" -v',
           P("Rev 29: known failures until the RECERT retrain -- 8 tests that compile the current live manifest into Phase C "
             "(test_phase_c_feature_contract 1, test_phase_c4_production_router 2, test_phase_c5_production_certification 2, "
             "test_tick_curation 3 errors) fail because 4 models select columns removed in Ch.18-19. Separately, "
             "test_live_bar_memory_authority's periodic-threshold test depends on the date (its fixture day 2026-07-02 is "
             "older than ADAPTIVE_THRESHOLD_MAX_DAY_AGE_DAYS). C5 cannot seal while any test fails: clear both before C5, "
             "and never weaken a test to pass."))

# -- Section 16C: the RECERT sequence --------------------------------------------------------------------------------
set_para("edge.yaml files changed (thesis blocks) and Gate E removed XAUUSD/volatilitycarry_long",
         "Rev 29 - the RECERT (the full list is config/mldp_implementation_backlog.json): (1) re-export every edge (187-entry "
         "schema; same-asset exports serially); (2) Task 2A, Steps 01-09 on venv312 and Gate D; (3) label audit and CV leakage "
         "audit (Steps 12D / 12E); (4) the RECERT gates: Level-1 DSR / FWER (D054) and strategy risk on bets (D055) -- edges "
         "are removed from config/edge_universe.json only now -- with D057 reported, D048 re-tested with --approved-only and "
         "the Ch.16 T1-T4 tests re-run (D060); (5) the venv39 pickle scan (D065); (6) allocator 00a-11 on venv39 including "
         "07b_dsr_psr_validation and 07c (D052 per-path selection, D059 mark-to-market streams, D010 denoising, D070 report); "
         "(7) live manifest (Section 8) and python portfolio_live/edge_versions.py; (8) Gate E, one look per new version "
         "(Step 12C; a FAIL removes the edge and the allocator is re-run); (9) Sections 9-18 with Phase C rebuilt (Section 11B: "
         "the selected features changed); (10) live redeploy only on the owner's go. Deployable now: 4 edges "
         "(XAUUSD/volatilitycarry_long removed by Gate E on 2026-10-23, JPN/volatilitycarry_long by the full 405-cell PBO on "
         "2026-10-26).")

# -- Appendix 46 -----------------------------------------------------------------------------------------------------
appendix = (
    H1("46. Appendix: 2026-10-31 AFML Chapters 20-22 and the Revision 29 Review")
    + BUL("Ch.20 (multiprocessing and vectorization, pp.303-317): no change (owner). Quantara's process pools follow the "
          "chapter's rules (serial path, ordered results, fine-grained tasks) except allocator 08 / 09, which return rows in "
          "completion order: no decision depends on it, run-CSV row order varies, and 09's disabled bootstrap would be "
          "order-dependent. Step 01 serial = parallel byte-identical. Book errata: Snippet 20.3 records the last barrier "
          "touch, 20.4 processes 10 % of its paths, 20.7 uses an undefined argList. logs/ch20_audit_evidence_20261031.txt.")
    + BUL("Ch.21 (brute force and quantum computers, pp.319-328): no change. Quantara has no trajectory optimiser; its grids "
          "are enumerated in full and paid for (D050-A). Snippet checks: Omega holds duplicates (3 assets: 42 of 80), the "
          "'global' optimum is global over the 1/k grid only. logs/ch21_audit_evidence_20261031.txt.")
    + BUL("Ch.22 (high-performance computational intelligence, pp.329-352): no change. VPIN, the fragmentation HHI and the "
          "non-uniform FFT need trade data; our CSV ticks read ~190x slower than a binary copy, but a full pass over one "
          "asset is ~5 minutes against hours of CPCV and Step 01 audits. logs/ch22_audit_evidence_20261031.txt.")
    + BUL("Revision 29 review: every paragraph of Revision 28 re-read against the code, config/mldp_deviation_register.json "
          "and config/mldp_implementation_backlog.json; the corrections are listed in the Revision 29 line at the top.")
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
