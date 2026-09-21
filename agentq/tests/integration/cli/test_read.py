"""Bounded read windows, redaction, and source-window merging."""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

from tests.support.cli_harness import (
    AGENTQ,
    AgentQIntegrationHarness,
)


class ReadCliTests(AgentQIntegrationHarness):
    def test_read_redacts_private_key_material_even_with_sensitive_override(
        self,
    ) -> None:
        key = self.repo / "fixture.pem"
        key.write_text(
            "-----BEGIN PRIVATE KEY-----\nBASE64KEYMATERIAL\n-----END PRIVATE KEY-----\n",
            encoding="utf-8",
        )
        data = self.data("read", "fixture.pem", "--include-sensitive")
        visible = json.dumps(data)
        self.assertIn("REDACTED_PRIVATE_KEY", visible)
        self.assertNotIn("BASE64KEYMATERIAL", visible)
        self.assertEqual(
            data["items"][0]["redaction"],
            {
                "private_key_blocks": 1,
                "redacted_lines": 3,
                "unterminated_private_key_blocks": 0,
            },
        )

    def test_read_private_key_redaction_reports_multiple_windows_blocks(self) -> None:
        key = self.repo / "windows.pem"
        key.write_bytes(
            b"-----BEGIN PRIVATE KEY-----\r\nFIRSTKEYMATERIAL\r\n-----END PRIVATE KEY-----\r\n"
            b"between\r\n"
            b"-----BEGIN RSA PRIVATE KEY-----\r\nSECONDKEYMATERIAL\r\n-----END RSA PRIVATE KEY-----\r\n"
        )

        data = self.data(
            "read",
            "windows.pem",
            "--include-sensitive",
            "--line",
            "1",
            "5",
            "--context",
            "2",
        )

        visible = json.dumps(data)
        self.assertNotIn("FIRSTKEYMATERIAL", visible)
        self.assertNotIn("SECONDKEYMATERIAL", visible)
        self.assertEqual(
            data["redaction"],
            {
                "private_key_blocks": 2,
                "redacted_lines": 6,
                "unterminated_private_key_blocks": 0,
            },
        )
        argv = [
            str(AGENTQ),
            "read",
            "--repo",
            str(self.repo),
            "windows.pem",
            "--include-sensitive",
            "--repeat",
        ]
        rendered = subprocess.run(
            argv, text=True, capture_output=True, env=self.env, cwd=self.repo
        )
        self.assertEqual(rendered.returncode, 0, msg=rendered.stderr or rendered.stdout)
        self.assertIn("[redacted 2 private-key block(s), 6 line(s)]", rendered.stdout)
        self.assertNotIn("FIRSTKEYMATERIAL", rendered.stdout)
        self.assertNotIn("SECONDKEYMATERIAL", rendered.stdout)

    def test_read_private_key_redaction_bounds_unterminated_sensitive_file(
        self,
    ) -> None:
        key = self.repo / "unfinished.pem"
        key.write_text(
            "prefix -----BEGIN PRIVATE KEY-----\nUNFINISHEDKEYMATERIAL\ntrailing material\n",
            encoding="utf-8",
        )

        data = self.data("read", "unfinished.pem", "--include-sensitive")

        visible = json.dumps(data)
        self.assertNotIn("UNFINISHEDKEYMATERIAL", visible)
        self.assertNotIn("trailing material", visible)
        self.assertEqual(
            data["items"][0]["redaction"],
            {
                "private_key_blocks": 1,
                "redacted_lines": 3,
                "unterminated_private_key_blocks": 1,
            },
        )
        argv = [
            str(AGENTQ),
            "read",
            "--repo",
            str(self.repo),
            "unfinished.pem",
            "--include-sensitive",
            "--repeat",
        ]
        rendered = subprocess.run(
            argv, text=True, capture_output=True, env=self.env, cwd=self.repo
        )
        self.assertEqual(rendered.returncode, 0, msg=rendered.stderr or rendered.stdout)
        self.assertIn("1 unterminated at EOF", rendered.stdout)
        self.assertNotIn("UNFINISHEDKEYMATERIAL", rendered.stdout)

    def test_read_does_not_treat_source_literals_or_comments_as_private_key_blocks(
        self,
    ) -> None:
        source = self.repo / "marker_source.py"
        source.write_text(
            textwrap.dedent("""\
            before = "visible before"
            marker = "-----BEGIN PRIVATE KEY-----"
            # -----BEGIN PRIVATE KEY-----
            after = "visible after"
        """),
            encoding="utf-8",
        )

        data = self.data("read", "marker_source.py")
        visible = json.dumps(data)

        self.assertIn("visible before", visible)
        self.assertIn("visible after", visible)
        self.assertNotIn("redaction", data["items"][0])

    def test_read_refuses_sensitive_file(self) -> None:
        data = self.data("read", ".env")
        self.assertTrue(data["items"][0]["refused"])

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
        self.assertEqual(
            [item["text"] for item in data["items"][0]["lines"]], ["two", "three"]
        )

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

    def test_multi_anchor_read_and_inspect_merge_source_windows(self) -> None:
        path = self.repo / "packages/a/src/windows.ts"
        path.write_text(
            "".join(f"line {index}\n" for index in range(1, 221)), encoding="utf-8"
        )

        read = self.data(
            "read",
            "packages/a/src/windows.ts",
            "--line",
            "30",
            "32",
            "110",
            "--context",
            "2",
        )
        self.assertTrue(read["windowed"])
        self.assertEqual(read["anchors"], [30, 32, 110])
        self.assertEqual(read["windows"], 2)
        self.assertEqual((read["items"][0]["start"], read["items"][0]["end"]), (28, 34))
        self.assertEqual(
            (read["items"][1]["start"], read["items"][1]["end"]), (108, 112)
        )
        self.assertEqual(
            sum(
                1
                for item in read["items"]
                for line in item["lines"]
                if line.get("anchor")
            ),
            3,
        )

        inspect = self.data(
            "inspect",
            "packages/a/src/windows.ts",
            "--line",
            "30",
            "--line",
            "110",
            "150",
            "220",
            "--context",
            "1",
            "--limit",
            "80",
        )
        self.assertEqual(inspect["kind"], "source-windows")
        source = inspect["source"]
        self.assertEqual(source["anchors"], [30, 110, 150, 220])
        self.assertEqual(source["windows"], 4)

        ranged = self.data(
            "read",
            "packages/a/src/windows.ts",
            "--lines",
            "30:35",
            "--lines",
            "34:40",
            "--lines",
            "100:102",
            "--repeat",
        )
        self.assertEqual(ranged["windows"], 2)
        self.assertEqual(
            (ranged["items"][0]["start"], ranged["items"][0]["end"]), (30, 40)
        )
