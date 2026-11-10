"""Phase D.4 exact candidate continuation infrastructure."""

from .base import (
    CandidateContinuationError,
    CandidateProviderDescriptor,
    CandidateSourceIdentity,
    extract_candidate_source_identity,
)
from .registry import create_candidate_provider, discover_candidate_providers

__all__ = [
    "CandidateContinuationError",
    "CandidateProviderDescriptor",
    "CandidateSourceIdentity",
    "create_candidate_provider",
    "discover_candidate_providers",
    "extract_candidate_source_identity",
]
