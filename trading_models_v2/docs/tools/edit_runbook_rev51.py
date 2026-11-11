"""Quantara_Runbook.docx Revision 51 (2026-11-11): firing rate for SHORT edges lowered to >= 1 a week (D094); longs
stay >= 2 (D092). Helpers as in edit_runbook_rev48.py.

usage: python edit_runbook_rev51.py IN_DOCX OUT_DOCX
"""
from pathlib import Path

HERE = Path(__file__).resolve().parent
src48 = (HERE / "edit_runbook_rev48.py").read_text(encoding="utf-8")
helpers = src48[: src48.index("# ---------------------------------------------------------------- status line")]
tail = src48[src48.index("zin = ns[\"zin\"]"):]
g: dict = {"__name__": "rev51", "__file__": str(HERE / "edit_runbook_rev48.py")}
exec(compile(helpers, "edit_runbook_rev48.py(helpers)", "exec"), g)
ns, P, H1, BUL = g["ns"], g["P"], g["H1"], g["BUL"]
set_para, after_para, edit_para = g["set_para"], g["after_para"], g["edit_para"]

ns["replace_once"]("Revision date: 10 November 2026 - Revision 50 (Revision 49: 10 November 2026;",
                   "Revision date: 11 November 2026 - Revision 51 (Revision 50: 10 November 2026; Revision 49: 10 November 2026;")
after_para("Revision 50 (10 November 2026): search grid narrowed from 405 to 120 cells",
           P("Revision 51 (11 November 2026): firing rate for SHORT edges lowered to >= 1 edge event per week on 2022+ "
             "(owner, D094); LONG edges stay >= 2 (D092). Stored D090 verdicts re-read under it (3 new SHORT passes to "
             "Gate A). Section 6A Step 6B, KEY RULES, STATE; Appendix 68."))
edit_para("Gate 0 passes on an asset and side (Rev 47, D090, owner 2026-11-09)",
          "(5) >= 2 edge events per week on 2022+ (D089, lowered by D092, Rev 49)",
          "(5) >= 2 edge events per week on 2022+ for LONG, >= 1 for SHORT (D089, lowered by D092 Rev 49 and D094 Rev 51)")
edit_para("EDGE LEVEL (decides config/edge_universe.json)", "+ >= 2 events/week on 2022+ (D092)",
          "+ >= 2 events/week on 2022+ for longs, >= 1 for shorts (D092, D094)")
edit_para("Calendar-time hours; EWM bar-count. No model-row floor (Rev 46, D085).",
          "Every edge fires >= 2 times a week on 2022+ (Rev 49, D092; was 3, Rev 47 D089).",
          "Every LONG edge fires >= 2 times a week and every SHORT edge >= 1 time a week on 2022+ (Rev 51, D094; "
          "Rev 49, D092; was 3, Rev 47 D089).")
edit_para("The AFML (Ch.1-22) and MLAM (Ch.1-8, Appendices A-B) walkthrough is complete",
          "Record: docs/mldp/AFML_MLAM_WALKTHROUGH.md",
          "Rev 51 (11 November 2026): shorts >= 1/week (D094). config/edge_universe.json holds 3 LONG edges "
          "(XAUUSD/roundlevelcascade_long, DAX/closehedgingflow_long, DAX/rangebreakout_long); no short yet. Under D094 "
          "three stored SHORT pairs now pass Gate 0 and go to Gate A: T13 USDMXN (1.32/wk), T14 NASDAQ (1.59/wk), "
          "T31 US500 (1.80/wk). Record: docs/mldp/AFML_MLAM_WALKTHROUGH.md")

appendix = (
    H1("68. Appendix: 2026-11-11 Firing Rate for Shorts Lowered to 1 a Week (D094) (Revision 51)")
    + BUL("Owner 2026-11-11 approved: 'Lower the firing floor for shorts only, to at least 1 a week. Longs keep 2 a "
          "week. Every statistical gate stays the same'.")
    + BUL("Why: the indices and gold drift up, so down-moves (and the short theses that trade them) are rarer; short "
          "cards that beat their counterfactual were failing on the firing rate alone (trend-pullback card: 0.81-1.51 "
          "shorts/week on every asset).")
    + BUL("Gate 0 item 5 (D090): LONG >= 2, SHORT >= 1 edge events per week on 2022-01-01 onward "
          "(shared/research/gate0/gate0_verdict_d090.py MIN_PER_WEEK by side). Nothing else changes. The rule is ours "
          "(prop-firm turnover), not a book test.")
    + BUL("Stored D090 verdicts re-read read-only. The firing rate does not select on returns, so the SHORT pairs that "
          "failed only on it at >= 1/week go to Gate A: T13 USDMXN (1.32, C1 z 2.46), T14 NASDAQ (1.59, z 1.35), "
          "T31 US500 (1.80, z 2.07). T12 JPN SHORT (0.44) still fails. Trials accumulate (D050-A).")
    + BUL("Updated: config/mldp_deviation_register.json (D094 new; D092 amended); "
          "config/research_methodology_standing_rules.json; docs/mldp/rules/THEORY_CHECK_RULE_D075.md; "
          "shared/research/gate0/gate0_verdict_d090.py.")
)
x = ns["x"]
i = x.rfind("<w:sectPr")
ns["x"] = x[:i] + appendix + x[i:]
exec(compile(tail, "edit_runbook_rev48.py(tail)", "exec"), g)
