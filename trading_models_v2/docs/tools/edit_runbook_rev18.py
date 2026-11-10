"""Quantara_Runbook.docx Revision 18 (2026-10-26): AFML Ch.10 walkthrough additions.

Helpers (run, H1, H2, P, BUL, CODE, BOX, WARN, insert_*, replace_once) are the Revision 17 editor's
(docs/tools/edit_runbook_rev17.py), imported from its head so both stay identical.

usage: python edit_runbook_rev18.py IN_DOCX OUT_DOCX
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

ns["replace_once"]("Revision date: 24 October 2026 - Revision 17 (Revision 16: 24 October 2026;",
                   "Revision date: 26 October 2026 - Revision 18 (Revision 17: 24 October 2026; Revision 16: 24 October 2026;")
ns["insert_after_para"](
    "Revision 17 (24 October 2026): AFML Chapter 9.",
    P("Revision 18 (26 October 2026): AFML Chapter 10 (bet sizing). Adds Section 6A Step 10F (the sizing stack and the "
      "Chapter 10 tests) and Appendix 35 (the Chapter 10 record)."))
ns["replace_once"]("Sizing = AFML 10.3 size x 10.2 budgeting per trade (risk / c_q concurrency quantile); no 10.4/10.5 (D017).",
                   "Sizing = AFML 10.3 size x 10.2 budgeting per trade (risk / c_q concurrency quantile); no 10.4/10.5 (D017, D047); "
                   "no 10.6 dynamic sizing or limit price (D048, D049; tested on our data).")
ns["insert_before_para"](
    f"6A {DOT} STEP 12B - GATE D: meta-model discrimination",
    H2(f"6A {DOT} STEP 10F - Bet sizing (Rev 18, AFML Ch.10 pp.141-149)")
    + P("Sizing stack: payoff-aware AFML s.10.3 size (D007: p rescaled so the empirical break-even maps to 1/2, then "
        "m = 2Z[z] - 1) x D017 budgeting per order (risk / c_q, the 0.95-quantile of the edge's concurrent signals) -> one "
        "independent broker order per signal with fixed TP / SL / vertical barrier. Not used, each tested on our data: s.10.4 "
        "averaging + s.10.5 discretisation (D047), s.10.6 dynamic position sizing (D048) and the s.10.6 limit price (D049).")
    + BUL("s.10.6 forecast price = expected exit price: f - p = (p_model - s)(TP - SL) = E[remaining R] x SL, with s the "
          "random-walk chance of TP first (1/3 at entry for 2:1). Payoff-aware s.10.3 is s.10.6 at the decision price.")
    + BUL("In-trade forecasters must be trained and judged on live-approved trades only (owner rule 2026-10-26); research "
          "rows come from each edge's own research bars and exclude rows at or after an intrabar barrier touch.")
    + WARN("Re-test D048 after the retrain",
           "When the edges are retrained (or new feature families arrive), re-run the Ch.10 tests with --approved-only: "
           "ch10_remaining_return_test.py (pre-registered GO rule), then ch10_dynamic_sizing_sim.py on GO edges. Implement "
           "s.10.6 only if it beats fixed brackets after Moneta costs.")
    + CODE(["python -m shared.research.ch10_remaining_return_test --edge <ASSET>/<edge> --approved-only --out <json>",
            "python -m shared.research.ch10_dynamic_sizing_sim --edge <ASSET>/<edge> --approved-only --out <json>",
            "python -m shared.research.ch10_averaging_cost_sim --edge <ASSET>/<edge> --out <json>",
            "python -m shared.research.ch10_path_giveback_audit --edge <ASSET>/<edge> --out <json>"]))
appendix = (
    H1("35. Appendix: 2026-10-26 AFML Chapter 10 Walkthrough (Revision 18)")
    + BUL("10.3: the book's p > 1/2 rule would never bet on 5 of 6 edges (calibrated p 0.38-0.50, payoff break-even "
          "0.34-0.40). Payoff-aware sizing kept (D007); clean OOS benchmark deferred until the owner asks.")
    + BUL("Floating-profit giveback (step 1): stop-loss exits that first reached 75 % of TP = 8.6-10.2 % (random walk "
          "10.0 %, random entries 9.2-9.7 %); about 1 trade in 16-19 reaches 75 % of TP and then loses - the normal rate.")
    + BUL("10.4/10.5 vs D017 (Moneta costs): D017 higher daily Sharpe on all 6 edges -> D047.")
    + BUL("10.6 in-trade forecast: edge features add nothing (NO-GO on all 6); trade state (R so far, entry probability) "
          "predicts remaining R on GBPJPY, HK, JPN vc (R2 1-3 %, approved trades). In-trade continuation: losing trades "
          "tend to keep losing, winning trades to keep winning.")
    + BUL("10.6 dynamic sizing vs fixed brackets (approved-only forecaster, daily Sharpe fixed / frozen f=TP / updated): "
          "GBPJPY 0.84/0.54/0.01, HK 0.81/0.57/-0.52, JPN vc 1.15/0.87/0.56 -> D048 (not used; re-test after retrain).")
    + BUL("10.6 limit price at entry: neutral (Sharpe -0.02..+0.02) -> D049 (not implemented).")
    + BUL("Entry meta-model: OOF AUC 0.518-0.563; approval filter lifts mean executed R +0.06..+0.26 on 5 of 6 (D028 open).")
    + BUL("Holdout stayed sealed; each test is logged as a trial in the edge's research trial ledger.")
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
