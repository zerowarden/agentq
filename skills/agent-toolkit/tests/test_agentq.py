#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

AGENTQ = Path(__file__).resolve().parents[1] / "scripts" / "agentq"
SCALE_BENCHMARK = Path(__file__).with_name("scale_benchmark.py")


def render_noop(data: dict, *args, **kwargs) -> str:
    return ""


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
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "PATH": str(self.bin) + os.pathsep + self.env.get("PATH", ""),
            "TERM": "dumb",
            "NO_COLOR": "1",
        })

        self.git("init", "-q")
        self.git("config", "user.email", "agentq@example.invalid")
        self.git("config", "user.name", "AgentQ Test")
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
        self.git("add", ".")
        self.git("commit", "-qm", "initial")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            text=True, capture_output=True, check=True, env=self.env,
        )

    def aq(self, *args: str, expect: int = 0, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        if not args:
            raise AssertionError("missing agentq subcommand")
        argv = [str(AGENTQ), args[0], "--repo", str(self.repo), "--format", "json", "--budget", "1000000", *args[1:]]
        env = self.env.copy()
        if extra_env:
            env.update(extra_env)
        result = subprocess.run(argv, text=True, capture_output=True, env=env, cwd=self.repo)
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
        symbol = self.data("search", "OldName")
        self.assertTrue(symbol["semantic_candidate"])

    def test_read_redacts_private_key_material_even_with_sensitive_override(self) -> None:
        key = self.repo / "fixture.pem"
        key.write_text("-----BEGIN PRIVATE KEY-----\nBASE64KEYMATERIAL\n-----END PRIVATE KEY-----\n", encoding="utf-8")
        data = self.data("read", "fixture.pem", "--include-sensitive")
        visible = json.dumps(data)
        self.assertIn("REDACTED_PRIVATE_KEY", visible)
        self.assertNotIn("BASE64KEYMATERIAL", visible)
        self.assertEqual(data["items"][0]["redaction"], {
            "private_key_blocks": 1,
            "redacted_lines": 3,
            "unterminated_private_key_blocks": 0,
        })

    def test_read_private_key_redaction_reports_multiple_windows_blocks(self) -> None:
        key = self.repo / "windows.pem"
        key.write_bytes(
            b"-----BEGIN PRIVATE KEY-----\r\nFIRSTKEYMATERIAL\r\n-----END PRIVATE KEY-----\r\n"
            b"between\r\n"
            b"-----BEGIN RSA PRIVATE KEY-----\r\nSECONDKEYMATERIAL\r\n-----END RSA PRIVATE KEY-----\r\n"
        )

        data = self.data(
            "read", "windows.pem", "--include-sensitive", "--line", "1", "5", "--context", "2",
        )

        visible = json.dumps(data)
        self.assertNotIn("FIRSTKEYMATERIAL", visible)
        self.assertNotIn("SECONDKEYMATERIAL", visible)
        self.assertEqual(data["redaction"], {
            "private_key_blocks": 2,
            "redacted_lines": 6,
            "unterminated_private_key_blocks": 0,
        })
        argv = [
            str(AGENTQ), "read", "--repo", str(self.repo), "windows.pem",
            "--include-sensitive", "--repeat",
        ]
        rendered = subprocess.run(argv, text=True, capture_output=True, env=self.env, cwd=self.repo)
        self.assertEqual(rendered.returncode, 0, msg=rendered.stderr or rendered.stdout)
        self.assertIn("[redacted 2 private-key block(s), 6 line(s)]", rendered.stdout)
        self.assertNotIn("FIRSTKEYMATERIAL", rendered.stdout)
        self.assertNotIn("SECONDKEYMATERIAL", rendered.stdout)

    def test_read_private_key_redaction_bounds_unterminated_sensitive_file(self) -> None:
        key = self.repo / "unfinished.pem"
        key.write_text(
            "prefix -----BEGIN PRIVATE KEY-----\nUNFINISHEDKEYMATERIAL\ntrailing material\n",
            encoding="utf-8",
        )

        data = self.data("read", "unfinished.pem", "--include-sensitive")

        visible = json.dumps(data)
        self.assertNotIn("UNFINISHEDKEYMATERIAL", visible)
        self.assertNotIn("trailing material", visible)
        self.assertEqual(data["items"][0]["redaction"], {
            "private_key_blocks": 1,
            "redacted_lines": 3,
            "unterminated_private_key_blocks": 1,
        })
        argv = [
            str(AGENTQ), "read", "--repo", str(self.repo), "unfinished.pem",
            "--include-sensitive", "--repeat",
        ]
        rendered = subprocess.run(argv, text=True, capture_output=True, env=self.env, cwd=self.repo)
        self.assertEqual(rendered.returncode, 0, msg=rendered.stderr or rendered.stdout)
        self.assertIn("1 unterminated at EOF", rendered.stdout)
        self.assertNotIn("UNFINISHEDKEYMATERIAL", rendered.stdout)

    def test_read_does_not_treat_source_literals_or_comments_as_private_key_blocks(self) -> None:
        source = self.repo / "marker_source.py"
        source.write_text(textwrap.dedent('''\
            before = "visible before"
            marker = "-----BEGIN PRIVATE KEY-----"
            # -----BEGIN PRIVATE KEY-----
            after = "visible after"
        '''), encoding="utf-8")

        data = self.data("read", "marker_source.py")
        visible = json.dumps(data)

        self.assertIn("visible before", visible)
        self.assertIn("visible after", visible)
        self.assertNotIn("redaction", data["items"][0])

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
        self.assertGreaterEqual(impact["observations"]["lexical_source_fanout"], 1)
        self.assertFalse(impact["heuristic_summary"]["calibrated"])
        self.assertEqual(impact["provenance"], "heuristic")
        self.assertIn(impact["coverage"]["status"], {"complete", "sampled"})
        scan = self.data("codemod-scan", "OldName", "--path", "packages")
        self.assertGreaterEqual(scan["matches"], 4)
        dry = self.data("codemod-apply", "OldName", "NewName", "--path", "packages", "--expect-count", str(scan["matches"]))
        self.assertFalse(dry["applied"])
        self.assertIn("OldName", (self.repo / "packages/a/src/index.ts").read_text())
        applied = self.data("codemod-apply", "OldName", "NewName", "--path", "packages", "--expect-count", str(scan["matches"]), "--apply")
        self.assertTrue(applied["applied"])
        self.assertEqual(applied["remaining_matches"], 0)

    def test_impact_reports_observations_instead_of_score(self) -> None:
        self.change_a("\nexport const fanout = true\n")
        impact = self.data("impact", "makeOldName", "--path", "packages")
        self.assertNotIn("score", impact)
        self.assertNotIn("blast_radius", impact)
        observations = impact["observations"]
        self.assertFalse(observations["public_shared_surface"])
        self.assertGreaterEqual(observations["lexical_source_fanout"], 1)
        self.assertGreaterEqual(observations["direct_test_references"], 0)
        summary = impact["heuristic_summary"]
        self.assertFalse(summary["calibrated"])
        self.assertIn(summary["level"], {"low", "medium", "high"})
        self.assertIsInstance(summary["rules"], list)
        rendered = subprocess.run(
            [str(AGENTQ), "impact", "--repo", str(self.repo), "--format", "text", "--budget", "100000",
             "makeOldName", "--path", "packages"],
            text=True, capture_output=True, env=self.env, cwd=self.repo,
        )
        self.assertEqual(rendered.returncode, 0, msg=rendered.stderr)
        self.assertIn("uncalibrated", rendered.stdout)
        self.assertNotIn("blast radius:", rendered.stdout)

    def test_inspect_reports_cross_language_ambiguity(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq_lib import inspectops as inspectops_module
            from agentq_lib import navigation as navigation_module

        ts_path = self.repo / "packages/a/src/index.ts"
        ts_result = {
            "action": "overview", "symbol": "Config",
            "candidates": [{"path": str(ts_path), "line": 1, "column": 16, "kind": "interface", "preview": "interface Config { a: string }"}],
            "candidate_count": 1, "ambiguous": True,
            "definition": {"results": [{"path": str(ts_path), "line": 1, "column": 16}]},
            "references": {"results": [], "shown": 0, "total": 0, "truncated": False},
            "implementations": {"results": [], "shown": 0, "total": 0, "truncated": False},
        }
        (self.repo / "packages/a/src/dup.py").write_text("class Config:\n    pass\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {**self.env, "AGENTQ_CONTEXT_CACHE": "0"}):
            with mock.patch.object(navigation_module, "ts_nav_data", return_value=ts_result):
                data = inspectops_module.inspect_data(self.repo, "Config", ["packages/a/src"])
        self.assertEqual(data["kind"], "ambiguous")
        self.assertEqual(data["provenance"], "semantic")
        self.assertEqual(data["coverage"]["status"], "complete")
        provider_names = [item["provider"] for item in data["providers"]]
        self.assertEqual(provider_names, ["typescript", "python"])
        rendered = inspectops_module.render_inspect(data, budget=100000)
        self.assertIn("multiple languages", rendered)
        self.assertIn("typescript", rendered)
        self.assertIn("python", rendered)

    def test_navigation_provider_layer_routes_and_reports(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq_lib import navigation as navigation_module

        for provider in (*navigation_module.LANGUAGE_PROVIDERS, navigation_module.LEXICAL_FALLBACK):
            self.assertIsInstance(provider, navigation_module.NavigationProvider)
        request = navigation_module.NavigationRequest(
            root=self.repo, symbol="X", paths=("packages",), limit=5, lang="python",
        )
        self.assertFalse(navigation_module.LANGUAGE_PROVIDERS[0].supports(request))
        self.assertTrue(navigation_module.LANGUAGE_PROVIDERS[1].supports(request))
        unrestricted = navigation_module.NavigationRequest(
            root=self.repo, symbol="X", paths=("packages",), limit=5,
        )
        self.assertTrue(all(provider.supports(unrestricted) for provider in navigation_module.LANGUAGE_PROVIDERS))

        with mock.patch.dict(os.environ, {**self.env, "AGENTQ_CONTEXT_CACHE": "0"}):
            with mock.patch.object(
                navigation_module, "ts_nav_data", side_effect=AssertionError("typescript queried"),
            ):
                resolution = navigation_module.resolve_symbol(
                    self.repo, "makeOldName", paths=["packages/a/src"], limit=5, lang="python",
                )
        self.assertEqual([outcome.name for outcome in resolution.outcomes], ["python"])
        self.assertIsNotNone(resolution.fallback)
        self.assertEqual(resolution.fallback.name, "lexical")
        entry_names = [entry["provider"] for entry in resolution.entries()]
        self.assertEqual(entry_names, ["python", "lexical"])
        # The python provider ran cleanly and simply found no Python candidate.
        self.assertEqual(resolution.entries()[0]["coverage"]["status"], "complete")

    def test_inspect_locate_intent_skips_references(self) -> None:
        (self.repo / "packages/a/src/located.py").write_text("class Located:\n    pass\n", encoding="utf-8")
        data = self.data("inspect", "Located", "--path", "packages/a/src/located.py", "--intent", "locate", "--lang", "python")
        self.assertEqual(data["kind"], "python")
        python = data["python"]
        self.assertEqual(python["references"]["results"], [])
        self.assertEqual(python["references"]["total"], 0)
        self.assertTrue(python["references_omitted"])

    def test_inspect_edit_intent_bundles_declaration_tests_and_package(self) -> None:
        (self.repo / "packages/a/src/edited.py").write_text(
            "class Edited:\n    def method(self) -> int:\n        return 7\n", encoding="utf-8",
        )
        (self.repo / "packages/a/src/edited.test.ts").write_text(
            "import { Edited } from './edited'\n", encoding="utf-8",
        )
        data = self.data("inspect", "Edited", "--path", "packages/a/src", "--intent", "edit", "--lang", "python")
        self.assertEqual(data["kind"], "edit")
        self.assertEqual(data["provenance"], "syntactic")
        edit = data["edit"]
        declaration = edit["declaration"]["items"][0]
        self.assertEqual(declaration["path"], "packages/a/src/edited.py")
        self.assertTrue(any("class Edited" in line["text"] for line in declaration["lines"]))
        self.assertTrue(any("edited.test.ts" in hit["path"] for hit in edit["tests"]))
        self.assertEqual(data["package"]["path"], "packages/a/package.json")
        self.assertTrue(data["verification"])
        rendered = subprocess.run(
            [
                str(AGENTQ), "inspect", "--repo", str(self.repo), "--format", "text", "--budget", "100000",
                "Edited", "--path", "packages/a/src", "--intent", "edit", "--lang", "python",
            ],
            text=True, capture_output=True, env=self.env, cwd=self.repo,
        )
        self.assertEqual(rendered.returncode, 0, msg=rendered.stderr)
        self.assertIn("edit bundle", rendered.stdout)
        self.assertIn("declaration:", rendered.stdout)

    def test_inspect_downgrades_coverage_on_parse_errors(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq_lib import inspectops as inspectops_module

        (self.repo / "packages/a/src/broken_nav.py").write_text("def broken(:\n", encoding="utf-8")
        (self.repo / "packages/a/src/nav_symbol.py").write_text(
            "def makeOldName(value):\n    return value\n", encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {**self.env, "AGENTQ_CONTEXT_CACHE": "0"}):
            data = inspectops_module.inspect_data(self.repo, "makeOldName", ["packages/a/src"], lang="python")
        self.assertEqual(data["kind"], "python")
        self.assertEqual(data["coverage"]["status"], "partial")
        self.assertIn("parse_error", data["coverage"]["reason"])
        self.assertEqual(data["python"]["parse_error_count"], 1)
        self.assertLessEqual(len(data["python"]["parse_errors"]), 5)

    def test_disabled_telemetry_avoids_importing_telemetry_module(self) -> None:
        script = (
            "import sys;"
            f"sys.path.insert(0, {str(AGENTQ.parent)!r});"
            "import agentq;"
            f"sys.argv = ['agentq', 'files', 'zzz-none', '--repo', {str(self.repo)!r}];"
            "agentq.main();"
            "assert 'agentq_lib.telemetry' not in sys.modules, 'telemetry module imported';"
            "print('LAZY-OK')"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            env={**self.env, "AGENTQ_TELEMETRY": "0"},
            capture_output=True, text=True, cwd=self.repo,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
        self.assertIn("LAZY-OK", result.stdout)

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

    def test_task_scoped_audit_excludes_unchanged_preexisting_work(self) -> None:
        self.change_a("\ndebugger;\n")
        self.data("task", "begin")
        changed = self.repo / "packages/b/src/index.ts"
        changed.write_text(changed.read_text() + "\nconsole.log('task change')\n", encoding="utf-8")

        audit = self.data("audit", "--task")

        self.assertTrue(audit["task_scope"])
        self.assertEqual(audit["scope"], "active-task")
        self.assertEqual(audit["patch"]["files"], 1)
        self.assertTrue(any(item["rule"] == "debug-output" for item in audit["findings"]))
        self.assertFalse(any(item["rule"] == "debugger" for item in audit["findings"]))
        self.assertEqual(audit["preexisting_unchanged_excluded"], 1)

    def test_git_diff_hunk_index_prioritizes_broad_changes_without_patch_bodies(self) -> None:
        fixtures = {
            "packages/a/src/public.ts": "export function changedApi() { return 1 }\n",
            "packages/a/tests/public.test.ts": "test('changed API', () => expect(true).toBe(true))\n",
            "migrations/001_add_table.sql": "create table indexed_change(id integer);\n",
            "config/settings.yaml": "indexed: true\n",
            "packages/a/src/suppressed.ts": "// eslint-disable-next-line no-console\nconsole.log('x')\n",
        }
        fixtures.update({
            f"packages/a/src/broad_{index}.ts": "".join(
                f"const broad_{index}_{line} = {line};\n" for line in range(20)
            )
            for index in range(15)
        })
        for relative, content in fixtures.items():
            path = self.repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self.git("add", *fixtures)

        indexed = self.aq("git-diff", "--hunks")
        patch = self.aq("git-diff", "--patch", "--max-lines", "1000")
        data = json.loads(indexed.stdout)

        self.assertEqual(data["total_files"], 20)
        self.assertEqual(len(data["hunks"]), 20)
        self.assertNotIn("patch", data)
        self.assertLess(len(indexed.stdout), len(patch.stdout))
        by_path = {item["path"]: item for item in data["hunks"]}
        self.assertIn("public-api", by_path["packages/a/src/public.ts"]["risk_flags"])
        self.assertIn("tests", by_path["packages/a/tests/public.test.ts"]["risk_flags"])
        self.assertIn("migrations", by_path["migrations/001_add_table.sql"]["risk_flags"])
        self.assertIn("config", by_path["config/settings.yaml"]["risk_flags"])
        self.assertIn("suppressions", by_path["packages/a/src/suppressed.ts"]["risk_flags"])
        self.assertTrue(all(item["follow_up"].startswith("agentq git-diff --patch --path ") for item in data["hunks"]))

    def test_sensitive_git_diff_omits_body(self) -> None:
        (self.repo / ".env").write_text("API_KEY=changed-secret-value\n", encoding="utf-8")
        diff = self.data("git-diff", "--patch", "--max-lines", "100")
        visible = json.dumps(diff)
        self.assertIn("sensitive diff content omitted", visible)
        self.assertNotIn("changed-secret-value", visible)
        hunks = self.data("git-diff", "--hunks")
        self.assertIn("sensitive", hunks["hunks"][0]["risk_flags"])
        self.assertNotIn("changed-secret-value", json.dumps(hunks))
        audit = self.data("audit")
        self.assertTrue(any(item["rule"] == "sensitive-file" for item in audit["findings"]))

    def test_sensitive_git_diff_overrides_mnemonic_prefixes(self) -> None:
        self.git("config", "diff.mnemonicPrefix", "true")
        (self.repo / ".env").write_text("EDGE_VALUE=mnemonic-sensitive-body\n", encoding="utf-8")

        diff = self.data("git-diff", "--patch", "--max-lines", "100")

        self.assertIn("diff --git a/.env b/.env", diff["patch"])
        self.assertIn("sensitive diff content omitted", diff["patch"])
        self.assertNotIn("mnemonic-sensitive-body", json.dumps(diff))

    def test_sensitive_git_diff_handles_path_and_status_edge_cases(self) -> None:
        secrets = self.repo / "secrets"
        public = self.repo / "public"
        secrets.mkdir()
        public.mkdir()
        spaced = secrets / "quoted ü file.txt"
        renamed = secrets / "rename source.txt"
        deleted = secrets / "delete me.txt"
        stable = "".join(f"stable line {index}\n" for index in range(30))
        spaced.write_text("quoted-original\n" + stable, encoding="utf-8")
        renamed.write_text("rename-original\n" + stable, encoding="utf-8")
        deleted.write_text("deleted-sensitive-body\n" + stable, encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "add sensitive path fixtures")

        spaced.write_text("quoted-sensitive-body\n" + stable, encoding="utf-8")
        renamed_target = public / "renamed ü file.txt"
        self.git("mv", str(renamed.relative_to(self.repo)), str(renamed_target.relative_to(self.repo)))
        renamed_target.write_text("rename-sensitive-body\n" + stable, encoding="utf-8")
        deleted.unlink()
        added = self.repo / ".env.local"
        added.write_text("EDGE_VALUE=added-sensitive-body\n", encoding="utf-8")
        self.git("add", str(added.relative_to(self.repo)))

        diff = self.data("git-diff", "--patch", "--max-lines", "300")
        files = {item["path"]: item for item in diff["files"]}
        self.assertEqual(files[".env.local"]["status"], "A")
        self.assertEqual(files["secrets/delete me.txt"]["status"], "D")
        self.assertTrue(files["public/renamed ü file.txt"]["status"].startswith("R"))
        self.assertEqual(files["public/renamed ü file.txt"]["old_path"], "secrets/rename source.txt")
        self.assertGreaterEqual(diff["patch"].count("sensitive diff content omitted"), 4)

        sentinels = (
            "added-sensitive-body", "deleted-sensitive-body", "quoted-sensitive-body", "rename-sensitive-body",
        )
        visible = json.dumps(diff, ensure_ascii=False)
        for sentinel in sentinels:
            self.assertNotIn(sentinel, visible)

        argv = [
            str(AGENTQ), "git-diff", "--repo", str(self.repo), "--patch",
            "--max-lines", "300", "--repeat",
        ]
        text_result = subprocess.run(argv, text=True, capture_output=True, env=self.env, cwd=self.repo)
        self.assertEqual(text_result.returncode, 0, msg=text_result.stderr or text_result.stdout)
        for sentinel in sentinels:
            self.assertNotIn(sentinel, text_result.stdout)

        telemetry = (self.telemetry / "events.jsonl").read_text(encoding="utf-8")
        for sentinel in sentinels:
            self.assertNotIn(sentinel, telemetry)

        audit = self.data("audit")
        sensitive_findings = {item.get("path") for item in audit["findings"] if item["rule"] == "sensitive-file"}
        self.assertIn(".env.local", sensitive_findings)
        self.assertIn("secrets/quoted ü file.txt", sensitive_findings)
        self.assertIn("secrets/delete me.txt", sensitive_findings)

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
        self.assertIn("agentq 1.8.0", result.stdout)

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

    def _make_single_ecosystem(self) -> None:
        for name in ("package.json", "pnpm-workspace.yaml", "pnpm-workspace.yml", "pnpm-lock.yaml", "tsconfig.json"):
            path = self.repo / name
            if path.exists():
                path.unlink()
        shutil.rmtree(self.repo / "packages", ignore_errors=True)

    def test_python_verification_provider(self) -> None:
        self._make_single_ecosystem()
        (self.repo / "pyproject.toml").write_text(textwrap.dedent("""
            [project]
            name = "pyapp"
            dependencies = ["requests>=2"]

            [tool.pytest.ini_options]
            testpaths = ["tests"]

            [tool.ruff]
            line-length = 100
        """), encoding="utf-8")
        (self.repo / "pkg").mkdir()
        (self.repo / "pkg" / "__init__.py").write_text("")
        (self.repo / "pkg" / "core.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_core.py").write_text(
            "from pkg.core import add\n\ndef test_add():\n    assert add(1, 2) == 3\n", encoding="utf-8",
        )
        self.git("add", ".")
        self.git("commit", "-qm", "python fixture")
        (self.repo / "pkg" / "core.py").write_text("def add(a, b):\n    return a + b + 1\n", encoding="utf-8")

        plan = self.data("test-plan")
        self.assertEqual(plan["package_manager"], "python")
        self.assertIn("python", [provider["name"] for provider in plan["providers"]])
        self.assertNotIn("node", [provider["name"] for provider in plan["providers"]])
        self.assertEqual(plan["changed_packages"], ["pyapp"])
        kinds = [step["kind"] for step in plan["steps"]]
        self.assertIn("candidate-tests", kinds)
        self.assertIn("lint", kinds)
        self.assertNotIn("direct-tests", kinds)
        self.assertTrue(any(step["argv"][:3] == ["python3", "-m", "pytest"] for step in plan["steps"]))
        self.assertTrue(any(step["argv"][:2] == ["ruff", "check"] for step in plan["steps"]))

        dry = self.data("verify", "--dry-run")
        self.assertEqual(dry["status"], "planned")
        self.assertEqual(dry["changed_packages"], ["pyapp"])

        (self.repo / "tests" / "test_core.py").write_text(
            "from pkg.core import add\n\ndef test_add_broken():\n    assert add(1, 2) == 4\n", encoding="utf-8",
        )
        plan = self.data("test-plan")
        self.assertIn("direct-tests", [step["kind"] for step in plan["steps"]])

    def test_cargo_verification_provider(self) -> None:
        self._make_single_ecosystem()
        (self.repo / "Cargo.toml").write_text('[workspace]\nmembers = ["crates/*"]\n', encoding="utf-8")
        (self.repo / "crates/core/src").mkdir(parents=True)
        (self.repo / "crates/core/Cargo.toml").write_text('[package]\nname = "core"\nversion = "0.1.0"\n', encoding="utf-8")
        (self.repo / "crates/core/src/lib.rs").write_text("pub fn add(a: i32, b: i32) -> i32 { a + b }\n", encoding="utf-8")
        (self.repo / "crates/app/src").mkdir(parents=True)
        (self.repo / "crates/app/Cargo.toml").write_text(
            '[package]\nname = "app"\nversion = "0.1.0"\n\n[dependencies]\ncore = { path = "../core" }\n', encoding="utf-8",
        )
        (self.repo / "crates/app/src/main.rs").write_text("fn main() { println!(\"{}\", core::add(1, 2)); }\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "cargo fixture")
        (self.repo / "crates/core/src/lib.rs").write_text("pub fn add(a: i32, b: i32) -> i32 { a + b + 1 }\n", encoding="utf-8")

        plan = self.data("test-plan")
        self.assertEqual(plan["package_manager"], "cargo")
        self.assertEqual(plan["changed_packages"], ["core"])
        self.assertEqual(plan["dependent_packages"], ["app"])
        kinds = [step["kind"] for step in plan["steps"]]
        self.assertIn("package-tests", kinds)
        self.assertIn("typecheck", kinds)
        self.assertIn("dependent-typecheck", kinds)
        self.assertTrue(any(step["argv"] == ["cargo", "test", "-p", "core"] for step in plan["steps"]))
        self.assertTrue(any(step["argv"] == ["cargo", "check", "-p", "app"] for step in plan["steps"]))

    def test_go_verification_provider(self) -> None:
        self._make_single_ecosystem()
        (self.repo / "go.mod").write_text("module example.com/app\n\ngo 1.22\n", encoding="utf-8")
        (self.repo / "main.go").write_text("package main\n\nfunc main() {}\n", encoding="utf-8")
        (self.repo / "util").mkdir()
        (self.repo / "util" / "util.go").write_text("package util\n\nfunc Hi() string { return \"hi\" }\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "go fixture")
        (self.repo / "util" / "util.go").write_text("package util\n\nfunc Hi() string { return \"hello\" }\n", encoding="utf-8")

        plan = self.data("test-plan")
        self.assertEqual(plan["package_manager"], "go")
        self.assertEqual(plan["changed_packages"], ["example.com/app"])
        steps = [step for step in plan["steps"] if step["kind"] == "package-tests"]
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["argv"], ["go", "test", "./util/..."])

        (self.repo / "go.mod").write_text("module example.com/app\n\ngo 1.23\n", encoding="utf-8")
        plan = self.data("test-plan")
        self.assertIn("module-tests", [step["kind"] for step in plan["steps"]])

    def test_agentq_toml_config_augments_verification(self) -> None:
        (self.repo / ".agentq.toml").write_text(textwrap.dedent("""
            [verify]
            providers = ["node"]
            commands = ["make check"]
            ignore = ["generated/**"]
            contract_patterns = ["public_api/**"]

            [ownership]
            "packages/a/src" = "@test/b"
        """), encoding="utf-8")
        (self.repo / "generated").mkdir()
        (self.repo / "generated" / "thing.ts").write_text("export const generated = 1\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "config fixture")
        self.change_a("\nexport const configured = true\n")

        plan = self.data("test-plan")
        self.assertNotIn("generated/thing.ts", plan["changed_files"])
        configured = [step for step in plan["steps"] if step["kind"] == "configured"]
        self.assertEqual(len(configured), 1)
        self.assertEqual(configured[0]["argv"], ["make", "check"])
        # The ownership override attributes packages/a/src files to @test/b.
        self.assertEqual(plan["changed_packages"], ["@test/b"])

        (self.repo / ".agentq.toml").write_text(
            '[verify]\nproviders = ["node", "nope"]\n', encoding="utf-8",
        )
        failed = self.aq("test-plan", expect=2)
        self.assertIn("unknown verification providers", failed.stderr)

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
        self.assertTrue(all(
            result["log"] is None and result["log_retention"] == "deleted"
            for result in data["results"]
        ))
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
        argv = [
            str(AGENTQ), "run", "--repo", str(self.repo), "--format", "json",
            "--isolated-cache", "--keep-log", "--", "python3", "-c", "print('ok')",
        ]
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
        self.assertIn("Render budget reached", result.stdout)
        self.assertIn("continue: agentq continue", result.stdout)
        self.assertNotIn("omitted by render budget", result.stdout)

    def test_read_telemetry_distinguishes_source_and_render_budget_caps(self) -> None:
        path = self.repo / "packages/a/src/capped.py"
        path.write_text("".join(f"line {index}\n" for index in range(1, 101)), encoding="utf-8")

        self.data(
            "read", "packages/a/src/capped.py:1-100",
            "--max-lines", "2", "--budget", "1000000", "--repeat",
        )
        self.aq(
            "read", "packages/a/src/capped.py:1-100",
            "--max-lines", "100", "--budget", "300", "--repeat",
        )

        events = [
            json.loads(line) for line in (self.telemetry / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if json.loads(line).get("command") == "read"
        ]
        self.assertTrue(events[0]["source_cap_truncated"])
        self.assertFalse(events[0]["render_budget_truncated"])
        self.assertFalse(events[1]["source_cap_truncated"])
        self.assertTrue(events[1]["render_budget_truncated"])

        stats = self.data("stats", "--since", "all")
        read = next(row for row in stats["commands"] if row["command"] == "read")
        self.assertEqual(read["source_cap_truncations"], 1)
        self.assertEqual(read["truncations"], 1)

    def test_output_attribution_partitions_json_and_text_exactly(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq_lib.output_attribution import attribute_output, attribution_total
            from agentq_lib.search import render_read

        hit = {
            "path": "packages/a/src/index.ts", "line": 1, "column": 1,
            "text": "export interface OldName { value: string }",
        }
        search = {
            "query": "OldName",
            "hits": [hit],
            "files": [{"path": hit["path"], "hits": [hit]}],
        }
        rendered_json = json.dumps(search, ensure_ascii=False, separators=(",", ":"))
        json_parts = attribute_output("search", search, rendered_json, output_format="json")
        self.assertEqual(attribution_total(json_parts), len(rendered_json))
        self.assertGreater(json_parts["unique_evidence_chars"], 0)
        self.assertGreater(json_parts["duplicate_evidence_chars"], 0)
        self.assertGreater(json_parts["serialization_chars"], 0)

        read = {
            "items": [{
                "path": "packages/a/src/index.ts", "total_lines": 3,
                "start": 1, "end": 1, "lines": [{"line": 1, "text": "export interface OldName"}],
                "truncated": False,
            }],
            "truncated": True,
            "max_lines": 1,
            "continuation": {"command": "agentq read packages/a/src/index.ts:2-3", "remaining_windows": 1},
        }
        rendered_text = render_read(read)
        text_parts = attribute_output("read", read, rendered_text, output_format="text")
        self.assertEqual(attribution_total(text_parts), len(rendered_text))
        self.assertGreater(text_parts["unique_evidence_chars"], 0)
        self.assertGreater(text_parts["advice_chars"], 0)

    def test_text_output_records_exact_attribution_and_view(self) -> None:
        argv = [
            str(AGENTQ), "read", "--repo", str(self.repo), "--format", "text",
            "packages/a/src/index.ts:1-3",
        ]
        result = subprocess.run(argv, text=True, capture_output=True, env=self.env, cwd=self.repo)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        visible = result.stdout.rstrip("\n")
        event = json.loads((self.telemetry / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(event["output_format"], "text")
        self.assertEqual(event["output_view"], "windowed")
        self.assertTrue(event["output_attributed"])
        self.assertEqual(sum(event["output_attribution"].values()), len(visible))
        self.assertGreater(event["output_attribution"]["unique_evidence_chars"], 0)

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
        self.assertIn("tokens=visible_chars/4", stats["measurement_note"])
        self.assertEqual(stats["successes"], stats["tool_ok"])
        self.assertEqual(stats["failures"], stats["tool_errors"])
        self.assertEqual(stats["success_rate"], stats["tool_reliability"])
        for row in stats["commands"]:
            self.assertEqual(row["successes"], row["tool_ok"])
            self.assertEqual(row["failures"], row["tool_errors"])

    def test_disabled_context_cache_advice_does_not_access_storage(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq_lib import context_cache as cache_module
            from agentq_lib import state as state_module

        read = {
            "items": [{
                "path": "packages/a/src/index.ts",
                "start": 1,
                "end": 2,
                "version": "fixture",
                "lines": [],
            }],
        }
        diff = {"scope": "HEAD+working-tree", "total_files": 0, "files": []}
        with mock.patch.dict(os.environ, {"AGENTQ_CONTEXT_CACHE": "0"}):
            with mock.patch.object(state_module, "connection", side_effect=AssertionError("state storage accessed")):
                self.assertIsNone(cache_module.read_repeat_advice(self.repo, read))
                key = cache_module.diff_cache_key(diff, {})
                self.assertIsNone(cache_module.diff_repeat_advice(self.repo, key))

    def test_online_read_and_diff_advice_never_load_historical_telemetry(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq_lib import gitops as gitops_module
            from agentq_lib import search as search_module
            from agentq_lib import telemetry as telemetry_module

        self.archive.parent.mkdir(parents=True, exist_ok=True)
        self.archive.write_text("{malformed historical telemetry}\n" * 10_000, encoding="utf-8")
        with mock.patch.dict(os.environ, self.env):
            with mock.patch.object(telemetry_module, "load_events", side_effect=AssertionError("history loaded")):
                read = search_module.read_data(self.repo, ["packages/a/src/index.ts:1-3"])
                diff = gitops_module.diff_data(self.repo)
        self.assertEqual(len(read["items"][0]["lines"]), 3)
        self.assertEqual(diff["total_files"], 0)

    def test_read_efficiency_merges_intervals_once_without_cross_context_double_counting(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq_lib.telemetry import read_efficiency

        def event(
            *,
            task: str | None = None,
            thread: str | None = None,
            ranges: list[tuple[int, int]],
            timestamp: float = 0,
            repository: str = "repo",
        ) -> dict:
            return {
                "command": "read", "task_id": task, "thread_id": thread,
                "time": timestamp, "repo_id": repository,
                "metrics": {"read_ranges": [
                    {"file": "file", "version": "v1", "start": start, "end": end}
                    for start, end in ranges
                ]},
            }

        measured = read_efficiency([
            event(task="a", ranges=[(1, 10), (3, 5)]),
            event(task="b", ranges=[(5, 15)]),
            event(task="c", ranges=[(7, 8)]),
            event(thread="thread", ranges=[(11, 12)]),
        ])
        self.assertEqual(measured["same_context_overlap_lines"], 3)
        self.assertEqual(measured["fully_redundant_ranges"], 1)
        self.assertEqual(measured["cross_task_overlap_lines"], 6)
        self.assertEqual(measured["cross_thread_overlap_lines"], 2)

        separate_sessions = read_efficiency([
            event(ranges=[(1, 10)], timestamp=100),
            event(ranges=[(1, 10)], timestamp=100 + 31 * 60),
        ])
        self.assertEqual(separate_sessions["same_context_overlap_lines"], 0)
        self.assertEqual(separate_sessions["fully_redundant_ranges"], 0)

        same_session = read_efficiency([
            event(ranges=[(1, 10)], timestamp=100),
            event(ranges=[(1, 10)], timestamp=100 + 29 * 60),
        ])
        self.assertEqual(same_session["same_context_overlap_lines"], 10)
        self.assertEqual(same_session["fully_redundant_ranges"], 1)

    def test_jsonl_loading_filters_before_retaining_and_hmac_key_is_cached(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq_lib import telemetry as telemetry_module

        records = [
            {"schema": 5, "id": "keep", "time": 100, "repo_id": "r1", "command": "search"},
            {"schema": 5, "id": "read", "time": 100, "repo_id": "r1", "command": "read"},
            {"schema": 5, "id": "other", "time": 100, "repo_id": "r2", "command": "search"},
            {"schema": 5, "id": "old", "time": 10, "repo_id": "r1", "command": "search"},
            {"schema": 5, "id": "task", "time": 100, "repo_id": "r1", "command": "task"},
        ]
        self.archive.parent.mkdir(parents=True, exist_ok=True)
        self.archive.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        with mock.patch.dict(os.environ, self.env):
            loaded, sources = telemetry_module.load_events(
                cutoff=50, repository_id="r1", operations={"search"},
            )
            telemetry_module._fingerprint_key_at.cache_clear()
            first = telemetry_module._fingerprint_key()
            second = telemetry_module._fingerprint_key()
            cache = telemetry_module._fingerprint_key_at.cache_info()
        self.assertEqual([event["id"] for event in loaded], ["keep", "task"])
        self.assertEqual(sources["archive"], 2)
        self.assertEqual(first, second)
        self.assertEqual(cache.hits, 1)

    def test_default_stats_orders_compact_events_across_rotated_storage(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq_lib import telemetry as telemetry_module

        with mock.patch.dict(os.environ, self.env):
            repository_id = telemetry_module.repo_id(self.repo)

            def read_event(identity: str, timestamp: float, start: int, end: int) -> dict:
                return {
                    "schema": telemetry_module.SCHEMA,
                    "id": identity,
                    "time": timestamp,
                    "repo_id": repository_id,
                    "repo_name": self.repo.name,
                    "command": "read",
                    "tool_status": "ok",
                    "subject_status": "not-applicable",
                    "duration_ms": 1,
                    "visible_chars": 10,
                    "prebudget_chars": 10,
                    "source_chars": 10,
                    "source_measured": True,
                    "task_id": "task",
                    "metrics": {"read_ranges": [{
                        "file": "file", "version": "v1", "start": start, "end": end,
                    }]},
                }

            self.archive.parent.mkdir(parents=True, exist_ok=True)
            self.archive.write_text(json.dumps(read_event("subset", 200, 3, 5)) + "\n", encoding="utf-8")
            rotated = telemetry_module.hot_file().with_suffix(".jsonl.1")
            rotated.parent.mkdir(parents=True, exist_ok=True)
            rotated.write_text(json.dumps(read_event("broad", 100, 1, 10)) + "\n", encoding="utf-8")

            default = telemetry_module.stats_data(self.repo, since="all")
            detailed = telemetry_module.stats_data(self.repo, since="all", detailed=True)

        self.assertEqual(default["reads"]["fully_redundant_ranges"], 1)
        self.assertEqual(default["reads"]["fully_redundant_ranges"], detailed["reads"]["fully_redundant_ranges"])

    def test_archive_bulk_appends_once_and_parallel_writers_do_not_lose_events(self) -> None:
        processes = [
            subprocess.Popen(
                [str(AGENTQ), "doctor", "--repo", str(self.repo), "--format", "json", "--budget", "100000"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env, cwd=self.repo,
            )
            for _ in range(12)
        ]
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, msg=stderr or stdout)

        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq_lib import telemetry as telemetry_module
        with mock.patch.dict(os.environ, self.env):
            with mock.patch.object(os, "open", wraps=os.open) as opened:
                archived = telemetry_module.archive_hot_events()
            destination_opens = [
                call for call in opened.call_args_list
                if call.args and Path(call.args[0]) == self.archive
            ]
            repeated = telemetry_module.archive_hot_events()
        self.assertEqual(archived["hot_events"], 12)
        self.assertEqual(archived["added"], 12)
        self.assertEqual(len(destination_opens), 1)
        self.assertEqual(repeated["added"], 0)

    def test_disabled_telemetry_does_not_create_storage_or_suppress_output(self) -> None:
        disabled_hot = Path(self.temp.name) / "disabled-hot"
        disabled_state = Path(self.temp.name) / "disabled-state" / "events.jsonl"
        disabled_cache = Path(self.temp.name) / "disabled-context"
        disabled_env = {
            "AGENTQ_TELEMETRY": "0",
            "AGENTQ_CONTEXT_CACHE": "0",
            "AGENTQ_TELEMETRY_HOT": str(disabled_hot),
            "AGENTQ_TELEMETRY_STATE": str(disabled_state),
            "AGENTQ_CONTEXT_CACHE_HOME": str(disabled_cache),
        }

        first_read = self.data("read", "packages/a/src/index.ts:1-3", extra_env=disabled_env)
        second_read = self.data("read", "packages/a/src/index.ts:1-3", extra_env=disabled_env)
        self.assertEqual(len(first_read["items"][0]["lines"]), 3)
        self.assertEqual(len(second_read["items"][0]["lines"]), 3)
        self.assertFalse(second_read["items"][0].get("suppressed", False))

        self.change_a("\nexport const telemetryDisabled = true\n")
        first_diff = self.data("git-diff", "--patch", extra_env=disabled_env)
        second_diff = self.data("git-diff", "--patch", extra_env=disabled_env)
        self.assertTrue(first_diff["patch"])
        self.assertEqual(second_diff["patch"], first_diff["patch"])
        self.assertFalse(second_diff.get("repeat_suppressed", False))

        doctor = self.data("doctor", extra_env=disabled_env)
        self.assertFalse(doctor["telemetry"]["enabled"])
        self.assertFalse(disabled_hot.exists())
        self.assertFalse(disabled_state.parent.exists())
        self.assertFalse(disabled_cache.exists())

    def test_stats_terminal_dashboard_and_archive(self) -> None:
        self.data("git-status")
        argv = [
            str(AGENTQ), "stats", "--repo", str(self.repo), "--since", "all",
            "--format", "text", "--color", "never", "--archive",
        ]
        result = subprocess.run(argv, text=True, capture_output=True, env=self.env)
        self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
        self.assertIn("agentq", result.stdout)
        self.assertIn("Operations", result.stdout)
        self.assertIn("~", result.stdout)
        self.assertTrue(self.archive.is_file())
        self.assertEqual(self.archive.stat().st_mode & 0o777, 0o600)


    def test_stats_archive_only_is_idempotent_and_storage_reads_both_sources(self) -> None:
        self.data("search", "OldName")
        first = self.data("stats", "--archive-only", "--all-repos")
        second = self.data("stats", "--archive-only", "--all-repos")
        self.assertEqual(first["added"], 1)
        self.assertEqual(second["added"], 0)
        storage = self.data("stats", "--storage")
        self.assertEqual(storage["persistent"]["events"], 1)
        self.assertEqual(storage["hot"]["events"], 1)
        stats = self.data("stats", "--since", "all")
        self.assertEqual(stats["events"], 1)

    def test_stats_reset_current_repo_preserves_other_repo_and_hot_only_preserves_archive(self) -> None:
        self.data("search", "OldName")
        self.data("stats", "--archive-only", "--all-repos")

        other = Path(self.temp.name) / "other"
        other.mkdir()
        subprocess.run(["git", "init", "-q", str(other)], check=True, env=self.env)
        argv = [str(AGENTQ), "search", "--repo", str(other), "--format", "json", "nothing"]
        result = subprocess.run(argv, text=True, capture_output=True, env=self.env)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

        reset = self.data("stats", "--reset")
        self.assertGreaterEqual(reset["hot_removed"] + reset["persistent_removed"], 1)
        current = self.data("stats", "--since", "all")
        self.assertEqual(current["events"], 0)
        all_stats = self.data("stats", "--since", "all", "--all-repos")
        self.assertEqual(all_stats["events"], 1)

        self.data("search", "OldName")
        self.data("stats", "--archive-only", "--all-repos")
        self.data("search", "Wrapped")
        hot_reset = self.data("stats", "--reset", "--hot-only")
        self.assertGreaterEqual(hot_reset["hot_removed"], 1)
        after = self.data("stats", "--since", "all")
        self.assertGreaterEqual(after["events"], 1)  # archived event intentionally remains

    def test_stats_reset_refuses_active_task_without_force(self) -> None:
        self.data("task", "begin")
        result = self.aq("stats", "--reset", expect=2)
        self.assertIn("task is active", result.stderr)
        forced = self.data("stats", "--reset", "--force")
        self.assertEqual(forced["active_tasks_preserved"], 1)
        self.data("task", "abandon")

    def test_stats_persistence_installer_writes_and_controls_user_units(self) -> None:
        config = Path(self.temp.name) / "config"
        fake_systemctl = self.bin / "systemctl-agentq-test"
        fake_systemctl.write_text(textwrap.dedent("""\
            #!/usr/bin/env bash
            set -euo pipefail
            case "$*" in
              *is-active*) echo active ;;
              *is-enabled*) echo enabled ;;
            esac
            exit 0
        """), encoding="utf-8")
        fake_systemctl.chmod(0o755)
        env = {
            "XDG_CONFIG_HOME": str(config),
            "AGENTQ_SYSTEMCTL": str(fake_systemctl),
        }
        installed = self.data("stats", "--install-persistence", "--persistence-interval", "5min", extra_env=env)
        self.assertEqual(installed["active"], "active")
        timer = config / "systemd/user/agentq-archive.timer"
        service = config / "systemd/user/agentq-archive.service"
        self.assertTrue(timer.is_file())
        self.assertTrue(service.is_file())
        self.assertIn("OnUnitActiveSec=5min", timer.read_text(encoding="utf-8"))
        self.assertIn("stats --archive-only --all-repos", service.read_text(encoding="utf-8"))
        self.assertIn("StandardOutput=null", service.read_text(encoding="utf-8"))
        storage = self.data("stats", "--storage", extra_env=env)
        self.assertTrue(storage["timer"]["installed"] )
        self.assertEqual(storage["timer"]["active"], "active")
        removed = self.data("stats", "--remove-persistence", extra_env=env)
        self.assertEqual(len(removed["removed"]), 2)
        self.assertFalse(timer.exists())
        self.assertFalse(service.exists())

    def test_stats_distinguishes_tool_health_from_child_command_failure(self) -> None:
        self.data("run", "--", "python3", "-c", "raise SystemExit(7)")
        stats = self.data("stats", "--since", "all")
        self.assertEqual(stats["tool_errors"], 0)
        self.assertEqual(stats["project_commands"]["failed"], 1)
        run_row = next(row for row in stats["commands"] if row["command"] == "run")
        self.assertEqual(run_row["tool_ok"], 1)
        self.assertEqual(run_row["tool_errors"], 0)
        self.assertEqual(run_row["subject_failures"], 1)

    def test_stats_uses_attributed_evidence_for_rendering_overhead(self) -> None:
        self.data("search", "OldName")
        stats = self.data("stats", "--since", "all")
        row = next(row for row in stats["commands"] if row["command"] == "search")
        self.assertIsNone(row["reduction_percent"])
        self.assertEqual(row["instrumented_calls"], 1)
        self.assertGreater(row["candidate_delta_chars"], 0)
        self.assertGreater(row["rendering_evidence_chars"], 0)
        self.assertGreater(row["rendering_overhead_chars"], 0)
        self.assertEqual(row["overhead_chars"], row["rendering_overhead_chars"])
        self.assertEqual(
            sum(row["rendering_overhead_components"].values()),
            row["rendering_overhead_chars"],
        )

    def test_stats_summary_without_evidence_reports_overhead_not_reduction(self) -> None:
        self.data("search", "NO_MATCH_FOR_SUMMARY", "--view", "summary")
        stats = self.data("stats", "--since", "all")
        row = next(row for row in stats["commands"] if row["command"] == "search")
        self.assertEqual(row["rendering_evidence_chars"], 0)
        self.assertEqual(row["rendering_overhead_chars"], row["attributed_visible_chars"])
        self.assertEqual(row["rendering_overhead_percent"], 100.0)
        self.assertIsNone(row["reduction_percent"])

    def test_default_stats_skip_detailed_analytics_and_detail_builds_one_context_index(self) -> None:
        self.data("task", "begin")
        self.data("search", "OldName")
        self.data("read", "packages/a/src/index.ts:1-3")
        self.data("verify-changed", "--dry-run")
        self.aq("inspect", "packages", "--bogus", expect=2)
        self.data("task", "accept")
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq_lib import telemetry as telemetry_module

        with mock.patch.dict(os.environ, self.env):
            with mock.patch.object(
                telemetry_module, "_build_context_index", wraps=telemetry_module._build_context_index,
            ) as build_index:
                summary = telemetry_module.stats_data(self.repo, since="all")
                build_index.assert_not_called()
                detailed = telemetry_module.stats_data(self.repo, since="all", detailed=True)
                build_index.assert_called_once()

        self.assertEqual(summary["transitions"], [])
        self.assertEqual(summary["command_chains"], [])
        self.assertEqual(summary["failures_detail"], {"rows": [], "signatures": []})
        self.assertNotIn("consecutive_read_pairs", summary["reads"])
        self.assertIsNone(summary["tasks"]["calls_distribution"]["p50"])
        self.assertIsNone(summary["verification"]["files_distribution"]["p50"])
        self.assertTrue(detailed["transitions"])
        self.assertTrue(detailed["command_chains"])
        self.assertTrue(detailed["failures_detail"]["rows"])
        self.assertIn("consecutive_read_pairs", detailed["reads"])
        self.assertIsNotNone(detailed["tasks"]["calls_distribution"]["p50"])
        self.assertIsNotNone(detailed["verification"]["files_distribution"]["p50"])

    def test_stats_exact_window_and_codex_thread_hash(self) -> None:
        thread = "0192b49b-example-thread-id"
        self.data("search", "OldName", extra_env={"CODEX_THREAD_ID": thread})
        stats = self.data("stats", "--since", "7d")
        self.assertEqual(stats["threads"], 1)
        self.assertGreaterEqual(stats["window_end"] - stats["window_start"], 7 * 86400 - 2)
        self.assertLessEqual(stats["window_end"] - stats["window_start"], 7 * 86400 + 2)
        raw = (self.telemetry / "events.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(thread, raw)

    def test_stats_compares_only_matching_seven_day_cohorts(self) -> None:
        self.data("search", "OldName", "--format", "compact-json", "--repeat")
        events_path = self.telemetry / "events.jsonl"
        current = json.loads(events_path.read_text(encoding="utf-8").splitlines()[-1])
        previous = {
            **current,
            "id": "previous-window-search",
            "time": float(current["time"]) - 8 * 86400,
        }
        events_path.write_text(
            events_path.read_text(encoding="utf-8") + json.dumps(previous) + "\n",
            encoding="utf-8",
        )

        stats = self.data("stats", "--since", "7d", "--detailed")
        cohort = stats["cohort_comparison"]
        self.assertTrue(cohort["available"])
        self.assertTrue(cohort["claim_eligible"])
        self.assertEqual(cohort["matched_fingerprints"], 1)
        self.assertEqual(cohort["matched_current_percent"], 100.0)
        self.assertEqual(cohort["matched_previous_percent"], 100.0)
        self.assertEqual(cohort["visible_reduction_percent"], 0.0)
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq_lib.telemetry import render_stats_plain
        rendered = render_stats_plain(stats)
        self.assertIn("Comparable cohort", rendered)
        self.assertIn("Comparable seven-day cohort", rendered)

    def test_read_overlap_is_version_aware_and_private(self) -> None:
        self.data("task", "begin")
        first = self.data("read", "packages/a/src/index.ts:1-3")
        self.assertNotIn("read_overlap", first)
        second = self.data("read", "packages/a/src/index.ts:2-4")
        self.assertEqual(second["read_overlap"]["overlap_lines"], 2)
        self.assertEqual(second["read_overlap"]["scope"], "task")

        stats = self.data("stats", "--since", "all")
        self.assertEqual(stats["reads"]["tracked_calls"], 2)
        self.assertEqual(stats["reads"]["unique_files"], 1)
        self.assertEqual(stats["reads"]["reread_ranges"], 1)
        self.assertEqual(stats["reads"]["overlap_lines"], 0)
        self.assertEqual(stats["reads"]["online_cache_overlap_lines"], 2)

        raw = (self.telemetry / "events.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("packages/a/src/index.ts", raw)

        path = self.repo / "packages/a/src/index.ts"
        path.write_text(path.read_text(encoding="utf-8") + "\nexport const versionChanged = 1\n", encoding="utf-8")
        third = self.data("read", "packages/a/src/index.ts:2-4")
        self.assertNotIn("read_overlap", third)

    def test_explicit_task_boundaries_produce_per_accepted_task_metrics(self) -> None:
        begin = self.data("task", "begin")
        self.assertEqual(begin["status"], "active")
        status = self.data("task", "status")
        self.assertTrue(status["active"])
        self.data("search", "OldName")
        self.data("read", "packages/a/src/index.ts:1-3")
        accepted = self.data("task", "accept")
        self.assertEqual(accepted["status"], "accepted")

        stats = self.data("stats", "--since", "all")
        self.assertEqual(stats["tasks"]["accepted"], 1)
        self.assertEqual(stats["tasks"]["active"], 0)
        self.assertEqual(stats["tasks"]["attributed_calls"], 2)
        self.assertEqual(stats["tasks"]["accepted_operation_calls"], 2)
        self.assertEqual(stats["tasks"]["calls_per_accepted_task"], 2.0)
        self.assertEqual(stats["tasks"]["reads_per_accepted_task"], 1.0)
        self.assertGreater(stats["tasks"]["token_proxy_per_accepted_task"], 0)
        self.assertNotIn("task", {row["command"] for row in stats["commands"]})

    def test_priority_commands_measure_candidates_and_visible_output(self) -> None:
        self.change_a()
        self.data("read", "packages/a/src/index.ts:1-3")
        self.data("search", "OldName")
        self.data("git-diff", "--hunks")
        self.data("outline", "packages/a/src")
        self.data("inspect", "OldName", "--path", "packages")

        events = [json.loads(line) for line in (self.telemetry / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        measured = {event["command"]: event for event in events if event["command"] in {"read", "search", "git-diff", "outline", "inspect"}}
        self.assertEqual(set(measured), {"read", "search", "git-diff", "outline", "inspect"})
        for event in measured.values():
            self.assertEqual(event["schema"], 6)
            self.assertTrue(event["source_measured"])
            self.assertGreaterEqual(event["source_chars"], 0)
            self.assertGreater(event["visible_chars"], 0)
            self.assertIn("candidate_chars", event["metrics"])
            self.assertIn("candidate_lines", event["metrics"])
            self.assertEqual(event["output_format"], "json")
            self.assertTrue(event["output_attributed"])
            self.assertEqual(sum(event["output_attribution"].values()), event["visible_chars"])
            self.assertFalse(event["repeat_requested"])

        stats = self.data("stats", "--since", "all", "--detailed")
        rows = {row["command"]: row for row in stats["commands"]}
        for command in measured:
            self.assertEqual(rows[command]["instrumented_calls"], 1)
            self.assertEqual(rows[command]["attributed_calls"], 1)
        self.assertEqual(stats["measurement"]["attributed_calls"], 5)
        self.assertEqual(
            sum(stats["measurement"]["output_attribution"].values()),
            stats["measurement"]["attributed_visible_chars"],
        )
        self.assertEqual(
            stats["measurement"]["rendering_evidence_chars"]
            + stats["measurement"]["rendering_overhead_chars"],
            stats["measurement"]["attributed_visible_chars"],
        )
        self.assertTrue(stats["output_profiles"])
        for profile in stats["output_profiles"]:
            self.assertEqual(sum(profile["output_attribution"].values()), profile["attributed_visible_chars"])
            self.assertEqual(profile["attributed_visible_chars"], profile["visible_chars"])
            self.assertEqual(
                profile["rendering_evidence_chars"] + profile["rendering_overhead_chars"],
                profile["attributed_visible_chars"],
            )

    def test_accepted_task_outcomes_track_retries_suppression_and_correction(self) -> None:
        self.data("task", "begin")
        self.data("search", "OldName", "--budget", "256")
        self.data("search", "OldName", "--budget", "2048")
        self.data("search", "OldName", "--budget", "2048")
        self.data("read", "packages/a/src/index.ts:1-3")
        self.data("read", "packages/a/src/index.ts:2-4")
        self.data("run", "--", "python3", "-c", "raise SystemExit(3)")
        self.data("search", "Wrapped")
        self.data("run", "--", "python3", "-c", "print('ok')")
        self.data("task", "accept")

        stats = self.data("stats", "--since", "all", "--detailed")
        tasks = stats["tasks"]
        self.assertEqual(tasks["accepted"], 1)
        self.assertEqual(tasks["same_context_overlap_lines"], 2)
        self.assertEqual(tasks["exact_repeat_suppressions"], 1)
        self.assertEqual(tasks["expanded_retries"], 1)
        self.assertEqual(tasks["correction_calls"], 1)
        outcome = tasks["accepted_tasks"][0]
        self.assertGreater(outcome["visible_chars"], 0)
        self.assertEqual(outcome["estimated_tokens"], round(outcome["visible_chars"] / 4))
        self.assertEqual(outcome["verification_result"], "passed")
        self.assertEqual(outcome["verification_calls"], 2)
        self.assertGreaterEqual(outcome["calls_by_command"]["search"], 4)
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq_lib.telemetry import render_stats_plain
        rendered = render_stats_plain(stats)
        self.assertIn("Accepted-task outcomes", rendered)
        self.assertIn("Accepted-task calls", rendered)
        self.assertIn("Accepted-task output", rendered)

    def test_stats_tracks_immediate_error_and_budget_retries_within_task(self) -> None:
        self.data("task", "begin")
        self.aq("search", "OldName", "unexpected", expect=2)
        self.data("search", "OldName", "--budget", "256")
        self.data("search", "OldName", "--budget", "100000")
        self.data("task", "accept")

        stats = self.data("stats", "--since", "all", "--detailed")
        retry = stats["retry_behavior"]
        self.assertEqual(retry["error_calls"], 1)
        self.assertEqual(retry["error_followups"], 1)
        self.assertEqual(retry["same_command_error_retries"], 1)
        self.assertEqual(retry["recovered_error_retries"], 1)
        self.assertEqual(retry["hinted_error_calls"], 1)
        self.assertEqual(retry["hinted_error_followups"], 1)
        self.assertEqual(retry["hinted_recovered_retries"], 1)
        self.assertEqual(retry["truncated_calls"], 1)
        self.assertEqual(retry["truncation_followups"], 1)
        self.assertEqual(retry["expanded_budget_retries"], 1)
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq_lib.telemetry import render_stats_plain
        rendered = render_stats_plain(stats)
        self.assertIn("Retry behavior", rendered)
        self.assertIn("Output attribution", rendered)
        events = [
            json.loads(line)
            for line in (self.telemetry / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        error = next(event for event in events if event.get("tool_status") == "error")
        self.assertEqual(error["output_format"], "json")
        self.assertTrue(error["output_attributed"])
        self.assertEqual(sum(error["output_attribution"].values()), error["visible_chars"])
        self.assertEqual(error["recovery_hint"], "search-path-form")

    def test_schema_five_events_gain_safe_attribution_defaults(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq_lib.telemetry import _normalize_event

        migrated = _normalize_event({"schema": 5, "command": "search", "visible_chars": 10})
        self.assertEqual(migrated["schema"], 6)
        self.assertEqual(migrated["source_schema"], 5)
        self.assertEqual(migrated["output_format"], "unknown")
        self.assertFalse(migrated["output_attributed"])
        self.assertEqual(sum(migrated["output_attribution"].values()), 0)
        current = _normalize_event({"schema": 6, "command": "search", "visible_chars": 0})
        self.assertEqual(current["source_schema"], 6)
        self.assertEqual(current["output_view"], "default")
        self.assertFalse(current["repeat_requested"])

    def test_seven_day_cohort_requires_exact_profiles_and_reports_exclusions(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq_lib.telemetry import SCHEMA, _cohort_comparison

        now = 1_800_000_000.0

        def event(age_days: int, visible: int, *, fingerprint: str = "same", **overrides: object) -> dict:
            value = {
                "schema": SCHEMA,
                "source_schema": SCHEMA,
                "time": now - age_days * 86400,
                "command": "search",
                "output_format": "compact-json",
                "output_view": "matches",
                "operation_fingerprint": fingerprint,
                "visible_chars": visible,
                "tool_status": "ok",
            }
            value.update(overrides)
            return value

        comparable = _cohort_comparison([
            event(1, 60), event(2, 80), event(8, 100), event(9, 120),
        ], now)
        self.assertTrue(comparable["claim_eligible"])
        self.assertEqual(comparable["matched_current_percent"], 100.0)
        self.assertEqual(comparable["matched_previous_percent"], 100.0)
        self.assertEqual(comparable["visible_reduction_percent"], 36.4)
        self.assertEqual(comparable["rows"][0]["current"]["visible_chars_distribution"]["p90"], 78.0)

        incomplete = _cohort_comparison([
            event(1, 60), event(8, 100),
            event(2, 50, source_schema=5),
            event(9, 50, output_format="unknown"),
            event(3, 50, fingerprint=""),
        ], now)
        self.assertFalse(incomplete["claim_eligible"])
        self.assertIsNone(incomplete["visible_reduction_percent"])
        self.assertIsNone(incomplete["rows"][0]["visible_reduction_percent"])
        self.assertEqual(incomplete["current"]["excluded"], {
            "incompatible_schema": 1,
            "missing_operation_fingerprint": 1,
        })
        self.assertEqual(incomplete["previous"]["excluded"], {"unknown_format": 1})

    def test_task_aliases_and_next_support_multiple_tasks_in_one_thread(self) -> None:
        started = self.data("task", "start")
        self.assertEqual(started["action"], "begin")
        self.data("search", "OldName", extra_env={"CODEX_THREAD_ID": "one-thread"})

        rotated = self.data("task", "next", extra_env={"CODEX_THREAD_ID": "one-thread"})
        self.assertEqual(rotated["action"], "next")
        self.assertEqual(rotated["completed_status"], "accepted")
        self.assertNotEqual(rotated["task_id"], rotated["completed_task_id"])

        self.data("search", "Wrapped", extra_env={"CODEX_THREAD_ID": "one-thread"})
        finished = self.data("task", "done", extra_env={"CODEX_THREAD_ID": "one-thread"})
        self.assertEqual(finished["action"], "accept")

        stats = self.data("stats", "--since", "all")
        self.assertEqual(stats["tasks"]["started"], 2)
        self.assertEqual(stats["tasks"]["accepted"], 2)
        self.assertEqual(stats["tasks"]["active"], 0)
        self.assertEqual(stats["tasks"]["attributed_calls"], 2)
        self.assertEqual(stats["threads"], 1)

    def test_task_without_action_reports_status(self) -> None:
        status = self.data("task")
        self.assertFalse(status["active"])
        self.data("task", "begin")
        status = self.data("task")
        self.assertTrue(status["active"])
        self.assertGreaterEqual(status["age_seconds"], 0)

    def test_task_state_is_repo_scoped_not_codex_thread_scoped(self) -> None:
        self.data("task", "begin")
        self.data("search", "OldName", extra_env={"CODEX_THREAD_ID": "thread-a"})
        self.data("search", "Wrapped", extra_env={"CODEX_THREAD_ID": "thread-b"})
        self.data("task", "accept")
        stats = self.data("stats", "--since", "all")
        self.assertEqual(stats["tasks"]["accepted"], 1)
        self.assertEqual(stats["tasks"]["attributed_calls"], 2)
        self.assertEqual(stats["threads"], 2)

    def test_stats_plain_render_uses_semantic_markers_and_local_window(self) -> None:
        self.data("run", "--", "python3", "-c", "raise SystemExit(3)")
        argv = [
            str(AGENTQ), "stats", "--repo", str(self.repo), "--since", "all",
            "--format", "text", "--plain", "--color", "never",
        ]
        result = subprocess.run(argv, text=True, capture_output=True, env=self.env)
        self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
        self.assertIn("Agentq CLI", result.stdout)
        self.assertIn("1 succeeded, 0 failed, 100.0%", result.stdout)
        self.assertIn("Project commands", result.stdout)
        self.assertIn("0 passed, 1 failed, 0 unknown", result.stdout)
        self.assertIn("Failures", result.stdout)
        self.assertNotIn("CLI failures", result.stdout)
        self.assertIn("Failure rate", result.stdout)
        self.assertIn("Rendering overhead", result.stdout)
        self.assertIn("Overhead", result.stdout)
        self.assertNotIn("Output change", result.stdout)
        self.assertNotIn("Output expansion", result.stdout)
        self.assertNotIn(" larger", result.stdout)
        self.assertNotIn(" smaller", result.stdout)
        self.assertNotIn("Saved", result.stdout)
        self.assertNotIn(" Tool ", result.stdout)
        self.assertNotIn(" Pass ", result.stdout)
        self.assertNotIn(" Fail ", result.stdout)
        self.assertNotIn("Attention", result.stdout)
        self.assertNotIn("unavailable", result.stdout)

    def test_stats_recent_is_opt_in_and_implies_detail(self) -> None:
        self.data("search", "OldName")
        normal = self.data("stats", "--since", "all")
        self.assertEqual(normal["recent"], [])
        detailed = self.data("stats", "--since", "all", "--detailed")
        self.assertTrue(detailed["detailed"])
        self.assertEqual(detailed["recent"], [])
        implied = self.data("stats", "--since", "all", "--recent", "1")
        self.assertTrue(implied["detailed"])
        self.assertEqual(len(implied["recent"]), 1)

    def test_stats_presentation_model_matches_plain_output(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq_lib.telemetry import render_stats_plain, stats_presentation_model

        self.data("task", "begin")
        self.data("read", "packages/a/src/index.ts:1-3")
        self.data("read", "packages/a/src/index.ts:2-3")
        stats = self.data("stats", "--since", "all", "--detail")
        self.assertEqual(stats["reads"]["top_files"][0]["file"], "packages/a/src/index.ts")
        model = stats_presentation_model(stats)
        plain = render_stats_plain(stats)

        for section in [*model["sections"], *model["detail_sections"]]:
            self.assertIn(section["name"], plain)
            for row in section["rows"]:
                self.assertIn(row["label"], plain)
                self.assertIn(row["value"], plain)
        self.assertIn("Same-task reread", plain)
        self.assertIn("Online cache", plain)
        self.assertIn("reads sampled", plain)
        self.assertIn("Attention", plain)
        self.assertNotIn("unavailable", plain)
        self.assertNotIn(" · ", plain)
        self.assertNotIn("Metric definitions", plain)
        self.assertNotIn("Inspect: agentq stats", plain)

    def test_stats_distinguishes_missing_verification_measurements_from_zero(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq_lib.telemetry import _verification_stats

        legacy = _verification_stats([{"subject_status": "passed", "metrics": {}}], detailed=True)
        measured_zero = _verification_stats([{
            "subject_status": "passed",
            "metrics": {
                "verification_checks_measured": True,
                "verification_files_measured": True,
                "verification_packages_measured": True,
            },
        }], detailed=True)
        self.assertEqual(legacy["checks_instrumented_runs"], 0)
        self.assertEqual(legacy["files_instrumented_runs"], 0)
        self.assertEqual(measured_zero["checks_instrumented_runs"], 1)
        self.assertEqual(measured_zero["checks_executed"], 0)
        self.assertEqual(measured_zero["files_distribution"]["p50"], 0)

    def test_stats_ansi_renderer_colors_headers_and_only_status_numbers_within_budget(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq_lib.telemetry import print_stats, render_stats_ansi, stats_presentation_model

        self.data("search", "OldName")
        self.data("run", "--", "python3", "-c", "raise SystemExit(3)")
        self.aq("inspect", "packages", "--bogus", expect=2)
        stats = self.data("stats", "--since", "all")
        model = stats_presentation_model(stats)

        health = next(section for section in model["sections"] if section["name"] == "Health")
        agentq = next(row for row in health["rows"] if row["label"] == "Agentq CLI")
        self.assertEqual(
            [(segment["text"], segment["style"]) for segment in agentq["segments"] if segment["style"]],
            [(str(stats["tool_ok"]), "green"), (str(stats["tool_errors"]), "red")],
        )

        status_colours = ("red", "green", "yellow")
        for section in [*model["sections"], *model["detail_sections"]]:
            for row in section["rows"]:
                for segment in row["segments"]:
                    if any(colour in segment["style"] for colour in status_colours):
                        self.assertRegex(segment["text"], r"^[\d,.]+%?$")
        output = next(section for section in model["sections"] if section["name"] == "Output")
        self.assertFalse(any(segment["style"] for row in output["rows"] for segment in row["segments"]))

        rendered = render_stats_ansi(stats)
        self.assertRegex(rendered, r"\x1b\[[0-9;]*31m")
        self.assertRegex(rendered, r"\x1b\[[0-9;]*32m")
        self.assertRegex(rendered, r"\x1b\[[0-9;]*38;5;208magentq stats")
        self.assertRegex(rendered, r"\x1b\[1;38;5;208mHealth")
        self.assertRegex(rendered, r"\x1b\[1;38;5;208mOperations")
        self.assertRegex(rendered, r"\x1b\[[0-9;]*38;5;208mOperation")
        self.assertNotIn("Attention", rendered)
        for ansi_colour in (33, 34, 35, 36):
            self.assertNotRegex(rendered, rf"\x1b\[[0-9;]*{ansi_colour}m")

        class TTYBuffer(__import__("io").StringIO):
            def isatty(self) -> bool:
                return True

        stream = TTYBuffer()
        with mock.patch.object(sys, "stdout", stream):
            print_stats(stats, color="auto", budget=100)
        self.assertLessEqual(len(stream.getvalue().rstrip("\n")), 100)

    def test_stats_reports_unknown_project_outcomes_and_separate_overlap_models(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq_lib.telemetry import _event_facets, read_efficiency

        project = _event_facets([{"command": "run", "subject_status": None}])["project_commands"]
        self.assertEqual(project["unknown"], 1)

        measured = read_efficiency([
            {
                "command": "read", "task_id": "task-a",
                "metrics": {"read_ranges": [{"file": "file", "version": "v1", "start": 1, "end": 3}]},
            },
            {
                "command": "read", "task_id": "task-a",
                "metrics": {
                    "read_ranges": [{"file": "file", "version": "v1", "start": 2, "end": 3}],
                    "same_context_overlap_lines": 1,
                },
            },
        ], detailed=True)
        self.assertEqual(measured["same_context_overlap_lines"], 2)
        self.assertEqual(measured["online_cache_overlap_lines"], 1)
        self.assertEqual(measured["fully_redundant_ranges"], 1)
        self.assertEqual(measured["top_contexts"][0]["lines"], 2)
        measured_zero = read_efficiency([{
            "command": "read", "task_id": "task-a",
            "metrics": {
                "read_ranges": [{"file": "file", "version": "v1", "start": 1, "end": 3}],
                "online_cache_measured": True,
            },
        }])
        self.assertEqual(measured_zero["online_cache_observed_calls"], 1)
        self.assertEqual(measured_zero["online_cache_overlap_lines"], 0)

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

        located = self.data("ts-nav", "locate", "OldName", "--path", "packages")
        self.assertEqual(located["resolution_mode"], "symbol")
        self.assertEqual(located["total"], 1)
        self.assertEqual(located["candidates"][0]["path"], "packages/a/src/index.ts")

        refs = self.data("ts-nav", "refs", "OldName", "--path", "packages")
        self.assertEqual(refs["resolution_mode"], "symbol")
        self.assertGreaterEqual(refs["total"], 2)
        paths = {item["path"] for item in refs["results"]}
        self.assertIn("packages/b/src/index.ts", paths)

        overview = self.data("ts-nav", "overview", "OldName", "--path", "packages")
        self.assertEqual(overview["resolution_mode"], "symbol")
        self.assertIn("references", overview)
        self.assertIn("declaration_span", overview)

        exact = self.data("ts-nav", "references", "packages/a/src/index.ts:1:18")
        self.assertEqual(exact["resolution_mode"], "position")
        self.assertGreaterEqual(exact["total"], 2)

        (self.repo / "packages/b/src/duplicate.ts").write_text(
            "export interface OldName { other: number }\n", encoding="utf-8"
        )
        ambiguous = self.data("ts-nav", "references", "OldName", "--path", "packages")
        self.assertTrue(ambiguous["ambiguous"])
        self.assertEqual(ambiguous["total"], 2)
        picked = self.data("ts-nav", "references", "OldName", "--path", "packages", "--pick", "1")
        self.assertEqual(picked["resolution_mode"], "symbol")
        self.assertEqual(picked["candidate_count"], 2)

        inspected = self.data("inspect", "OldName", "--path", "packages", "--limit", "20")
        self.assertEqual(inspected["kind"], "semantic")

        stats = self.data("stats", "--since", "all", "--detail")
        self.assertEqual(stats["navigation"]["semantic_calls"], 7)
        self.assertEqual(stats["navigation"]["semantic_actions"]["references"], 4)
        self.assertEqual(stats["navigation"]["semantic_actions"]["overview"], 2)
        self.assertEqual(stats["navigation"]["semantic_sources"]["ts-nav"], 6)
        self.assertEqual(stats["navigation"]["semantic_sources"]["inspect"], 1)
        self.assertEqual(stats["navigation"]["semantic_ambiguous"], 1)
        self.assertTrue(any(row["action"] == "overview" and row["calls"] == 2 for row in stats["navigation"]["semantic_action_rows"]))
        self.assertTrue(any(row["from"].startswith("ts-nav:") for row in stats["command_chains"]))


    def test_compatibility_aliases_avoid_common_agent_cli_failures(self) -> None:
        read = self.data("read", "packages/a/src/index.ts", "--lines", "1:3")
        self.assertEqual(read["items"][0]["start"], 1)
        self.assertEqual(read["items"][0]["end"], 3)

        search = self.data(
            "search", "OldName", "--path", "packages/a", "packages/b",
            "--max-results", "180", "--samples-per-file", "20",
        )
        self.assertGreaterEqual(search["matching_files"], 2)
        self.assertGreaterEqual(search["total_matching_lines"], 3)

        missing = self.aq("search", "OldName", "--path", "does/not/exist", expect=2)
        self.assertIn("search path does not exist", missing.stderr)
        self.assertNotIn("rg exited", missing.stderr)
        self.assertNotIn("usage:", missing.stderr)

        trailing_scope = self.data("search", "OldName", "packages/a")
        self.assertTrue(all(item["path"].startswith("packages/a/") for item in trailing_scope["files"]))
        self.assertNotIn("packages/b/src/index.ts", {item["path"] for item in trailing_scope["files"]})

        unexpected = self.aq("search", "OldName", "not-a-repository-path", expect=2)
        self.assertIn("search accepts one QUERY", unexpected.stderr)
        self.assertIn("agentq search QUERY --path PATH", unexpected.stderr)
        self.assertLess(len(unexpected.stderr), 600)

        canonical_files = self.data("files", "index", "--limit", "2")
        alias_files = self.data("files", "index", "--max-results", "2")
        self.assertEqual(alias_files["files"], canonical_files["files"])

        events = [
            json.loads(line) for line in (self.telemetry / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        alias_event = next(event for event in reversed(events) if event.get("command") == "files")
        self.assertEqual(alias_event["compatibility_alias"], "files-max-results")
        recovery_event = next(event for event in events if event.get("recovery_hint") == "search-path-form")
        self.assertEqual(recovery_event["error_category"], "invalid-arguments")
        self.assertEqual(recovery_event["error_signature"], "unexpected-positional:search")

    def test_missing_read_paths_are_normalized_ranked_and_bounded(self) -> None:
        relative = self.aq("read", "packages/a/src/indx.ts", expect=2)
        relative_error = json.loads(relative.stderr)["error"]
        self.assertIn("file not found: packages/a/src/indx.ts", relative_error)
        self.assertIn("packages/a/src/index.ts", relative_error)
        self.assertNotIn("OldName", relative_error)
        suggestions = relative_error.partition("did you mean: ")[2].split(", ")
        self.assertLessEqual(len(suggestions), 3)

        absolute_path = self.repo / "packages/a/src/indx.ts"
        absolute = self.aq("read", str(absolute_path), expect=2)
        absolute_error = json.loads(absolute.stderr)["error"]
        self.assertIn("file not found: packages/a/src/indx.ts", absolute_error)
        self.assertNotIn(str(self.repo), absolute_error)

        tracked = self.repo / "packages/a/src/index.ts"
        tracked.unlink()
        deleted = self.aq("read", "packages/a/src/index.ts", expect=2)
        self.assertIn("tracked but deleted", json.loads(deleted.stderr)["error"])

    def test_search_totals_are_truthful_and_sampling_is_explicit(self) -> None:
        path = self.repo / "packages/a/src/many.ts"
        path.write_text("".join(f"export const sample{i} = 'Needle'\n" for i in range(12)), encoding="utf-8")
        data = self.data(
            "search", "Needle", "--path", "packages/a/src/many.ts",
            "--samples-per-file", "3", "--max-results", "180",
        )
        self.assertEqual(data["total_matching_lines"], 12)
        self.assertEqual(data["matching_files"], 1)
        self.assertEqual(data["shown"], 3)
        self.assertEqual(data["coverage"]["status"], "sampled")
        self.assertEqual(data["coverage"]["reason"], ["result_limit"])
        self.assertEqual(data["match_file_summary"][0]["matching_lines"], 12)

    def test_compact_search_json_is_canonical_and_substantially_smaller(self) -> None:
        path = self.repo / "packages/a/src/compact.ts"
        path.write_text(
            "".join(f"export const compact{index} = 'COMPACT_HIT'\n" for index in range(12)),
            encoding="utf-8",
        )
        common = (
            "search", "COMPACT_HIT", "--path", "packages/a/src/compact.ts",
            "--context", "1", "--max-results", "12", "--samples-per-file", "12", "--repeat",
        )
        legacy_result = self.aq(*common)
        compact_result = self.aq(*common, "--format", "compact-json")
        legacy = json.loads(legacy_result.stdout)
        compact = json.loads(compact_result.stdout)

        self.assertTrue({"hits", "files", "context_lines", "match_file_summary"} <= set(legacy))
        self.assertEqual(set(compact), {"summary", "files", "continuation"})
        self.assertEqual(compact["summary"]["matches"], {"shown": 12, "total": 12})
        self.assertEqual(compact["summary"]["coverage"]["status"], "complete")
        self.assertIsNone(compact["continuation"])
        self.assertNotIn("hits", compact["files"][0])
        self.assertNotIn("snippets", compact["files"][0])
        source_line = "export const compact0 = 'COMPACT_HIT'"
        self.assertGreaterEqual(legacy_result.stdout.count(source_line), 2)
        self.assertEqual(compact_result.stdout.count(source_line), 1)
        self.assertLessEqual(len(compact_result.stdout.strip()), len(legacy_result.stdout.strip()) * 0.60)

        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq_lib.output_attribution import attribute_output
        legacy_attribution = attribute_output("search", legacy, legacy_result.stdout.strip(), output_format="json")
        compact_attribution = attribute_output(
            "search", compact, compact_result.stdout.strip(), output_format="compact-json",
        )
        self.assertGreater(legacy_attribution["duplicate_evidence_chars"], 0)
        self.assertLessEqual(
            compact_attribution["duplicate_evidence_chars"],
            legacy_attribution["duplicate_evidence_chars"] * 0.20,
        )
        events = [
            json.loads(line)
            for line in (self.telemetry / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        compact_event = next(event for event in reversed(events) if event.get("output_format") == "compact-json")
        self.assertEqual(compact_event["output_view"], "snippets")
        self.assertTrue(compact_event["output_attributed"])
        self.assertTrue(compact_event["source_measured"])
        self.assertGreater(compact_event["metrics"]["candidate_chars"], 0)
        self.assertEqual(sum(compact_event["output_attribution"].values()), compact_event["visible_chars"])
        format_usage = self.data("stats", "--since", "all")["search_format_usage"]
        self.assertEqual(format_usage["compact_json_calls"], 1)
        self.assertEqual(format_usage["legacy_json_calls"], 1)
        self.assertEqual(format_usage["compact_structured_percent"], 50.0)

    def test_compact_search_continuation_is_exact_and_budget_selects_complete_records(self) -> None:
        path = self.repo / "packages/a/src/continuation.ts"
        path.write_text(
            "".join(f"export const continuation{index} = 'CONTINUE_HIT'\n" for index in range(12)),
            encoding="utf-8",
        )
        sampled_result = self.aq(
            "search", "CONTINUE_HIT", "--path", "packages/a/src/continuation.ts",
            "--samples-per-file", "3", "--format", "compact-json", "--repeat",
        )
        sampled = json.loads(sampled_result.stdout)
        self.assertEqual(sampled["summary"]["coverage"]["status"], "sampled")
        self.assertEqual(sampled["continuation"]["omitted"]["matches"], 9)
        continuation_argv = shlex.split(sampled["continuation"]["command"])
        continuation_argv[0] = str(AGENTQ)
        continued = subprocess.run(
            continuation_argv, cwd=self.repo, env=self.env, text=True, capture_output=True,
        )
        self.assertEqual(continued.returncode, 0, msg=continued.stderr)
        self.assertEqual(json.loads(continued.stdout)["summary"]["coverage"]["status"], "complete")

        budgeted_result = subprocess.run(
            [
                str(AGENTQ), "search", "--repo", str(self.repo), "--format", "compact-json",
                "--budget", "900", "CONTINUE_HIT", "--path", "packages/a/src/continuation.ts",
                "--max-results", "12", "--samples-per-file", "12", "--repeat",
            ],
            cwd=self.repo, env=self.env, text=True, capture_output=True,
        )
        self.assertEqual(budgeted_result.returncode, 0, msg=budgeted_result.stderr)
        self.assertLessEqual(len(budgeted_result.stdout.strip()), 900)
        budgeted = json.loads(budgeted_result.stdout)
        self.assertIn("render-budget", budgeted["continuation"]["reason"])
        self.assertTrue(all(item.get("text") for item in budgeted["files"][0]["evidence"]))

        invalid = subprocess.run(
            [str(AGENTQ), "search", "--repo", str(self.repo), "--format", "compact-json"],
            cwd=self.repo, env=self.env, text=True, capture_output=True,
        )
        self.assertEqual(invalid.returncode, 2)
        self.assertEqual(json.loads(invalid.stderr)["type"], "AgentQError")
        events = [
            json.loads(line)
            for line in (self.telemetry / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(events[-1]["output_format"], "compact-json")

    def test_search_auto_view_and_text_header_are_concise(self) -> None:
        exact = self.data("search", "OldName", "--format", "compact-json", "--repeat")
        self.assertEqual(exact["summary"]["intent"], "exact-symbol")
        self.assertEqual(exact["summary"]["view"], "matches")
        kinds = {
            evidence["kind"]
            for item in exact["files"]
            for evidence in item["evidence"]
        }
        self.assertTrue({"definition", "import", "reference"} <= kinds)

        broad_path = self.repo / "packages/a/src/broad.ts"
        broad_path.write_text(
            "".join(f"export const broad{index} = 'BROAD_AUTO'\n" for index in range(45)),
            encoding="utf-8",
        )
        broad = self.data(
            "search", "BROAD_AUTO", "--path", "packages/a/src/broad.ts",
            "--max-results", "45", "--samples-per-file", "45",
            "--format", "compact-json", "--repeat",
        )
        self.assertEqual(broad["summary"]["intent"], "broad-summary")
        self.assertEqual(broad["summary"]["view"], "summary")
        self.assertEqual(len(broad["files"][0]["evidence"]), 2)
        self.assertEqual(broad["summary"]["coverage"]["status"], "sampled")
        self.assertTrue(broad["continuation"]["command"].startswith("agentq continue "))
        self.assertTrue(broad["continuation"]["cursor"])

        rendered = subprocess.run(
            [str(AGENTQ), "search", "--repo", str(self.repo), "OldName", "--repeat"],
            cwd=self.repo, env=self.env, text=True, capture_output=True,
        )
        self.assertEqual(rendered.returncode, 0, msg=rendered.stderr)
        first_block = rendered.stdout.split("\n\n", 1)[0]
        self.assertEqual(len(first_block.splitlines()), 1)
        self.assertIn("[matches; complete]", first_block)
        self.assertNotIn("continue:", rendered.stdout)
        self.assertNotIn(" · ", rendered.stdout)

    def test_search_crops_around_match_and_classifies_config_generated(self) -> None:
        long = self.repo / "packages/a/src/long.ts"
        long.write_text("x" * 320 + "CENTER_NEEDLE" + "y" * 320 + "\n", encoding="utf-8")
        cropped = self.data("search", "CENTER_NEEDLE", "--path", "packages/a/src/long.ts", "--max-chars", "80")
        self.assertIn("CENTER_NEEDLE", cropped["hits"][0]["text"])

        config = self.repo / "vitest.config.ts"
        generated = self.repo / "packages/a/src/database.generated.ts"
        config.write_text("export const marker = 'ROLE_MARK'\n", encoding="utf-8")
        generated.write_text("export const marker = 'ROLE_MARK'\n", encoding="utf-8")
        roles = self.data("search", "ROLE_MARK", "--samples-per-file", "10")
        by_path = {item["path"]: item["role"] for item in roles["match_file_summary"]}
        self.assertEqual(by_path["vitest.config.ts"], "config")
        self.assertEqual(by_path["packages/a/src/database.generated.ts"], "generated")

    def test_repeated_unchanged_read_is_suppressed_inside_task(self) -> None:
        self.data("task", "begin")
        first = self.data("read", "packages/a/src/index.ts:1-3")
        self.assertEqual(len(first["items"][0]["lines"]), 3)
        second = self.data("read", "packages/a/src/index.ts:1-3")
        self.assertTrue(second["items"][0]["suppressed"])
        self.assertEqual(second["items"][0]["lines"], [])
        forced = self.data("read", "packages/a/src/index.ts:1-3", "--repeat")
        self.assertEqual(len(forced["items"][0]["lines"]), 3)

        partially_covered = self.data("read", "packages/a/src/index.ts:2-4")
        self.assertFalse(partially_covered["items"][0].get("suppressed", False))
        self.assertEqual([line["line"] for line in partially_covered["items"][0]["lines"]], [4])

    def test_partial_read_overlap_subtracts_unions_and_stays_task_local(self) -> None:
        path = self.repo / "packages/a/src/overlap.py"
        path.write_text("".join(f"line {index}\n" for index in range(1, 13)), encoding="utf-8")
        self.data("task", "begin")

        self.data("read", "packages/a/src/overlap.py:3-4")
        self.data("read", "packages/a/src/overlap.py:7-8")
        partial = self.data("read", "packages/a/src/overlap.py:1-10")
        self.assertEqual(
            [(item["start"], item["end"]) for item in partial["items"]],
            [(1, 2), (5, 6), (9, 10)],
        )
        self.assertEqual(partial["read_overlap"]["overlap_lines"], 4)
        self.assertEqual(
            [line["line"] for item in partial["items"] for line in item["lines"]],
            [1, 2, 5, 6, 9, 10],
        )

        covered_by_union = self.data("read", "packages/a/src/overlap.py:1-10")
        self.assertTrue(covered_by_union["items"][0]["suppressed"])
        forced = self.data("read", "packages/a/src/overlap.py:1-10", "--repeat")
        self.assertEqual(len(forced["items"][0]["lines"]), 10)

        self.data("task", "next")
        new_task = self.data("read", "packages/a/src/overlap.py:1-10")
        self.assertEqual(len(new_task["items"][0]["lines"]), 10)
        self.assertNotIn("read_overlap", new_task)

    def test_exact_operation_cache_suppresses_and_invalidates_search_outline_and_inspect(self) -> None:
        self.data("task", "begin")
        operations = [
            ("search", ("OldName",)),
            ("outline", ("packages/a/src/index.ts",)),
            ("inspect", ("packages/a/src/index.ts",)),
        ]
        for command, arguments in operations:
            first = self.data(command, *arguments)
            self.assertFalse(first.get("repeat_suppressed", False))
            repeated = self.data(command, *arguments)
            self.assertTrue(repeated["repeat_suppressed"])
            forced = self.data(command, *arguments, "--repeat")
            self.assertFalse(forced.get("repeat_suppressed", False))

        self.change_a("\nexport const cacheInvalidated = true\n")
        refreshed = self.data("search", "OldName")
        self.assertFalse(refreshed.get("repeat_suppressed", False))

    def test_context_cache_is_bounded_private_and_skips_repeated_diff_rendering(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq_lib import context_cache as cache_module
            from agentq_lib import gitops as gitops_module
            from agentq_lib import state as state_module

        with mock.patch.dict(os.environ, {**self.env, "AGENTQ_SESSION_ID": "cache-bounds"}):
            secret = "PRIVATE_CACHE_ARGUMENT_91fdb"
            operation_key = cache_module.operation_cache_key(self.repo, "search", {"query": secret})
            cache_module.remember_operation(self.repo, "search", operation_key)
            for index in range(160):
                cache_module.remember_operation(
                    self.repo, "search", __import__("hashlib").sha256(f"key-{index}".encode()).hexdigest(),
                )
            db_path = state_module.database_path()
            self.assertTrue(db_path.is_file())
            self.assertNotIn(secret.encode("utf-8"), db_path.read_bytes())
            reader = sqlite3.connect(db_path)
            try:
                stored = reader.execute(
                    "SELECT COUNT(*) FROM context_entries WHERE repo_id = ?",
                    (cache_module.repo_id(self.repo),),
                ).fetchone()[0]
            finally:
                reader.close()
            self.assertLessEqual(stored, 128)

            binary = self.repo / "packages/a/src/asset.bin"
            binary.write_bytes(b"\x00old")
            self.git("add", str(binary.relative_to(self.repo)))
            self.git("commit", "-qm", "add binary fixture")
            binary.write_bytes(b"\x00new")
            with mock.patch.object(gitops_module, "_stream_diff", side_effect=AssertionError("diff body streamed")):
                summary = gitops_module.diff_data(self.repo, budget=100000)
            self.assertEqual(summary["total_files"], 1)

            self.change_a("\nexport const cachedDiff = true\n")
            first = gitops_module.diff_data(self.repo, patch=True, budget=100000)
            self.assertTrue(first["patch"])
            with mock.patch.object(gitops_module, "_stream_bounded_patch", side_effect=AssertionError("diff rendered again")):
                repeated = gitops_module.diff_data(self.repo, patch=True, budget=100000)
            self.assertTrue(repeated["repeat_suppressed"])

            source = self.repo / "packages/a/src/index.ts"
            source.write_text(source.read_text(encoding="utf-8").replace("cachedDiff", "editedDiff"), encoding="utf-8")
            changed = gitops_module.diff_data(self.repo, patch=True, budget=100000)
            self.assertFalse(changed.get("repeat_suppressed", False))
            self.assertIn("editedDiff", changed["patch"])

    def test_repeat_suppression_is_independent_of_telemetry(self) -> None:
        env = {**self.env, "AGENTQ_TELEMETRY": "0"}
        self.data("task", "begin", extra_env=env)
        self.change_a("\nexport const decoupled = true\n")
        first = self.data("git-diff", "--task", "--patch", extra_env=env)
        self.assertNotIn("repeat_suppressed", first)
        second = self.data("git-diff", "--task", "--patch", extra_env=env)
        self.assertTrue(second["repeat_suppressed"])
        self.assertEqual(second["repeat_scope"], "task")

    def test_repeat_suppression_requires_explicit_session_identity(self) -> None:
        no_identity = {"AGENTQ_SESSION_ID": "", "CODEX_THREAD_ID": ""}
        plain = self.data("read", "packages/a/src/index.ts:1-3", extra_env=no_identity)
        self.assertNotIn("read_overlap", plain)
        plain_again = self.data("read", "packages/a/src/index.ts:1-3", extra_env=no_identity)
        self.assertFalse(plain_again["items"][0].get("suppressed", False))

        secret_session = "SESSION_SECRET_9f2c"
        session_env = {**self.env, "AGENTQ_SESSION_ID": secret_session}
        first = self.data("read", "packages/a/src/index.ts:1-3", extra_env=session_env)
        self.assertNotIn("read_overlap", first)
        second = self.data("read", "packages/a/src/index.ts:1-3", extra_env=session_env)
        self.assertTrue(second["items"][0].get("suppressed", False))
        self.assertEqual(second["read_overlap"]["scope"], "session")

        other = self.data("read", "packages/a/src/index.ts:1-3", extra_env={**self.env, "AGENTQ_SESSION_ID": "other-session"})
        self.assertFalse(other["items"][0].get("suppressed", False))

        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq_lib import state as state_module

        with mock.patch.dict(os.environ, self.env, clear=False):
            raw = state_module.database_path().read_bytes()
        self.assertNotIn(secret_session.encode("utf-8"), raw)

    def test_emit_cached_skips_workspace_identity_when_suppression_inactive(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            import agentq
            from agentq_lib import context_cache as cache_module

        from types import SimpleNamespace

        args = SimpleNamespace(budget=100000, format="json", repeat=False, command="search")
        producer = mock.Mock(return_value={"command": "search"})

        with mock.patch.dict(os.environ, {**self.env, "AGENTQ_CONTEXT_CACHE": "0", "AGENTQ_SESSION_ID": "probe"}):
            with mock.patch("builtins.print"):
                with mock.patch.object(cache_module, "run_cmd", side_effect=AssertionError("git ran")) as run_mock:
                    agentq.emit_cached(args, self.repo, "search", {"query": "x"}, producer, render_noop)
                self.assertEqual(run_mock.call_count, 0)
                self.assertEqual(producer.call_count, 1)

        with mock.patch.dict(os.environ, {**self.env, "AGENTQ_SESSION_ID": "probe"}):
            with mock.patch("builtins.print"):
                with mock.patch.object(
                    cache_module, "run_cmd",
                    return_value=SimpleNamespace(returncode=0, stdout="head\n"),
                ) as run_mock:
                    agentq.emit_cached(args, self.repo, "search", {"query": "x"}, producer, render_noop)
                self.assertGreaterEqual(run_mock.call_count, 1)

    def test_search_coverage_policies_control_counting_work(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq_lib import search as search_module

        broad = self.repo / "packages/a/src/broad_cov.ts"
        broad.write_text("".join(f"export const covItem{i} = {i}\n" for i in range(120)), encoding="utf-8")

        with mock.patch.object(
            search_module, "_matching_line_counts", side_effect=AssertionError("count pass ran"),
        ) as count_mock:
            fast = search_module.search_data(
                self.repo, "covItem", ["packages/a/src"], limit=5, scan_cap=30, coverage_policy="fast",
            )
        self.assertEqual(count_mock.call_count, 0)
        self.assertEqual(fast["coverage_policy"], "fast")
        self.assertEqual(fast["count_quality"], "lower-bound")
        self.assertEqual(fast["total_matching_lines"], 30)
        self.assertFalse(fast["scan_complete"])
        self.assertIn("scan_cap", fast["coverage"]["reason"])

        with mock.patch.object(
            search_module, "_matching_line_counts", wraps=search_module._matching_line_counts,
        ) as count_mock:
            exact = search_module.search_data(
                self.repo, "covItem", ["packages/a/src"], limit=5, coverage_policy="exact",
            )
        self.assertEqual(count_mock.call_count, 1)
        self.assertEqual(exact["count_quality"], "exact")
        self.assertEqual(exact["total_matching_lines"], 120)

        auto = self.data("search", "covItem", "--path", "packages/a/src")
        self.assertEqual(auto["coverage_policy"], "auto")
        self.assertEqual(auto["count_quality"], "exact")
        self.assertEqual(auto["total_matching_lines"], 120)

    def test_search_lower_bound_counts_are_labeled_in_text(self) -> None:
        broad = self.repo / "packages/a/src/broad_lb.ts"
        broad.write_text("".join(f"export const lbItem{i} = {i}\n" for i in range(120)), encoding="utf-8")
        rendered = subprocess.run(
            [
                str(AGENTQ), "search", "--repo", str(self.repo), "--format", "text", "--budget", "100000",
                "lbItem", "--path", "packages/a/src", "--coverage", "fast",
                "--scan-cap", "30", "--max-results", "5",
            ],
            text=True, capture_output=True, env=self.env, cwd=self.repo,
        )
        self.assertEqual(rendered.returncode, 0, msg=rendered.stderr)
        self.assertIn("(lower bound; scan cap reached)", rendered.stdout)
        self.assertIn("continue: agentq continue", rendered.stdout)

    def test_continuation_cursors_are_short_scoped_and_replayable(self) -> None:
        session = {"AGENTQ_SESSION_ID": "cursor-test"}
        rendered = subprocess.run(
            [
                str(AGENTQ), "search", "--repo", str(self.repo), "--format", "text", "--budget", "2000",
                "OldName", "--path", "packages", "--max-results", "2", "--samples-per-file", "1",
            ],
            text=True, capture_output=True, env={**self.env, **session}, cwd=self.repo,
        )
        self.assertEqual(rendered.returncode, 0, msg=rendered.stderr)
        match = re.search(r"agentq continue ([A-Za-z0-9_-]+)", rendered.stdout)
        self.assertIsNotNone(match, msg=rendered.stdout)
        cursor = match.group(1)

        replay = self.aq("continue", cursor, extra_env=session)
        self.assertIn("OldName", replay.stdout)

        other = self.aq("continue", cursor, expect=2, extra_env={**session, "AGENTQ_SESSION_ID": "other"})
        self.assertIn("unknown or expired continuation cursor", other.stderr)

        self.change_a("\nexport const workspaceMoved = true\n")
        moved = self.aq("continue", cursor, expect=2, extra_env=session)
        self.assertIn("workspace changed", moved.stderr)

        compact = self.data("search", "OldName", "--path", "packages", "--max-results", "2",
                            "--samples-per-file", "1", "--budget", "2000", "--format", "compact-json",
                            extra_env=session)
        block = compact["continuation"]
        self.assertTrue(block["cursor"])
        self.assertEqual(block["command"], f"agentq continue {block['cursor']}")
        self.assertIn("T", block["expires_at"])

    def test_continuation_cursors_expire(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq_lib import context_cache as cache_module
            from agentq_lib import state as state_module

        env = {**self.env, "AGENTQ_SESSION_ID": "cursor-ttl", "AGENTQ_STATE_DB": str(Path(self.temp.name) / "ttl.db")}
        with mock.patch.dict(os.environ, env, clear=False):
            stored = cache_module.remember_continuation(self.repo, "agentq files zz-none")
            self.assertIsNotNone(stored)
            self.assertTrue(stored["expires_at"])
            record = cache_module.continuation_record(self.repo, stored["cursor"])
            self.assertIsNotNone(record)
            self.assertEqual(record["command"], "agentq files zz-none")

            reader = sqlite3.connect(state_module.database_path())
            try:
                reader.execute("UPDATE continuations SET expires_at = 1")
                reader.commit()
            finally:
                reader.close()
            self.assertIsNone(cache_module.continuation_record(self.repo, stored["cursor"]))

    def test_inspect_python_and_tsnav_continuations_carry_cursors(self) -> None:
        session = {"AGENTQ_SESSION_ID": "cursor-nav"}
        (self.repo / "packages/a/src/cursor_nav.py").write_text(
            "def cursorNav(value):\n"
            "    return value\n"
            "\n"
            "one = cursorNav(1)\n"
            "two = cursorNav(one)\n"
            "three = cursorNav(two)\n",
            encoding="utf-8",
        )
        inspected = self.data("inspect", "cursorNav", "--path", "packages/a/src", "--lang", "python",
                              "--limit", "1", "--repeat", extra_env=session)
        block = inspected["python"]["continuation"]
        self.assertEqual(block["command"], f"agentq continue {block['cursor']}")
        self.assertTrue(block["cursor"])

    def test_legacy_json_state_migrates_into_sqlite_store(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq_lib import context_cache as cache_module
            from agentq_lib import state as state_module
            from agentq_lib import tasking as tasking_module

        state_db = Path(self.temp.name) / "legacy-state" / "state.db"
        state_db.parent.mkdir(parents=True, exist_ok=True)
        legacy_repo_id = cache_module.repo_id(self.repo)
        legacy_task = self.telemetry / "tasks" / f"{legacy_repo_id}.json"
        legacy_task.parent.mkdir(parents=True, exist_ok=True)
        legacy_task.write_text(json.dumps({
            "task_id": "legacy123", "started_at": 1.0, "baseline": {},
        }), encoding="utf-8")

        env = {**self.env, "AGENTQ_STATE_DB": str(state_db)}
        with mock.patch.dict(os.environ, env, clear=False):
            from agentq_lib.runtime import context_cache_dir

            legacy_context = context_cache_dir() / f"{legacy_repo_id}.json"
            legacy_context.parent.mkdir(parents=True, exist_ok=True)
            legacy_context.write_text(json.dumps({
                "schema": 1,
                "entries": [{
                    "context": "task:legacy123", "command": "search",
                    "key": "a" * 64, "time": time.time(),
                }],
            }), encoding="utf-8")
            malformed = legacy_context.with_name(f"{'b' * 16}.json")
            malformed.write_text("{broken legacy state", encoding="utf-8")
            state_module._legacy_imported = False
            restored = tasking_module._read_state(self.repo)
            self.assertIsNotNone(restored)
            self.assertEqual(restored["task_id"], "legacy123")
            hits, scope = cache_module._lookup(self.repo, "search", ["a" * 64])
        self.assertEqual(hits, {"a" * 64})
        self.assertEqual(scope, "task")
        self.assertFalse(legacy_context.exists())
        self.assertFalse(legacy_task.exists())
        self.assertTrue(malformed.exists())

    def test_concurrent_processes_preserve_context_state(self) -> None:
        env = {**self.env, "AGENTQ_SESSION_ID": "concurrent"}
        processes = [
            subprocess.Popen(
                [
                    str(AGENTQ), "search", "OldName", "--repo", str(self.repo),
                    "--format", "json", "--budget", str(4000 + index), "--path", "packages/a/src",
                ],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=self.repo,
            )
            for index in range(8)
        ]
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, msg=stderr or stdout)

        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq_lib import context_cache as cache_module
            from agentq_lib import state as state_module

        with mock.patch.dict(os.environ, env, clear=False):
            reader = sqlite3.connect(state_module.database_path())
            try:
                stored = reader.execute(
                    "SELECT COUNT(*) FROM context_entries WHERE repo_id = ? AND context_id = ? AND command = ?",
                    (cache_module.repo_id(self.repo), f"session:{cache_module.session_id()}", "search"),
                ).fetchone()[0]
            finally:
                reader.close()
        self.assertEqual(stored, 8)

    def test_run_profiles_control_environment_and_retention(self) -> None:
        failing = "import os, sys; print('CI=%s' % os.environ.get('CI'), file=sys.stderr); raise SystemExit(3)"
        transparent = self.data("run", "--profile", "transparent", "--", "python3", "-c", failing)
        self.assertEqual(transparent["profile"], "transparent")
        self.assertIn("CI=None", transparent["tail"][-1])
        self.assertEqual(transparent["log_retention"], "retained")
        self.assertTrue(Path(transparent["log"]).is_file())
        self.assertEqual(Path(transparent["log"]).stat().st_mode & 0o777, 0o600)

        passing = self.data("run", "--profile", "ci", "--", "python3", "-c", "print('CI=%s' % __import__('os').environ.get('CI'))")
        self.assertEqual(passing["profile"], "ci")
        self.assertIn("CI=1", passing["tail"][-1])
        self.assertEqual(passing["log_retention"], "deleted")
        self.assertIsNone(passing["log"])

        kept = self.data("run", "--profile", "ci", "--keep-log", "--", "python3", "-c", "print('kept')")
        self.assertEqual(kept["log_retention"], "retained")
        self.assertTrue(Path(kept["log"]).is_file())

        offline = self.data("run", "--offline", "--", "python3", "-c", "print('offline')")
        self.assertEqual(offline["profile"], "offline")
        self.assertIn("not a network sandbox", offline["network_isolation"])

        conflict = self.aq("run", "--profile", "transparent", "--offline", "--", "python3", "-c", "print('x')", expect=2)
        self.assertIn("--profile offline", conflict.stderr)

    def test_run_log_cleanup_enforces_ttl_and_quota(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq_lib import runops as runops_module

        log_dir = Path(self.temp.name) / "logs"
        log_dir.mkdir()
        now = time.time()
        expired = log_dir / "expired.log"
        expired.write_text("x" * 10, encoding="utf-8")
        os.utime(expired, (now - 10 * 24 * 3600,) * 2)
        oldest = log_dir / "oldest.log"
        oldest.write_text("y" * 1000, encoding="utf-8")
        os.utime(oldest, (now - 300,) * 2)
        newer = log_dir / "newer.log"
        newer.write_text("z" * 1000, encoding="utf-8")
        os.utime(newer, (now - 290,) * 2)
        fresh = log_dir / "fresh.log"
        fresh.write_text("w" * 10, encoding="utf-8")
        os.utime(fresh, (now - 280,) * 2)

        with mock.patch.object(runops_module, "_LOG_QUOTA_BYTES", 1500):
            runops_module._cleanup_logs(log_dir)

        self.assertFalse(expired.exists())
        self.assertFalse(oldest.exists())
        self.assertTrue(newer.exists())
        self.assertTrue(fresh.exists())

    def test_json_inspect_caches_only_source_lines_visible_inside_wrapper(self) -> None:
        path = self.repo / "packages/a/src/budgeted_inspect.py"
        path.write_text(
            "".join(f"line_{index:02d} = {'x' * 32!r}\n" for index in range(1, 21)),
            encoding="utf-8",
        )
        self.data("task", "begin")

        first = self.data(
            "inspect", "packages/a/src/budgeted_inspect.py", "--lines", "1:20",
            "--budget", "1200",
        )
        first_lines = [
            line["line"]
            for item in first["source"]["items"]
            for line in item["lines"]
        ]
        self.assertTrue(first_lines)
        self.assertNotIn("_agentq", first)

        resumed = self.data(
            "inspect", "packages/a/src/budgeted_inspect.py", "--lines", "1:20",
            "--budget", "100000",
        )
        resumed_lines = [
            line["line"]
            for item in resumed["source"]["items"]
            for line in item["lines"]
        ]
        self.assertEqual(sorted(first_lines + resumed_lines), list(range(1, 21)))

    def test_task_changes_excludes_unchanged_preexisting_dirty_files(self) -> None:
        self.change_a("\nexport const beforeTask = true\n")
        self.data("task", "begin")
        initial = self.data("task", "changes")
        self.assertEqual(initial["files"], [])
        self.assertIn("packages/a/src/index.ts", initial["excluded_preexisting_unchanged"])
        task_diff = self.data("git-diff", "--task")
        self.assertEqual(task_diff["total_files"], 0)
        task_verify = self.data("verify-task", "--dry-run")
        self.assertEqual(task_verify["changed_files"], [])

        self.change_a("\nexport const duringTask = true\n")
        current = self.data("task", "changes")
        self.assertIn("packages/a/src/index.ts", current["files"])
        self.assertIn("packages/a/src/index.ts", current["ambiguous_preexisting"])

    def test_telemetry_records_private_fingerprints_transitions_and_coverage(self) -> None:
        secret = "QUERY_THAT_MUST_NOT_APPEAR_74ca"
        self.data("task", "begin")
        self.data("search", secret)
        self.data("read", "packages/a/src/index.ts:1-2")
        self.data("task", "accept")
        stats = self.data("stats", "--since", "all", "--detailed")
        self.assertGreaterEqual(stats["invocation_chars"], 1)
        self.assertTrue(any(item["transition"] == "search → read" for item in stats["transitions"]))
        self.assertIsNotNone(stats["tasks"]["calls_distribution"]["p50"])
        self.assertIn("instrumented_call_percent", stats["measurement"])
        raw = (self.telemetry / "events.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(secret, raw)
        events = [json.loads(line) for line in raw.splitlines() if line.strip()]
        search_event = next(event for event in events if event.get("command") == "search")
        self.assertIn("query_fingerprint", search_event["metrics"])

    def test_budgeted_json_keeps_useful_data(self) -> None:
        path = self.repo / "packages/a/src/many-budget.ts"
        path.write_text("".join(f"export const n{i} = 'BUDGET_HIT'\n" for i in range(80)), encoding="utf-8")
        argv = [
            str(AGENTQ), "search", "--repo", str(self.repo), "--format", "json",
            "--budget", "900", "BUDGET_HIT", "--path", "packages/a/src/many-budget.ts",
            "--max-results", "80", "--samples-per-file", "80",
        ]
        result = subprocess.run(argv, text=True, capture_output=True, env=self.env)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["query"], "BUDGET_HIT")
        self.assertTrue(data["_agentq"]["truncated"])
        self.assertIn("total_matching_lines", data)

    def test_budgeted_text_reports_explicit_truncation_and_prebudget_size(self) -> None:
        path = self.repo / "packages/a/src/many_python_definitions.py"
        path.write_text(
            "\n".join(
                f"def calculate_total(value: int, marker={index}) -> int:\n    return value + marker"
                for index in range(30)
            ) + "\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                str(AGENTQ), "inspect", "--repo", str(self.repo), "--format", "text",
                "--budget", "320", "calculate_total", "--path", str(path), "--limit", "80", "--repeat",
            ],
            cwd=self.repo, env=self.env, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertLessEqual(len(result.stdout.strip()), 320)
        self.assertIn("complete Python records omitted", result.stdout)

        events = [
            json.loads(line)
            for line in (self.telemetry / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if json.loads(line).get("command") == "inspect"
        ]
        event = events[-1]
        self.assertTrue(event["render_budget_truncated"])
        self.assertGreater(event["prebudget_chars"], event["visible_chars"])

        stats = self.data("stats", "--since", "all")
        inspect = next(row for row in stats["commands"] if row["command"] == "inspect")
        self.assertGreater(inspect["budget_removed_chars"], 0)

    def test_structural_budgeting_is_valid_bounded_and_preserves_complete_records(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq_lib.budgeting import budget_text_records, project_json

        records = [
            {"id": index, "path": f"packages/example/{index}.py", "lines": [f"line {index}", "detail"]}
            for index in range(24)
        ]
        payload = {
            "command": "inspect",
            "nested": {"items": records, "secondary": [{"value": index} for index in range(12)]},
            "summary": {"shown": 24, "total": 24},
        }
        saw_nested_omission = False
        for budget in range(256, 12001):
            visible, truncated = project_json(payload, budget)
            self.assertLessEqual(len(visible), budget)
            projected = json.loads(visible)
            if truncated:
                omissions = projected["_agentq"].get("omitted", {})
                saw_nested_omission = saw_nested_omission or any(
                    path.startswith("/nested/items") for path in omissions
                )
            kept = projected.get("nested", {}).get("items", [])
            self.assertTrue(all(record in records for record in kept))
        self.assertTrue(saw_nested_omission)

        text_records = [f"record-{index}:" + "x" * 80 for index in range(20)]
        for budget in range(256, 1201):
            visible, truncated = budget_text_records(
                "header", text_records, budget,
                omission="… {count} complete records omitted by render budget",
            )
            self.assertLessEqual(len(visible), budget)
            if truncated:
                self.assertIn("complete records omitted", visible)
                self.assertTrue(visible.truncated)
                self.assertGreater(visible.prebudget_chars, len(visible))
            for line in visible.splitlines():
                if line.startswith("record-"):
                    self.assertIn(line, text_records)

        pulled: list[int] = []

        def lazy_records():
            for index in range(10_000):
                pulled.append(index)
                yield f"lazy-{index}:" + "x" * 80

        visible, truncated = budget_text_records(
            "header", lazy_records(), 320,
            omission="… {count} records omitted; query contained event = {",
            total_count=10_000,
        )
        self.assertTrue(truncated)
        self.assertLess(len(pulled), 10)
        self.assertIn("query contained event = {", visible)
        self.assertLessEqual(len(visible), 320)

    def test_scale_benchmark_reports_metrics_and_enforces_integrity(self) -> None:
        result = subprocess.run(
            [
                sys.executable, str(SCALE_BENCHMARK), "--sizes", "1000",
                "--result-records", "1000", "--budget", "2000", "--json",
            ],
            cwd=self.repo,
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["event_sizes"], [1000])
        self.assertEqual(report["result_records"], 1000)
        self.assertEqual(report["result_record_sizes"], [1000])
        self.assertEqual(len(report["measurements"]), 6)
        self.assertIn("read-render", {row["workload"] for row in report["measurements"]})
        self.assertTrue(all(
            all(row["integrity"].values()) for row in report["measurements"]
        ))
        self.assertTrue(all(row["wall_seconds"] >= 0 for row in report["measurements"]))
        self.assertTrue(all(row["peak_python_bytes"] > 0 for row in report["measurements"]))
        self.assertIn("default_faster_percent", report["comparisons"][0])
        self.assertIn("default_wall_reduction_percent", report["comparisons"][0])

    def test_multi_anchor_read_and_inspect_merge_source_windows(self) -> None:
        path = self.repo / "packages/a/src/windows.ts"
        path.write_text("".join(f"line {index}\n" for index in range(1, 221)), encoding="utf-8")

        read = self.data("read", "packages/a/src/windows.ts", "--line", "30", "32", "110", "--context", "2")
        self.assertTrue(read["windowed"])
        self.assertEqual(read["anchors"], [30, 32, 110])
        self.assertEqual(read["windows"], 2)
        self.assertEqual((read["items"][0]["start"], read["items"][0]["end"]), (28, 34))
        self.assertEqual((read["items"][1]["start"], read["items"][1]["end"]), (108, 112))
        self.assertEqual(sum(1 for item in read["items"] for line in item["lines"] if line.get("anchor")), 3)

        inspect = self.data(
            "inspect", "packages/a/src/windows.ts",
            "--line", "30", "--line", "110", "150", "220", "--context", "1", "--limit", "80",
        )
        self.assertEqual(inspect["kind"], "source-windows")
        source = inspect["source"]
        self.assertEqual(source["anchors"], [30, 110, 150, 220])
        self.assertEqual(source["windows"], 4)

        ranged = self.data(
            "read", "packages/a/src/windows.ts",
            "--lines", "30:35", "--lines", "34:40", "--lines", "100:102", "--repeat",
        )
        self.assertEqual(ranged["windows"], 2)
        self.assertEqual((ranged["items"][0]["start"], ranged["items"][0]["end"]), (30, 40))

    def test_python_inspect_uses_ast_definitions_and_bounded_lexical_references(self) -> None:
        path = self.repo / "packages/a/src/python_nav.py"
        path.write_text(textwrap.dedent('''\
            def calculate_total(value: int, tax: float = 0.2) -> float:
                return value * (1 + tax)

            class Calculator:
                async def calculate_total(self, value: int) -> float:
                    return calculate_total(value)

            result = calculate_total(10)
        '''), encoding="utf-8")

        inspected = self.data(
            "inspect", "calculate_total", "--path", "packages/a/src/python_nav.py", "--limit", "20",
        )
        self.assertEqual(inspected["kind"], "python")
        python = inspected["python"]
        self.assertEqual(python["engine"], "stdlib-python-ast")
        self.assertEqual(python["candidate_count"], 2)
        self.assertTrue(any(item["signature"].startswith("calculate_total(") for item in python["candidates"]))
        self.assertTrue(any(item["signature"].startswith("async calculate_total(") for item in python["candidates"]))
        self.assertGreaterEqual(python["references"]["total"], 2)
        self.assertIn("not semantic proof", python["evidence"])

        absolute = self.data(
            "inspect", "calculate_total", "--path", str(path), "--limit", "20", "--repeat",
        )
        self.assertEqual(absolute["kind"], "python")
        self.assertEqual(absolute["python"]["candidate_count"], 2)

        rendered = subprocess.run(
            [
                str(AGENTQ), "inspect", "--repo", str(self.repo), "--format", "text",
                "calculate_total", "--path", "packages/a/src/python_nav.py", "--limit", "20", "--repeat",
            ],
            cwd=self.repo, env=self.env, text=True, capture_output=True,
        )
        self.assertEqual(rendered.returncode, 0, msg=rendered.stderr)
        self.assertIn("python overview calculate_total", rendered.stdout)
        self.assertIn("[complete]", rendered.stdout)
        self.assertNotIn("continue:", rendered.stdout)
        self.assertNotIn("not semantic proof", rendered.stdout)

        sampled = subprocess.run(
            [
                str(AGENTQ), "inspect", "--repo", str(self.repo), "--format", "text",
                "calculate_total", "--path", "packages/a/src/python_nav.py", "--limit", "1", "--repeat",
            ],
            cwd=self.repo, env=self.env, text=True, capture_output=True,
        )
        self.assertEqual(sampled.returncode, 0, msg=sampled.stderr)
        self.assertIn("[sampled]", sampled.stdout)
        self.assertEqual(sampled.stdout.count("continue: agentq continue"), 1)
        continuation = shlex.split(sampled.stdout.split("continue: ", 1)[1].strip())
        continuation[0] = str(AGENTQ)
        completed = subprocess.run(continuation, cwd=self.repo, env=self.env, text=True, capture_output=True)
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        self.assertIn("[complete]", completed.stdout)

        outline = self.data("outline", "packages/a/src/python_nav.py", "--match", "calculate_total")
        self.assertEqual(outline["engine"], "stdlib-python-ast")
        self.assertTrue(all("(" in item["signature"] for item in outline["symbols"]))

    def test_phase_four_workflow_replays_bound_calls_output_and_evidence(self) -> None:
        fixture = json.loads(Path(__file__).with_name("workflow-replays-v1.json").read_text(encoding="utf-8"))
        constraints = fixture["workflows"]
        env = {**self.env, "AGENTQ_TELEMETRY": "0"}

        def run_text(*args: str, expect: int = 0, output_format: str = "text", budget: int = 12000) -> str:
            argv = [
                str(AGENTQ), args[0], "--repo", str(self.repo), "--format", output_format,
                "--budget", str(budget), *args[1:],
            ]
            result = subprocess.run(argv, cwd=self.repo, env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, expect, msg=result.stderr or result.stdout)
            return (result.stdout if result.returncode == 0 else result.stderr).strip()

        replays: dict[str, tuple[list[str], list[str]]] = {}

        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq_lib.inspectops import inspect_data, render_inspect
            from agentq_lib.tsnav import render_ts_nav

        definition = {
            "path": "packages/a/src/index.ts", "line": 1, "column": 18,
            "definition": True, "preview": "export interface OldName { value: string }",
        }
        caller = {
            "path": "packages/b/src/index.ts", "line": 2, "column": 23,
            "preview": "export type Wrapped = OldName",
        }
        test_reference = {
            "path": "packages/a/src/index.test.ts", "line": 8, "column": 12,
            "preview": "expect(makeOldName('value')).toEqual({ value: 'value' })",
        }
        implementation = {
            "path": "packages/a/src/index.ts", "line": 2, "column": 17,
            "preview": "export function makeOldName(value: string): OldName",
        }

        def semantic_action(action: str, items: list[dict[str, object]]) -> dict[str, object]:
            return {
                "action": action, "symbol": "OldName", "candidate": 1, "candidate_count": 1,
                "target": "packages/a/src/index.ts", "line": 1, "column": 18,
                "config": "tsconfig.json", "shown": len(items), "total": len(items),
                "truncated": False, "results": items,
            }

        overview = {
            **semantic_action("overview", []),
            "candidates": [{"path": "packages/a/src/index.ts", "line": 1, "column": 18}],
            "declaration_span": {"start_line": 1, "end_line": 1},
            "definition": {"shown": 1, "total": 1, "truncated": False, "results": [definition]},
            "references": {"shown": 2, "total": 2, "truncated": False, "results": [caller, test_reference]},
            "implementations": {"shown": 1, "total": 1, "truncated": False, "results": [implementation]},
        }
        locate = {
            "action": "locate", "symbol": "OldName", "ambiguous": False,
            "candidates": [{
                "path": "packages/a/src/index.ts", "line": 1, "column": 18,
                "kind": "interface", "config": "tsconfig.json", "preview": definition["preview"],
            }],
        }
        legacy_ts = [
            render_ts_nav(locate),
            render_ts_nav(semantic_action("definition", [definition])),
            render_ts_nav(semantic_action("references", [caller, test_reference])),
            render_ts_nav(semantic_action("implementations", [implementation])),
        ]
        with mock.patch("agentq_lib.navigation.ts_nav_data", return_value=overview) as semantic_overview:
            current_ts_data = inspect_data(self.repo, "OldName", ["packages"], limit=80)
        semantic_overview.assert_called_once()
        self.assertEqual(semantic_overview.call_args.args[1], "overview")
        current_ts = [render_inspect(current_ts_data, budget=constraints["exact_typescript_symbol"]["visible_budget"])]
        self.assertIn("[complete]", current_ts[0])
        self.assertIn("packages/b/src/index.ts", current_ts[0])
        self.assertIn("packages/a/src/index.test.ts", current_ts[0])
        sampled_overview = {
            **overview,
            "paths": ["packages"],
            "limit": 1,
            "references": {"shown": 1, "total": 3, "truncated": True, "results": [caller]},
        }
        sampled_ts = render_ts_nav(sampled_overview)
        self.assertIn("[sampled]", sampled_ts)
        self.assertEqual(sampled_ts.count("continue: agentq ts-nav overview"), 1)
        self.assertIn("--path packages --limit 3", sampled_ts)
        replays["exact_typescript_symbol"] = legacy_ts, current_ts

        python_path = self.repo / "packages/a/src/workflow.py"
        python_path.write_text(textwrap.dedent('''\
            def calculate_total(value: int) -> int:
                return value * 2

            def caller() -> int:
                return calculate_total(4)
        '''), encoding="utf-8")
        current_python = [run_text("inspect", "calculate_total", "--path", "packages/a/src/workflow.py", "--repeat")]
        legacy_python = [
            run_text("search", "calculate_total", "--path", "packages/a/src/workflow.py", "--repeat"),
            run_text("outline", "packages/a/src/workflow.py", "--match", "calculate_total", "--repeat"),
            run_text("read", "packages/a/src/workflow.py:1-5", "--repeat"),
        ]
        self.assertIn("[complete]", current_python[0])
        self.assertIn("calculate_total(value: int) -> int", current_python[0])
        self.assertIn("return calculate_total(4)", current_python[0])
        replays["exact_python_symbol"] = legacy_python, current_python

        config = self.repo / "config/workflow.yml"
        config.parent.mkdir(exist_ok=True)
        config.write_text("service:\n  role: ROLE_WORKFLOW\n  enabled: true\n", encoding="utf-8")
        current_config = [run_text("search", "ROLE_WORKFLOW", "--path", "config/workflow.yml", "--context", "1", "--repeat")]
        legacy_config = [*current_config, run_text("read", "config/workflow.yml:1-3", "--repeat")]
        self.assertIn("[snippets; complete]", current_config[0])
        self.assertIn("enabled: true", current_config[0])
        self.assertNotIn("continue:", current_config[0])
        replays["configuration_literal"] = legacy_config, current_config

        broad = self.repo / "packages/a/src/workflow_broad.ts"
        broad.write_text("".join(f"export const broad{index} = 'WORKFLOW_BROAD'\n" for index in range(30)), encoding="utf-8")
        first_broad = run_text(
            "search", "WORKFLOW_BROAD", "--path", "packages/a/src/workflow_broad.ts",
            "--repeat", output_format="compact-json",
        )
        first_data = json.loads(first_broad)
        continuation = shlex.split(first_data["continuation"]["command"])
        continuation[0] = str(AGENTQ)
        continued = subprocess.run(continuation, cwd=self.repo, env=env, text=True, capture_output=True)
        self.assertEqual(continued.returncode, 0, msg=continued.stderr)
        current_broad = [first_broad, continued.stdout.strip()]
        legacy_broad = [*current_broad, run_text("read", "packages/a/src/workflow_broad.ts", "--repeat")]
        self.assertIn("broad0", "".join(current_broad))
        self.assertIn("broad29", "".join(current_broad))
        replays["broad_search"] = legacy_broad, current_broad

        anchor_a = self.repo / "packages/a/src/workflow_anchor_a.ts"
        anchor_b = self.repo / "packages/b/src/workflow_anchor_b.ts"
        anchor_a.write_text("".join(f"a line {index}\n" for index in range(1, 61)), encoding="utf-8")
        anchor_b.write_text("".join(f"b line {index}\n" for index in range(1, 61)), encoding="utf-8")
        legacy_anchors = [
            run_text("read", "packages/a/src/workflow_anchor_a.ts:10-14", "--repeat"),
            run_text("read", "packages/a/src/workflow_anchor_a.ts:40-44", "--repeat"),
            run_text("read", "packages/b/src/workflow_anchor_b.ts:20-24", "--repeat"),
        ]
        current_anchors = [run_text(
            "read", "packages/a/src/workflow_anchor_a.ts:10-14,40-44",
            "packages/b/src/workflow_anchor_b.ts:20-24", "--repeat",
        )]
        self.assertIn("a line 10", current_anchors[0])
        self.assertIn("a line 44", current_anchors[0])
        self.assertIn("b line 24", current_anchors[0])
        replays["known_source_anchors"] = legacy_anchors, current_anchors

        missing = run_text("read", "packages/a/src/indx.ts", expect=2)
        legacy_missing = [run_text("files", "indx"), missing]
        current_missing = [missing]
        self.assertIn("packages/a/src/index.ts", missing)
        self.assertNotIn("OldName", missing)
        replays["missing_path"] = legacy_missing, current_missing

        self.data("task", "begin")
        self.change_a("\nexport const workflowTaskChange = true\n")
        legacy_patch = [
            run_text("git-status"),
            run_text("git-diff", "--hunks", "--repeat"),
            run_text("test-plan", "--task"),
            run_text("verify", "--dry-run", "--skip-lint"),
        ]
        current_patch = [
            run_text("git-diff", "--task", "--hunks", "--repeat"),
            run_text("verify", "--dry-run", "--skip-lint"),
        ]
        self.assertIn("packages/a/src/index.ts", current_patch[0])
        self.assertIn("hunk index", current_patch[0])
        self.assertIn("scope=task", current_patch[1])
        replays["task_patch_and_verification"] = legacy_patch, current_patch

        call_reductions = []
        visible_reductions = []
        for name, (legacy, current) in replays.items():
            constraint = constraints[name]
            baseline = fixture["baseline"][name]
            current_chars = sum(len(value) for value in current)
            self.assertLessEqual(len(current), constraint["max_calls"], msg=name)
            self.assertLessEqual(current_chars, constraint["visible_budget"], msg=name)
            self.assertEqual(len(legacy), baseline["calls"], msg=name)
            self.assertGreater(baseline["visible_chars"], 0, msg=name)
            call_reductions.append((baseline["calls"] - len(current)) * 100 / baseline["calls"])
            visible_reductions.append(
                (baseline["visible_chars"] - current_chars) * 100 / baseline["visible_chars"]
            )

        median_call_reduction = sorted(call_reductions)[len(call_reductions) // 2]
        median_visible_reduction = sorted(visible_reductions)[len(visible_reductions) // 2]
        self.assertGreaterEqual(median_call_reduction, fixture["minimum_call_reduction_percent"])
        self.assertGreaterEqual(median_visible_reduction, fixture["minimum_visible_reduction_percent"])

    def test_ctags_signatures_are_qualified_with_symbol_names(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq_lib import search as search_module

        output = json.dumps({
            "_type": "tag", "name": "build", "kind": "function", "path": "fixture.py",
            "line": 3, "signature": "(value, *, strict=False)", "language": "Python",
        })
        completed = __import__("types").SimpleNamespace(returncode=0, stdout=output)
        with mock.patch.object(search_module, "find_executable", return_value="/fake/ctags"):
            with mock.patch.object(search_module, "list_repo_files", return_value=["fixture.py"]):
                with mock.patch.object(search_module, "run_cmd", return_value=completed):
                    outline = search_module._outline_ctags(self.repo, ["."], None, False, 20)
        self.assertEqual(outline["symbols"][0]["signature"], "build(value, *, strict=False)")

    def test_multi_file_inline_windows_and_continuation_recipe(self) -> None:
        first = self.repo / "packages/a/src/first_windows.py"
        second = self.repo / "packages/a/src/second_windows.py"
        first.write_text("".join(f"first {index}\n" for index in range(1, 181)), encoding="utf-8")
        second.write_text("".join(f"second {index}\n" for index in range(1, 181)), encoding="utf-8")

        batched = self.data(
            "read",
            "packages/a/src/first_windows.py:30,85,140",
            "packages/a/src/second_windows.py:20-25,110",
            "--context", "2", "--max-lines", "100",
        )

        self.assertTrue(batched["windowed"])
        self.assertEqual(batched["windows"], 5)
        self.assertEqual({item["path"] for item in batched["items"]}, {
            "packages/a/src/first_windows.py", "packages/a/src/second_windows.py",
        })
        self.assertEqual(
            sum(1 for item in batched["items"] for line in item["lines"] if line.get("anchor")),
            4,
        )

        capped = self.data(
            "read",
            "packages/a/src/first_windows.py:30,85,140",
            "packages/a/src/second_windows.py:20-25,110",
            "--context", "2", "--max-lines", "7", "--max-chars", "77",
            "--include-sensitive", "--allow-outside", "--repeat",
        )
        self.assertTrue(capped["truncated"])
        self.assertEqual(sum(len(item["lines"]) for item in capped["items"]), 7)
        self.assertGreaterEqual(capped["continuation"]["remaining_windows"], 1)
        self.assertTrue(capped["continuation"]["command"].startswith("agentq continue "))
        self.assertTrue(capped["continuation"]["cursor"])
        self.assertIn("T", capped["continuation"]["expires_at"])
        continuation = shlex.split(capped["continuation"]["command"])
        continuation[0] = str(AGENTQ)
        continued = subprocess.run(
            continuation, cwd=self.repo, env=self.env, text=True, capture_output=True,
        )
        self.assertEqual(continued.returncode, 0, msg=continued.stderr)
        self.assertIsInstance(json.loads(continued.stdout), dict)

        coalesced = self.data(
            "read",
            "packages/a/src/first_windows.py:1-5",
            "packages/a/src/first_windows.py:5-10",
            "--repeat",
        )
        self.assertEqual(coalesced["windows"], 1)
        self.assertEqual(
            [(item["start"], item["end"]) for item in coalesced["items"]],
            [(1, 10)],
        )

        many = self.repo / "packages/a/src/many_windows.py"
        many.write_text("".join(f"many {index}\n" for index in range(1, 141)), encoding="utf-8")
        anchors = ",".join(str(index) for index in range(10, 121, 10))
        current = self.data(
            "read", f"packages/a/src/many_windows.py:{anchors}",
            "--context", "0", "--max-lines", "1", "--repeat",
        )
        delivered = [line["line"] for item in current["items"] for line in item["lines"]]
        self.assertEqual(current["continuation"]["remaining_windows"], 11)
        self.assertEqual(current["continuation"]["shown_windows"], 11)
        while current.get("continuation"):
            continued = subprocess.run(
                [
                    str(AGENTQ), "continue", current["continuation"]["cursor"],
                    "--repo", str(self.repo), "--format", "json", "--budget", "1000000",
                ],
                cwd=self.repo, env=self.env, text=True, capture_output=True,
            )
            self.assertEqual(continued.returncode, 0, msg=continued.stderr)
            current = json.loads(continued.stdout)
            delivered.extend(line["line"] for item in current["items"] for line in item["lines"])
        self.assertEqual(delivered, list(range(10, 121, 10)))

    def test_inspect_source_windows_have_independent_cap_and_repeat_escape(self) -> None:
        path = self.repo / "packages/a/src/inspect_windows.py"
        path.write_text("".join(f"inspect {index}\n" for index in range(1, 181)), encoding="utf-8")
        self.data("task", "begin")

        first = self.data(
            "inspect", "packages/a/src/inspect_windows.py", "--line", "30", "90",
            "--context", "2", "--limit", "1", "--max-lines", "5",
        )["source"]
        self.assertEqual(sum(len(item["lines"]) for item in first["items"]), 5)
        self.assertTrue(first["truncated"])
        self.assertIn("continuation", first)

        repeated = self.data(
            "inspect", "packages/a/src/inspect_windows.py", "--line", "30", "90",
            "--context", "2", "--limit", "1", "--max-lines", "5",
        )
        self.assertTrue(repeated["repeat_suppressed"])
        forced = self.data(
            "inspect", "packages/a/src/inspect_windows.py", "--line", "30", "90",
            "--context", "2", "--limit", "1", "--max-lines", "5", "--repeat",
        )["source"]
        self.assertEqual(sum(len(item["lines"]) for item in forced["items"]), 5)
        inspect_events = [
            json.loads(line) for line in (self.telemetry / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if json.loads(line).get("command") == "inspect"
        ]
        self.assertTrue(inspect_events[0]["source_cap_truncated"])
        self.assertFalse(inspect_events[0]["render_budget_truncated"])

    def test_detailed_stats_group_agentq_failures_and_hide_generic_recent(self) -> None:
        failed = self.aq("inspect", "packages", "--line", "30", expect=2)
        self.assertIn("--line/--lines require inspect TARGET to be a file", failed.stderr)
        malformed = self.aq("inspect", "packages/a/src/index.ts", "--bogus", expect=2)
        self.assertIn("usage:", malformed.stderr)
        self.assertLess(len(malformed.stderr), 600)

        stats = self.data("stats", "--since", "all", "--detail")
        self.assertTrue(stats["detailed"])
        self.assertEqual(stats["recent"], [])
        row = next(row for row in stats["failures_detail"]["rows"] if row["command"] == "inspect")
        self.assertEqual(row["errors"], 2)
        signatures = {item["signature"] for item in stats["failures_detail"]["signatures"]}
        self.assertTrue(any(signature.startswith("invalid-option:inspect:hmac-") for signature in signatures))
        self.assertNotIn("--bogus", json.dumps(stats))

    def test_failure_telemetry_redacts_unknown_values_in_hot_and_archive_storage(self) -> None:
        secret_query = "PRIVATE_QUERY_91fdb"
        secret_path = "private-path-91fdb.ts"
        secret_source = "PRIVATE_SOURCE_FRAGMENT_91fdb"
        secret_option = "--private-option-91fdb"
        secret_choice = "private_choice_91fdb"
        secret_trailing = "private-trailing-path-91fdb"
        (self.repo / secret_path).write_text(f"export const value = '{secret_source}'\n", encoding="utf-8")

        self.data("search", secret_query)
        self.data("read", secret_path)
        self.aq("search", secret_query, secret_trailing, expect=2)
        self.aq("inspect", secret_path, secret_option, expect=2)
        self.aq("ts-nav", secret_choice, expect=2)

        hot_raw = (self.telemetry / "events.jsonl").read_text(encoding="utf-8")
        self.data("stats", "--archive-only", "--all-repos")
        archive_raw = self.archive.read_text(encoding="utf-8")
        for raw in (hot_raw, archive_raw):
            self.assertNotIn(secret_query, raw)
            self.assertNotIn(secret_path, raw)
            self.assertNotIn(secret_source, raw)
            self.assertNotIn(secret_option, raw)
            self.assertNotIn(secret_choice, raw)
            self.assertNotIn(secret_trailing, raw)
            events = [json.loads(line) for line in raw.splitlines() if line.strip()]
            signatures = [str(event.get("error_signature", "")) for event in events]
            self.assertTrue(any(signature.startswith("invalid-option:inspect:hmac-") for signature in signatures))
            self.assertTrue(any(signature.startswith("invalid-choice:ts-nav:hmac-") for signature in signatures))

    def test_common_agent_conventions_are_accepted_or_get_one_concise_hint(self) -> None:
        summary = self.data("git-diff", "--stat")
        self.assertNotIn("patch", summary)
        self.assertNotIn("hunks", summary)

        include_source = self.aq(
            "inspect", "packages/a/src/index.ts", "--include-source", expect=2,
        )
        self.assertIn("use --line N or --lines START:END", include_source.stderr)
        self.assertLess(len(include_source.stderr), 600)

        typo = self.aq("search", "OldName", "--max-reslts", "20", expect=2)
        self.assertIn("did you mean --max-results?", typo.stderr)
        invalid_command = subprocess.run(
            [str(AGENTQ), "searh"], text=True, capture_output=True, env=self.env, cwd=self.repo,
        )
        self.assertEqual(invalid_command.returncode, 2)
        self.assertIn("did you mean search?", invalid_command.stderr)
        self.assertLess(len(invalid_command.stderr), 600)

        stats = self.data("stats", "--since", "all", "--detail")
        signatures = {item["signature"] for item in stats["failures_detail"]["signatures"]}
        self.assertIn("invalid-option:inspect:source-inclusion", signatures)

    def test_canonical_verify_uses_task_scope_and_stats_render_scope_distribution(self) -> None:
        self.change_a("\nexport const beforeVerifyTask = true\n")
        self.data("task", "begin")
        self.change_a("\nexport const duringVerifyTask = true\n")
        plan = self.data("verify", "--dry-run", "--skip-lint")
        self.assertEqual(plan["verification_scope"], "task")
        self.assertEqual(plan["status"], "planned")
        self.assertIn("packages/a/src/index.ts", plan["changed_files"])

        summary = self.data("stats", "--since", "all")
        self.assertEqual(summary["verification"]["checks_instrumented_runs"], 1)
        self.assertEqual(summary["verification"]["files_instrumented_runs"], 1)
        self.assertEqual(summary["verification"]["packages_instrumented_runs"], 1)

        stats = self.data("stats", "--since", "all", "--detail")
        scope = next(row for row in stats["verification"]["scope_rows"] if row["scope"] == "task")
        self.assertEqual(scope["dry_runs"], 1)
        self.assertEqual(stats["verification"]["recent"][0]["status"], "dry-run")

        argv = [
            str(AGENTQ), "verify", "--repo", str(self.repo), "--format", "text",
            "--dry-run", "--skip-lint",
        ]
        rendered = subprocess.run(argv, text=True, capture_output=True, env=self.env, cwd=self.repo)
        self.assertEqual(rendered.returncode, 0, msg=rendered.stderr or rendered.stdout)
        self.assertIn("DRY: verify [DRY-RUN] · scope=task", rendered.stdout)

    def test_identical_git_diff_is_suppressed_inside_context_unless_repeated(self) -> None:
        self.data("task", "begin")
        self.change_a("\nexport const diffRepeat = true\n")
        first = self.data("git-diff", "--task", "--patch")
        self.assertNotIn("repeat_suppressed", first)
        second = self.data("git-diff", "--task", "--patch")
        self.assertTrue(second["repeat_suppressed"])
        self.assertEqual(second["repeat_scope"], "task")
        forced = self.data("git-diff", "--task", "--patch", "--repeat")
        self.assertNotIn("repeat_suppressed", forced)

    def test_doctor_reports_installed_skill_version_alignment(self) -> None:
        doctor = self.data("doctor")
        self.assertEqual(doctor["stats_renderer"], "built-in plain/ANSI")
        installation = doctor["installation"]
        self.assertGreaterEqual(installation["skills_total"], 8)
        self.assertEqual(installation["skills_current"], installation["skills_total"] )
        self.assertEqual(installation["stale_skills"], [])

    def test_benchmark_fallback_or_hyperfine(self) -> None:
        data = self.data("benchmark", "--warmup", "0", "--runs", "2", "--command", "python3 -c 'pass'")
        self.assertEqual(len(data["results"]), 1)
        self.assertGreaterEqual(data["results"][0]["mean"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
