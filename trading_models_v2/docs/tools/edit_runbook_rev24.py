"""Quantara_Runbook.docx Revision 24 (2026-10-29): GEOMETRY RULE D050-A (owner decision) -- search the grid and pay for every trial.

The rule block is read from docs/mldp/rules/GEOMETRY_RULE_D050A.md (the canonical file), so later revisions
regenerate the [RULE D050-A] block from the same source instead of retyping it.

Helpers are the Revision 17 editor's (docs/tools/edit_runbook_rev17.py head), as in edit_runbook_rev18.py - rev23.py.

usage: python edit_runbook_rev24.py IN_DOCX OUT_DOCX
"""
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
head = (HERE / "edit_runbook_rev17.py").read_text(encoding="utf-8")
head = head[: head.index('replace_once("Revision date: 24 October 2026 - Revision 16')]
ns: dict = {"__name__": "rev17_head"}
src, dst = Path(sys.argv[1]), Path(sys.argv[2])
sys.argv = [sys.argv[0], str(src), str(dst)]
exec(compile(head, "edit_runbook_rev17.py(head)", "exec"), ns)
P, H1, H2, BUL, CODE, WARN = ns["P"], ns["H1"], ns["H2"], ns["BUL"], ns["CODE"], ns["WARN"]
DOT = ns["DOT"]

import re as _re
RULE_MD = (HERE.parents[1] / "docs" / "mldp" / "rules" / "GEOMETRY_RULE_D050A.md").read_text(encoding="utf-8")


def _section(md, title):
    m = _re.search(r"^## " + _re.escape(title) + r".*?$(.*?)(?=^## |\Z)", md, flags=_re.M | _re.S)
    return m.group(1).strip() if m else ""


def _items(block):
    out, cur = [], None
    for line in block.splitlines():
        s = line.rstrip()
        st = s.strip()
        if _re.match(r"^(\d+\.|-)\s", st) and not s.startswith("   "):
            if cur:
                out.append(cur)
            cur = _re.sub(r"^(\d+\.|-)\s+", "", st)
        elif st and cur is not None:
            cur += " " + st
    if cur:
        out.append(cur)
    return [_re.sub(r"[*`]", "", t) for t in out]


rule_items = _items(_section(RULE_MD, "THE RULE"))
not_allowed = _items(_section(RULE_MD, "What is NOT allowed"))
basis = _items(_section(RULE_MD, "Book basis"))
assert len(rule_items) == 5 and not_allowed and basis, "canonical rule file changed shape -- update the editor"

ns["replace_once"]("Revision date: 27 October 2026 - Revision 23 (Revision 22: 27 October 2026;",
                   "Revision date: 29 October 2026 - Revision 24 (Revision 23: 27 October 2026; Revision 22: 27 October 2026;")
ns["insert_after_para"](
    "Revision 23 (27 October 2026): AFML Chapter 15",
    P("Revision 24 (29 October 2026): GEOMETRY RULE D050-A (owner decision 2026-10-29): the 405-cell grid search stays and "
      "every trial is paid for (full-N PBO, all-trials DSR / FWER, strategy risk; trials accumulate across re-selection "
      "runs). Adds the [RULE D050-A] block to Section 6A Step 7 and Appendix 41 (the D050 analysis)."))
ns["replace_once"]("GATES A / B / C + PBO: GEOMETRY SELECTION",
                   "GATES A / B / C + PBO: GEOMETRY SELECTION (GEOMETRY RULE D050-A: search and pay)")
ns["insert_before_para"](
    "Only Gate C locks a geometry.",
    P("[RULE D050-A] GEOMETRY RULE D050-A -- IN FORCE since 29 October 2026 (owner). Canonical text: "
      "docs/mldp/rules/GEOMETRY_RULE_D050A.md. Register D050.")
    + "".join(BUL(t) for t in basis)
    + "".join(BUL(t) for t in rule_items)
    + P("NOT allowed:") + "".join(BUL(t) for t in not_allowed)
    + P("[END RULE D050-A]"))
ns["replace_once"]("Re-selection rule: every LIVE edge is re-run from Gate A whenever the cost model or label definition changes",
                   "Re-selection rule (D050-A: the re-selection run's cells JOIN the edge's trial record -- trials accumulate, "
                   "never reset; pass every run's stamps to dsr_all_trials): every LIVE edge is re-run from Gate A whenever the "
                   "cost model or label definition changes")
appendix = (
    H1("41. Appendix: 2026-10-29 GEOMETRY RULE D050-A (Revision 24)")
    + BUL("D050 analysis (each deployed edge's full Gate C run): GBPJPY best of 405 +0.087 R vs median cell -0.008 R, 34 % "
          "of cells profitable; HK +0.074 vs -0.042, 8 %; JPN +0.099 vs +0.008, 68 %; XAUUSD +0.135 vs +0.050, 100 %. "
          "PSR[0] without search vs DSR with the 406 trials: 0.955 / 0.685, 0.945 / 0.650, 0.981 / 0.938, 1.000 / 0.998.")
    + BUL("Decision history 2026-10-29: option B (geometry fixed on the thesis card) was approved, then option C (2-3 "
          "pre-registered geometries) was preferred, then the owner chose option A (search the grid, pay for every trial), "
          "the book's second legitimate path. The B / C drafts were reverted; D050-A is the rule.")
)
x = ns["x"]
i = x.rfind("<w:sectPr")
x = x[:i] + appendix + x[i:]
zin = ns["zin"]
tmp = Path(tempfile.mkdtemp()) / "out.docx"
with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
    for item in zin.infolist():
        data = x.encode("utf-8") if item.filename == "word/document.xml" else zin.read(item.filename)
        zout.writestr(item, data)
shutil.move(str(tmp), dst)
print(f"written {dst}")
