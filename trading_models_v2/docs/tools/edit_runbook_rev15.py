"""Quantara_Runbook.docx Revision 15 (2026-10-24): AFML Ch.8 (+ MLAM Ch.2-4, 6) walkthrough additions.

Adds: revision note; Section 6A Step 10D (Step 01 = MLAM clustered feature importance); Appendix 32 (Ch.8 record).
Inserts Word XML at unique anchor paragraphs and fails if an anchor is missing or not unique. Source of truth:
docs/mldp/AFML_MLAM_WALKTHROUGH.md section 4H; deviations D038-D044.

usage: python edit_runbook_rev15.py IN_DOCX OUT_DOCX
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
replace_once("Revision date: 24 October 2026 - Revision 14 (Revision 13: 24 October 2026;",
             "Revision date: 24 October 2026 - Revision 15 (Revision 14: 24 October 2026; Revision 13: 24 October 2026;")
insert_after_para(
    "Revision 14 (24 October 2026): AFML Chapter 7.",
    P("Revision 15 (24 October 2026): AFML Chapter 8 with MLAM Chapters 2-4 and 6. Adds Section 6A Step 10D (Step 01 "
      "feature selection = MLAM clustered feature importance) and Appendix 32 (the Chapter 8 record)."))

insert_before_para(
    f"6A {DOT} STEP 12B - GATE D: meta-model discrimination",
    H2(f"6A {DOT} STEP 10D - Step 01 feature selection (Rev 15, AFML Ch.8, MLAM Ch.6)")
    + P("Step 01 runs one pass of MLAM clustered feature importance (shared/features/clustered_feature_importance.py): "
        "the candidate features are clustered with ONC (MLAM Snippets 4.1-4.2) on the normalised variation-of-"
        "information observation matrix, PCA-reduced per MLAM p.55; clustered MDA (out-of-sample, AFML-weighted neg log "
        "loss, 10-fold purged + embargoed) and clustered MDI are measured with MLAM Snippet 6.8's trees (max_features=1, "
        "balanced, min_weight_fraction_leaf 0.05 per D041, 1,000 trees, sequential bootstrap); a cluster is kept only if "
        "its MDA mean minus 1.645 standard errors is above zero (rule B, MLAM p.79 / p.89).")
    + BUL("If no cluster survives, the edge fails Step 01: its features carry no detectable out-of-sample information "
          "(AFML p.116).")
    + BUL("Runs in repo mode only (flat Colab retired for Step 01); ~3 minutes per edge with QUANTARA_STEP01_WORKERS=4-6.")
    + BUL("Deviations in force: D039 (sequential bootstrap + purged CV), D040 (seeded k-means), D041 (early stopping 5%), "
          "D042 (no residual features), D043 (no SFI). Any new deviation is proposed with evidence and approved first.")
    + CODE(["set QUANTARA_STEP01_WORKERS=4",
            "python ./assets/<ASSET>/edges/<edge>/research/01_feature_surgery_clustered_mda.py",
            "python -m unittest tests.test_afml_ch8_clustered_importance",
            "python ./docs/tools/patch_afml_ch8_step01.py --edges <ASSET>/<edge>   # only if the wiring test fails"]))

appendix = (
    H1("32. Appendix: 2026-10-24 AFML Chapter 8 Walkthrough (Revision 15)")
    + P("Chapter 8 (feature importance, pp.113-127) checked against every edge, with the MLAM methods that revise it "
        "(owner rule: where MLAM revises AFML, follow MLAM in full). Record: docs/mldp/AFML_MLAM_WALKTHROUGH.md section "
        "4H; deviations D038-D044.")
    + BUL("Finding: the old Step 01 formed 2 clusters of ~80 features on 3 of 6 edges and removed nothing (HK, JPN "
          "rangestructure, XAUUSD rangestructure kept all 157), used XGBoost, and looped up to 50 refits.")
    + BUL("Validation: MLAM's own experiment reproduced book-exact (6 clusters; noise cluster MDA 0.0006 vs 0.14-0.23).")
    + BUL("Edge audit: fully grown trees scored worse than a coin flip out of sample; 5% early stopping fixed it "
          "(GBPJPY -0.703 -> -0.678, XAUUSD rs -0.705 -> -0.668). XAUUSD volatilitycarry has no significant cluster.")
    + BUL("Thesis-field test (owner request): adding each edge's own event fields gave no OOS improvement (paired t "
          "<= 1.33 on 4 edges); uniform feature schema kept.")
    + BUL("New: Step 01 wired to the shared module (D044), 3 minutes per edge; diagnostics feature_importance_audit.py, "
          "cross_edge_feature_importance.py, mlam_ch6_synthetic_replication.py.")
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
