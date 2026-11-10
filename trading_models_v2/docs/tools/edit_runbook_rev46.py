"""Quantara_Runbook.docx Revision 46 (2026-11-04): row floor removed (D085); Gate 0 records of the NASDAQ campaign
(D075 rule C universe, tiered drift bar, D083 H1_V1 cell, gate0_tool.py, D084 mechanism features); STATE.

Helpers as in edit_runbook_rev45.py.

usage: python edit_runbook_rev46.py IN_DOCX OUT_DOCX
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
ns["replace_once"]("Revision date: 4 November 2026 - Revision 45 (Revision 44: 4 November 2026;",
                   "Revision date: 4 November 2026 - Revision 46 (Revision 45: 4 November 2026; Revision 44: 4 November 2026;")
after_para("Revision 45 (4 November 2026): Gate D v4.1 in force",
           P("Revision 46 (4 November 2026): the >= 2000 model-row floor is removed (owner, D085) -- an edge goes "
             "through the pipeline with the event count its own thesis produces and the statistical gates judge it; "
             "Gate 0 records of the NASDAQ campaign: D075 rule C universe, the tiered drift bar, the D083 H1_V1 cell, "
             "the Gate 0 tool, the D084 mechanism features. Section 6A Steps 4, 6B, 7, 13, KEY RULES, STATE; "
             "Appendix 63."))

# ---------------------------------------------------------------- Step 4: D084
after_para("Every thesis must be tried on EVERY asset and BOTH sides",
           P("Rev 46 (D084, owner 2026-11-04, code pending): four mechanism features -- the day's move up to the entry "
             "time, realised volatility, an options-expiry calendar (monthly third Friday, daily 0DTE) and days to "
             "month-end -- are built once in the shared feature pipeline (research = live, Phase C certifiable) and "
             "added identically to every edge's feature_schema (uniformity rule) before the first NASDAQ campaign "
             "export; Step 01 decides which columns a model uses."))

# ---------------------------------------------------------------- Step 6B: Gate 0
edit_para("Gate 0 cell (Rev 33, D075 A):",
          "lookback 1, 4 or 8 h x vertical 4, 24 or 96 h matched to the thesis horizon;",
          "lookback 1, 4 or 8 h x vertical 4, 24 or 96 h matched to the thesis horizon (Rev 46, D083: H1_V1 -- 1 h "
          "lookback, 1 h vertical -- is allowed for a thesis whose stated horizon is about 1 hour);")
edit_para("(a) Drift baseline: the event set vs 20,000 random bars",
          "PASS = diff - 1.645 SE > 0 (one-sided 95 %, Rev 33 D075 B: the claim is directional).",
          "PASS = diff - 1.645 SE > 0 (one-sided 95 %, Rev 33 D075 B: the claim is directional). Rev 46 tiered drift "
          "bar (owner 2026-11-04): when EVERY card counterfactual passes at one-sided 95 %, the drift baseline needs "
          "one-sided 90 % (diff - 1.28 SE > 0); otherwise 95 %. The counterfactuals stay at 95 %. Why: the drift test "
          "alone has about 41 % power for a 0.05 R effect on our ~4 years of data; passing counterfactuals raise the "
          "prior that the mechanism is real (MLAM s.8.2, s.8.8.4 p.121).")
edit_para("(c) Pooled across assets (Rev 33, D075 C):",
          "A thesis firing on one asset is tested alone.",
          "A thesis firing on one asset is tested alone. Rev 46 (D075 C amended, owner 2026-11-04, cards written from "
          "that date): the card pre-registers its asset UNIVERSE -- the assets where its mechanism exists -- before "
          "any data; Gate 0 pools over that universe only and the consistency check applies within it; every asset is "
          "still computed and reported (coverage rule).")
edit_para("Tools: shared/research/counterfactual_tests/run_counterfactuals.py",
          "(a single Gate 0 tool is backlog D075)",
          "(RECERT tools); Rev 46: new theses use shared/research/gate0/gate0_tool.py <THESIS> <OUT_DIR> (venv312) -- "
          "all 10 assets per side, pooled over the card's universe, joint-SE counterfactuals, tiered drift bar; each "
          "thesis is coded in its THESES table and points to its card (a version that reads the card itself stays "
          "backlog D075)")

# ---------------------------------------------------------------- Step 7
edit_para("Rev 32/33 (D075): Gate 0 (Step 6B) must pass before this step.",
          "on the raw event set (one-sided 95 %);",
          "on the raw event set (one-sided 95 %, or 90 % under the Rev 46 tiered drift bar when every card "
          "counterfactual passed at Gate 0);")
set_para("Floor: >= 2000 model rows, strict, no exceptions.",
         "Row floor: none (Rev 46, D085, owner 2026-11-04: the >= 2000 model-row floor is removed). An edge goes "
         "through the pipeline with the event count its own thesis produces; a thin sample is judged by the "
         "statistical gates (Gate 0 and drift re-check standard errors, PBO and Prob[OOS <= 0], Gate D v4.1 "
         "bootstrap and clustered SE, DSR / FWER) and dies there if it is too thin. Still in force: a counterfactual "
         "needs >= 100 inverted events (else INCONCLUSIVE, Step 6B), and the per-asset consistency check counts "
         "assets with >= 100 events. Every gate record quotes the model-row count; the label audit quotes the "
         "effective number of independent outcomes.")

# ---------------------------------------------------------------- Step 13 + KEY RULES + STATE
edit_para("PASS = every EDGE-LEVEL gate in the KEY RULES chain",
          "the post-Gate C drift re-check (D075), the row floor, Task 2A exact mean,",
          "the post-Gate C drift re-check (D075), Task 2A exact mean,")
edit_para("PASS = every EDGE-LEVEL gate in the KEY RULES chain",
          "Gate D v4 (D078)",
          "Gate D v4.1 (D078)")
edit_para("EDGE LEVEL (decides config/edge_universe.json)",
          "Gate 0 (counterfactuals + month-matched drift, one-sided 95 %, D075)",
          "Gate 0 (counterfactuals one-sided 95 % + month-matched drift one-sided 95 %, or 90 % when every "
          "counterfactual passes; pooled over the card's universe; D075)")
edit_para("EDGE LEVEL (decides config/edge_universe.json)",
          "drift re-check at the locked geometry, one-sided 95 % (D075)",
          "drift re-check at the locked geometry, same bar as Gate 0 (D075)")
edit_para("EDGE LEVEL (decides config/edge_universe.json)",
          "export (>= 2000 rows)",
          "export (no row floor, D085)")
set_para("Calendar-time hours; EWM bar-count. 2000-row floor.",
         "Calendar-time hours; EWM bar-count. No model-row floor (Rev 46, D085). Test long AND short on every asset.")
edit_para("The AFML (Ch.1-22) and MLAM (Ch.1-8, Appendices A-B) walkthrough is complete",
          "(Gate D v4 after the owner settles backlog D078)",
          "(Gate D v4.1 in force, Rev 45). Rev 46: NASDAQ campaign under way (docs/mldp/campaign/NASDAQ_2026-11/"
          "CAMPAIGN.md; owner: at least one live long and one live short per asset, NASDAQ then US500): T1 and T2 "
          "failed Gate 0; T1b SHORT passed Gate 0 (owner-ordered post-hoc universe and tiered bar, both logged as "
          "trials) and is being built as NASDAQ/closehedgingflow_short; T1b LONG failed; T3 needs its source "
          "confirmed")

# ---------------------------------------------------------------- Section 23.4 (historical v5.2 summary): pointer
_s = _find("23.5 2026-10-17 live-edge re-certification result")[0]
ns["x"] = ns["x"][:_s] + P("Rev 46: the '>= 2000 model rows' in this v5.2 summary is removed (D085); the current gate "
                           "chain is Section 6A KEY RULES.") + ns["x"][_s:]

# ---------------------------------------------------------------- Appendix 63
appendix = (
    H1("63. Appendix: 2026-11-04 Row Floor Removed and the NASDAQ Campaign Gate 0 Records (Revision 46)")
    + BUL("Row floor removed (D085). Owner 2026-11-04: 'now that we have changed a lot of rules and gates, i dont "
          "think the edge floor matters anymore, because we have gates that will kill it regardless ... any edge with "
          "its own thesis floor gets to go through the pipeline'.\n"
          "  - Trigger: NASDAQ/closehedgingflow_short trades once per down day at 15:00 New York; its ceiling is 451 "
          "events on ~4 years of data.\n"
          "  - The floor (2000 rows since 2026-09-29) was a runbook rule, not a book rule; no code enforced it.\n"
          "  - Gates that judge a thin sample: Gate 0 and the drift re-check (week-clustered SE), PBO and Prob[OOS <= 0], "
          "Gate D v4.1 (ISO-week bootstrap, clustered SE), DSR / FWER (D054).\n"
          "  - Unchanged: counterfactuals need >= 100 inverted events; the CV embargo stays max(100, 1 % of rows).")
    + BUL("NASDAQ campaign Gate 0 records (owner 2026-11-04):\n"
          "  - D075 rule C amended: the card pre-registers its asset universe; Gate 0 pools over it.\n"
          "  - Tiered drift bar: one-sided 90 % when every counterfactual passes at 95 % (Gate 0 and the post-Gate C "
          "re-check).\n"
          "  - D083: Gate 0 cell H1_V1 allowed for about-1-hour theses.\n"
          "  - D084: four mechanism features, uniform in every schema (code pending).\n"
          "  - Tool: shared/research/gate0/gate0_tool.py.\n"
          "  - Results: T1 FAIL, T2 FAIL, T1b SHORT PASS (post-hoc universe + tiered bar), T1b LONG FAIL. Records in "
          "docs/mldp/campaign/NASDAQ_2026-11/.")
    + BUL("Updated: Section 6A Steps 4, 6B, 7, 13, KEY RULES and STATE; docs/mldp/rules/THEORY_CHECK_RULE_D075.md; "
          "config/mldp_deviation_register.json (D085 new), config/research_methodology_standing_rules.json, "
          "config/us500_edge_search_ledger.json (MINIMUM_MODEL_ROWS_LAW retired; addendum Rev 46).")
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
