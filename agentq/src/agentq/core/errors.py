"""Foundational application errors.
"""

from __future__ import annotations


class ContractError(ValueError):
    """An external payload does not satisfy its typed contract."""


class AgentQError(RuntimeError):
    pass


class AgentQCancelled(AgentQError):
    """A supervised command was cancelled by SIGINT or SIGTERM.

    ``exit_code`` is the shared shell mapping supplied by
    :func:`agentq.execution.supervisor.cli_exit_code` (130 for SIGINT, 143 for
    SIGTERM),
    so callers never re-derive the cancellation policy.
    """

    def __init__(self, *, exit_code: int = 130, signum: int | None = None) -> None:
        self.exit_code = exit_code
        self.signum = signum
        super().__init__(
            f"interrupted by signal {signum}" if signum is not None else "interrupted"
        )
