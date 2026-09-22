"""Doctor runtime and installation reporting."""

from __future__ import annotations

from tests.support.cli_harness import (
    AgentQIntegrationHarness,
)


class DoctorCliTests(AgentQIntegrationHarness):
    def test_doctor_reports_installed_skills(self) -> None:
        doctor = self.data("doctor")
        self.assertEqual(doctor["stats_renderer"], "built-in plain/ANSI")
        installation = doctor["installation"]
        self.assertGreaterEqual(installation["skills_total"], 8)
        self.assertEqual(len(installation["skills"]), installation["skills_total"])
        self.assertIn("repo-exploration", installation["skills"])
