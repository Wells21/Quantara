"""Quantara_Runbook.docx Revision 8 (2026-10-23): AFML Ch.1 walkthrough additions.

Adds: revision note; Section 6A Step 4B (thesis block), Step 12C (Gate E holdout); Section 16C (live
oversight: version registry, decommission/shrink, shadow bets, drift dashboard, go-live prerequisites);
Appendix 25 (AFML Ch.1 record). Inserts Word XML at unique anchor paragraphs and fails if an anchor is
missing or not unique. Source of truth for the content: docs/mldp/AFML_MLAM_WALKTHROUGH.md.

usage: python edit_runbook_rev8.py IN_DOCX OUT_DOCX
"""
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

src, dst = Path(sys.argv[1]), Path(sys.argv[2])
zin = zipfile.ZipFile(src)
x = zin.read("word/document.xml").decode("utf-8")
DOT = "·"
SANS = '<w:rPr><w:rFonts w:ascii="Liberation Sans" w:hAnsi="Liberation Sans"/><w:sz w:val="19"/></w:rPr>'


def run(t, rpr=""):
    return f'<w:r>{rpr}<w:t xml:space="preserve">{escape(t)}</w:t></w:r>'


def H1(t):
    return f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>{run(t)}</w:p>'


def H2(t):
    return f'<w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr>{run(t)}</w:p>'


def P(t):
    return f"<w:p>{run(t)}</w:p>"


def BUL(t):
    return ('<w:p><w:pPr><w:pStyle w:val="ListBullet"/><w:spacing w:after="40"/></w:pPr>' + run(t, SANS) + "</w:p>")


def CODE(lines):
    rpr = '<w:rPr><w:rFonts w:ascii="Liberation Mono" w:hAnsi="Liberation Mono"/><w:color w:val="232D37"/><w:sz w:val="16"/></w:rPr>'
    body = "<w:br/>".join(f'<w:t xml:space="preserve">{escape(l)}</w:t>' for l in lines)
    return ('<w:tbl><w:tblPr><w:tblW w:type="auto" w:w="0"/><w:jc w:val="center"/><w:tblLayout w:type="autofit"/>'
            '<w:tblLook w:firstColumn="1" w:firstRow="1" w:lastColumn="0" w:lastRow="0" w:noHBand="0" w:noVBand="1" w:val="04A0"/></w:tblPr>'
            '<w:tblGrid><w:gridCol w:w="10166"/></w:tblGrid><w:tr><w:tc><w:tcPr><w:tcW w:type="dxa" w:w="10166"/><w:shd w:fill="F3F5F7"/>'
            '<w:tcBorders><w:top w:val="single" w:sz="4" w:color="D4DAE0"/><w:left w:val="single" w:sz="4" w:color="D4DAE0"/>'
            '<w:bottom w:val="single" w:sz="4" w:color="D4DAE0"/><w:right w:val="single" w:sz="4" w:color="D4DAE0"/></w:tcBorders></w:tcPr>'
            f'<w:p><w:pPr><w:spacing w:before="60" w:after="60"/></w:pPr><w:r>{rpr}{body}</w:r></w:p></w:tc></w:tr></w:tbl><w:p/>')


def BOX(title, text, fill="EAF3F8", bar="4A90A4"):
    label = ('<w:p><w:pPr><w:spacing w:before="80" w:after="40"/></w:pPr>'
             + run(title, '<w:rPr><w:rFonts w:ascii="Liberation Sans" w:hAnsi="Liberation Sans"/><w:b/><w:color w:val="192A37"/><w:sz w:val="20"/></w:rPr>')
             + "</w:p>")
    return ('<w:tbl><w:tblPr><w:tblW w:type="auto" w:w="0"/><w:jc w:val="center"/><w:tblLook w:firstColumn="1" w:firstRow="1" '
            'w:lastColumn="0" w:lastRow="0" w:noHBand="0" w:noVBand="1" w:val="04A0"/></w:tblPr><w:tblGrid><w:gridCol w:w="10166"/></w:tblGrid>'
            f'<w:tr><w:trPr><w:cantSplit/></w:trPr><w:tc><w:tcPr><w:tcW w:type="dxa" w:w="10166"/><w:shd w:fill="{fill}"/><w:tcBorders>'
            f'<w:top w:val="nil"/><w:left w:val="single" w:sz="18" w:color="{bar}"/><w:bottom w:val="nil"/><w:right w:val="nil"/></w:tcBorders></w:tcPr>'
            + label + '<w:p><w:pPr><w:spacing w:after="80"/></w:pPr>' + run(text, SANS) + "</w:p></w:tc></w:tr></w:tbl><w:p/>")


