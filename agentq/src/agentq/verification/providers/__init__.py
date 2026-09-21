"""Verification providers: one module per ecosystem, in detection order."""

from __future__ import annotations

from .cargo import CargoVerificationProvider
from .go import GoVerificationProvider
from .node import NodeVerificationProvider
from .python import PythonVerificationProvider

VERIFICATION_PROVIDERS = (
    NodeVerificationProvider(),
    PythonVerificationProvider(),
    CargoVerificationProvider(),
    GoVerificationProvider(),
)
PROVIDER_NAMES = tuple(provider.name for provider in VERIFICATION_PROVIDERS)

__all__ = [
    "PROVIDER_NAMES",
    "VERIFICATION_PROVIDERS",
    "CargoVerificationProvider",
    "GoVerificationProvider",
    "NodeVerificationProvider",
    "PythonVerificationProvider",
]
