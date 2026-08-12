import re
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class RepositorySetupContractTest(unittest.TestCase):
    def test_docker_context_excludes_sensitive_and_large_local_state(self):
        patterns = {
            line.strip()
            for line in (REPOSITORY_ROOT / ".dockerignore")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertTrue(
            {".git", ".env", ".env_encoded", ".env.*", "data/"}
            <= patterns
        )

    def test_runtime_dockerfiles_copy_only_declared_sources(self):
        for dockerfile_name in ("Dockerfile", "Dockerfile.linkaja"):
            dockerfile = (REPOSITORY_ROOT / dockerfile_name).read_text(
                encoding="utf-8"
            )
            self.assertNotRegex(dockerfile, r"(?mi)^COPY\s+\.\s+")

    def test_local_kestra_validation_config_is_ephemeral_and_secret_free(self):
        config = (
            REPOSITORY_ROOT / "config" / "kestra-validation.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("jdbc:h2:mem:kestra", config)
        self.assertRegex(config, r"(?m)^\s+type: h2$")
        self.assertNotIn("FINPAY_DB_PASSWORD", config)
        self.assertNotIn("basic-auth", config)

    def test_documented_kestra_port_matches_compose_host_port(self):
        compose = (REPOSITORY_ROOT / "docker-compose.yml").read_text(
            encoding="utf-8"
        )
        readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
        match = re.search(r'^\s*- "(\d+):8080"\s*$', compose, re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertIn(f"http://localhost:{match.group(1)}", readme)

    def test_root_readme_documents_current_finpay_orchestration_boundary(self):
        readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
        for required_contract in (
            ".dockerignore",
            "config/kestra-validation.yml",
            "finance.finpay.finpay_daily_pipeline_v5",
            "no Kestra trigger or schedule",
            "csv_file",
            "dry_run",
            "Hermes",
        ):
            with self.subTest(contract=required_contract):
                self.assertIn(required_contract, readme)

    def test_root_agent_guide_defines_disjoint_edit_lanes(self):
        guide = (REPOSITORY_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        for required_boundary in (
            "| FinPay agent |",
            "| LinkAja agent |",
            "| Integrating agent only |",
            "finpay_pipeline/summary_sheets.py",
            "domain agents do not stage, commit, stash, reset, or",
            "If work requires a file outside the declared lane, stop",
        ):
            with self.subTest(boundary=required_boundary):
                self.assertIn(required_boundary, guide)


if __name__ == "__main__":
    unittest.main()
