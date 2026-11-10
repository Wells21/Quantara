"""Quantara_Runbook.docx Revision 21 (2026-10-26): AFML Ch.13 walkthrough + D052 implementation record.

Helpers are the Revision 17 editor's (docs/tools/edit_runbook_rev17.py head), as in edit_runbook_rev18.py - rev20.py.

usage: python edit_runbook_rev21.py IN_DOCX OUT_DOCX
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

ns["replace_once"]("Revision date: 26 October 2026 - Revision 20 (Revision 19: 26 October 2026;",
                   "Revision date: 26 October 2026 - Revision 21 (Revision 20: 26 October 2026; Revision 19: 26 October 2026;")
ns["insert_after_para"](
    "Revision 20 (26 October 2026): AFML Chapter 12",
    P("Revision 21 (26 October 2026): AFML Chapter 13 (backtesting on synthetic data) and the D052 implementation. Adds "
      "Section 6A Step 10I (optimal trading rule test, D053) and Appendix 38 (the Chapter 13 record and the D052 code)."))
ns["insert_before_para"](
    f"6A {DOT} STEP 12B - GATE D: meta-model discrimination",
    H2(f"6A {DOT} STEP 10I - Optimal trading rule on synthetic data (Rev 21, AFML Ch.13 pp.169-193)")
    + P("AFML s.13.4-13.5: fit a discrete O-U process to the in-trade price paths (eq. 13.2, OLS eq. 13.5-13.7), simulate "
        "100,000 synthetic paths per PT/SL node (Snippet 13.2) and read the Sharpe surface. s.13.6 (pp.178, 180, 187): when "
        "phi -> 1 (random walk) the surface is flat, 'there is no optimal trading rule', and optimising PT/SL on history is "
        "backtest overfitting. D053 (owner, 2026-10-26): tested on all 4 deployable edges, both populations -- phi ~ 1 "
        "inside the random-walk null, flat surface (best - median 0.02-0.06), optima on the mesh corners; the per-trade "
        "forecast E0 from p (s.10.6) closes only 2-11 % of the gap. Result: NO OTR; PT/SL/vertical are NOT tuned on history; "
        "the rule is fixed by thesis / risk and pre-registered (feeds D050 after Ch.14); edge must come from entry "
        "information. Footnote 2 p.172: a fixed rule adds no overfitting; the strategy is still judged by PBO (D051) and "
        "DSR (Ch.14).")
    + BUL("Fit the FULL price path from entry to the vertical deadline, never the path truncated at our own barriers "
          "(truncation fakes mean reversion), and always compare phi with a random-walk null of the same path lengths.")
    + CODE(["python -m shared.research.ch13_optimal_trading_rule --edge <ASSET>/<edge>"]))
appendix = (
    H1("38. Appendix: 2026-10-26 AFML Chapter 13 Walkthrough and D052 Implementation (Revision 21)")
    + BUL("OTR v2 (approved trades): GBPJPY phi 0.9999 (null 0.9998-1.0002), HK 0.9998, JPN rs 1.0007, XAUUSD rs 0.9996; "
          "half-lives 1,900-8,300 bars vs holding periods 41-312; current 2:1 rule at the 21st-55th percentile of a flat "
          "surface; the O-U model under-predicts the realised Sharpe. All primary events: same picture. D053: no OTR, "
          "geometry unchanged, not tuned on history.")
    + BUL("D052 code done (takes effect at the retrain + allocator re-run): Step 03 CPCV(10, 2) with 9 saved paths "
          "(cpcv_path_XX_raw_probability); Step 04 live calibrator on all paths stacked (fail closed); 04b per-path trade "
          "ledger v2_threshold_grid_cpcv_path_trade_ledger.csv; 04c selection score = mean across paths of the "
          "drawdown-adjusted broker compound velocity at the main path's optimised f* (owner confirmed 2026-10-26: f* "
          "once on the main path, gates on the main path), worst path / losing share reported; "
          "--allow-legacy-single-path only for pre-D052 artifacts. Tests: tests/test_afml_ch12_cpcv_paths.py.")
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
