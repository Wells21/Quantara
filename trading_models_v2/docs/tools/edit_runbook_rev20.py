"""Quantara_Runbook.docx Revision 20 (2026-10-26): AFML Ch.12 walkthrough additions.

Helpers are the Revision 17 editor's (docs/tools/edit_runbook_rev17.py head), as in edit_runbook_rev18.py / rev19.py.

usage: python edit_runbook_rev20.py IN_DOCX OUT_DOCX
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

ns["replace_once"]("Revision date: 26 October 2026 - Revision 19 (Revision 18: 26 October 2026;",
                   "Revision date: 26 October 2026 - Revision 20 (Revision 19: 26 October 2026; Revision 18: 26 October 2026;")
ns["insert_after_para"](
    "Revision 19 (26 October 2026): AFML Chapter 11",
    P("Revision 20 (26 October 2026): AFML Chapter 12 (backtesting through cross-validation). Adds Section 6A Step 10H "
      "(CPCV backtest paths, D052) and Appendix 37 (the Chapter 12 record, the full 405-cell PBO and the removal of "
      "JPN/volatilitycarry_long)."))
ns["insert_before_para"](
    f"6A {DOT} STEP 12B - GATE D: meta-model discrimination",
    H2(f"6A {DOT} STEP 10H - CPCV backtest paths (Rev 20, AFML Ch.12 pp.161-168)")
    + P("AFML s.12.4.2 step 5: CPCV assembles phi[N, k] = (k/N) C(N, N-k) complete out-of-sample backtest paths, one "
        "Sharpe ratio per path -> a distribution, not one number. D052 (owner, 2026-10-26; implemented in the retrain "
        "batch): Step 03 standard = CombinatorialPurgedCV N = 10, k = 2 (45 splits, 9 paths, training share 0.8), every "
        "path's predictions saved (no averaging into one series); Step 04 fits the calibrator on all path predictions "
        "together; the portfolio allocator scores every threshold of the 04b grid on every path and 04c picks by the "
        "MEAN across paths (s.12.5), reporting the worst path and the share of losing paths; the path distribution feeds "
        "Ch.14 PSR / DSR.")
    + BUL("Where the live threshold is chosen (verified in code): 04b builds the 0.01-step threshold grid on the live "
          "calibrator's probabilities (build_live_threshold_sweep); 04c select_representatives_for_edge picks one threshold "
          "per edge by broker-optimised drawdown-adjusted compound velocity (gated); 05-11 carry it; "
          "portfolio_live/00_build_live_deployment_manifest.py copies it into final_live_threshold.")
    + BUL("A Ch.12 test fits with the current Step 03 code; its probabilities do not match a calibrator / threshold fitted "
          "on older OOF -- refit calibration and thresholds before comparing levels.")
    + CODE(["python -m shared.research.cpcv_path_backtest --edge <ASSET>/<edge> --out <json>"]))
appendix = (
    H1("37. Appendix: 2026-10-26 AFML Chapter 12 Walkthrough (Revision 20)")
    + BUL("Finding: Step 03 ran CPCV(5, 2) (4 paths) but averaged each row's 4 forecasts into one series used by "
          "calibration, thresholds, the allocator and Gate E -- a single path.")
    + BUL("Path test (9 paths, daily Sharpe mean / sd / worst / share losing): JPN rs 0.74 / 0.01 / 0.71 / 0, XAUUSD rs "
          "1.58 / 0.04 / 1.52 / 0, GBPJPY 0.67 / 0.06 / 0.61 / 0, XAUUSD vc 0.90 / 0.07 / 0.76 / 0, HK 0.52 / 0.17 / 0.20 / "
          "0, JPN vc 0.06 / 0.34 / -0.62 / 33 %.")
    + BUL("Full 405-cell Gate C replay (D051, one fresh run per edge; JPN after a fresh Gate A on the re-exported data): "
          "HK PBO 0.013 / Prob 0.338, GBPJPY 0.153 / 0.430, JPN rs 0.349 / 0.400, XAUUSD rs 0.085 / 0.058, XAUUSD vc 0.288 / "
          "0.171 -> PASS; JPN vc 0.119 / 0.527 -> KILLED_PBO, removed from config/edge_universe.json 2026-10-26 (folder kept).")
    + BUL("Deployable edges now: GBPJPY dailyrangecompletionfade_long, JPN rangestructure_long, HK "
          "drawdownhazardcontinuation_short, XAUUSD rangestructure_long.")
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
