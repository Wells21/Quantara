"""Quantara_Runbook.docx Revision 12 (2026-10-23): AFML Ch.5 walkthrough additions.

Adds: revision note; Section 6A Step 0C (FFD d on the full research history, ADF gate, frozen d for holdout/live);
Appendix 29 (AFML Ch.5 record). Inserts Word XML at unique anchor paragraphs and fails if an anchor is missing or
not unique. Source of truth: docs/mldp/AFML_MLAM_WALKTHROUGH.md section 4E; deviations D031-D033.

usage: python edit_runbook_rev12.py IN_DOCX OUT_DOCX
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
replace_once("Revision date: 23 October 2026 - Revision 11 (Revision 10: 23 October 2026;",
             "Revision date: 23 October 2026 - Revision 12 (Revision 11: 23 October 2026; Revision 10: 23 October 2026;")
insert_after_para(
    "Revision 11 (23 October 2026): AFML Chapter 4.",
    P("Revision 12 (23 October 2026): AFML Chapter 5. Adds Section 6A Step 0C (FFD d chosen on the full research "
      "history with an ADF gate, then frozen for holdout and live) and Appendix 29 (the Chapter 5 record)."))

insert_before_para(
    f"6A {DOT} STEP 1 - scaffold",
    H2(f"6A {DOT} STEP 0C - FFD d selection (Rev 12, AFML Ch.5 s.5.6, Snippet 5.4 pp.84-85)")
    + P("FFD features (Open/High/Low/Close_FFD, their z-scores and momenta, and the HMM input Close_FFD) use one frozen "
        "d per asset: the minimum d on [0, 1] (precision 0.01) whose FFD(log Close), fixed 1,000-term window, passes "
        "ADF at p < 0.05 (statsmodels defaults, AIC lags) on the FULL research history. The build fails if the "
        "estimated d does not pass; ffd_manifest.json records the ADF result for the full sample and each half.")
    + BUL("d is frozen. The holdout workspace (holdout_replay.py) and live receive it via --frozen-d / the state "
          "bundle and never re-estimate it, so no holdout bar ever influences d.")
    + BUL("Re-estimate d only when the research history itself changes (new research ticks, curation change), and "
          "then treat every edge on that asset as a new version (re-export, Steps 01-09, Gates C/D/E).")
    + BUL("After FFD always rebuild the HMM regime features (Close_FFD is an HMM input).")
    + CODE(["python ./shared/ffd/build_asset_ffd_features.py --asset <ASSET> --overwrite        # estimates d on full history",
            "python ./shared/regime/build_asset_hmm_regime_features.py --asset <ASSET> --overwrite",
            "python -m unittest tests.test_afml_ch5_ffd"]))

appendix = (
    H1("29. Appendix: 2026-10-23 AFML Chapter 5 Walkthrough (Revision 12)")
    + P("Chapter 5 (fractionally differentiated features, pp.75-89) checked against all 10 assets. Record: "
        "docs/mldp/AFML_MLAM_WALKTHROUGH.md section 4E; deviations D031-D033.")
    + BUL("Matches the book: weights (-1)^k binom(d,k) equal Snippet 5.1 to 1e-16; fixed-width FFD (no expanding "
          "window); FFD on log prices; memory reported as corr(log price, FFD); Exercise 5.4 inverse recovers x.")
    + BUL("Fixed (D031): d had been chosen on the first 20,000 bars only; 8 of 10 assets' Close_FFD then failed ADF "
          "on the full history (GBPJPY d = 0 meant Close_FFD was the log price). d is now chosen on the full "
          "research history (Snippet 5.4), ADF-gated, frozen; all 10 assets' FFD and HMM rebuilt.")
    + BUL("D032: fixed 1,000-term window instead of tau = 1e-5. Measured: tau = 1e-5 needs 1,369-4,092 bars and a "
          "0.02-0.06 lower d, but the feature correlates 0.993-0.9996 with the 1,000-bar version; live pins 1,000.")
    + BUL("D033: s.5.7 cumsum-then-FFD of other features not adopted (owner decision).")
    + BUL("All edges must be re-exported and retrained (planned batch after Ch.6/7/9).")
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
