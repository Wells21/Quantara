"""Quantara_Runbook.docx Revision 9 (2026-10-23): AFML Ch.2 walkthrough additions.

Adds: revision note; Section 6A Step 0B (curated tick layer); Section 16D (live quote-quality filter, USOIL live
roll check); Appendix 26 (AFML Ch.2 record). Inserts Word XML at unique anchor paragraphs and fails if an anchor is
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
replace_once("Revision date: 23 October 2026 - Revision 8 (Revision 7: 22 October 2026;",
             "Revision date: 23 October 2026 - Revision 9 (Revision 8: 23 October 2026; Revision 7: 22 October 2026;")
insert_after_para(
    "Revision 8 (23 October 2026): AFML/MLAM chapter-by-chapter walkthrough, Chapter 1.",
    P("Revision 9 (23 October 2026): AFML Chapter 2. Adds Section 6A Step 0B (curated tick layer: quote-quality filter "
      "and futures roll adjustment; all research reads curated ticks), Section 16D (the same quote filter in live; "
      "USOIL live roll check) and Appendix 26 (the Chapter 2 record)."))

insert_before_para(
    f"6A {DOT} STEP 1 - scaffold",
    H2(f"6A {DOT} STEP 0B - curated tick layer (Rev 9, AFML Ch.1 p.7, Ch.2 s.2.4.3)")
    + P("Research never reads raw vendor ticks. shared/data/tick_curation.py turns assets/<ASSET>/raw/ticks into "
        "assets/<ASSET>/curated/ticks: a causal quote-quality filter (reject a quote whose mid jumps more than max(3%, "
        "10 x the asset's EWMA daily sigma) from the last accepted mid; a level that persists 4 hours is accepted) and, "
        "for futures-based CFDs, a multiplicative backward roll adjustment (prices before a roll scaled by resume/stop "
        "mid; percentage returns preserved; latest prices equal raw). Unchanged files are hard links.")
    + BUL("config/asset_data_policy.json: tick_source=curated for every asset; roll_adjust=true for USOIL. "
          "AssetLayout.tick_root follows it and fails closed if curated ticks are missing.")
    + BUL("After any new raw tick download: curate, then rebuild bars/FFD/HMM/vol cache, then the bar-quality report. "
          "Read curated/quality_report.json (rejected episodes, re-anchors) and roll_calendar.json (detected rolls, "
          "undetected months) before using the data.")
    + BUL("One bar type for every asset: quote-tick bars (D024). Check bar_quality_report.json (Exercise 2.1).")
    + CODE([
        "python ./shared/data/tick_curation.py report --assets <ASSET|ALL>   # dry run, writes nothing",
        "python ./shared/data/tick_curation.py curate --assets <ASSET|ALL>",
        "python ./shared/bars/forge_asset_standard_quote_tick_bars.py --asset <ASSET> --overwrite --engine streaming",
        "python ./shared/ffd/build_asset_ffd_features.py --asset <ASSET> --overwrite",
        "python ./shared/regime/build_asset_hmm_regime_features.py --asset <ASSET> --overwrite",
        "python ./shared/bars/bar_quality_report.py --assets <ASSET|ALL> --days 365",
    ]))

insert_before_para(
    "17. Start the Dashboard ALONE",
    H1("16D. Live Quote-Quality Filter and Futures Rolls (Rev 9)")
    + P("portfolio_live_dashboard_orchestrator.apply_tick_quality_filter runs the research filter on every Moneta tick "
        "batch before bars and before the daily tick counts that set bar thresholds. Its state is stored per asset in "
        "the live state (tick_quality_filter_by_asset, part of the asset transaction snapshot) and is seeded with the "
        "daily sigma of the curated research report. Rejections are logged as LIVE_TICK_QUALITY_REJECTED.")
    + WARN("USOIL (and any futures-based CFD)",
           "Research history is roll-adjusted; live Moneta prices are raw. Before any USOIL edge goes live, verify how "
           "Moneta rolls its oil CFD (price gap and position adjustment) and decide how live bars and open positions "
           "handle the roll."))

appendix = (
    H1("26. Appendix: 2026-10-23 AFML Chapter 2 Walkthrough (Revision 9)")
    + P("Chapter 2 (financial data structures) checked against all 10 assets. Record: "
        "docs/mldp/AFML_MLAM_WALKTHROUGH.md section 4B; deviations D024-D026.")
    + BUL("D024: one bar type for all 10 assets, quote-tick bars; no volume, dollar or information-driven bars (OTC "
          "quotes have no traded volume; Dukascopy/Moneta feed mismatch).")
    + BUL("D025: no ETF trick or PCA weights (single instruments).")
    + BUL("D026: curated tick layer. 1.55 billion ticks scanned: one bad-quote episode (JPN 2025-08-26 06:34-09:10 UTC, "
          "quotes ~64% below the market) - removed; JPN bars rebuilt. USOIL: 43 roll jumps adjusted multiplicatively "
          "(the additive Snippet 2.2 form was rejected: -30.64 USD cumulative shift inflated 2022 returns ~1.5x).")
    + BUL("Exercise 2.1 over all 10: quote-tick bars beat equal-count time bars on normality for 8/10 and variance "
          "stability for 7/10; US500 and DAX have fatter tails (session gaps).")
    + BUL("JPN edges must be re-certified (their research data held the bad quotes) - planned once after Ch.3-4.")
    + BUL("Event clustering (CUSUM reset, s.2.5.2.1) is handled in Chapter 4.")
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
