from __future__ import annotations

"""Dynamic source-signature provider discovery for Phase D.4 primary-side state."""

import importlib
import pkgutil
from types import ModuleType
from typing import Any, Iterable, Mapping, Optional

from .base import (
    PrimaryProviderDescriptor,
    PrimarySideContinuationError,
)

PROVIDER_PACKAGE = "shared.edges.primary_side_continuations.providers"


def _descriptor(module: ModuleType) -> PrimaryProviderDescriptor:
    spec = getattr(module, "PROVIDER_SPEC", None)
    if not isinstance(spec, Mapping):
        raise PrimarySideContinuationError(
            f"{module.__name__}: PROVIDER_SPEC mapping missing"
        )

    provider_id = str(spec.get("provider_id") or "").strip()
    version = str(spec.get("provider_version") or "").strip()
    signatures = spec.get("supported_signatures")

    if not provider_id or not version:
        raise PrimarySideContinuationError(
            f"{module.__name__}: provider identity/version missing"
        )
    if not isinstance(signatures, Mapping) or not signatures:
        raise PrimarySideContinuationError(
            f"{module.__name__}: supported_signatures must be a non-empty mapping"
        )
    if not callable(getattr(module, "create_provider", None)):
        raise PrimarySideContinuationError(
            f"{module.__name__}: create_provider callable missing"
        )

    normalized = tuple(sorted(str(value) for value in signatures))
    if any(len(value) != 64 for value in normalized):
        raise PrimarySideContinuationError(
            f"{module.__name__}: invalid primary_logic_signature"
        )

    return PrimaryProviderDescriptor(
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


def discover_primary_side_providers(
    module_names: Optional[Iterable[str]] = None,
) -> dict[str, PrimaryProviderDescriptor]:
    names = (
        _default_module_names()
        if module_names is None
        else sorted(set(module_names))
    )
    out: dict[str, PrimaryProviderDescriptor] = {}

    for name in names:
        descriptor = _descriptor(importlib.import_module(name))
        for signature in descriptor.supported_signatures:
            if signature in out:
                other = out[signature]
                raise PrimarySideContinuationError(
                    "Duplicate primary-side continuation provider binding for "
                    f"{signature}: {other.module_name} and "
                    f"{descriptor.module_name}"
                )
            out[signature] = descriptor

    return out


def create_primary_side_provider(
    primary_logic_signature: str,
    *,
    d1_edge: Mapping[str, Any],
    v2_root: str,
) -> Any:
    signature = str(primary_logic_signature or "").strip()
    registry = discover_primary_side_providers()
    descriptor = registry.get(signature)

    if descriptor is None:
        raise PrimarySideContinuationError(
            "No primary-side continuation provider installed for "
            f"primary_logic_signature={signature}"
        )

    module = importlib.import_module(descriptor.module_name)
    return module.create_provider(
        primary_logic_signature=signature,
        d1_edge=d1_edge,
        v2_root=v2_root,
    )
