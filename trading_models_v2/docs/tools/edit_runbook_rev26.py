"""Quantara_Runbook.docx Revision 26 (2026-10-30): AFML Ch.17 walkthrough (structural breaks) and the pandas-3 pickle fix.

Helpers are the Revision 17 editor's (docs/tools/edit_runbook_rev17.py head), as in edit_runbook_rev18.py - rev25.py.

usage: python edit_runbook_rev26.py IN_DOCX OUT_DOCX
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

ns["replace_once"]("Revision date: 30 October 2026 - Revision 25 (Revision 24: 29 October 2026;",
                   "Revision date: 30 October 2026 - Revision 26 (Revision 25: 30 October 2026; Revision 24: 29 October 2026;")
ns["insert_after_para"](
    "Revision 25 (30 October 2026): AFML Chapter 16",
    P("Revision 26 (30 October 2026): AFML Chapter 17 (structural breaks). Adds Section 6A Step 10M (structural-break "
      "features: SADF every bar, QADF, CADF, CSW, SMT; D061-D064) and the venv39 pickle rule (D065), and Appendix 43 "
      "(the Chapter 17 record)."))
ns["insert_before_para"](
    f"6A {DOT} STEP 12B - GATE D: meta-model discrimination",
    H2(f"6A {DOT} STEP 10M - Structural-break features (Rev 26, AFML Ch.17 pp.249-262)")
    + P("shared/features/afml_ch17_structural_breaks.structural_break_features computes 19 columns at EVERY bar on the 50 "
        "closes ending at that bar, identically in research (feature_expansion.py) and live (native_one_row_feature_engine). "
        "Phase C.2 certifies live against the research batch and against this research rule (oracle) at every step.")
    + BUL("SADF_ct_50 (s.17.4.2; Snippets 17.1-17.4, 'ct', 1 lag, minSL 10) -- the former stride-10 forward-fill made "
          "research 0-9 bars stale and live always 9 (D061). The 50-bar window instead of the book's whole-sample start "
          "points is a registered deviation (D062).")
    + BUL("QADF / CADF (s.17.4.2.4-17.4.2.5; q 0.95, v 0.025) from the same ADF set (D063); CSW up / down (s.17.3.2; down = "
          "on log 1/y, exercise 17.5) and SMT Poly1 / Poly2 / Exp / Power x phi 0 / 0.5 / 1 (s.17.4.3) (D064). BDE not "
          "used (no regressors defined for a price series; reduces to CSW).")
    + BUL("All edges carry the same 186-entry feature_schema; Step 01 decides which columns enter each model.")
    + BUL("venv39 rule (D065): pandas-3 pickles do not load on pandas 2. FFD / HMM bundles and live continuation states "
          "are written with shared/io/portable_pickle.py; after any venv312 run that writes pickles (the retrain), run the "
          "scan below on venv39 before Phase C.")
    + CODE(["..\\venv39\\Scripts\\python.exe -m pytest tests/test_afml_ch17_sadf.py tests/test_portable_pickle.py",
            "..\\venv39\\Scripts\\python.exe docs\\tools\\scan_pickles_loadable.py"]))
appendix = (
    H1("43. Appendix: 2026-10-30 AFML Chapter 17 Walkthrough (Revision 26)")
    + BUL("SADF skew before the fix: research = live on 10.0-10.5 % of model rows, correlation 0.43-0.46 (HK dhc, JPN rs, "
          "XAU rs, XAU vc); SADF_ct_50 carried no importance (MDA within one std of 0, MDI = 1/157).")
    + BUL("Book checks: Snippets 17.1-17.4 run verbatim = the port on every non-degenerate case; Table 17.1 and the FLOP "
          "totals reproduce exactly (tau = 1,000; the full total is sum g(N, t, tau)); exercise 17.5: ADF on log(1/y) = "
          "ADF on log y exactly. Errata: H1 delta > 1 (should be > 0), explosive case alpha/beta (should be -alpha/beta), "
          "'nc' adds no constant in the code.")
    + BUL("Pickle fix (D065): 31 of 72 artifacts unreadable on venv39 before (10 FFD bundles, 10 HMM bundles, 11 live "
          "states); after: 0 of 76, fingerprints identical, Phase C.2 PASS on venv39 for GBPJPY / HK / JPN / XAUUSD. "
          "Originals archived at ../archive_pandas3_pickles_20261030.")
    + BUL("Not reproduced: Figures 17.1-17.3 and exercises 17.1-17.4 (E-mini S&P 500 dollar bars, 2000-2017; not held).")
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
