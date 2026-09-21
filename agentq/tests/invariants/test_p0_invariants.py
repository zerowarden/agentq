#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentq.core import AgentQError, evidence, resolve_repo_path, resolve_repo_scopes
from agentq.mutation import (
    ApplyRequest,
    ApplyResult,
    MutationApplyError,
    MutationPlan,
    PlanRequest,
    ScanMode,
    ScanRequest,
    ScanResult,
    apply,
    build_plan,
    scan,
    write_plan,
)
from agentq.redaction import StreamingRedactor, redact_text

AGENTQ = str(Path(sys.executable).with_name("agentq"))


class PathConfinementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="p0-paths-")
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "package.json").write_text("{}")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "a.txt").write_text("hello\n")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_outside_relative_rejected(self) -> None:
        with self.assertRaises(AgentQError):
            resolve_repo_path(self.repo, "../outside/package.json")

    def test_outside_absolute_rejected(self) -> None:
        outside = Path(self.temp.name) / "outside" / "package.json"
        with self.assertRaises(AgentQError):
            resolve_repo_path(self.repo, str(outside))

    def test_nested_valid_path(self) -> None:
        rp = resolve_repo_path(self.repo, "src/a.txt")
        self.assertEqual(rp.relative, "src/a.txt")
        self.assertTrue(rp.absolute.exists())

    def test_repository_root_allowed(self) -> None:
        rp = resolve_repo_path(self.repo, ".")
        self.assertEqual(rp.relative, ".")
        self.assertEqual(rp.absolute, self.repo.resolve())

    def test_scopes_require_existence(self) -> None:
        with self.assertRaises(AgentQError):
            resolve_repo_scopes(self.repo, ["does-not-exist"])

    def test_symlink_escaping_repository_rejected(self) -> None:
        outside = Path(self.temp.name) / "outside" / "secret.txt"
        outside.write_text("x")
        link = self.repo / "escape"
        link.symlink_to(outside)
        with self.assertRaises(AgentQError):
            resolve_repo_path(self.repo, "escape")

    def test_symlink_inside_repository_allowed(self) -> None:
        target = self.repo / "src" / "a.txt"
        link = self.repo / "link.txt"
        link.symlink_to(target)
        rp = resolve_repo_path(self.repo, "link.txt")
        self.assertTrue(rp.absolute.exists())

    def test_impact_refuses_outside_manifest(self) -> None:
        env = dict(os.environ, AGENTQ_TELEMETRY="0")
        out = self._run(
            ["impact", "../outside/package.json", "--repo", str(self.repo)], env=env
        )
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("outside repository", out.stdout + out.stderr)

    def _run(self, args, env=None):
        return __import__("subprocess").run(
            [sys.executable, AGENTQ, *args],
            capture_output=True,
            text=True,
            env=env or os.environ,
            timeout=60,
        )


class RegexEquivalenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="p0-regex-")
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        (self.repo / "a.txt").write_text("foo bar foo\nbaz foo\n")
        (self.repo / "b.txt").write_text("foo\nfoo\nfoo\n")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _scan(self, pattern: str, mode: ScanMode) -> ScanResult:
        return scan(
            ScanRequest(self.repo, pattern, scopes=(".",), mode=mode)
        )

    def _apply(self, pattern: str, rewrite: str) -> ApplyResult:
        return apply(
            ApplyRequest(
                self.repo,
                apply=True,
                pattern=pattern,
                rewrite=rewrite,
                scopes=(".",),
                mode=ScanMode.REGEX,
            )
        )

    def test_invalid_regex_rejected_before_plan(self) -> None:
        with self.assertRaises(AgentQError):
            self._scan(r"\p{L}+", ScanMode.REGEX)

    def test_scan_apply_match_sets_equal(self) -> None:
        scanned = self._scan("foo", ScanMode.REGEX)
        self.assertEqual(scanned.matches, 6)
        result = self._apply("foo", "QUX")
        self.assertEqual(result.match_count, 6)
        self.assertEqual(result.outcome.remaining_matches, 0)
        self.assertEqual((self.repo / "a.txt").read_text(), "QUX bar QUX\nbaz QUX\n")

    def test_backreference_replacement(self) -> None:
        (self.repo / "c.txt").write_text("a1 b2 c3\n")
        self._apply(r"(\w)(\d)", r"\2\1")
        self.assertEqual((self.repo / "c.txt").read_text(), "1a 2b 3c\n")

    def test_zero_width_and_unicode_patterns(self) -> None:
        # A pattern accepted by Python re but not necessarily by ripgrep's default
        # regex engine must still scan/apply identically.
        (self.repo / "d.txt").write_text("abc123\n")
        self._apply(r"(?<=a)\w+", "X")
        self.assertIn("aX", (self.repo / "d.txt").read_text())


class CodemodPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="p0-plan-")
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        (self.repo / "a.txt").write_text("foo\nfoo\n")
        (self.repo / "b.txt").write_text("foo\n")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _build(self, *, pattern: str, rewrite: str | None, mode: str) -> MutationPlan:
        return build_plan(
            PlanRequest(
                root=self.repo,
                pattern=pattern,
                rewrite=rewrite,
                mode=ScanMode(mode),
                scopes=(".",),
            )
        )

    def _apply_from_plan(self, plan_file: Path) -> ApplyResult:
        return apply(
            ApplyRequest(root=self.repo, apply=True, plan_path=str(plan_file))
        )

    def test_plan_id_deterministic(self) -> None:
        p1 = self._build(pattern="foo", rewrite="X", mode="regex")
        p2 = self._build(pattern="foo", rewrite="X", mode="regex")
        self.assertEqual(p1.plan_id, p2.plan_id)

    def test_stale_plan_rejected(self) -> None:
        plan = self._build(pattern="foo", rewrite="X", mode="fixed")
        plan_file = Path(self.temp.name) / "plan.json"
        write_plan(str(plan_file), plan)
        # Mutate a file out of band so the preimage no longer matches.
        (self.repo / "a.txt").write_text("CHANGED\n")
        with self.assertRaises(AgentQError):
            self._apply_from_plan(plan_file)
        self.assertEqual((self.repo / "a.txt").read_text(), "CHANGED\n")
        self.assertEqual((self.repo / "b.txt").read_text(), "foo\n")

    def test_rollback_on_failure(self) -> None:
        plan = self._build(pattern="foo", rewrite="X", mode="fixed")
        plan_file = Path(self.temp.name) / "rollback-plan.json"
        write_plan(str(plan_file), plan)
        original_a = (self.repo / "a.txt").read_text()
        original_b = (self.repo / "b.txt").read_text()
        calls = {"n": 0}

        real_replace = os.replace

        def flaky_replace(src, dst):
            if Path(dst).parent == self.repo:
                calls["n"] += 1
                if calls["n"] == 2:
                    raise OSError("simulated write failure")
            real_replace(src, dst)

        with mock.patch("agentq.mutation.apply.os.replace", flaky_replace):
            with self.assertRaises(MutationApplyError) as caught:
                self._apply_from_plan(plan_file)
        self.assertEqual(caught.exception.result.status.value, "rolled_back")
        self.assertEqual(caught.exception.result.restored, ("a.txt",))
        # First file was replaced, then the failure triggered rollback of it.
        self.assertEqual((self.repo / "a.txt").read_text(), original_a)
        self.assertEqual((self.repo / "b.txt").read_text(), original_b)

    def test_apply_from_plan_succeeds(self) -> None:
        plan = self._build(pattern="foo", rewrite="Z", mode="regex")
        plan_file = Path(self.temp.name) / "success-plan.json"
        write_plan(str(plan_file), plan)
        result = self._apply_from_plan(plan_file)
        self.assertTrue(result.applied)
        self.assertEqual((self.repo / "a.txt").read_text(), "Z\nZ\n")


