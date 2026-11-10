"""Local-import wrapper around the canonical MLAM denoising/ONC snippet.

The actual implementation lives at
`shared/colab_portable/denoise_onc_snippet.py` -- it is written to be
dependency-free and standalone-copyable because edge Step 01 (feature
surgery / clustered feature importance) runs on Google Colab and cannot
import `shared.*` at runtime (see that module's docstring, and
docs/mldp/A3_PURGE_EMBARGO_VERIFICATION.md for the same constraint as it
applies to Step 02). Consumers that DO run locally (e.g. portfolio_allocator
scripts, or shared/ test code) should import from here rather than reaching
into shared.colab_portable directly, so there is exactly one import path for
locally-run code.
"""
from __future__ import annotations

from shared.colab_portable.denoise_onc_snippet import (
    DenoiseONCError,
    cluster_kmeans_base,
    cluster_kmeans_top,
    corr_to_cov,
    cov_to_corr,
    denoise_correlation,
    denoised_corr_constant_residual_eigenvalue,
    denoised_corr_targeted_shrinkage,
    detone_corr,
    find_max_eval,
    fit_kde,
    get_pca,
    mp_pdf,
)

__all__ = [
    "DenoiseONCError",
    "cluster_kmeans_base",
    "cluster_kmeans_top",
    "corr_to_cov",
    "cov_to_corr",
    "denoise_correlation",
    "denoised_corr_constant_residual_eigenvalue",
    "denoised_corr_targeted_shrinkage",
    "detone_corr",
    "find_max_eval",
    "fit_kde",
    "get_pca",
    "mp_pdf",
]
