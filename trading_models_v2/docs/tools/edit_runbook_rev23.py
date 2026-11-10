"""Quantara_Runbook.docx Revision 23 (2026-10-27): AFML Ch.15 walkthrough (understanding strategy risk).

Helpers are the Revision 17 editor's (docs/tools/edit_runbook_rev17.py head), as in edit_runbook_rev18.py - rev22.py.

usage: python edit_runbook_rev23.py IN_DOCX OUT_DOCX
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

ns["replace_once"]("Revision date: 27 October 2026 - Revision 22 (Revision 21: 26 October 2026;",
                   "Revision date: 27 October 2026 - Revision 23 (Revision 22: 27 October 2026; Revision 21: 26 October 2026;")
ns["insert_after_para"](
    "Revision 22 (27 October 2026): AFML Chapter 14",
    P("Revision 23 (27 October 2026): AFML Chapter 15 (understanding strategy risk). Adds Section 6A Step 10K (strategy-risk "
      "gate, D055; decommission on bets, D056) and Appendix 40 (the Chapter 15 record)."))
ns["insert_before_para"](
    f"6A {DOT} STEP 12B - GATE D: meta-model discrimination",
    H2(f"6A {DOT} STEP 10K - Strategy risk on bets (Rev 23, AFML Ch.15 pp.211-220)")
    + P("AFML s.15.3: theta = ((pi+ - pi-) p + pi-) / ((pi+ - pi-) sqrt(p(1 - p))) sqrt(n); binHR gives the precision p_theta* "
        "needed for a target theta* (Snippet 15.3). s.15.4: strategy risk = P[p < p_theta*], NOT portfolio risk. s.15.4.1: "
        "pi- = E[pi | pi <= 0], pi+ = E[pi | pi > 0], n = bets / year over BET outcomes (Snippet 14.1 bets); bootstrap floor(n k) "
        "bets with replacement, KDE of the precision, integral below p_theta*. s.15.4.2: disregard strategies with P > .05.")
    + BUL("GATE (D055, owner 2026-10-27): P[p < p_theta*] <= .05 on bets with theta* = 0 and k = 2 years "
          "(config/strategy_risk.json; phase-1 rules have no time limit, so only a true SR <= 0 fails the challenge regardless "
          "of sizing). Applied at the RECERT; theta* = 1, k = 1 and trades reported alongside. Snippet 15.5's p(1 - p) "
          "shortcut contradicts its own algorithm and is only a flagged diagnostic.")
    + BUL("Exercise 15.1-15.2 sensitivities in 07c: required precision, frequency, PT, SL; elasticities (precision is the "
          "lowest-hanging fruit on every deployed edge).")
    + BUL("Live (D056): the decommission rule's PSR[0], PSR[SR_bt], 30-bet minimum and backtest benchmark use bets, not "
          "overlapping trades; its report adds live bet precision vs p_theta* (diagnostic, allocation unchanged).")
    + CODE(["python -m unittest tests.test_afml_ch15_strategy_risk tests.test_decommission_rule"]))
appendix = (
    H1("40. Appendix: 2026-10-27 AFML Chapter 15 Walkthrough (Revision 23)")
    + BUL("Pre-retrain P[fail] on bets (theta* 0, k 2): GBPJPY 0.023, HK 0.022, JPN 0.057 (fail), XAUUSD 0.000; at theta* 1: "
          "0.27 / 0.27 / 0.44 / 0.005; on trades all <= 0.0001 (overlapping trades are not independent).")
    + BUL("Backtest trades -> bets: GBPJPY 2,738 -> 529, HK 2,571 -> 400, JPN 4,428 -> 366, XAUUSD 2,251 -> 459.")
    + BUL("Entry timing after the event: tested (Ch.10 limit price neutral, D049; Ch.13 in-trade random walk); confirmation-style "
          "entries only as new pre-registered theses.")
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
