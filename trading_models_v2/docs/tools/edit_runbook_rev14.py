"""Quantara_Runbook.docx Revision 14 (2026-10-24): AFML Ch.7 walkthrough additions.

Adds: revision note; Section 6A Step 12E (purge on the label t1, embargo max(100, 1% of rows), CV leakage audit);
Appendix 31 (AFML Ch.7 record). Inserts Word XML at unique anchor paragraphs and fails if an anchor is missing or
not unique. Source of truth: docs/mldp/AFML_MLAM_WALKTHROUGH.md section 4G; deviations D036-D037.

usage: python edit_runbook_rev14.py IN_DOCX OUT_DOCX
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
replace_once("Revision date: 24 October 2026 - Revision 13 (Revision 12: 23 October 2026;",
             "Revision date: 24 October 2026 - Revision 14 (Revision 13: 24 October 2026; Revision 12: 23 October 2026;")
insert_after_para(
    "Revision 13 (24 October 2026): AFML Chapter 6.",
    P("Revision 14 (24 October 2026): AFML Chapter 7. Adds Section 6A Step 12E (purging on the training label's t1, "
      "an embargo that scales with the data, and the CV leakage audit) and Appendix 31 (the Chapter 7 record)."))

insert_before_para(
    f"6A {DOT} STEP 13 - promote / fail",
    H2(f"6A {DOT} STEP 12E - purged CV and leakage audit (Rev 14, AFML Ch.7 pp.103-111)")
    + P("Every train/test split in Steps 01-04 (feature surgery, Optuna, OOF, calibration) is purged and embargoed: a "
        "training event is dropped if its [t0, t1] overlaps any test event's (t1 = the Task 2A training-label end "
        "label_t1, fallback gross_t1; AFML Snippet 7.1), and the max(100, ceil(1% of rows)) events after each test "
        "block are embargoed (s.7.4.2, h ~ .01T). Never shuffle, never use cross_val_score or out-of-bag scores.")
    + BUL("After Step 03, run the CV leakage audit (diagnostic, not a gate): 10-fold unshuffled vs shuffled vs purged, "
          "and the k = 5/10/20/40 sweep (p.107: purged performance must not keep rising with k). Verdict LEAK_SUSPECT "
          "means the purge/embargo is not stopping leakage on this edge: stop and investigate before Gate D.")
    + BUL("Quote the verdict and the shuffle inflation (shuffled minus purged AUC) in the promotion note.")
    + CODE(["python ./shared/research/cv_leakage_audit.py --edges <ASSET>/<family>_<side>",
            "python -m unittest tests.test_afml_ch7_cv",
            "python ./docs/tools/patch_afml_ch7_cv.py --edges <ASSET>/<family>_<side>   # only if the test fails"]))

appendix = (
    H1("31. Appendix: 2026-10-24 AFML Chapter 7 Walkthrough (Revision 14)")
    + P("Chapter 7 (cross-validation in finance, pp.103-111) checked against every edge. Record: "
        "docs/mldp/AFML_MLAM_WALKTHROUGH.md section 4G; deviations D036-D037.")
    + BUL("Matches the book: no shuffled CV anywhere; purge covers all three overlap conditions (Snippet 7.1) in Steps "
          "01-04; embargo after each test block; leakage also limited by regularised, avgU-sampled bagged trees "
          "(s.7.3); cross_val_score and out-of-bag scores never used.")
    + BUL("Measured (Ex 7.2/7.3, p.107): shuffling inflates AUC on 3 of 4 edges (e.g. HK 0.543 -> 0.553); purged AUC "
          "does not rise with k = 5..40; purging on label_t1 vs gross_t1 changes AUC by <= 0.001.")
    + BUL("Changed (D036): purge on label_t1 (fallback gross_t1), embargo max(100, 1% of rows); new "
          "shared/research/cv_leakage_audit.py run at each certification.")
    + BUL("Open (D037): test-fold scoring is unweighted F1, the book's cvScore (Snippet 7.4) uses sample-weighted "
          "log loss -> Ch.9.")
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