def WARN(title, text):
    return BOX(title, text, "FFF3D6", "D59C1A")


def _unique(key):
    if x.count(key) != 1:
        raise SystemExit(f"anchor count {x.count(key)}: {key[:80]}")
    return x.find(key)


def insert_after_para(key, xml):
    global x
    i = _unique(key)
    e = x.find("</w:p>", i) + len("</w:p>")
    x = x[:e] + xml + x[e:]


def insert_before_para(key, xml):
    global x
    i = _unique(key)
    s = max(x.rfind("<w:p>", 0, i), x.rfind("<w:p ", 0, i))
    x = x[:s] + xml + x[s:]


def replace_once(old, new):
    global x
    _unique(old)
    x = x.replace(old, new)


# ------------------------------------------------------------------ header
replace_once("Revision date: 22 October 2026 - Revision 7 (Revision 6: 17 October 2026;",
             "Revision date: 23 October 2026 - Revision 8 (Revision 7: 22 October 2026; Revision 6: 17 October 2026;")
insert_after_para(
    "Revision 7 (22 October 2026): adds Section 6A",
    P("Revision 8 (23 October 2026): AFML/MLAM chapter-by-chapter walkthrough, Chapter 1. Adds Section 6A Step 4B "
      "(required thesis block in edge.yaml), Step 12C (Gate E: sealed holdout / embargo consistency test), Section 16C "
      "(live oversight: edge-version registry, automatic decommission/shrink with shadow bets, drift dashboard, "
      "go-live prerequisites) and Appendix 25 (the Chapter 1 record). The current state and the place to continue "
      "are always in trading_models_v2/docs/mldp/AFML_MLAM_WALKTHROUGH.md."))

# ------------------------------------------------------------------ 6A Step 4B (before Step 5)
insert_before_para(
    f"6A {DOT} STEP 5 - syntax check",
    H2(f"6A {DOT} STEP 4B - thesis block in edge.yaml (Rev 8, AFML Ch.1 p.7)")
    + P("AFML s.1.3.1.3 (p.7): a strategy is the experiment that tests a theory naming the economic mechanism that makes "
        "some agent lose money to us. Every edge.yaml carries a 'thesis:' block, and shared/edges/edge_yaml_contract.py "
        "rejects an edge at deployability_pass / allocator_candidate / live_selected without it.")
    + BUL("Fields: schema_version, written_utc, provenance (post_hoc if written after the edge was found), review_status "
          "(owner approval), trigger_summary, mechanism, who_loses_and_why, regimes.should_work / should_fail, "
          "counterfactuals {C1: {statement, test}}, falsifiers {F1: {statement, test}}.")
    + BUL("Write it in the subset the fallback YAML parser reads: nested mappings, '- item' scalar lists, one-line "
          "double-quoted strings (no folded '>' blocks, no lists of mappings).")
    + BUL("edge.yaml is hashed into the allocator and live manifests: editing it requires allocator re-ingestion (01 onward) "
          "and a live manifest rebuild before going live.")
    + CODE(["python ./shared/edges/edge_yaml_contract.py --edge-root assets/<ASSET>/edges/<family>_<side>"]))

