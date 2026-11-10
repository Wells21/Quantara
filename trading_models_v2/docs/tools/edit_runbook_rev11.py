"""Quantara_Runbook.docx Revision 11 (2026-10-23): AFML Ch.4 walkthrough additions.

Adds: revision note; Section 6A Step 10B (sample weights: single class balance, label_t1 spans, time decay c=1,
mandatory edge patch check); Appendix 28 (AFML Ch.4 record). Inserts Word XML at unique anchor paragraphs and fails
if an anchor is missing or not unique. Source of truth: docs/mldp/AFML_MLAM_WALKTHROUGH.md section 4D; D029-D030.

usage: python edit_runbook_rev11.py IN_DOCX OUT_DOCX
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
replace_once("Revision date: 23 October 2026 - Revision 10 (Revision 9: 23 October 2026;",
             "Revision date: 23 October 2026 - Revision 11 (Revision 10: 23 October 2026; Revision 9: 23 October 2026;")
insert_after_para(
    "Revision 10 (23 October 2026): AFML Chapter 3.",
    P("Revision 11 (23 October 2026): AFML Chapter 4. Adds Section 6A Step 10B (sample weights: class balance applied "
      "once, spans on the training label's t1, time decay wired at c = 1) and Appendix 28 (the Chapter 4 record)."))

insert_before_para(
    f"6A {DOT} STEP 12B - GATE D: meta-model discrimination",
    H2(f"6A {DOT} STEP 10B - sample weights check before Steps 01-03 (Rev 11, AFML Ch.4 pp.59-73)")
    + P("Steps 01-03 weight each event by AFML return attribution (Snippet 4.10) on the quote-tick bar clock, with "
        "uniqueness (Snippets 4.1/4.2) and the sequential bootstrap (Snippets 4.3-4.5) computed on the training "
        "label's own t1 (Task 2A label_t1, fallback gross_t1, snapped to the bar that contains it). Class balance is "
        "applied once, by the trees' class_weight='balanced' on each bootstrap draw (s.4.8). Time decay (Snippet "
        "4.11) is wired with TIME_DECAY_C = 1.0 (no decay) until Ch.9 (D029).")
    + BUL("A new edge copied from a sibling must carry weight contract "
          "quantara_afml_ch4_bar_clock_return_attribution_v2_label_t1. Run the test below before Step 01; if an old "
          "template slipped in, run the patch tool.")
    + BUL("The label audit (Step 12D) reports mean uniqueness, the effective number of independent outcomes (sum of "
          "uniqueness, typically ~0.2 x rows) and standard vs sequential bootstrap uniqueness. A STALE_BAR_CLOCK status "
          "means the research dataset predates the bars: re-export before training.")
    + WARN("Models trained before 2026-10-23",
           "All 6 current edge models were trained with class balance applied twice (D030). They are retrained once, "
           "after the Ch.6/7/9 reviews, as new versions with Gates C/D/E re-run.")
    + CODE(["python -m unittest tests.test_afml_ch4_weights",
            "python ./docs/tools/patch_afml_ch4_sample_weights.py --edges <ASSET>/<family>_<side>   # only if the test fails"]))

appendix = (
    H1("28. Appendix: 2026-10-23 AFML Chapter 4 Walkthrough (Revision 11)")
    + P("Chapter 4 (sample weights, pp.59-73) checked against every edge. Record: docs/mldp/AFML_MLAM_WALKTHROUGH.md "
        "section 4D; deviations D029-D030.")
    + BUL("Matches the book: overlapping outcomes kept; concurrency and average uniqueness (Snippets 4.1/4.2); "
          "sequential bootstrap reproducing the s.4.5.3 worked example and raising uniqueness on real edges "
          "(e.g. 0.207 -> 0.232); return attribution scaled to sum I (Snippet 4.10); balanced class weights; OOB "
          "accuracy never used.")
    + BUL("Fixed (D030): class balance was applied twice in Steps 02/03 (no-information leaf 0.62 instead of 0.50); "
          "weight spans moved from gross_t1 to label_t1. Retraining deferred to one batch after Ch.6/7/9.")
    + BUL("D029: time decay implemented (Snippet 4.11) but c = 1 until Ch.9.")
    + BUL("Event clustering (Ch.2 item) closed: 86-92% of events start while an earlier one is open; the book's "
          "remedy is uniqueness weighting plus the sequential bootstrap (p.62: do not drop overlaps), already in "
          "place. Effective independent outcomes per edge ~870-1,280 (sum of uniqueness). Overlapping live bets on "
          "one edge belong to Ch.10 (averaging active bets) and Ch.14 (bet merging).")
    + BUL("Purge/embargo still use gross_t1; reviewed in Ch.7.")
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
