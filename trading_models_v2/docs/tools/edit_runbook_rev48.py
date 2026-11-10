"""Quantara_Runbook.docx Revision 48 (2026-11-09): Gate D is no longer a gate (D091); XAUUSD/roundlevelcascade_long
promoted; D090 post-hoc edges at Gate A; STATE.

Helpers as in edit_runbook_rev46.py.

usage: python edit_runbook_rev47.py IN_DOCX OUT_DOCX
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
    hits = [m for m in PARA.finditer(ns["x"]) if key in html.unescape("".join(t[1] for t in TEXT.findall(m.group(0))))]
    if len(hits) != 1:
        raise SystemExit(f"paragraph count {len(hits)} for: {key[:80]}")
    m = hits[0]
    if len(TEXT.findall(m.group(0))) != 1:
        raise SystemExit(f"paragraph is not a single run: {key[:80]}")
    return m.start(), m.end(), m.group(0)


def set_para(key, new_text):
    s, e, p = _find(key)
    p2 = TEXT.sub(lambda t: t.group(1) + escape(new_text) + t.group(3), p, count=1)
    ns["x"] = ns["x"][:s] + p2 + ns["x"][e:]


def after_para(key, xml):
    e = _find(key)[1]
    ns["x"] = ns["x"][:e] + xml + ns["x"][e:]


def para_text(key):
    return html.unescape("".join(t[1] for t in TEXT.findall(_find(key)[2])))


def edit_para(key, old, new):
    t = para_text(key)
    if t.count(old) != 1:
        raise SystemExit(f"substring count {t.count(old)} in paragraph: {key[:60]} / {old[:60]}")
    set_para(key, t.replace(old, new))


# ---------------------------------------------------------------- status line + revision note
ns["replace_once"]("Revision date: 9 November 2026 - Revision 47 (Revision 46: 4 November 2026;",
                   "Revision date: 9 November 2026 - Revision 48 (Revision 47: 9 November 2026; Revision 46: 4 November 2026;")
after_para("Revision 47 (9 November 2026): Gate 0 tests the THEORY on each asset's own events",
           P("Revision 48 (9 November 2026): Gate D is no longer a gate (owner, D091) -- it is run and reported for "
             "every edge, a FAIL never blocks config/edge_universe.json, and the allocator's threshold choice decides "
             "how the meta-model is used; XAUUSD/roundlevelcascade_long promoted; the D090 post-hoc Gate 0 passes "
             "sent to Gate A by owner order. Section 6A Steps 12B, 13, KEY RULES, STATE; Appendix 65."))

# ---------------------------------------------------------------- Step 12B
set_para("STEP 12B - GATE D v4: the meta-model must act/pass and size better than no skill (Rev 43, D078)",
         "6A - STEP 12B - GATE D v4.1: reported diagnostic, NOT a gate (Rev 48, D091; was Rev 43, D078)")
edit_para("Rev 45 (D078 v4.1, docs/mldp/rules/GATE_D_V4_D078.md;",
          "A Gate D fail is a real finding.",
          "Rev 48 (D091, owner 2026-11-09: 'make Gate D not a Gate anymore, because whatever the model learns its his "
          "business'): Gate D is run and its D1 / D2 / AUC are quoted in the promotion note, but a FAIL never stops "
          "the edge. Book: meta-labelling is an optional layer on a primary that stands alone (AFML s.3.7 p.53; MLAM "
          "s.5.5 pp.69-70); s.14.8 pp.206-207 gives overlay statistics, no pass mark. The allocator decides use: 04b "
          "sweeps live-calibrated thresholds from the lowest calibrated probability (= every trade, the primary "
          "alone) upward and 04c keeps the best DD-adjusted compound velocity. No separate primary-only design "
          "(D087 item 3 superseded).")

# ---------------------------------------------------------------- Step 13 + KEY RULES
edit_para("PASS = every EDGE-LEVEL gate in the KEY RULES chain, in order (Rev 43)",
          "CV leakage (D080), Gate D v4.1 (D078) and Level-1 DSR / FWER (D054, Step 10J) -- with the threshold-free "
          "label audit (Step 12D) and the CV leakage verdict quoted in the promotion note.",
          "CV leakage (D080) and Level-1 DSR / FWER (D054, Step 10J) -- with the threshold-free label audit (Step "
          "12D), the CV leakage verdict and the Gate D v4.1 diagnostics (not a gate since Rev 48, D091) quoted in "
          "the promotion note.")
edit_para("EDGE LEVEL (decides config/edge_universe.json)",
          "-> Gate D v4.1: D1 F1 at calibrated P(win) > b AND D2 weighted log loss (D078) ->",
          "-> Gate D v4.1 reported, NOT a gate (D091; D1 / D2 / AUC quoted) ->")
edit_para("The AFML (Ch.1-22) and MLAM (Ch.1-8, Appendices A-B) walkthrough is complete",
          "(Gate D v4.1 in force, Rev 45)", "(Gate D v4.1 a reported diagnostic since Rev 48, D091)")
edit_para("The AFML (Ch.1-22) and MLAM (Ch.1-8, Appendices A-B) walkthrough is complete",
          "Record: docs/mldp/AFML_MLAM_WALKTHROUGH.md",
          "Rev 48 (9 November 2026): Gate D is no longer a gate (D091); XAUUSD/roundlevelcascade_long passed the "
          "D090 re-check at its locked cell (C1 z 2.71, 2022+ C1 +0.041, 3.02 events/week) and is PROMOTED "
          "(config/edge_universe.json: 1 edge; Gate D D1 fail, AUC 0.52, quoted); next for it: the allocator, "
          "strategy risk, Gate E. T18 closed (all 6 dead at Gate C). The D090 post-hoc passes (NASDAQ short; XAUUSD "
          "T20 long/short; BTCUSD T17 short, T21 long/short, T22 long/short; USDMXN T16 long) are at Gate A by "
          "owner order (triggers shared/edges/thesis_triggers.py, Gate 0 parity identical). Record: "
          "docs/mldp/AFML_MLAM_WALKTHROUGH.md")

# ---------------------------------------------------------------- Appendix 65
appendix = (
    H1("65. Appendix: 2026-11-09 Gate D Is No Longer a Gate (D091) (Revision 48)")
    + BUL("Owner 2026-11-09: 'we just need to make Gate D not a Gate anymore, because whatever the model learns its "
          "his busniess, because at the end of the day before the edge could even reach Gate D, the primary model has "
          "passed other stuff, so it shows the primary model itself is good alone' -- and the allocator 'always "
          "selects thresholds that helps compound faster, so that is why u see in some edges it selects the all "
          "trades threshold'.")
    + BUL("Books: AFML s.3.6 p.50 (the meta-model learns the size, including no bet); s.3.7 pp.52-53 (it raises F1 by "
          "filtering false positives; 'You can always add a meta-labeling layer to any primary model'); s.14.8 "
          "pp.206-207 (accuracy, precision, recall, F1, log loss to understand the overlay 'in isolation' -- no pass "
          "mark); MLAM s.5.5 pp.69-70. No book rule kills a primary because its overlay adds nothing; that was ours "
          "(Rev 43). Note: for meta-labelling the book tunes on F1, not log loss (AFML p.130, s.9.4 p.134); AUC was "
          "never a book test and stays a diagnostic.")
    + BUL("Checked in code: no allocator, live or promotion script requires a Gate D PASS; 04b's grid starts at the "
          "lowest calibrated probability (THRESHOLD_STEP 0.01), so the take-every-trade threshold is always a "
          "candidate. Sizing stays payoff-aware AFML s.10.3 on the live calibrated probability (D007, D082).")
    + BUL("First use: XAUUSD/roundlevelcascade_long. Every primary gate passed (Gate C full history +0.070 R, PBO "
          "0.140, DSR 0.992, stress +0.069 R, D090 re-check at the lock PASS); Gate D D1 FAIL (F1 0.510 vs 0.719), D2 "
          "PASS, AUC 0.52. Promoted 2026-11-09.")
    + BUL("Updated: docs/mldp/rules/GATE_D_V4_D078.md (D091 header); config/mldp_deviation_register.json (D091 new; "
          "D078, D087 status); config/research_methodology_standing_rules.json; config/edge_universe.json.")
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