# ------------------------------------------------------------------ 6A Step 12C (before Step 13)
insert_before_para(
    f"6A {DOT} STEP 13 - promote / fail",
    H2(f"6A {DOT} STEP 12C - GATE E: sealed holdout (embargo) consistency (Rev 8, AFML Ch.1 p.8)")
    + P("AFML s.1.3.1.6 (p.8): the strategy is first run on data observed after the end of its backtest and is promoted "
        "if its embargoed performance is consistent with its backtest results. Gate E runs after Gate D + PBO and before "
        "the allocator, for every new edge and for every re-certified version.")
    + BUL("Holdout data: assets/<ASSET>/holdout/ticks/ (git-ignored), downloaded by shared/data/download_holdout_ticks.py "
          "(--parity must pass first; files use the raw naming <asset>_ticks_YYYY-MM-DD_to_YYYY-MM-DD.csv, which the "
          "edges parse). Research code never reads holdout/. holdout_manifest.json records the files and sha256.")
    + BUL("Replay: shared/research/holdout_replay.py builds an isolated workspace outside git (code copied, raw + holdout "
          "ticks hard-linked) and runs the SAME research code there: bars, FFD, HMM, minute vol cache, the edge's export, "
          "frozen model + calibrator, live threshold (calibrated_probability >= final_live_threshold), Task 2A exact "
          "execution with the broker cost model and the live entry delay.")
    + BUL("verify must report IDENTICAL for every edge (workspace rows up to the research end equal the repo's); "
          "otherwise the replay is not trusted.")
    + BUL("Rule (D023): windows of the holdout's span, stepped every 7 days through the backtest (meta-approved bets at "
          "the live threshold, broker costs, >= 20 bets each); share_as_bad = share of windows with mean R <= the "
          "holdout's. PASS if >= 30 holdout bets and share_as_bad >= 5%; FAIL if < 5%; INCONCLUSIVE below 30 bets (may "
          "go live under the shrink rule). PSR is reported as a diagnostic only.")
    + BUL("Events within the vertical horizon of the data end are censored (cannot be judged yet).")
    + BUL("One look per frozen edge version, logged as a trial (trial_type gate_e_holdout) in the edge's "
          "research_trial_ledger.jsonl. Never change an edge after seeing its holdout and re-test on the same holdout. "
          "When >= 3 months of newer data exist, the old holdout becomes research data and a fresh holdout is downloaded.")
    + BUL("A FAIL removes the edge from config/edge_universe.json; the edge folder stays untouched.")
    + CODE([
        "python ./shared/data/download_holdout_ticks.py --parity --assets <ASSETS>",
        "python ./shared/data/download_holdout_ticks.py --download --assets <ASSETS> --end <YYYY-MM-DDT00:00:00+00:00>",
        "python ./shared/research/holdout_replay.py setup  --assets <ASSETS>",
        "python ./shared/research/holdout_replay.py build  --assets <ASSETS>",
        "python ./shared/research/holdout_replay.py export --edges <ASSET/family_side ...>",
        "python ./shared/research/holdout_replay.py verify --edges <...>",
        "python ./shared/research/holdout_replay.py replay --edges <...>",
        "python ./shared/research/holdout_replay.py judge  --edges <...>",
    ]))

# ------------------------------------------------------------------ Section 16C (before Section 17)
insert_before_para(
    "17. Start the Dashboard ALONE",
    H1("16C. Live Oversight: Edge Versions, Decommission / Shrink, Shadow Bets (Rev 8)")
    + P("AFML s.1.3.1.6 (p.8-9): allocations are re-assessed frequently and automatically and shrink as performance "
        "decays; a strategy is decommissioned when the evidence no longer supports it. Quantara applies this inside the "
        "execution bot (D019, D021, D022).")
    + BUL("Edge versions: portfolio_live/edge_versions.py records, for every live manifest, the sha256 of each edge's "
          "meta-model and calibrator (portfolio_live/config/edge_version_registry.json). 00_build_live_deployment_manifest.py "
          "registers each new manifest automatically. Only bets placed by an edge's current version are judged.")
    + BUL("Decommission / shrink (portfolio_live/decommission_rule.py, config portfolio_live/config/decommission_rule.json): "
          "Probabilistic Sharpe Ratio per bet (AFML s.14.7.2 p.203). Off if PSR[0] <= 0.05; otherwise the allocation follows "
          "PSR vs the meta-approved backtest Sharpe: > 0.25 -> 100%, > 0.15 -> 80%, > 0.10 -> 60%, > 0.05 -> 40%, else "
          "20%. No verdict below 30 bets (trades at 100%). Recomputed at most hourly by the bot; last good verdicts "
          "(outputs/decommission/latest_verdicts.json) are used if a recompute fails; with none the bot fails closed and "
          "retries.")
    + BUL("Off is not permanent: an off edge places no orders, and each approved signal becomes a shadow bet "
          "(portfolio_live/shadow_bets.py), resolved on live bars with the signal's own barriers and charged the broker "
          "cost model. Shadow bets count as evidence, so a recovering edge returns at the level its PSR gives.")
    + BUL("Drift dashboard: python -m portfolio_live.drift_dashboard.build_report (Decommission / Shrink panel; compares "
          "live trades of the current version with meta-approved backtest bets).")
    + WARN("Before going live again after the AFML/MLAM walkthrough",
           "edge.yaml files changed (thesis blocks) and Gate E removed XAUUSD/volatilitycarry_long from "
           "config/edge_universe.json. Re-run the allocator from 00a/01 (Section 7), rebuild the live manifest (Section 8), "
           "confirm python portfolio_live/edge_versions.py lists the new manifest, then continue with Sections 9-18.")
    + CODE([
        "python ./portfolio_live/edge_versions.py",
        "python ./portfolio_live/decommission_rule.py",
        "python -m unittest tests.test_decommission_rule tests.test_decommission_live",
    ]))

