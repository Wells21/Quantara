"""Quantara_Runbook.docx Revision 31 (2026-10-31): geometry grid EWM spans 20,50,100 -> 100,150,200 (register D074).

Helpers as in edit_runbook_rev30.py.

usage: python edit_runbook_rev31.py IN_DOCX OUT_DOCX
"""
import html
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
head = (HERE / "edit_runbook_rev17.py").read_text(encoding="utf-8")
head = head[: head.index('replace_once("Revision date: 24 October 2026 - Revision 16')]
ns: dict = {"__name__": "rev17_head"}
src, dst = Path(sys.argv[1]), Path(sys.argv[2])
sys.argv = [sys.argv[0], str(src), str(dst)]
exec(compile(head, "edit_runbook_rev17.py(head)", "exec"), ns)
P, H1, BUL = ns["P"], ns["H1"], ns["BUL"]

PARA = re.compile(r"<w:p[ >].*?</w:p>", re.S)
TEXT = re.compile(r"(<w:t(?: [^>]*)?>)(.*?)(</w:t>)", re.S)


def _find(key):
    """The one paragraph whose plain text contains key -> (start, end, xml)."""
    hits = [m for m in PARA.finditer(ns["x"]) if key in html.unescape("".join(t[1] for t in TEXT.findall(m.group(0))))]
    if len(hits) != 1:
        raise SystemExit(f"paragraph count {len(hits)} for: {key[:80]}")
    m = hits[0]
    if len(TEXT.findall(m.group(0))) != 1:
        raise SystemExit(f"paragraph is not a single run: {key[:80]}")
    return m.start(), m.end(), m.group(0)


def para_text(key):
    return html.unescape(TEXT.search(_find(key)[2]).group(2))


def set_para(key, new_text):
    s, e, p = _find(key)
    p2 = TEXT.sub(lambda t: t.group(1) + escape(new_text) + t.group(3), p, count=1)
    ns["x"] = ns["x"][:s] + p2 + ns["x"][e:]


def sub_para(key, old, new):
    t = para_text(key)
    if t.count(old) != 1:
        raise SystemExit(f"'{old[:60]}' occurs {t.count(old)} times in: {key[:60]}")
    set_para(key, t.replace(old, new))


def after_para(key, xml):
    e = _find(key)[1]
    ns["x"] = ns["x"][:e] + xml + ns["x"][e:]


# -- header ----------------------------------------------------------------------------------------------------------
ns["replace_once"]("Revision date: 31 October 2026 - Revision 30 (Revision 29: 31 October 2026;",
                   "Revision date: 31 October 2026 - Revision 31 (Revision 30: 31 October 2026; Revision 29: 31 October 2026;")
after_para("Revision 30 (31 October 2026): the owner-approved fixes",
           P("Revision 31 (31 October 2026): the geometry grid's EWM spans become 100,150,200 (were 20,50,100; still 405 "
             "cells), owner decision on the EWM span test (register D074); Section 6A Step 7; Appendix 48."))

# -- Section 6A: the grid ---------------------------------------------------------------------------------------------
ns["replace_once"]("h x EWM 20,50,100 x verticals", "h x EWM 100,150,200 (D074; was 20,50,100) x verticals")
ns["replace_once"]("8h x EWM 20,50,100 x horizon", "8h x EWM 100,150,200 x horizon")
ns["replace_once"]("--event-vol-ewm-spans 20,50,100 ", "--event-vol-ewm-spans 100,150,200 ")
ns["replace_once"]("EWM span stays bar-count.",
                   "EWM span stays bar-count (100,150,200 bars since Rev 31, D074; AFML Snippet 3.1's span0 = 100 is the lowest).")

# -- Appendix 48 ------------------------------------------------------------------------------------------------------
appendix = (
    H1("48. Appendix: 2026-10-31 EWM Span Change (D074)")
    + BUL("Grid-dimension study (shared/research/grid_dimension_sensitivity/): EWM 100 won all 12 earlier full Gate C "
          "replays; spans 20 and 50 never won. Pre-registered EWM span test (logs/ewm_span_test_20261031/PREREGISTRATION.md): "
          "Gate A + Gate C over EWM 100,150,200,300 on the 6 Gate C edges; the EWM 100 cells reproduce the old replays.")
    + BUL("Grid A {100,150,200} vs the old grid, winner compound velocity and PBO / Prob[OOS<=0]: HK dhc x1.46, "
          "0.013 / 0.338 -> 0.008 / 0.273; GBPJPY drcf x1.17, 0.153 / 0.430 -> 0.164 / 0.344; JPN rs x1.00 (same cell), "
          "0.349 / 0.400 -> 0.360 / 0.323; JPN vc (killed) x1.19, 0.119 / 0.527 -> 0.145 / 0.524 (still FAIL); XAU rs x1.23, "
          "0.085 / 0.058 -> 0.073 / 0.043; XAU vc x1.22, 0.288 / 0.171 -> 0.422 / 0.155.")
    + BUL("The pre-registered rule was not met (PBO worse on JPN vc and XAU vc). The owner chose grid A ('Go with option 2') "
          "as a recorded deviation (register D074); caveat: XAU vc PBO 0.42, still < 0.5.")
    + BUL("New Gate A command: --event-vol-lookbacks 1,2,4,6,8 --event-vol-ewm-spans 100,150,200 --vertical-horizons "
          "1,2,4,8,16,24,48,96,168 --barriers 1:1,1.5:1,2:1. Rule D050-A is unchanged. No deployed edge changes now; each "
          "edge re-selects on the new grid at the RECERT, and the 2026-10-31 test runs join its trial record.")
)
x = ns["x"]
i = x.rfind("<w:sectPr")
ns["x"] = x[:i] + appendix + x[i:]

zin = ns["zin"]
tmp = Path(tempfile.mkdtemp()) / "out.docx"
with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
    for item in zin.infolist():
        data = ns["x"].encode("utf-8") if item.filename == "word/document.xml" else zin.read(item.filename)
        zout.writestr(item, data)
shutil.move(str(tmp), dst)
print(f"written {dst}")
