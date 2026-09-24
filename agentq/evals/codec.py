"""Strict codecs, split by domain; this module re-exports them.

Implementations live under :mod:`evals.wire`: JSON primitives
(:mod:`evals.wire.json`), runtime-contract mirrors
(:mod:`evals.wire.contracts`), captures (:mod:`evals.wire.capture`), decision
configuration (:mod:`evals.wire.config`), and judgments, locks, cases, and
capture attempts (:mod:`evals.wire.judgment`).
"""

from __future__ import annotations

from .wire.capture import capture_digest, decode_capture, encode_capture
from .wire.config import config_digest, decode_config, encode_config
from .wire.contracts import decision_input_digest
from .wire.json import decode_json, read_json_file
from .wire.judgment import (
    decode_attempts,
    decode_case_suite,
    decode_judgment,
    decode_lock,
    encode_attempts,
    encode_case_spec,
    encode_case_suite,
    encode_judgment,
    encode_lock,
    judgment_digest,
)

__all__ = [
    "capture_digest",
    "config_digest",
    "decision_input_digest",
    "decode_attempts",
    "decode_capture",
    "decode_case_suite",
    "decode_config",
    "decode_json",
    "decode_judgment",
    "decode_lock",
    "encode_attempts",
    "encode_capture",
    "encode_case_spec",
    "encode_case_suite",
    "encode_config",
    "encode_judgment",
    "encode_lock",
    "judgment_digest",
    "read_json_file",
]
