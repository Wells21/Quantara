"""Quantara_Runbook.docx Revision 10 (2026-10-23): AFML Ch.3 walkthrough additions.

Adds: revision note; Section 6A Step 12D (label audit, AFML Exercises 3.4/3.5); Appendix 27 (AFML Ch.3 record).
Inserts Word XML at unique anchor paragraphs and fails if an anchor is missing or not unique. Source of truth for
the content: docs/mldp/AFML_MLAM_WALKTHROUGH.md section 4C; deviations D027-D028.

usage: python edit_runbook_rev10.py IN_DOCX OUT_DOCX
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
replace_once("Revision date: 23 October 2026 - Revision 9 (Revision 8: 23 October 2026;",
             "Revision date: 23 October 2026 - Revision 10 (Revision 9: 23 October 2026; Revision 8: 23 October 2026;")
insert_after_para(
    "Revision 9 (23 October 2026): AFML Chapter 2.",
    P("Revision 10 (23 October 2026): AFML Chapter 3. Adds Section 6A Step 12D (label audit: primary vs meta-model "
      "precision/recall/F1, AFML Exercises 3.4/3.5) and Appendix 27 (the Chapter 3 record)."))

insert_before_para(
    f"6A {DOT} STEP 13 - promote / fail",
    H2(f"6A {DOT} STEP 12D - label audit (Rev 10, AFML Ch.3 s.3.7 pp.51-53, Exercises 3.4/3.5 p.55)")
    + P("After Step 04 calibration (and again after the allocator sets final_live_threshold), run the label audit. It "
        "is a diagnostic, not a gate: it records the label population, cost per R by Event_Vol quintile (minRet "
        "evidence, D027) and the primary model alone vs primary + meta-model at the live threshold (accuracy, "
        "precision, recall, F1), plus Step 02 CPCV F1 against the approve-all baseline (D028).")
    + BUL("Output: assets/<ASSET>/edges/<edge>/artifacts/label_audit/label_audit.json. Quote the primary -> meta "
          "precision and F1 and the approved share in the promotion note.")
    + BUL("Read it with D028 in mind: a meta-model that approves ~100% of events adds nothing; the edge then trades on "
          "its primary rule alone and must be judged that way.")
    + BUL("Vertical-barrier exits are labeled by the sign of the side-adjusted return everywhere (model_rows, Task 2A, "
          "v3_horizon_lab), AFML Snippet 3.7 p.51.")
    + CODE(["python ./shared/research/label_audit.py --edges <ASSET>/<family>_<side>",
            "python ./shared/research/label_audit.py            # every edge folder on every asset"]))

appendix = (
    H1("27. Appendix: 2026-10-23 AFML Chapter 3 Walkthrough (Revision 10)")
    + P("Chapter 3 (labeling, pp.43-56) checked against every edge and the shared labelers every future edge on all "
        "10 assets uses. Record: docs/mldp/AFML_MLAM_WALKTHROUGH.md section 4C; deviations D027-D028.")
    + BUL("Matches the book: no fixed-horizon labels; volatility-scaled barriers (H-hour log-return EWM std, documented "
          "variant of Snippet 3.1); triple barrier [1,1,1] PT 2 / SL 1 resolved on the tick-mid path; vertical "
          "barrier = first bar at/after t0 + H hours; meta-label = 1 if side-adjusted return > 0 (Snippet 3.7); "
          "side-specialised primaries (s.3.7 p.53); rule primary + thesis card (s.3.8). dropLabels not needed (37-48% "
          "positives).")
    + BUL("Fixed: v3_horizon_lab wrote meta_label=0 on every vertical exit (an unused diagnostic column; Gate A-C use "
          "realized R, so no gate result changed). Now sign of return, like model_rows and Task 2A.")
    + BUL("D027: no minRet filter. Cost per R by Event_Vol quintile is 0.02-0.07R and the narrowest quintile is net "
          "positive on 5/6 edges; a cutoff picked from this data would be another trial. Re-checked at every "
          "certification by the label audit.")
    + BUL("D028 (open): meta-models do not raise F1 over the primary (s.3.7). Step 02 CPCV F1 minus approve-all: "
          "-0.003 to +0.0015; XAUUSD meta-models approve 99-100% of events. Edges passed on economics (Gates C/D/E). "
          "Scoring objective, bet sizing and metrics are revisited in Ch.9, Ch.10 and Ch.14.")
    + BUL("New tool: shared/research/label_audit.py (Exercises 3.4/3.5 report) - Section 6A Step 12D.")
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
