"""Default adapter composition boundary.

This package is the only place where inspection meets concrete navigation,
discovery, and workspace primitives. Adapters normalize their provider results
into inspection contracts; the service receives a registry assembled here (or
by a test) and never imports an adapter itself.
"""

from __future__ import annotations

from ..capabilities import CapabilityRegistry
from ..contracts import CapabilityHandler

__all__ = ["CapabilityRegistry", "default_handlers", "default_registry"]


def default_handlers() -> tuple[CapabilityHandler, ...]:
    """The production adapter set; explicit and replaceable."""
    return ()


def default_registry() -> CapabilityRegistry:
    return CapabilityRegistry(default_handlers())
