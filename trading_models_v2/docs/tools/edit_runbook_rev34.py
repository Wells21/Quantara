"""Quantara_Runbook.docx Revision 34 (2026-11-02): MLAM Ch.2 -- backtest trials clustered on the denoised (+ conditionally
detoned) correlation (D076), nFacts floor removed, bWidth .01 kept (owner approved 1, 2 and 3).

Helpers as in edit_runbook_rev33.py.

usage: python edit_runbook_rev34.py IN_DOCX OUT_DOCX
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


RULE = "config/mldp_deviation_register.json D076"
TRIAL = ("the trials' return correlation is denoised first (MLAM p.24, p.87; Snippets 2.4-2.5, bWidth .01, nFacts with no "
         "floor, constant residual eigenvalue) and detoned when the market test passes (as Step 07: first eigenvalue > "
         "lambda+ and first-eigenvector loadings of one sign in >= 95 % of 1,000 iid row bootstraps, seed 0) -- D076, "
         "shared/backtest_statistics/afml_ch14_dsr.py")

# -- header ----------------------------------------------------------------------------------------------------------
ns["replace_once"]("Revision date: 2 November 2026 - Revision 33 (Revision 32: 2 November 2026;",
                   "Revision date: 2 November 2026 - Revision 34 (Revision 33: 2 November 2026; Revision 32: 2 November 2026;")
after_para("Revision 33 (2 November 2026): D075 amended (owner approved)",
           P("Revision 34 (2 November 2026): MLAM Chapter 2. Backtest trials are clustered (ONC E[K] for DSR / FWER, 07b "
             "portfolio trials, threshold PBO) on the denoised, conditionally detoned correlation (D076); the shared "
             "denoise function's floor of 1 signal factor removed; KDE bandwidth .01 kept. Section 2, Section 6A Steps 7 "
             "and 10L, Appendix 51."))

# -- Section 2 --------------------------------------------------------------------------------------------------------
after_para("Rev 33 - Gate 0 is the screen, promotion is the strict bar",
           BUL("Rev 34 - denoise before clustering or inverting (MLAM p.24, p.87; D076): every correlation matrix that is "
               "clustered (Step 07 HRP, backtest-trial ONC) is Marcenko-Pastur denoised, and detoned only when a market "
               "component is shown by the bootstrap test. Step 01's variation-of-information matrix is the exception "
               "(MLAM Ch.3; Marcenko-Pastur does not apply)."))

# -- Step 7 pay / Ch.14 text ------------------------------------------------------------------------------------------
set_para("Pay -- all mandatory, nothing optional: - PBO / CSCV over EVERY replayed configuration",
         "Pay -- all mandatory, nothing optional: - PBO / CSCV over EVERY replayed configuration of the run (D051): PBO < "
         "0.5 AND Prob[OOS <= 0] < 0.5. - Level-1 DSR over every Gate C trial of the edge (ONC E[K] on the denoised, "
         "conditionally detoned trial correlation (D076), Lopez de Prado & Lewis 2018 cluster weights): DSR > 0.95 AND "
         "FWER alpha_K < 0.05 (D054); Level 2 (thesis search on the asset) reported alongside. - Strategy risk on bets "
         "P[p < p_theta] <= .05 (theta 0, k 2; D055).")
set_para("AFML third law (p.205): every backtest result is reported with ALL the trials that produced it.",
         "AFML third law (p.205): every backtest result is reported with ALL the trials that produced it. DSR = PSR[SR*], "
         "SR* = the expected maximum Sharpe of the trials under no skill (AFML p.204; MLAM s.8.5). MLAM s.8.7 revises N "
         "and V: E[K] = ONC clusters of the trials' return correlations (Rev 34: " + TRIAL + "); V from one series per "
         "cluster weighted by Lopez de Prado & Lewis (2018) Snippet 4 getIVP (robust minimum-variance approximation), "
         "annualised, rescaled to the selected strategy's frequency. MLAM s.8.8: FWER alpha_K = 1 - (1 - alpha)^K and "
         "Type II power.")
after_para("07 (D010, MLAM Snippets 2.4-2.5 pp.27-29): Marcenko-Pastur fit",
           BUL("Rev 34 (D076): 07b (portfolio trials) and the D057 threshold PBO cluster their trials with the same rule "
               "(afml_ch14_dsr.trial_clustering_corr, rule mlam_ch2_denoise_conditional_detone_v1); the shared "
               "denoise_correlation has no floor of 1 signal factor (Snippet 2.4); bWidth .01 (T2: nFacts identical at "
               "0.005-0.1 and the cross-validated bandwidth)."))

# -- Appendix 51 ------------------------------------------------------------------------------------------------------
appendix = (
    H1("51. Appendix: 2026-11-02 MLAM Chapter 2 (Denoising and Detoning) -- D076")
    + BUL("Audit (walkthrough section 4X): code matches Snippets 2.1-2.6; Step 07 book-exact since Rev 25. Gaps: backtest "
          "trials clustered on the RAW correlation (DSR E[K], 07b, threshold PBO) although MLAM p.24 assumes every "
          "correlation matrix denoised and p.87 asks to denoise and detone before clustering (s.4N.1's 'the section does "
          "not ask for it' was wrong); denoise_correlation floored nFacts at 1.")
    + BUL("Tests (pre-registered, logs/mlam_ch2_tests_20261102/): denoising alone changes nothing (E[K] 2, DSR within "
          "0.001); every trial matrix has a market component (first eigenvalue 308-325 of 406; bootstrap share 1.000). "
          "Denoised + detoned E[K] / DSR / FWER: GBPJPY 4 / 0.370 / 0.168 (was 2 / 0.685 / 0.088); HK 2 / 0.738 / 0.108 "
          "(2 / 0.650 / 0.108); JPN rangestructure 3 / 0.707 / 0.057 (2 / 0.938 / 0.038: FWER pass -> fail); XAUUSD "
          "rangestructure 3 / 0.998 / 0.0002 (unchanged). DSR > 0.95 verdict unchanged on all 4. Bandwidth: nFacts "
          "identical at 0.005-0.1 and the cross-validated bandwidth on all 5 matrices.")
    + BUL("Owner 2026-11-02: 'Approved 1, 2 and 3'. 1: trial ONC on the denoised, conditionally detoned correlation "
          "(dsr_all_trials, 07b, allocator_threshold_pbo); stored records re-run at the RECERT. 2: nFacts floor removed "
          "(canonical snippet + the 6 Step 01 copies). 3: bWidth .01 kept. Record: " + RULE + "; standing rule "
          "DENOISE_BEFORE_CLUSTERING_D076.")
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
