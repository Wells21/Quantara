"""Quantara_Runbook.docx Revision 22 (2026-10-27): AFML Ch.14 walkthrough (backtest statistics, MLAM Ch.8).

Helpers are the Revision 17 editor's (docs/tools/edit_runbook_rev17.py head), as in edit_runbook_rev18.py - rev21.py.

usage: python edit_runbook_rev22.py IN_DOCX OUT_DOCX
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

ns["replace_once"]("Revision date: 26 October 2026 - Revision 21 (Revision 20: 26 October 2026;",
                   "Revision date: 27 October 2026 - Revision 22 (Revision 21: 26 October 2026; Revision 20: 26 October 2026;")
ns["insert_after_para"](
    "Revision 21 (26 October 2026): AFML Chapter 13",
    P("Revision 22 (27 October 2026): AFML Chapter 14 (backtest statistics) with MLAM Chapter 8. Adds Section 6A Step 10J "
      "(all-trials DSR / FWER gate, D054; backtest statistics) and Appendix 39 (the Chapter 14 record)."))
ns["insert_before_para"](
    f"6A {DOT} STEP 12B - GATE D: meta-model discrimination",
    H2(f"6A {DOT} STEP 10J - All-trials DSR / FWER and backtest statistics (Rev 22, AFML Ch.14 pp.195-210, MLAM Ch.8)")
    + P("AFML third law (p.205): every backtest result is reported with ALL the trials that produced it. DSR = PSR[SR*], SR* = the "
        "expected maximum Sharpe of the trials under no skill (AFML p.204; MLAM s.8.5). MLAM s.8.7 revises N and V: E[K] = ONC "
        "clusters of the trials' return correlations; V from one series per cluster weighted by Lopez de Prado & Lewis (2018) "
        "Snippet 4 getIVP (robust minimum-variance approximation), annualised, rescaled to the selected strategy's frequency. "
        "MLAM s.8.8: FWER alpha_K = 1 - (1 - alpha)^K and Type II power.")
    + BUL("GATE (D054, owner 2026-10-27): DSR > 0.95 (AFML p.206) AND FWER alpha_K < 0.05 on Level 1 = the edge's own full Gate C "
          "run (every replayed configuration). Applied at the RECERT after the retrain -- nothing removed before. Level 2 (thesis "
          "search on the asset, E[K] = 2 x theses, flagged approximation) is reported alongside and does not gate.")
    + BUL("Allocator 07b reads each edge's record artifacts/feature_importance_audit/ch14_dsr_all_trials.json (fails closed "
          "without it); portfolio trials = the Step 07 HRP scenarios. 07c reports s.14.3-14.9: bets (Snippet 14.1), holding "
          "period (14.2), HHI on bets (14.3), DD/TuW (14.4), leverage / turnover / capacity headroom, cost split, classification "
          "scores at the live threshold, attribution vs the primary-event universe, correlation to the underlying.")
    + BUL("Trial counts already spent cannot be reset: re-registering an existing edge's geometry after seeing results is "
          "hindsight; D050 lowers the bar only for NEW pre-registered edges.")
    + CODE(["python -m shared.research.dsr_all_trials --edge-root assets/<ASSET>/edges/<edge> --stamps <Gate C stamps> --out <json>",
            "python -m shared.research.ch14_benchmark_vs_underlying --edge <ASSET>/<edge> --out <json>"]))
appendix = (
    H1("39. Appendix: 2026-10-27 AFML Chapter 14 Walkthrough (Revision 22)")
    + BUL("Old 07b used the deployed edges as the trials (K = 2) -> 6 / 7 'significant'. All-trials Level 1 (406 Gate C trials, "
          "E[K] = 2, LdP-Lewis weights): XAUUSD rs 0.998 pass; JPN rs 0.938, GBPJPY 0.685, HK 0.650 fail. Level 2: all fail.")
    + BUL("Random-timing benchmark (AFML p.197): beta share of edge mean R -- HK ~0 %, GBPJPY 26 %, XAUUSD 36 %, JPN 84 % (timing "
          "not significant, p 0.285); information ratio 1.35 / 1.01 / 1.48 / 0.23. JPN's meta-model takes 100 % of events at "
          "its live threshold.")
    + BUL("Legacy review: D001, D004, D009, D012 superseded (mid-label decision, D036, D031, Ch.1 lifecycle decisions); D002 "
          "closed with naming debt (quote_executable_meta_label holds the mid label).")
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
