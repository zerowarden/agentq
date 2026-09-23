"""Internal acquisition and delivery limits.

Coding agents never set these values: acquisition limits bound what the
pipeline may collect, and the delivery budget bounds only serialized output.
Both are explicit, named configurations so that a limit change is a deliberate
policy change rather than an implicit side effect of rendering.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentq.core import ContractError, require_int

if TYPE_CHECKING:
    from .contracts import Capability

DEFAULT_DELIVERY_CHARS = 12_000
DEFAULT_ENVELOPE_CHARS = 256
DELIVERY_FORMATS = frozenset({"text", "json", "compact-json"})
# The transport terminates the rendered text with one newline; the delivery
# ceiling covers those bytes, so rendering is bounded by max_chars - this.
DELIVERY_TERMINATOR_CHARS = 1
# One code for every explicit delivery-budget reduction: omitted evidence and
# reduced ambiguity both report why the delivered answer is smaller.
DELIVERY_BUDGET_CODE = "delivery_budget"


@dataclass(frozen=True)
class AcquisitionLimits:
    """Bounded acquisition: what collection may spend before selection."""

    max_candidates: int = 80
    max_observations: int = 400
    max_source_files: int = 40
    max_source_lines: int = 240
    max_provider_calls: int = 16
    reference_limit: int = 40
    lexical_test_mentions: int = 12
    deadline_seconds: float = 10.0

    def __post_init__(self) -> None:
        for name in (
            "max_candidates",
            "max_observations",
            "max_source_files",
            "max_source_lines",
            "max_provider_calls",
            "reference_limit",
            "lexical_test_mentions",
        ):
            require_int(getattr(self, name), f"acquisition limits {name}", minimum=1)
        if isinstance(self.deadline_seconds, bool) or not isinstance(
            self.deadline_seconds, (int, float)
        ):
            raise ContractError("acquisition limits deadline_seconds must be a number")
        if self.deadline_seconds <= 0:
            raise ContractError("acquisition limits deadline_seconds must be > 0")

    def limit_for(self, capability: Capability) -> int:
        """The canonical observation limit for one capability."""
        from .contracts import Capability as CapabilityValue

        return {
            CapabilityValue.FIND_DECLARATIONS: self.max_candidates,
            CapabilityValue.RESOLVE_LOCATION: self.max_candidates,
            CapabilityValue.READ_SOURCE: self.max_source_lines,
            CapabilityValue.OUTLINE: self.max_source_files,
            CapabilityValue.SEMANTIC_REFERENCES: self.reference_limit,
            CapabilityValue.SYNTACTIC_MENTIONS: self.reference_limit,
            CapabilityValue.LEXICAL_MENTIONS: self.lexical_test_mentions,
            CapabilityValue.IMPLEMENTATIONS: self.reference_limit,
            CapabilityValue.OWNING_PACKAGE: 1,
        }[capability]


@dataclass(frozen=True)
class DeliveryBudget:
    """Bounded delivery: the serialized output ceiling, envelope included."""

    max_chars: int = DEFAULT_DELIVERY_CHARS
    envelope_chars: int = DEFAULT_ENVELOPE_CHARS

    def __post_init__(self) -> None:
        require_int(self.max_chars, "delivery budget max_chars", minimum=1)
        require_int(self.envelope_chars, "delivery budget envelope_chars", minimum=0)
        if self.envelope_chars >= self.max_chars:
            raise ContractError(
                "delivery budget envelope_chars must be smaller than max_chars"
            )

    def available_chars(self) -> int:
        """Capacity left for evidence after the response envelope allowance."""
        return max(0, self.max_chars - self.envelope_chars)

    def payload_capacity(self) -> int:
        """The rendered-text ceiling: ``max_chars`` minus the terminator."""
        return max(0, self.max_chars - DELIVERY_TERMINATOR_CHARS)

    def to_wire(self) -> dict[str, object]:
        return {
            "max_chars": self.max_chars,
            "envelope_chars": self.envelope_chars,
            "available_chars": self.available_chars(),
        }
