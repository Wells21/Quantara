"""Quantara_Runbook.docx Revision 13 (2026-10-24): AFML Ch.6 walkthrough additions.

Adds: revision note; Section 6A Step 10C (bagged ensemble settings: per-tree seeds, avgU x n samples, N = 1000);
Appendix 30 (AFML Ch.6 record). Inserts Word XML at unique anchor paragraphs and fails if an anchor is missing or
not unique. Source of truth: docs/mldp/AFML_MLAM_WALKTHROUGH.md section 4F; deviations D034-D035.

usage: python edit_runbook_rev13.py IN_DOCX OUT_DOCX
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
replace_once("Revision date: 23 October 2026 - Revision 12 (Revision 11: 23 October 2026;",
             "Revision date: 24 October 2026 - Revision 13 (Revision 12: 23 October 2026; Revision 11: 23 October 2026;")
insert_after_para(
    "Revision 12 (23 October 2026): AFML Chapter 5.",
    P("Revision 13 (24 October 2026): AFML Chapter 6. Adds Section 6A Step 10C (the bagged ensemble: per-tree seeds, "
      "average-uniqueness sample size, 1,000 trees) and Appendix 30 (the Chapter 6 record)."))

insert_before_para(
    f"6A {DOT} STEP 12B - GATE D: meta-model discrimination",
    H2(f"6A {DOT} STEP 10C - bagged ensemble settings (Rev 13, AFML Ch.6 pp.93-102)")
    + P("The Step 02 final model and every Step 03 out-of-fold model is AFML Snippet 6.2's clf1 resampled with the "
        "sequential bootstrap (Ch.4): entropy decision trees, max_features='sqrt', class_weight='balanced', each tree "
        "with its own seed drawn from the seeded ensemble generator, each tree trained on round(avgU x n) sequential-"
        "bootstrap draws (s.6.3.3: sample about the average uniqueness of the observations, not more), 1,000 trees "
        "(Snippet 6.2). Out-of-bag accuracy is never used (s.6.3.3); purged CPCV is.")
    + BUL("Step 02's manifest records rho_bar, avg uniqueness, sample length and N. A rho_bar near 1 means the trees "
          "are copies and bagging is not reducing variance (s.6.3.1).")
    + BUL("A new edge copied from a sibling must pass the tests below; patch an old template with the tool.")
    + BUL("Step 02's trial-ledger record now logs the real tree search space (OPTUNA_SEARCH_SPACE). Records before "
          "24 October 2026 list a stale XGBoost space; the correct space is in register entry D035.")
    + WARN("Models trained before 24 October 2026",
           "All 6 current models were built with one shared tree seed (all trees drew the same feature subsets) and "
           "full-size samples (D034). They are retrained in the planned batch.")
    + CODE(["python -m unittest tests.test_afml_ch6_ensemble tests.test_sequential_bootstrap_bagging_sync",
            "python ./docs/tools/patch_afml_ch6_ensemble.py --edges <ASSET>/<family>_<side>   # only if the tests fail"]))

appendix = (
    H1("30. Appendix: 2026-10-24 AFML Chapter 6 Walkthrough (Revision 13)")
    + P("Chapter 6 (ensemble methods, pp.93-102) checked against every edge. Record: docs/mldp/AFML_MLAM_WALKTHROUGH.md "
        "section 4F; deviations D034-D035.")
    + BUL("Matches the book: bagged entropy trees with balanced classes (Snippet 6.2 clf1), averaged probabilities, "
          "sequential bootstrap, no out-of-bag accuracy, rho_bar reported, bagging (not boosting) for the final model.")
    + BUL("Fixed (D034): every tree used random_state 42, so all trees drew the same feature subsets (GBPJPY: all 100 "
          "trees split the root on one feature; models used 10-24 of 42-157 features). Now per-tree seeds, "
          "round(avgU x n) rows per tree, 1,000 trees; on XAUUSD rangestructure 157/157 features used, rho_bar 0.237.")
    + BUL("Fixed (D035): Step 02 logged a stale XGBoost search space to the trial ledger; now the real tree space.")
    + BUL("Forward: best max_depth = 2 (search lower bound) and single-tree Optuna -> Ch.9; PCA of features and the "
          "XGBoost model in Step 01 feature importance -> Ch.8.")
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
