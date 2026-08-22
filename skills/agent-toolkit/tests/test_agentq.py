#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

AGENTQ = Path(__file__).resolve().parents[1] / "scripts" / "agentq"


class AgentQIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agentq-test-")
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.telemetry = Path(self.temp.name) / "telemetry"
        self.archive = Path(self.temp.name) / "state" / "events.jsonl"
        self.bin = Path(self.temp.name) / "bin"
        self.bin.mkdir()

        fake_pnpm = self.bin / "pnpm"
        fake_pnpm.write_text(textwrap.dedent("""\
            #!/usr/bin/env bash
            set -euo pipefail
            printf 'fake pnpm cwd=%s args=%s\\n' "$PWD" "$*"
            case "${AGENTQ_TEST_FAIL:-}" in
              a-typecheck)
                if [[ "$PWD" == */packages/a && "$*" == "run typecheck" ]]; then
                  echo 'ERROR simulated a typecheck failure' >&2
                  exit 7
                fi
                ;;
              b-typecheck)
                if [[ "$PWD" == */packages/b && "$*" == "run typecheck" ]]; then
                  echo 'ERROR simulated b typecheck failure' >&2
                  exit 8
                fi
                ;;
            esac
            exit 0
        """), encoding="utf-8")
        fake_pnpm.chmod(0o755)

        self.env = os.environ.copy()
        self.env.update({
            "AGENTQ_TELEMETRY_HOT": str(self.telemetry),
            "AGENTQ_TELEMETRY_STATE": str(self.archive),
            "PATH": str(self.bin) + os.pathsep + self.env.get("PATH", ""),
            "TERM": "dumb",
            "NO_COLOR": "1",
        })

        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "agentq@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "AgentQ Test"], check=True)
        (self.repo / "packages/a/src").mkdir(parents=True)
        (self.repo / "packages/a/tests").mkdir(parents=True)
        (self.repo / "packages/b/src").mkdir(parents=True)
        (self.repo / "package.json").write_text(json.dumps({
            "name": "root", "private": True, "workspaces": ["packages/*"],
            "devDependencies": {"vitest": "^4.0.0"}
        }), encoding="utf-8")
        (self.repo / "pnpm-workspace.yaml").write_text("packages:\n  - 'packages/*'\n", encoding="utf-8")
        (self.repo / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
        (self.repo / "tsconfig.json").write_text(json.dumps({
            "compilerOptions": {"module": "ESNext", "target": "ES2022"},
            "include": ["packages/**/*.ts"]
        }), encoding="utf-8")
        (self.repo / "packages/a/package.json").write_text(json.dumps({
            "name": "@test/a", "private": True,
            "scripts": {"test": "vitest run", "typecheck": "tsc --noEmit", "lint": "eslint ."},
            "devDependencies": {"vitest": "^4.0.0"}
        }), encoding="utf-8")
        (self.repo / "packages/b/package.json").write_text(json.dumps({
            "name": "@test/b", "private": True,
            "scripts": {"test": "vitest run", "typecheck": "tsc --noEmit"},
            "dependencies": {"@test/a": "workspace:*"},
            "devDependencies": {"vitest": "^4.0.0"}
        }), encoding="utf-8")
        (self.repo / "packages/a/src/index.ts").write_text(textwrap.dedent("""\
            export interface OldName { value: string }
            export function makeOldName(value: string): OldName {
              return { value }
            }
            export const literal = 'A|B'
        """), encoding="utf-8")
        (self.repo / "packages/a/tests/index.test.ts").write_text("import { makeOldName } from '../src/index'\n", encoding="utf-8")
        (self.repo / "packages/b/src/index.ts").write_text("import type { OldName } from '../../a/src/index'\nexport type Wrapped = OldName\n", encoding="utf-8")
        (self.repo / ".env").write_text("API_KEY=secret-do-not-read\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "initial"], check=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def aq(self, *args: str, expect: int = 0, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        if not args:
            raise AssertionError("missing agentq subcommand")
        argv = [str(AGENTQ), args[0], "--repo", str(self.repo), "--format", "json", "--budget", "1000000", *args[1:]]
        env = self.env.copy()
        if extra_env:
            env.update(extra_env)
        result = subprocess.run(argv, text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, expect, msg=result.stderr or result.stdout)
        return result

    def data(self, *args: str, expect: int = 0, extra_env: dict[str, str] | None = None) -> dict:
        return json.loads(self.aq(*args, expect=expect, extra_env=extra_env).stdout)

    def change_a(self, text: str = "\nexport const changed = true\n") -> None:
        path = self.repo / "packages/a/src/index.ts"
        path.write_text(path.read_text() + text, encoding="utf-8")

    def test_search_is_fixed_by_default_and_sensitive_paths_are_excluded(self) -> None:
        data = self.data("search", "A|B")
        self.assertEqual(data["shown"], 1)
        self.assertEqual(data["hits"][0]["path"], "packages/a/src/index.ts")
        files = self.data("files", ".env")
        self.assertEqual(files["shown"], 0)

    def test_read_redacts_private_key_material_even_with_sensitive_override(self) -> None:
        key = self.repo / "fixture.pem"
        key.write_text("-----BEGIN PRIVATE KEY-----\nBASE64KEYMATERIAL\n-----END PRIVATE KEY-----\n", encoding="utf-8")
        data = self.data("read", "fixture.pem", "--include-sensitive")
        visible = json.dumps(data)
        self.assertIn("REDACTED_PRIVATE_KEY", visible)
        self.assertNotIn("BASE64KEYMATERIAL", visible)

    def test_search_context_is_bounded_and_returned(self) -> None:
        data = self.data("search", "makeOldName", "--context", "1", "--limit", "10")
        self.assertEqual(data["context"], 1)
        self.assertGreaterEqual(len(data["context_lines"]), 1)
        self.assertLessEqual(len(data["context_lines"]), 10)

    def test_read_refuses_sensitive_file(self) -> None:
        data = self.data("read", ".env")
        self.assertTrue(data["items"][0]["refused"])

    def test_repo_map_outline_and_dependencies(self) -> None:
        repo_map = self.data("repo-map")
        self.assertGreaterEqual(repo_map["files"], 8)
        outline = self.data("outline", "packages/a/src", "--match", "OldName")
        self.assertGreaterEqual(outline["shown"], 1)
        deps = self.data("dependencies", "--target", "@test/a", "--depth", "2")
        dependents = deps["matches"][0]["dependents"]
        self.assertTrue(any(item["name"] == "@test/b" for item in dependents))

    def test_impact_and_guarded_codemod(self) -> None:
        impact = self.data("impact", "OldName", "--path", "packages")
        self.assertGreaterEqual(impact["source_reference_files"], 1)
        scan = self.data("codemod-scan", "OldName", "--path", "packages")
        self.assertGreaterEqual(scan["matches"], 4)
        dry = self.data("codemod-apply", "OldName", "NewName", "--path", "packages", "--expect-count", str(scan["matches"]))
        self.assertFalse(dry["applied"])
        self.assertIn("OldName", (self.repo / "packages/a/src/index.ts").read_text())
        applied = self.data("codemod-apply", "OldName", "NewName", "--path", "packages", "--expect-count", str(scan["matches"]), "--apply")
        self.assertTrue(applied["applied"])
        self.assertEqual(applied["remaining_matches"], 0)

    def test_git_summary_and_patch_audit(self) -> None:
        path = self.repo / "packages/a/src/index.ts"
        path.write_text(path.read_text() + "\ndebugger;\n", encoding="utf-8")
        status = self.data("git-status")
        self.assertEqual(status["total"], 1)
        diff = self.data("git-diff", "--patch", "--max-lines", "100")
        self.assertEqual(diff["total_files"], 1)
        self.assertIn("debugger", diff["patch"])
        audit = self.data("audit")
        self.assertTrue(any(item["rule"] == "debugger" for item in audit["findings"]))

    def test_sensitive_git_diff_omits_body(self) -> None:
        (self.repo / ".env").write_text("API_KEY=changed-secret-value\n", encoding="utf-8")
        diff = self.data("git-diff", "--patch", "--max-lines", "100")
        visible = json.dumps(diff)
        self.assertIn("sensitive diff content omitted", visible)
        self.assertNotIn("changed-secret-value", visible)
        audit = self.data("audit")
        self.assertTrue(any(item["rule"] == "sensitive-file" for item in audit["findings"]))

    def test_compact_run_redacts_output_and_retains_local_log(self) -> None:
        code = "import sys; print('token=supersecretvalue'); print('-----BEGIN PRIVATE KEY-----'); print('BASE64KEYMATERIAL'); print('-----END PRIVATE KEY-----'); print('ERROR sample', file=sys.stderr); raise SystemExit(3)"
        data = self.data("run", "--label", "redaction-test", "--", "python3", "-c", code)
        self.assertEqual(data["exit_code"], 3)
        visible = json.dumps(data)
        self.assertNotIn("supersecretvalue", visible)
        log = Path(data["log"])
        self.assertTrue(log.is_file())
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("supersecretvalue", log.read_text())
        self.assertIn("[REDACTED]", log.read_text())
        self.assertIn("[REDACTED_PRIVATE_KEY_BLOCK]", log.read_text())
        self.assertNotIn("BASE64KEYMATERIAL", log.read_text())
        self.assertFalse(any("-raw-" in candidate.name for candidate in log.parent.glob("*.log")))

    def test_dot_prefixed_scope_and_outside_log_range_read(self) -> None:
        (self.repo / ".github/workflows").mkdir(parents=True)
        workflow = self.repo / ".github/workflows/ci.yml"
        workflow.write_text("name: CI\nrun: pnpm test\n", encoding="utf-8")
        files = self.data("files", "ci", "--path", ".github")
        self.assertEqual(files["shown"], 1)
        self.assertEqual(files["files"][0]["path"], ".github/workflows/ci.yml")

        outside = Path(self.temp.name) / "outside-redacted.log"
        outside.write_text("one\ntwo\nthree\n", encoding="utf-8")
        data = self.data("read", str(outside) + ":2-3", "--allow-outside")
        self.assertEqual([item["text"] for item in data["items"][0]["lines"]], ["two", "three"])

    def test_agentq_wrapper_works_through_symlink(self) -> None:
        link = Path(self.temp.name) / "agentq-link"
        link.symlink_to(AGENTQ)
        result = subprocess.run([str(link), "--version"], text=True, capture_output=True, env=self.env)
        self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
        self.assertIn("agentq 1.2.1", result.stdout)

    def test_test_plan_is_workspace_aware_and_includes_direct_dependent(self) -> None:
        self.change_a()
        plan = self.data("test-plan")
        kinds = [step["kind"] for step in plan["steps"]]
        self.assertIn("related-tests", kinds)
        self.assertIn("typecheck", kinds)
        self.assertIn("dependent-typecheck", kinds)
        related = next(step for step in plan["steps"] if step["kind"] == "related-tests")
        self.assertIn("--reporter=minimal", related["argv"])
        self.assertEqual(plan["changed_packages"], ["@test/a"])
        self.assertEqual(plan["dependent_packages"], ["@test/b"])
        self.assertEqual(plan["workspace_packages"], 3)
        self.assertEqual(plan["workspace_edges"], 1)

    def test_verify_changed_dry_run_and_alias(self) -> None:
        self.change_a()
        plan = self.data("verified-changed", "--dry-run", "--skip-lint")
        self.assertEqual(plan["status"], "planned")
        self.assertEqual(plan["executed_steps"], 0)
        self.assertIn("@test/b", plan["dependent_packages"])
        self.assertGreater(plan["planned_steps"], 0)

    def test_verify_changed_executes_bounded_ladder(self) -> None:
        self.change_a()
        data = self.data("verify-changed", "--skip-lint")
        self.assertEqual(data["status"], "passed")
        self.assertTrue(data["ok"])
        self.assertGreaterEqual(data["executed_steps"], 3)
        self.assertTrue(any(result["kind"] == "dependent-typecheck" for result in data["results"]))
        self.assertGreater(data["raw_output_chars"], 0)
        self.assertTrue(all(Path(result["log"]).is_file() for result in data["results"]))
        events = [json.loads(line) for line in (self.telemetry / "events.jsonl").read_text().splitlines()]
        event = events[-1]
        self.assertEqual(event["command"], "verify-changed")
        self.assertEqual(event["metrics"]["verification_mode"], "standard")
        self.assertGreaterEqual(event["metrics"]["affected_packages"], 2)

    def test_verify_changed_propagates_failure_and_stops(self) -> None:
        self.change_a()
        data = self.data(
            "verify-changed", "--skip-lint",
            expect=1,
            extra_env={"AGENTQ_TEST_FAIL": "b-typecheck"},
        )
        self.assertEqual(data["status"], "failed")
        self.assertFalse(data["ok"])
        self.assertEqual(data["failed_steps"], 1)
        self.assertEqual(data["results"][-1]["exit_code"], 8)

    def test_root_workspace_config_change_widens_affected_scope(self) -> None:
        path = self.repo / "tsconfig.json"
        path.write_text(path.read_text() + "\n", encoding="utf-8")
        plan = self.data("test-plan", "--mode", "standard")
        self.assertIn("tsconfig.json", plan["global_changes"])
        self.assertIn("@test/a", plan["affected_packages"])
        self.assertIn("@test/b", plan["affected_packages"])

    def test_verify_changed_clean_tree(self) -> None:
        data = self.data("verify-changed")
        self.assertEqual(data["status"], "clean")
        self.assertEqual(data["executed_steps"], 0)

    def test_runtime_cache_ignores_unwritable_home_cache(self) -> None:
        env = self.env.copy()
        env["HOME"] = "/proc/agentq-no-home"
        env.pop("XDG_CACHE_HOME", None)
        env.pop("AGENTQ_CACHE_HOME", None)
        argv = [str(AGENTQ), "run", "--repo", str(self.repo), "--format", "json", "--", "python3", "-c", "print('ok')"]
        result = subprocess.run(argv, text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
        data = json.loads(result.stdout)
        self.assertTrue(data["log"].startswith(tempfile.gettempdir()))
        self.assertNotIn("/.cache/", data["log"])

    def test_text_output_has_global_character_budget(self) -> None:
        argv = [str(AGENTQ), "read", "--repo", str(self.repo), "--budget", "180", "packages/a/src/index.ts"]
        result = subprocess.run(argv, text=True, capture_output=True, env=self.env)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertLessEqual(len(result.stdout.rstrip("\n")), 180)
        self.assertIn("agentq output truncated", result.stdout)

    def test_telemetry_is_private_minimized_and_stats_are_aggregated(self) -> None:
        secret_query = "SENSITIVE_QUERY_VALUE_91fdb"
        self.data("search", secret_query)
        self.data("run", "--", "python3", "-c", "print('x' * 500)")
        event_file = self.telemetry / "events.jsonl"
        self.assertTrue(event_file.is_file())
        self.assertEqual(self.telemetry.stat().st_mode & 0o777, 0o700)
        self.assertEqual(event_file.stat().st_mode & 0o777, 0o600)
        raw = event_file.read_text(encoding="utf-8")
        self.assertNotIn(secret_query, raw)
        self.assertNotIn(str(self.repo), raw)
        stats = self.data("stats", "--since", "all")
        self.assertGreaterEqual(stats["events"], 2)
        commands = {row["command"] for row in stats["commands"]}
        self.assertIn("search", commands)
        self.assertIn("run", commands)
        self.assertGreater(stats["visible_chars"], 0)
        self.assertIn("not provider token accounting", stats["measurement_note"])

    def test_stats_terminal_dashboard_and_archive(self) -> None:
        self.data("git-status")
        argv = [
            str(AGENTQ), "stats", "--repo", str(self.repo), "--since", "all",
            "--format", "text", "--color", "never", "--archive",
        ]
        result = subprocess.run(argv, text=True, capture_output=True, env=self.env)
        self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
        self.assertIn("agentq activity", result.stdout)
        self.assertIn("operations", result.stdout)
        self.assertIn("note:", result.stdout)
        self.assertTrue(self.archive.is_file())
        self.assertEqual(self.archive.stat().st_mode & 0o777, 0o600)


    def test_stats_distinguishes_tool_health_from_child_command_failure(self) -> None:
        self.data("run", "--", "python3", "-c", "raise SystemExit(7)")
        stats = self.data("stats", "--since", "all")
        self.assertEqual(stats["tool_errors"], 0)
        self.assertEqual(stats["project_commands"]["failed"], 1)
        run_row = next(row for row in stats["commands"] if row["command"] == "run")
        self.assertEqual(run_row["tool_ok"], 1)
        self.assertEqual(run_row["tool_errors"], 0)
        self.assertEqual(run_row["subject_failures"], 1)

    def test_stats_unknown_reduction_is_null_not_zero(self) -> None:
        self.data("search", "OldName")
        stats = self.data("stats", "--since", "all")
        row = next(row for row in stats["commands"] if row["command"] == "search")
        self.assertIsNone(row["reduction_percent"])
        self.assertEqual(row["instrumented_calls"], 0)

    def test_stats_exact_window_and_codex_thread_hash(self) -> None:
        thread = "0192b49b-example-thread-id"
        self.data("search", "OldName", extra_env={"CODEX_THREAD_ID": thread})
        stats = self.data("stats", "--since", "7d")
        self.assertEqual(stats["threads"], 1)
        self.assertGreaterEqual(stats["window_end"] - stats["window_start"], 7 * 86400 - 2)
        self.assertLessEqual(stats["window_end"] - stats["window_start"], 7 * 86400 + 2)
        raw = (self.telemetry / "events.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(thread, raw)

    def test_stats_plain_render_uses_semantic_markers_and_local_window(self) -> None:
        self.data("run", "--", "python3", "-c", "raise SystemExit(3)")
        argv = [
            str(AGENTQ), "stats", "--repo", str(self.repo), "--since", "all",
            "--format", "text", "--plain", "--color", "never",
        ]
        result = subprocess.run(argv, text=True, capture_output=True, env=self.env)
        self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
        self.assertIn("agentq 100.0%", result.stdout)
        self.assertIn("project run  0 passed · 1 failed", result.stdout)
        self.assertIn("source→visible —", result.stdout)
        self.assertIn("◇", result.stdout)

    def test_legacy_v1_run_failure_migrates_to_subject_failure(self) -> None:
        self.telemetry.mkdir(parents=True, exist_ok=True)
        legacy = {
            "schema": 1, "id": "legacy1", "time": 1_700_000_000.0,
            "repo_id": __import__("hashlib").sha256(str(self.repo.resolve()).encode()).hexdigest()[:16],
            "repo_name": self.repo.name, "command": "run", "success": False,
            "duration_ms": 12, "visible_chars": 100, "prebudget_chars": 100,
            "source_chars": 400, "source_lines": 5, "truncated": False,
            "metrics": {"child_exit_code": 2, "output_chars": 400},
        }
        (self.telemetry / "events.jsonl").write_text(json.dumps(legacy) + "\n", encoding="utf-8")
        stats = self.data("stats", "--since", "all")
        self.assertEqual(stats["tool_errors"], 0)
        self.assertEqual(stats["project_commands"]["failed"], 1)

    def test_typescript_semantic_navigation_when_project_typescript_available(self) -> None:
        resolved = subprocess.run(["node", "-e", "console.log(require.resolve('typescript/package.json'))"], text=True, capture_output=True)
        if resolved.returncode != 0:
            self.skipTest("global TypeScript unavailable in test environment")
        global_pkg = Path(resolved.stdout.strip()).parent
        node_modules = self.repo / "node_modules"
        node_modules.mkdir(exist_ok=True)
        (node_modules / "typescript").symlink_to(global_pkg, target_is_directory=True)
        refs = self.data("ts-nav", "references", "--file", "packages/a/src/index.ts", "--line", "1", "--column", "18")
        self.assertGreaterEqual(refs["total"], 2)
        paths = {item["path"] for item in refs["results"]}
        self.assertIn("packages/b/src/index.ts", paths)

    def test_benchmark_fallback_or_hyperfine(self) -> None:
        data = self.data("benchmark", "--warmup", "0", "--runs", "2", "--command", "python3 -c 'pass'")
        self.assertEqual(len(data["results"]), 1)
        self.assertGreaterEqual(data["results"][0]["mean"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
