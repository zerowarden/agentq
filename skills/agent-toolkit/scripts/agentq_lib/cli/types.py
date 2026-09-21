from __future__ import annotations

from collections.abc import Callable

from ..emission import DispatchResult

Formatter = Callable[..., str]
Outcome = int | DispatchResult
