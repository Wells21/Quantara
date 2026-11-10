"""Quantara_Runbook.docx Revision 50 (2026-11-10): search grid narrowed to 120 cells (D093: EWM 150 only, 168 h
vertical dropped); STATE.  Helpers as in edit_runbook_rev48.py.

usage: python edit_runbook_rev50.py IN_DOCX OUT_DOCX
"""
from pathlib import Path

HERE = Path(__file__).resolve().parent
src48 = (HERE / "edit_runbook_rev48.py").read_text(encoding="utf-8")
helpers = src48[: src48.index("# ---------------------------------------------------------------- status line")]
tail = src48[src48.index("zin = ns[\"zin\"]"):]
g: dict = {"__name__": "rev50", "__file__": str(HERE / "edit_runbook_rev48.py")}
exec(compile(helpers, "edit_runbook_rev48.py(helpers)", "exec"), g)
ns, P, H1, BUL = g["ns"], g["P"], g["H1"], g["BUL"]
set_para, after_para, edit_para = g["set_para"], g["after_para"], g["edit_para"]

GRID = ("120 cells: lookbacks 1,2,4,6,8 h x EWM 150 (D093; was 100,150,200, D074) x verticals 1,2,4,8,16,24,48,96 h "
        "(D093; 168 h dropped) x barriers 1:1, 1.5:1, 2:1; never lab defaults; canonical shared/research/gate_a_grid.sh")
CMD = ("Gate A command since Rev 50 (D093): source shared/research/gate_a_grid.sh, then python "
       "shared/research/v3_horizon_lab.py --edge-root <edge> $GRID_ARGS --cost-bps <broker mean> --hybrid-fast-grid "
       "--save  ($GRID_ARGS = --event-vol-lookbacks 1,2,4,6,8 --event-vol-ewm-spans 150 --vertical-horizons "
       "1,2,4,8,16,24,48,96 --barriers 1:1,1.5:1,2:1). The 405-cell commands below are history.")

ns["replace_once"]("Revision date: 10 November 2026 - Revision 49 (Revision 48: 9 November 2026;",
                   "Revision date: 10 November 2026 - Revision 50 (Revision 49: 10 November 2026; Revision 48: 9 November 2026;")
after_para("Revision 49 (10 November 2026): firing rate lowered from >= 3 to >= 2",
           P("Revision 50 (10 November 2026): search grid narrowed from 405 to 120 cells (owner, D093): EWM span "
             "fixed at 150 and the 168 h vertical dropped; every cell is still searched and paid for (D050-A); no "
             "per-card holding-period band. Section 6A Search, Gate A, STATE; Appendix 67."))
edit_para("Search. Gates A-C search the runbook grid exactly",
          "405 cells: lookbacks 1,2,4,6,8 h x EWM 100,150,200 (D074; was 20,50,100) x verticals 1,2,4,8,16,24,48,96,168 h "
          "x barriers 1:1, 1.5:1, 2:1; never lab defaults", GRID)
set_para("GATE A: signal existence (405 cells, exactly this grid, never lab defaults)",
         "GATE A: signal existence (120 cells since Rev 50, D093; exactly this grid, never lab defaults)")
after_para("GATE A: signal existence (120 cells since Rev 50", P(CMD))
edit_para("Using the lab's default grid or any grid other than the runbook's 405 cells.",
          "the runbook's 405 cells.", "the runbook's grid (120 cells since Rev 50, D093; shared/research/gate_a_grid.sh).")
edit_para("The AFML (Ch.1-22) and MLAM (Ch.1-8, Appendices A-B) walkthrough is complete",
          "Record: docs/mldp/AFML_MLAM_WALKTHROUGH.md",
          "Rev 50 (10 November 2026): Gate A-C grid = 120 cells (D093). T29 (started at 405) keeps its 405 paid trials; "
          "the six D092 pairs and every later card run on 120. Record: docs/mldp/AFML_MLAM_WALKTHROUGH.md")

appendix = (
    H1("67. Appendix: 2026-11-10 Search Grid Narrowed to 120 Cells (D093) (Revision 50)")
    + BUL("Owner 2026-11-10: 'yes approve EWM fixed at 150 and drop 168h. Leave the band stuff out, no need for the card "
          "to state its holding period band'.")
    + BUL("Evidence (50 full Gate C runs, 21 edges): EWM 100/150/200 cell results correlate 0.978-0.992 and every ONC "
          "trial cluster held the three spans equally; the 168 h vertical was reached by 0.3 % of trades (max 3.1 %) and "
          "tracks 96 h (corr 0.996, mean |diff| 0.003 R). Share of trades timing out by vertical: 1 h 83 %, 2 h 74 %, "
          "4 h 62 %, 8 h 44 %, 16 h 26 %, 24 h 15 %, 48 h 9 %, 96 h 1.6 %, 168 h 0.3 %.")
    + BUL("Effect: Gate A/C run time about 3.4x shorter. DSR counts ONC clusters of trials (MLAM s.8.7.1 p.114), and the "
          "removed cells were near-duplicates inside existing clusters, so DSR changes little; D050-A (search and pay "
          "for every cell; trials accumulate) is unchanged.")
    + BUL("Cards from T30 pre-register their Gate 0 cell with EWM 150; T1-T29 keep their EWM 100 cells.")
    + BUL("Updated: shared/research/gate_a_grid.sh (new, canonical); config/mldp_deviation_register.json (D093); "
          "config/research_methodology_standing_rules.json (SEARCH_GRID_D093); shared/research/gate0/gate0_tool.py.")
)
x = ns["x"]
i = x.rfind("<w:sectPr")
ns["x"] = x[:i] + appendix + x[i:]
exec(compile(tail, "edit_runbook_rev48.py(tail)", "exec"), g)
