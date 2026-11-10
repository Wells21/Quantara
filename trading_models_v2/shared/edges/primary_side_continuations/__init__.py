"""Phase D.4 exact primary-side continuation infrastructure."""

from .base import (
    PrimaryProviderDescriptor,
    PrimarySideContinuationError,
    PrimarySourceIdentity,
    extract_primary_source_identity,
)
from .registry import (
    create_primary_side_provider,
    discover_primary_side_providers,
)

__all__ = [
    "PrimaryProviderDescriptor",
    "PrimarySideContinuationError",
    "PrimarySourceIdentity",
    "create_primary_side_provider",
    "discover_primary_side_providers",
    "extract_primary_source_identity",
]
