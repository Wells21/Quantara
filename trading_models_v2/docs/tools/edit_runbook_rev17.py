"""Quantara_Runbook.docx Revision 17 (2026-10-24): AFML Ch.9 walkthrough additions.

usage: python edit_runbook_rev17.py IN_DOCX OUT_DOCX
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


replace_once("Revision date: 24 October 2026 - Revision 16 (Revision 15: 24 October 2026;",
             "Revision date: 24 October 2026 - Revision 17 (Revision 16: 24 October 2026; Revision 15: 24 October 2026;")
insert_after_para(
    "Revision 16 (24 October 2026): Chapter 8 closed.",
    P("Revision 17 (24 October 2026): AFML Chapter 9. Adds Section 6A Step 10E (Step 02 hyper-parameter tuning per AFML "
      "Snippets 9.1/9.3) and Appendix 34 (the Chapter 9 record)."))
insert_before_para(
    f"6A {DOT} STEP 12B - GATE D: meta-model discrimination",
    H2(f"6A {DOT} STEP 10E - Step 02 hyper-parameter tuning (Rev 17, AFML Ch.9 pp.129-136)")
    + P("Step 02 tunes the base decision tree the AFML Snippet 9.1/9.3 way: purged + embargoed 5-fold cross-validation on "
        "the label t1; scoring F1 at the classifier's own decision (p > 0.5), the book's score for meta-labels; randomized "
        "search (seeded) over max_depth 1-8, min_samples_leaf 5-200 (log), min_samples_split 10-400 (log), "
        "min_weight_fraction_leaf 0-0.05 and ccp_alpha 1e-6-1e-2 (log), 30 trials; the tuned tree is then bagged on all "
        "rows (Ch.6). No time decay (c = 1). CPCV is not used for tuning; it stays for the Ch.12 backtest paths.")
    + BUL("A nested-CV test on 4 edges found no consistent OOS difference between tuning scores or search methods once the "
          "tree is bagged; the book's setup was kept. Time decay made results worse and stays off.")
    + CODE(["python -m unittest tests.test_afml_ch9_tuning",
            "python ./docs/tools/patch_afml_ch9_step02.py --edges <ASSET>/<family>_<side>   # only if the test fails"]))
appendix = (
    H1("34. Appendix: 2026-10-24 AFML Chapter 9 Walkthrough (Revision 17)")
    + BUL("Finding: Step 02's F1 at a tuned threshold (0.30-0.80) drove every edge toward approving all events (threshold "
          "0.30, recall 0.84-1.00; XAUUSD volatilitycarry approved everything); best max_depth = 2 = search bound everywhere.")
    + BUL("Book rule (Snippets 9.1/9.3, p.130; s.14.8): labels {0,1} (meta-labeling) -> F1; other labels -> neg log loss.")
    + BUL("Test: nested CV, 8 configurations x 30 trials x 5 outer folds per edge (5-7 min per edge, parallel).")
    + BUL("Decisions: D046 (purged 5-fold, F1 at p > 0.5, randomized search, depth 1-8); D029 closed (c = 1); D037 closed.")
    + BUL("Correction: live bets are probability-sized (AFML Ch.10 bet size x weights x caps), not fixed risk.")
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
