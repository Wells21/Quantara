"""Installer and edge-runtime patcher for the V2 live streaming hot path."""

from __future__ import annotations

from types import ModuleType
from typing import Any

import pandas as pd

from . import state as s
from .absorption import install_absorption_event_gate
from .continuation import (
    load_verified_ffd_features_hot,
    load_verified_hmm_features_hot,
    refresh_verified_feature_dependencies_hot,
    verify_asset_ffd_manifest_hot,
    verify_asset_hmm_regime_manifest_hot,
)

HOTPATH_VERSION = s.HOTPATH_VERSION


def _patch_function_globals(function: Any, asset: str, seen: set[int]) -> int:
    if (
        not callable(function)
        or not hasattr(function, "__globals__")
        or id(function) in seen
    ):
        return 0
    seen.add(id(function))
    globals_map = function.__globals__
    patched = 0
    if "load_verified_ffd_features" in globals_map:

        def ffd_loader(requested_asset: str = asset) -> pd.DataFrame:
            return load_verified_ffd_features_hot(requested_asset)

        globals_map["load_verified_ffd_features"] = ffd_loader
        patched += 1
    if "load_verified_regime_features" in globals_map:

        def hmm_loader(requested_asset: str = asset) -> pd.DataFrame:
            return load_verified_hmm_features_hot(requested_asset)

        globals_map["load_verified_regime_features"] = hmm_loader
        patched += 1
    if "verify_asset_ffd_manifest" in globals_map:
        globals_map["verify_asset_ffd_manifest"] = verify_asset_ffd_manifest_hot
        patched += 1
    if "verify_asset_hmm_regime_manifest" in globals_map:
        globals_map["verify_asset_hmm_regime_manifest"] = (
            verify_asset_hmm_regime_manifest_hot
        )
        patched += 1
    return patched


def patch_runtime_shared_loaders(runtime: ModuleType) -> dict[str, Any]:
    asset = s.asset_key(getattr(runtime, "ASSET", ""))
    if not asset:
        return {
            "runtime": getattr(runtime, "__name__", "unknown"),
            "patched": 0,
            "reason": "missing_asset",
        }
    seen: set[int] = set()
    patched = 0
    for value in list(vars(runtime).values()):
        patched += _patch_function_globals(value, asset, seen)
        if callable(value) and hasattr(value, "__globals__"):
            for nested in list(value.__globals__.values()):
                if callable(nested):
                    patched += _patch_function_globals(nested, asset, seen)
    return {
        "runtime": getattr(runtime, "__name__", "unknown"),
        "asset": asset,
        "patched": patched,
        "absorption_event_first_gate": install_absorption_event_gate(runtime),
        "hotpath_version": HOTPATH_VERSION,
    }


def install_hotpath_verifiers() -> dict[str, Any]:
    with s.INSTALL_LOCK:
        if s.INSTALLED:
            return {
                "installed": True,
                "already_installed": True,
                "version": HOTPATH_VERSION,
            }
        import shared.ffd.verify_asset_ffd_manifest as ffd_module
        import shared.regime.verify_asset_hmm_regime_manifest as hmm_module

        s.FFD_MODULE = ffd_module
        s.HMM_MODULE = hmm_module
        s.ORIGINAL_FFD_VERIFY = ffd_module.verify_asset_ffd_manifest
        s.ORIGINAL_HMM_VERIFY = hmm_module.verify_asset_hmm_regime_manifest
        ffd_module.verify_asset_ffd_manifest = verify_asset_ffd_manifest_hot
        hmm_module.verify_asset_hmm_regime_manifest = (
            verify_asset_hmm_regime_manifest_hot
        )
        s.INSTALLED = True
        return {
            "installed": True,
            "already_installed": False,
            "version": HOTPATH_VERSION,
            "ffd_mode": "authoritative_once_then_fixed_width_append",
            "hmm_mode": "authoritative_once_then_causal_state_append",
            "fail_closed_fallback": True,
            "in_memory_dependency_refresh": True,
        }


def cache_status() -> dict[str, Any]:
    return s.cache_status()


__all__ = [
    "HOTPATH_VERSION",
    "cache_status",
    "install_hotpath_verifiers",
    "load_verified_ffd_features_hot",
    "load_verified_hmm_features_hot",
    "patch_runtime_shared_loaders",
    "refresh_verified_feature_dependencies_hot",
    "verify_asset_ffd_manifest_hot",
    "verify_asset_hmm_regime_manifest_hot",
]
