"""Signal coordination for supervised CLI dispatch.

The CLI adapter routes work through :func:`run_with_cancellation` so a
SIGINT/SIGTERM reaches the active supervised child through the shared
cancellation token before the process exits.
"""

from __future__ import annotations

import os
import signal
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


def run_with_cancellation(action: Callable[[], T]) -> T:
    """Run *action* with CLI signal handlers installed."""
    from agentq.process import CancellationToken, set_active_cancellation

    token = CancellationToken()
    previous: dict[int, Any] = {}

    def handler(signum: int, frame: Any) -> None:
        token.cancel(signum)
        if token.in_use:
            return
        prior = previous.get(signum)
        if callable(prior):
            prior(signum, frame)
        elif signum == signal.SIGINT:
            raise KeyboardInterrupt
        else:
            # Restore the default disposition and re-raise: without this the
            # handler would silently swallow SIGTERM outside supervised windows.
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, handler)
    set_active_cancellation(token)
    try:
        return action()
    finally:
        set_active_cancellation(None)
        for signum, prior in previous.items():
            signal.signal(signum, prior)