# ------------------------------------------------------------------ Appendix 25 (end of body)
appendix = (
    H1("25. Appendix: 2026-10-23 AFML Chapter 1 Walkthrough (Revision 8)")
    + P("The owner and Claude go through AFML (Ch.1-22) and then MLAM (Ch.1-8, App. A) one chapter at a time: what the book "
        "says, what Quantara does, and an owner decision on every deviation. Live trading is stopped until the walkthrough "
        "is complete. Full record: trading_models_v2/docs/mldp/AFML_MLAM_WALKTHROUGH.md; deviations D018-D023 in "
        "config/mldp_deviation_register.json.")
    + H2("25.1 Decisions")
    + BUL("Thesis card per edge, required before promotion (Step 4B). All 6 cards approved by the owner.")
    + BUL("Embargo = Gate E sealed holdout (Step 12C, D023).")
    + BUL("Prop challenge account serves as paper trading + graduation (D018).")
    + BUL("Re-allocation: shrink only, any account, no ramp-up (D019); decommission/shrink applied automatically by the bot "
          "(D021); off is not permanent, shadow bets (D022).")
    + BUL("No parallel old/new versions on one prop account (D020).")
    + H2("25.2 First Gate E run (holdout to 22 October 2026)")
    + P("Holdout mean R per bet and share of same-length backtest periods as bad or worse: XAUUSD rangestructure +0.02 "
        "(19.9%) PASS; HK drawdownhazardcontinuation -0.05 (17.8%) PASS; JPN rangestructure -0.12 (16.3%) PASS; GBPJPY "
        "dailyrangecompletionfade -0.19 (8.0%) PASS narrowly; XAUUSD volatilitycarry -0.22 (0.0%) FAIL, removed from "
        "edge_universe.json; JPN volatilitycarry 3 bets INCONCLUSIVE. Backtest expectancy is regime-concentrated (both "
        "XAUUSD edges earned mostly in 2024-26); the Deflated Sharpe (Ch.14) and strategy risk (Ch.15) still apply.")
    + H2("25.3 Gotchas found")
    + BUL("Tick files must be named <asset>_ticks_YYYY-MM-DD_to_YYYY-MM-DD.csv: some edges' build_tick_file_catalog skips "
          "names with a time part.")
    + BUL("The broker TABLE spread model reads a per-minute volatility cache; any replay must rebuild it over its own data.")
    + BUL("Scripts that print emoji fail when their output is redirected to a cp1252 file: set PYTHONIOENCODING=utf-8.")
    + BUL("Phase C/D tests still require venv39 (unchanged).")
)
i = x.rfind("<w:sectPr")
x = x[:i] + appendix + x[i:]

# ------------------------------------------------------------------ write
tmp = Path(tempfile.mkdtemp()) / "out.docx"
with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
    for item in zin.infolist():
        data = x.encode("utf-8") if item.filename == "word/document.xml" else zin.read(item.filename)
        zout.writestr(item, data)
shutil.move(str(tmp), dst)
print(f"written {dst}")