class StreamingRedactionTests(unittest.TestCase):
    def test_same_line_key_block_preserves_tail(self) -> None:
        r = StreamingRedactor()
        out = r.feed(
            "head\n-----BEGIN PRIVATE KEY-----secret-----END PRIVATE KEY-----\nERROR after key\n"
        )
        out += r.finish()
        self.assertNotIn("secret", out)
        self.assertIn("head", out)
        self.assertIn("ERROR after key", out)

    def test_multiline_block_suppressed(self) -> None:
        r = StreamingRedactor()
        out = r.feed(
            "line1\n-----BEGIN PRIVATE KEY-----\nSECRET BODY\n-----END PRIVATE KEY-----\ntail\n"
        )
        out += r.finish()
        self.assertNotIn("SECRET BODY", out)
        self.assertIn("tail", out)

    def test_multiple_blocks_same_chunk(self) -> None:
        r = StreamingRedactor()
        text = "x\n-----BEGIN PRIVATE KEY-----a-----END PRIVATE KEY-----\ny\n-----BEGIN PRIVATE KEY-----b-----END PRIVATE KEY-----\nz\n"
        out = r.feed(text) + r.finish()
        self.assertNotIn("a", out)
        self.assertNotIn("b", out)
        self.assertIn("x", out)
        self.assertIn("y", out)
        self.assertIn("z", out)

    def test_marker_split_across_chunks(self) -> None:
        r = StreamingRedactor()
        out = r.feed("pre\n-----BEGIN PRIV")
        out += r.feed("ATE KEY-----\nbody\n-----END PRIVATE KEY-----\npost\n")
        out += r.finish()
        self.assertNotIn("body", out)
        self.assertIn("pre", out)
        self.assertIn("post", out)

    def test_unterminated_block_redacted(self) -> None:
        r = StreamingRedactor()
        out = r.feed("before\n-----BEGIN PRIVATE KEY-----\nleak\n")
        out += r.finish()
        self.assertNotIn("leak", out)
        self.assertTrue(r.stats()["unterminated_private_key_blocks"])

    def test_inline_secrets_redacted(self) -> None:
        self.assertNotIn("AKIA", redact_text("key=AKIA1234567890ABCDEF"))


class SensitiveExclusionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="p0-sensitive-")
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        (self.repo / "main.txt").write_text("secret123\n")
        (self.repo / ".env").write_text("password=secret123\n")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _apply(self, *, include_sensitive: bool) -> ApplyResult:
        return apply(
            ApplyRequest(
                root=self.repo,
                apply=True,
                pattern="secret123",
                rewrite="REDACTED",
                scopes=(".",),
                mode=ScanMode.FIXED,
                include_sensitive=include_sensitive,
            )
        )

    def test_sensitive_excluded_by_default(self) -> None:
        self._apply(include_sensitive=False)
        self.assertEqual((self.repo / "main.txt").read_text(), "REDACTED\n")
        self.assertEqual((self.repo / ".env").read_text(), "password=secret123\n")

    def test_sensitive_included_with_flag(self) -> None:
        self._apply(include_sensitive=True)
        self.assertEqual((self.repo / ".env").read_text(), "password=REDACTED\n")


class EvidenceVocabularyTests(unittest.TestCase):
    def test_merge_coverage_takes_weakest_status_and_merges_reasons(self) -> None:
        merged = evidence.merge_coverage(
            evidence.complete(),
            evidence.coverage(evidence.PARTIAL, evidence.PARSE_ERROR),
            evidence.coverage(evidence.SAMPLED, evidence.RESULT_LIMIT),
        )
        self.assertEqual(merged["status"], "partial")
        self.assertEqual(merged["reason"], ["parse_error", "result_limit"])
        self.assertEqual(evidence.merge_coverage()["status"], evidence.UNKNOWN)

    def test_downgrade_never_upgrades_status(self) -> None:
        current = evidence.coverage(evidence.PARTIAL, evidence.PARSE_ERROR)
        self.assertEqual(
            evidence.downgrade(current, evidence.COMPLETE)["status"], evidence.PARTIAL
        )
        self.assertEqual(
            evidence.downgrade(current, evidence.UNKNOWN)["status"], evidence.UNKNOWN
        )
        self.assertEqual(
            evidence.downgrade(
                current, evidence.PARTIAL, evidence.PROVIDER_UNAVAILABLE
            )["reason"],
            ["parse_error", "provider_unavailable"],
        )

    def test_status_of_accepts_blocks_legacy_strings_and_garbage(self) -> None:
        self.assertEqual(evidence.status_of("complete"), evidence.COMPLETE)
        self.assertEqual(
            evidence.status_of({"status": "sampled", "reason": ["result_limit"]}),
            evidence.SAMPLED,
        )
        self.assertEqual(evidence.status_of(None), evidence.UNKNOWN)
        self.assertEqual(evidence.status_of("bogus"), evidence.UNKNOWN)

    def test_best_provenance_prefers_strongest_evidence(self) -> None:
        self.assertEqual(
            evidence.best_provenance("lexical", "semantic", "syntactic"), "semantic"
        )
        self.assertEqual(evidence.best_provenance("heuristic"), "heuristic")
        self.assertIsNone(evidence.best_provenance(None, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
