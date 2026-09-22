"""Verification capability: typed planning, execution, and rendering."""

from __future__ import annotations

from .models import (
    CHECK_PHASE_BY_KIND,
    CHECK_RESULT_SCHEMA,
    EMPTY_CONFIG,
    VERIFICATION_PLAN_SCHEMA,
    ChangeSummary,
    CheckKind,
    CheckPhase,
    CheckResult,
    CheckSpec,
    CheckStatus,
    PackageRow,
    ProviderPlan,
    VerificationPlan,
    VerificationProvider,
    VerificationRun,
    VerificationSelection,
    VerificationStatus,
    VerifyConfig,
    check_identity,
    check_phase,
    verification_plan_id,
)
from .planner import load_verify_config, plan_verification
from .providers import PROVIDER_NAMES, VERIFICATION_PROVIDERS
from .rendering import render_plan, render_verification
from .runner import RunSettings, run_verification

__all__ = [
    "CHECK_RESULT_SCHEMA",
    "EMPTY_CONFIG",
    "PROVIDER_NAMES",
    "VERIFICATION_PLAN_SCHEMA",
    "VERIFICATION_PROVIDERS",
    "CHECK_PHASE_BY_KIND",
    "ChangeSummary",
    "CheckKind",
    "CheckPhase",
    "CheckResult",
    "CheckSpec",
    "CheckStatus",
    "PackageRow",
    "ProviderPlan",
    "RunSettings",
    "VerificationPlan",
    "VerificationProvider",
    "VerificationRun",
    "VerificationSelection",
    "VerificationStatus",
    "VerifyConfig",
    "check_identity",
    "check_phase",
    "load_verify_config",
    "plan_verification",
    "render_plan",
    "render_verification",
    "run_verification",
    "verification_plan_id",
]
