"""Quantara_Runbook.docx Revision 52 (2026-11-11): VPS portability (git = the backup), promotion checklist (00d CSV +
all-trials DSR record force-added), 00b window on Moneta (2012+), full allocator 01-11 verified on a fresh clone;
STATE = 3 deployable edges. Helpers as in edit_runbook_rev48.py.

usage: python edit_runbook_rev52.py IN_DOCX OUT_DOCX
"""
from pathlib import Path

HERE = Path(__file__).resolve().parent
src48 = (HERE / "edit_runbook_rev48.py").read_text(encoding="utf-8")
helpers = src48[: src48.index("# ---------------------------------------------------------------- status line")]
tail = src48[src48.index("zin = ns[\"zin\"]"):]
g: dict = {"__name__": "rev52", "__file__": str(HERE / "edit_runbook_rev48.py")}
exec(compile(helpers, "edit_runbook_rev48.py(helpers)", "exec"), g)
ns, P, H1, BUL = g["ns"], g["P"], g["H1"], g["BUL"]
set_para, after_para, edit_para = g["set_para"], g["after_para"], g["edit_para"]

ns["replace_once"]("Revision date: 11 November 2026 - Revision 51 (Revision 50: 10 November 2026;",
                   "Revision date: 11 November 2026 - Revision 52 (Revision 51: 11 November 2026; Revision 50: 10 November 2026;")
after_para("Revision 51 (11 November 2026): firing rate for SHORT edges lowered",
           P("Revision 52 (11 November 2026): the VPS is temporary and git (Wells21/Quantara) is the backup (owner): "
             "promotion checklist (edge folder, edge_universe entry, 00d CSV and all-trials DSR record force-added, "
             "commit and push); new-VPS procedure; 00b on Moneta starts 2012; full allocator 01-11 verified on a fresh "
             "clone with 3 edges. Section 6A Step 13 and STATE, Section 7 allocator flow; Appendix 69."))

# Step 13: promotion checklist
after_para("PASS = every EDGE-LEVEL gate in the KEY RULES chain, in order (Rev 43)",
           P("Promotion checklist (Rev 52; owner: a new VPS must only need a clone of the repo): (1) edge folder "
             "committed; (2) config/edge_universe.json entry with every gate number in its notes; (3) allocator 00d "
             "--firm <trading firm> --edge <ASSET/edge>, then git add -f portfolio_allocator/outputs/broker_repricing/"
             "<firm>/<ASSET>__<edge>.csv (00d replays curated ticks, which are not in git); (4) copy the post-PBO "
             "all-trials DSR file logs/<edge>_<ASSET>_dsr_full_era.json to <edge>/artifacts/feature_importance_audit/"
             "ch14_dsr_all_trials.json and git add -f it (allocator 07b stops without it); (5) commit and push to "
             "origin main. Reference: docs/VPS_MIGRATION.md."))

# Section 7: allocator flow on a new VPS + 00b window
after_para("When to rerun the allocator (and therefore 00c/00d), then portfolio_live",
           P("New VPS, same firm (Rev 52, verified 11 November 2026): clone Wells21/Quantara (deploy key, git lfs pull), "
             "create venv39 / venv312, put the MT5 login in portfolio_live/local_live_settings.json (never in git), then "
             "run the allocator 01 -> 11 on the committed 00a-00d outputs (broker profile, conversions, cost model, "
             "00d CSVs) and portfolio_live 00..04. 00a-00c can be rerun from MT5 on the new VPS; 00d cannot (it needs "
             "the curated research ticks), so a firm switch re-prices on the research VPS or on re-downloaded, curated "
             "ticks. Step 09 is the owner's manual variant choice. Full procedure: docs/VPS_MIGRATION.md."))
after_para("New VPS, same firm (Rev 52, verified 11 November 2026)",
           P("00b on Moneta (Rev 52): the MT5 server holds EURUSD / USDJPY history only from 2010-09 and USDHKD has "
             "1-3.5 month holes in 2011, so 00b runs with --start 2012-01-01 (manifest PASS; 04b refuses a FAIL "
             "manifest). Enough for the current edges (XAUUSD profit currency USD; DAX trades from 2014). An edge "
             "converting through these pairs with trades before 2012 fails 04b with missing_prior_conversion: decide "
             "then (shorter trade window for the allocator, or another conversion source)."))

# STATE
edit_para("Deployable (config/edge_universe.json): 0. The RECERT of 3-4 November 2026",
          "Deployable (config/edge_universe.json): 0.",
          "Deployable (config/edge_universe.json): 3 since Rev 51 -- XAUUSD/roundlevelcascade_long, "
          "DAX/closehedgingflow_long, DAX/rangebreakout_long (all LONG; no short yet). Before them it was 0:")
edit_para("The AFML (Ch.1-22) and MLAM (Ch.1-8, Appendices A-B) walkthrough is complete",
          "Record: docs/mldp/AFML_MLAM_WALKTHROUGH.md and logs/recert_20261103/RECERT_LOG.md",
          "Rev 52 (11 November 2026): full allocator 01-11 verified on a fresh clone with the 3 edges (VPS "
          "portability). D094 shorts at Gate A: T13 USDMXN and T14 NASDAQ killed (best gross < +0.05); T31 "
          "US500/failedbreakout_short at Gate C. T34 trend pullback failed Gate 0 (test 47). "
          "Record: docs/mldp/AFML_MLAM_WALKTHROUGH.md and logs/recert_20261103/RECERT_LOG.md")

appendix = (
    H1("69. Appendix: 2026-11-11 VPS Portability -- Git Is the Backup (Revision 52)")
    + BUL("Owner 2026-11-10: the VPS is temporary; a new VPS must only need to pull the repo and continue from "
          "portfolio_allocator, then portfolio_live, without redoing the edge search. Repo: Wells21/Quantara (private, "
          "fresh start fcc501a 2026-11-10; full old history in a bundle on the Desktop).")
    + BUL("Verified 2026-11-11: a fresh clone (3 edges) ran allocator 01, 02, 03, 04b, 04bb, 04c, 04d, 05, 06, 07, "
          "07b, 07b_validate_geometry_handoff, 07c, 08, 08b, 08c, 09 (defaults), 10, 11, every step rc 0, with nothing "
          "copied from this VPS.")
    + BUL("Found on the way: (a) 04b needs a PASS 00b manifest -> 00b from 2012 on Moneta (Section 7); (b) 07b needs "
          "each edge's all-trials DSR record inside the edge folder -> promotion checklist item 4 (Step 13); (c) the "
          "broker_repricing folders are git-ignored -> 00d CSVs are force-added.")
    + BUL("Updated: docs/VPS_MIGRATION.md; portfolio_allocator/config/broker_conversions (00b 2012+); "
          "artifacts/feature_importance_audit/ch14_dsr_all_trials.json in the 3 edges.")
)
x = ns["x"]
i = x.rfind("<w:sectPr")
ns["x"] = x[:i] + appendix + x[i:]
exec(compile(tail, "edit_runbook_rev48.py(tail)", "exec"), g)
