"""Compact Git status, diff, history, and patch audit behavior."""

from __future__ import annotations

import json
import subprocess

from tests.support.cli_harness import (
    AGENTQ,
    AgentQIntegrationHarness,
)


class GitCliTests(AgentQIntegrationHarness):
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
        changed.write_text(
            changed.read_text() + "\nconsole.log('task change')\n", encoding="utf-8"
        )

        audit = self.data("audit", "--task")

        self.assertTrue(audit["task_scope"])
        self.assertEqual(audit["scope"], "active-task")
        self.assertEqual(audit["patch"]["files"], 1)
        self.assertTrue(
            any(item["rule"] == "debug-output" for item in audit["findings"])
        )
        self.assertFalse(any(item["rule"] == "debugger" for item in audit["findings"]))
        self.assertEqual(audit["preexisting_unchanged_excluded"], 1)

    def test_git_diff_hunk_index_prioritizes_broad_changes_without_patch_bodies(
        self,
    ) -> None:
        fixtures = {
            "packages/a/src/public.ts": "export function changedApi() { return 1 }\n",
            "packages/a/tests/public.test.ts": "test('changed API', () => expect(true).toBe(true))\n",
            "migrations/001_add_table.sql": "create table indexed_change(id integer);\n",
            "config/settings.yaml": "indexed: true\n",
            "packages/a/src/suppressed.ts": "// eslint-disable-next-line no-console\nconsole.log('x')\n",
        }
        fixtures.update(
            {
                f"packages/a/src/broad_{index}.ts": "".join(
                    f"const broad_{index}_{line} = {line};\n" for line in range(20)
                )
                for index in range(15)
            }
        )
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
        self.assertIn(
            "migrations", by_path["migrations/001_add_table.sql"]["risk_flags"]
        )
        self.assertIn("config", by_path["config/settings.yaml"]["risk_flags"])
        self.assertIn(
            "suppressions", by_path["packages/a/src/suppressed.ts"]["risk_flags"]
        )
        follow_ups = [item["follow_up"] for item in data["hunks"]]
        self.assertTrue(
            all(
                block["command"].startswith("agentq continue ") and block["cursor"]
                for block in follow_ups
            )
        )
        continued = self.aq(
            "continue", by_path["packages/a/src/public.ts"]["follow_up"]["cursor"]
        )
        patch = json.loads(continued.stdout)["patch"]
        self.assertIn("packages/a/src/public.ts", patch)
        self.assertIn("changedApi", patch)

    def test_sensitive_git_diff_omits_body(self) -> None:
        (self.repo / ".env").write_text(
            "API_KEY=changed-secret-value\n", encoding="utf-8"
        )
        diff = self.data("git-diff", "--patch", "--max-lines", "100")
        visible = json.dumps(diff)
        self.assertIn("sensitive diff content omitted", visible)
        self.assertNotIn("changed-secret-value", visible)
        hunks = self.data("git-diff", "--hunks")
        self.assertIn("sensitive", hunks["hunks"][0]["risk_flags"])
        self.assertNotIn("changed-secret-value", json.dumps(hunks))
        audit = self.data("audit")
        self.assertTrue(
            any(item["rule"] == "sensitive-file" for item in audit["findings"])
        )

    def test_sensitive_git_diff_overrides_mnemonic_prefixes(self) -> None:
        self.git("config", "diff.mnemonicPrefix", "true")
        (self.repo / ".env").write_text(
            "EDGE_VALUE=mnemonic-sensitive-body\n", encoding="utf-8"
        )

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
        self.git(
            "mv",
            str(renamed.relative_to(self.repo)),
            str(renamed_target.relative_to(self.repo)),
        )
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
        self.assertEqual(
            files["public/renamed ü file.txt"]["old_path"], "secrets/rename source.txt"
        )
        self.assertGreaterEqual(
            diff["patch"].count("sensitive diff content omitted"), 4
        )

        sentinels = (
            "added-sensitive-body",
            "deleted-sensitive-body",
            "quoted-sensitive-body",
            "rename-sensitive-body",
        )
        visible = json.dumps(diff, ensure_ascii=False)
        for sentinel in sentinels:
            self.assertNotIn(sentinel, visible)

        argv = [
            str(AGENTQ),
            "git-diff",
            "--repo",
            str(self.repo),
            "--patch",
            "--max-lines",
            "300",
            "--repeat",
        ]
        text_result = subprocess.run(
            argv, text=True, capture_output=True, env=self.env, cwd=self.repo
        )
        self.assertEqual(
            text_result.returncode, 0, msg=text_result.stderr or text_result.stdout
        )
        for sentinel in sentinels:
            self.assertNotIn(sentinel, text_result.stdout)

        telemetry = (self.telemetry / "events.jsonl").read_text(encoding="utf-8")
        for sentinel in sentinels:
            self.assertNotIn(sentinel, telemetry)

        audit = self.data("audit")
        sensitive_findings = {
            item.get("path")
            for item in audit["findings"]
            if item["rule"] == "sensitive-file"
        }
        self.assertIn(".env.local", sensitive_findings)
        self.assertIn("secrets/quoted ü file.txt", sensitive_findings)
        self.assertIn("secrets/delete me.txt", sensitive_findings)
