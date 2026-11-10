"""Quantara_Runbook.docx Revision 49 (2026-11-10): firing rate lowered to >= 2 events/week (D092); the stored D090
verdicts re-read under it; STATE.

Helpers as in edit_runbook_rev48.py.

usage: python edit_runbook_rev49.py IN_DOCX OUT_DOCX
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
src48 = (HERE / "edit_runbook_rev48.py").read_text(encoding="utf-8")
helpers = src48[: src48.index("# ---------------------------------------------------------------- status line")]
tail = src48[src48.index("zin = ns[\"zin\"]"):]
g: dict = {"__name__": "rev49", "__file__": str(HERE / "edit_runbook_rev48.py")}
exec(compile(helpers, "edit_runbook_rev48.py(helpers)", "exec"), g)
ns, P, H1, BUL = g["ns"], g["P"], g["H1"], g["BUL"]
set_para, after_para, edit_para = g["set_para"], g["after_para"], g["edit_para"]

ns["replace_once"]("Revision date: 9 November 2026 - Revision 48 (Revision 47: 9 November 2026;",
                   "Revision date: 10 November 2026 - Revision 49 (Revision 48: 9 November 2026; Revision 47: 9 November 2026;")
after_para("Revision 48 (9 November 2026): Gate D is no longer a gate (owner, D091)",
           P("Revision 49 (10 November 2026): firing rate lowered from >= 3 to >= 2 edge events per week on 2022+ "
             "(owner, D092); stored D090 verdicts re-read under it (6 new (asset, side) passes to Gate A). Section 6A "
             "Step 6B, KEY RULES, STATE; Appendix 66."))
edit_para("Gate 0 passes on an asset and side (Rev 47, D090, owner 2026-11-09)",
          "(5) >= 3 edge events per week on 2022+ (D089)", "(5) >= 2 edge events per week on 2022+ (D089, lowered by D092, Rev 49)")
edit_para("EDGE LEVEL (decides config/edge_universe.json)", "+ >= 3 events/week on 2022+", "+ >= 2 events/week on 2022+ (D092)")
edit_para("Calendar-time hours; EWM bar-count. No model-row floor (Rev 46, D085).",
          "Every edge fires >= 3 times a week on 2022+ (Rev 47, D089).",
          "Every edge fires >= 2 times a week on 2022+ (Rev 49, D092; was 3, Rev 47 D089).")
edit_para("The AFML (Ch.1-22) and MLAM (Ch.1-8, Appendices A-B) walkthrough is complete",
          "each firing >= 3 times a week.", "each firing >= 2 times a week (D092, Rev 49).")
edit_para("The AFML (Ch.1-22) and MLAM (Ch.1-8, Appendices A-B) walkthrough is complete",
          "Record: docs/mldp/AFML_MLAM_WALKTHROUGH.md",
          "Rev 49 (10 November 2026): firing rate >= 2/week (D092). Tests 36-42 (T23-T29) run since Rev 48; Gate 0 "
          "passes mostly die at PBO Prob[OOS<=0] or at DSR; config/edge_universe.json still holds 1 edge "
          "(XAUUSD/roundlevelcascade_long). Under D092 six stored (asset, side) pairs now pass Gate 0 and go to Gate A: "
          "T1c DAX long + short, T13 XAUUSD short, T22 HK long + short, T22 XAUUSD short. "
          "Record: docs/mldp/AFML_MLAM_WALKTHROUGH.md")

appendix = (
    H1("66. Appendix: 2026-11-10 Firing Rate Lowered to 2 a Week (D092) (Revision 49)")
    + BUL("Owner 2026-11-10: 'lets make the standing rule a fire rate greater than or equal to 2 instead of the current "
          "3 because like you said the fire rate is becoming the bottlekneck'.")
    + BUL("Gate 0 item 5 (D090) now needs >= 2 edge events per week on 2022-01-01 onward "
          "(shared/research/gate0/gate0_verdict_d090.py MIN_PER_WEEK = 2.0). Nothing else in Gate 0 or later gates "
          "changes. The rule is ours (prop-firm turnover), not a book test.")
    + BUL("Stored D090 verdicts re-read read-only. The firing rate does not select on returns, so the six pairs that "
          "failed only on it at 2.28-2.56/week go to Gate A: T1c DAX LONG (2.56) + SHORT (2.28); T13 XAUUSD SHORT "
          "(2.35); T22 HK LONG (2.28) + SHORT (2.52); T22 XAUUSD SHORT (2.45). Trials accumulate (D050-A).")
    + BUL("Updated: config/mldp_deviation_register.json (D092 new; D089, D090 amended); "
          "config/research_methodology_standing_rules.json; docs/mldp/rules/THEORY_CHECK_RULE_D075.md; "
          "shared/research/gate0/gate0_verdict_d090.py; gate0_tool.py comment.")
)
x = ns["x"]
i = x.rfind("<w:sectPr")
ns["x"] = x[:i] + appendix + x[i:]
exec(compile(tail, "edit_runbook_rev48.py(tail)", "exec"), g)
