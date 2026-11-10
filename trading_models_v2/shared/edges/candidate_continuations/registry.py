from __future__ import annotations

"""Dynamic source-signature provider discovery for Phase D.4."""

import importlib
import pkgutil
from types import ModuleType
from typing import Any, Iterable, Mapping, Optional

from .base import CandidateContinuationError, CandidateProviderDescriptor

PROVIDER_PACKAGE = "shared.edges.candidate_continuations.providers"


def _descriptor(module: ModuleType) -> CandidateProviderDescriptor:
    spec = getattr(module, "PROVIDER_SPEC", None)
    if not isinstance(spec, Mapping):
        raise CandidateContinuationError(
            f"{module.__name__}: PROVIDER_SPEC mapping missing"
        )
    provider_id = str(spec.get("provider_id") or "").strip()
    version = str(spec.get("provider_version") or "").strip()
    signatures = spec.get("supported_signatures")
    if not provider_id or not version:
        raise CandidateContinuationError(
            f"{module.__name__}: provider identity/version missing"
        )
    if not isinstance(signatures, Mapping) or not signatures:
        raise CandidateContinuationError(
            f"{module.__name__}: supported_signatures must be a non-empty mapping"
        )
    if not callable(getattr(module, "create_provider", None)):
        raise CandidateContinuationError(
            f"{module.__name__}: create_provider callable missing"
        )

    normalized = tuple(sorted(str(value) for value in signatures))
    if any(len(value) != 64 for value in normalized):
        raise CandidateContinuationError(
            f"{module.__name__}: invalid candidate_logic_signature"
        )
    return CandidateProviderDescriptor(
        provider_id=provider_id,
        provider_version=version,
        module_name=module.__name__,
        supported_signatures=normalized,
    )


def _default_module_names() -> list[str]:
    package = importlib.import_module(PROVIDER_PACKAGE)
    return sorted(
        f"{PROVIDER_PACKAGE}.{info.name}"
        for info in pkgutil.iter_modules(package.__path__)
        if not info.name.startswith("_")
    )


def discover_candidate_providers(
    module_names: Optional[Iterable[str]] = None,
) -> dict[str, CandidateProviderDescriptor]:
    names = _default_module_names() if module_names is None else sorted(set(module_names))
    out: dict[str, CandidateProviderDescriptor] = {}
    for name in names:
        descriptor = _descriptor(importlib.import_module(name))
        for signature in descriptor.supported_signatures:
            if signature in out:
                other = out[signature]
                raise CandidateContinuationError(
                    "Duplicate candidate continuation provider binding for "
                    f"{signature}: {other.module_name} and {descriptor.module_name}"
                )
            out[signature] = descriptor
    return out


def create_candidate_provider(
    candidate_logic_signature: str,
    *,
    d1_edge: Mapping[str, Any],
    v2_root: str,
) -> Any:
    signature = str(candidate_logic_signature or "").strip()
    registry = discover_candidate_providers()
    descriptor = registry.get(signature)
    if descriptor is None:
        raise CandidateContinuationError(
            "No candidate continuation provider installed for "
            f"candidate_logic_signature={signature}"
        )
    module = importlib.import_module(descriptor.module_name)
    return module.create_provider(
        candidate_logic_signature=signature,
        d1_edge=d1_edge,
        v2_root=v2_root,
    )
