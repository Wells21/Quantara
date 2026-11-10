"""Quantara_Runbook.docx Revision 19 (2026-10-26): AFML Ch.11 walkthrough additions.

Helpers are the Revision 17 editor's (docs/tools/edit_runbook_rev17.py head), as in edit_runbook_rev18.py.

usage: python edit_runbook_rev19.py IN_DOCX OUT_DOCX
"""
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
head = (HERE / "edit_runbook_rev17.py").read_text(encoding="utf-8")
head = head[: head.index('replace_once("Revision date: 24 October 2026 - Revision 16')]
ns: dict = {"__name__": "rev17_head"}
src, dst = Path(sys.argv[1]), Path(sys.argv[2])
sys.argv = [sys.argv[0], str(src), str(dst)]
exec(compile(head, "edit_runbook_rev17.py(head)", "exec"), ns)
P, H1, H2, BUL, CODE, WARN = ns["P"], ns["H1"], ns["H2"], ns["BUL"], ns["CODE"], ns["WARN"]
DOT = ns["DOT"]

ns["replace_once"]("Revision date: 26 October 2026 - Revision 18 (Revision 17: 24 October 2026;",
                   "Revision date: 26 October 2026 - Revision 19 (Revision 18: 26 October 2026; Revision 17: 24 October 2026;")
ns["insert_after_para"](
    "Revision 18 (26 October 2026): AFML Chapter 10 (bet sizing).",
    P("Revision 19 (26 October 2026): AFML Chapter 11 (the dangers of backtesting). Gate C PBO now uses every "
      "configuration tried (D051); adds Section 6A Step 7B (backtest discipline) and Appendix 36 (the Chapter 11 record)."))
ns["replace_once"]("PBO/CSCV (AFML Ch.11 SS11.6-11.8): cscv_pbo(), s=16, from shared/backtest_statistics/afml_ch11_pbo_reference.py "
                   "on a DAILY prorated compound-velocity series per candidate",
                   "PBO/CSCV (AFML Ch.11 s.11.6 pp.155-157): cscv_pbo(), s=16, from shared/backtest_statistics/afml_ch11_pbo_reference.py "
                   "on a DAILY prorated compound-velocity series per configuration, over EVERY configuration replayed in the "
                   "Gate C run, controls included (the book's N trials; D051, 2026-10-26; the old positive-only set is "
                   "reported as diagnostic_positive_only)")
ns["insert_before_para"](
    f"6A {DOT} STEP 12B - GATE D: meta-model discrimination",
    H2(f"6A {DOT} STEP 7B - Backtest discipline (Rev 19, AFML Ch.11 pp.151-160)")
    + P("Backtesting is not a research tool; feature importance is (AFML s.11.4 p.153). A backtest discards bad models, it "
        "does not improve them; if it fails, start over (s.11.5 #6, Snippet 11.1). Quantara's Gates A-C still backtest a "
        "405-cell geometry grid before the research steps: recorded as deviation D050, mitigated by the all-trials PBO "
        "(D051), the research trial ledgers and the one-look holdout (Gate E); a theory-first redesign is decided after "
        "Ch.12-14.")
    + BUL("New edges: the thesis card is written and marked provenance pre_registered BEFORE the first backtest; post_hoc "
          "cards are accepted only for cards written on or before 2026-10-26 (edge_yaml_contract; AFML sin 3, p.152).")
    + BUL("Cross-asset consistency (s.11.5 #1 p.154): config/edge_cross_asset_consistency.json reports 'worked on k of n "
          "assets tried' per deployable edge; single-asset edges are judged with the Ch.14 deflated Sharpe ratio.")
    + BUL("PBO gate (D051): PBO < 0.5 AND Prob[OOS <= 0] < 0.5 over all N trials; PBO is a rank statistic, so adding weak "
          "configurations lowers it -- Prob[OOS <= 0] is the more robust of the two and must also pass.")
    + CODE(["python -m unittest tests.test_gate_c_pbo_regression tests.test_thesis_preregistration",
            "python -m shared.research.pbo_all_trials --edge-root assets/<ASSET>/edges/<edge> --stamps <S1,S2> --out <json>",
            "python -m shared.research.cross_asset_consistency"]))
appendix = (
    H1("36. Appendix: 2026-10-26 AFML Chapter 11 Walkthrough (Revision 19)")
    + BUL("Audit: costs, look-ahead, bagging match the book; survivorship (548 deleted edge folders, 461 family-side-asset "
          "attempts), post hoc thesis cards, regime-concentrated returns and N = 2 in the DSR remain open (Ch.12, Ch.14).")
    + BUL("PBO with the book's N (all replayed cells) reproduced the recorded values first, then: GBPJPY 0.454 -> 0.273, HK "
          "0.254 -> 0.093, JPN rs 0.401 -> 0.366, JPN vc 0.507 -> 0.245, XAUUSD rs 0.085, XAUUSD vc 0.305 -> 0.288; Prob[OOS<=0] "
          "unchanged within 0.03. The full 405-cell replay was skipped (adds only Gate-B-negative cells).")
    + BUL("Decisions: D050 (backtest-first Gates A-C, recorded), D051 (book-N PBO is the gate), cross-asset consistency flag, "
          "pre-registered thesis cards for new edges. Citation errors in afml_ch11_pbo_reference.py corrected.")
    + BUL("Book note: C(16, 8) = 12,870 CSCV splits (the book prints 12,780).")
)
x = ns["x"]
i = x.rfind("<w:sectPr")
x = x[:i] + appendix + x[i:]
zin = ns["zin"]
tmp = Path(tempfile.mkdtemp()) / "out.docx"
with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
    for item in zin.infolist():
        data = x.encode("utf-8") if item.filename == "word/document.xml" else zin.read(item.filename)
        zout.writestr(item, data)
shutil.move(str(tmp), dst)
print(f"written {dst}")
