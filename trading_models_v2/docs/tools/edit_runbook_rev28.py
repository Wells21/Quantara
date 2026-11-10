"""Quantara_Runbook.docx Revision 28 (2026-10-31): AFML Ch.19 walkthrough (microstructural features).

Helpers are the Revision 17 editor's (docs/tools/edit_runbook_rev17.py head), as in edit_runbook_rev18.py - rev27.py.

usage: python edit_runbook_rev28.py IN_DOCX OUT_DOCX
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

ns["replace_once"]("Revision date: 31 October 2026 - Revision 27 (Revision 26: 30 October 2026;",
                   "Revision date: 31 October 2026 - Revision 28 (Revision 27: 31 October 2026; Revision 26: 30 October 2026;")
ns["insert_after_para"](
    "Revision 27 (31 October 2026): AFML Chapter 18",
    P("Revision 28 (31 October 2026): AFML Chapter 19 (microstructural features). Adds Section 6A Step 10O (High/Low "
      "estimators: Parkinson and Snippet 19.2 kept, Corwin-Schultz spread removed because our bars are mid quotes; "
      "D071-D073) and Appendix 45 (the Chapter 19 record)."))
ns["insert_before_para"](
    f"6A {DOT} STEP 12B - GATE D: meta-model discrimination",
    H2(f"6A {DOT} STEP 10O - Microstructural features (Rev 28, AFML Ch.19 pp.281-300)")
    + P("shared/features/afml_ch19_microstructure.py; research (feature_expansion.py) and live "
        "(native_one_row_feature_engine v1_7) compute the same two columns at every bar, certified every step by Phase C.2.")
    + BUL("Parkinson_Vol_20: Parkinson (1980) high-low volatility, k1 = 4 log 2 form (p.284), 20-bar window.")
    + BUL("Becker_Parkinson_Vol_1: Snippet 19.2 as printed, sl = 1. It uses k2 = sqrt(8/pi) where Corwin and Schultz eq.(12) "
          "uses sqrt(4 log 2): 1.0435 sigma at zero spread, a constant factor (D073).")
    + BUL("Warm-up rows stay NaN, as in Snippets 19.1-19.2 (D071).")
    + BUL("Corwin_Schultz_Spread_1 is NOT a feature (D072): the estimator assumes bar highs trade at the offer and lows at the "
          "bid (p.284); our bars are OHLC of the mid quote, so it does not measure a spread (37-41 % of bars 0, rank correlation "
          "with the quoted spread -0.03..+0.11). The function stays in the module as the book's code.")
    + BUL("No other Ch.19 feature: the tick rule, Roll, Kyle / Amihud / Hasbrouck lambdas, PIN / VPIN and s.19.6 need trade "
          "prints, traded volume, order-book messages or options, which we do not hold; an observed-spread feature would be "
          "Dukascopy's spread, not the live broker's.")
    + BUL("All edges carry the same 187-entry feature_schema.")
    + WARN("Phase C before the RECERT retrain",
           "Until the RECERT retrain, models that select a removed column (Entropy_Kontoyiannis_100, Corwin_Schultz_Spread_1: "
           "HK dhc, JPN rs, XAU rs, XAU vc) cannot be compiled into Phase C.1; retrain them before any Phase C regeneration.")
    + CODE(["..\\venv39\\Scripts\\python.exe -m unittest tests.test_afml_ch19_microstructure"]))
appendix = (
    H1("45. Appendix: 2026-10-31 AFML Chapter 19 Walkthrough (Revision 28)")
    + BUL("Before: Parkinson_Vol_20, Corwin_Schultz_Spread_1, Becker_Parkinson_Vol_1 in all 6 edges (model inputs in 5). Port = "
          "Snippets 19.1-19.2 as printed to 2.2e-16 on all 10 assets, except warm-up rows set to 0 instead of NaN (bar 0).")
    + BUL("Corwin-Schultz on mid bars (all 10 assets): 37-41 % zeros; Spearman with the quoted spread of the bar -0.03..+0.11 "
          "(-0.01..+0.11 from ask-High / bid-Low); median 0.6x-6.5x the quoted spread; MDA within about 1-2 std of 0. Removed.")
    + BUL("Parkinson (k1) = 0.93-1.06 x close-to-close std on bars, days and weeks (exercise 19.3); Roll on mid ticks finds "
          "0.00-0.38 of the half quoted spread (no bid-ask bounce in mid quotes).")
    + BUL("Book errata: p.287 Kyle alpha printed without its minus (alpha = -p0 sqrt(sigma_u^2 / Sigma0)); p.292 E[VB - VS] "
          "middle line swaps the good- and bad-news terms; Snippet 19.2 k2 vs the paper's sqrt(4 log 2); Snippets 19.1-19.2 use "
          "pd.stats.moments (removed in pandas 0.23) and typeset en-dashes; Becker vs Beckers [1983]; Carlin et al. 2007 / 2005.")
    + BUL("Not applied (data): s.19.3.1-19.3.2, s.19.4, s.19.5, s.19.6, s.19.7. Exercises 19.1-19.12 use E-mini tick data and "
          "FIX tags; 19.2-19.4 run in adapted form (logs/ch19_audit_evidence_20261031.txt).")
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
