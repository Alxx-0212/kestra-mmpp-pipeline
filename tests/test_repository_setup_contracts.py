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
            {".git", ".env", ".env_encoded", ".env.*", "secrets/", "data/"}
            <= patterns
        )

    def test_runtime_dockerfiles_copy_only_declared_sources(self):
        for dockerfile_name in (
            "finpay_pipeline/Dockerfile",
            "finpay_topup_pipeline/Dockerfile",
            "services/telegram_bot/Dockerfile",
            "linkaja_fee_pipeline/Dockerfile",
        ):
            dockerfile = (REPOSITORY_ROOT / dockerfile_name).read_text(
                encoding="utf-8"
            )
            self.assertNotRegex(dockerfile, r"(?mi)^COPY\s+\.\s+")

    def test_telegram_bot_image_publish_contract(self):
        workflow = (
            REPOSITORY_ROOT / ".github" / "workflows" / "telegram-bot-image.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("packages: write", workflow)
        self.assertIn("ghcr.io/alxx-0212/kestra-mmpp-telegram-bot", workflow)
        self.assertIn("file: services/telegram_bot/Dockerfile", workflow)

    def test_telegram_bot_compose_is_pull_and_webhook_automated(self):
        compose = (REPOSITORY_ROOT / "docker-compose.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("dockerfile: services/telegram_bot/Dockerfile", compose)
        self.assertIn("image: mmpp-telegram-bot:local", compose)
        self.assertIn("pull_policy: never", compose)
        self.assertIn(
            "TELEGRAM_ALLOWED_CHAT_IDS: ${TELEGRAM_ALLOWED_CHAT_IDS:-2142781580}",
            compose,
        )
        self.assertIn(
            "TELEGRAM_ALLOWED_USER_IDS: ${TELEGRAM_ALLOWED_USER_IDS:-2142781580}",
            compose,
        )
        self.assertIn("TELEGRAM_BOT_PUBLIC_BASE_URL:", compose)
        self.assertIn(
            "image: ngrok/ngrok:3.39.8-debian",
            compose,
        )
        self.assertIn("http://telegram-bot:8096", compose)
        self.assertIn("NGROK_AUTHTOKEN: ${NGROK_AUTHTOKEN:-}", compose)
        self.assertIn("telegram-webhook-register:", compose)
        self.assertIn("finpay-topup-flow-deploy:", compose)
        self.assertIn("/app/kestra flow updates /flows --no-delete --namespace=finance.finpay", compose)
        self.assertIn("curlimages/curl:8.11.1", compose)
        self.assertIn("/run/secrets/telegram_bot_token", compose)
        self.assertIn("http://ngrok:4040/api/tunnels", compose)
        self.assertNotIn("--token-file", compose)

    def test_telegram_bot_refresh_maps_shared_kestra_environment(self):
        compose = (REPOSITORY_ROOT / "docker-compose.yml").read_text(
            encoding="utf-8"
        )
        for required_env in (
            "KESTRA_BASE_URL: http://kestra:8080",
            "KESTRA_NAMESPACE: ${FINPAY_TOPUP_KESTRA_NAMESPACE:-finance.finpay}",
            "KESTRA_FLOW_ID: ${FINPAY_TOPUP_KESTRA_FLOW_ID:-finpay_topup_pipeline_v1}",
            "KESTRA_PUBLIC_URL: ${KESTRA_PUBLIC_URL:-}",
            "KESTRA_BASIC_AUTH_USERNAME: ${KESTRA_BASIC_AUTH_USERNAME}",
            "KESTRA_BASIC_AUTH_PASSWORD: ${KESTRA_BASIC_AUTH_PASSWORD}",
            "TELEGRAM_REFRESH_COOLDOWN_SECONDS: "
            "${TELEGRAM_REFRESH_COOLDOWN_SECONDS:-600}",
            "TELEGRAM_SELECTIVE_REFRESH_ENABLED: "
            "${TELEGRAM_SELECTIVE_REFRESH_ENABLED:-true}",
        ):
            with self.subTest(env=required_env):
                self.assertIn(required_env, compose)
        # The bot password must come from the same Compose variable the Kestra
        # server itself interpolates, not a second credential source.
        self.assertIn("password: ${KESTRA_BASIC_AUTH_PASSWORD}", compose)
        # No new Docker secret may be introduced for the bot's Kestra auth.
        self.assertNotIn("kestra_bot_auth", compose)

    def test_telegram_bot_depends_on_kestra_service_start(self):
        compose = (REPOSITORY_ROOT / "docker-compose.yml").read_text(
            encoding="utf-8"
        )
        bot_block = compose.split("  telegram-bot:", 1)[1].split("  ngrok:", 1)[0]
        self.assertRegex(
            bot_block,
            r"depends_on:\s*\n\s+postgres:\s*\n\s+condition: service_healthy"
            r"\s*\n\s+kestra:\s*\n\s+condition: service_started\s*\n",
        )

    def test_finpay_topup_yaml_keeps_exactly_webhook_and_schedule_triggers(self):
        flow = (
            REPOSITORY_ROOT
            / "finpay_topup_pipeline"
            / "workflows"
            / "finpay_topup_pipeline.yml"
        ).read_text(encoding="utf-8")
        trigger_types = re.findall(
            r"(?m)^\s*-?\s*type:\s*(io\.kestra\.plugin\.core\.trigger\.\w+)\s*$",
            flow,
        )
        self.assertEqual(
            trigger_types,
            [
                "io.kestra.plugin.core.trigger.Webhook",
                "io.kestra.plugin.core.trigger.Schedule",
            ],
        )

    def test_finpay_review_contract_is_timestamped_and_stage_first(self):
        schema = (REPOSITORY_ROOT / "finpay_topup_pipeline" / "schema.py").read_text(
            encoding="utf-8"
        )
        flow = (REPOSITORY_ROOT / "finpay_topup_pipeline" / "workflows" / "finpay_topup_pipeline.yml").read_text(
            encoding="utf-8"
        )
        for column in (
            "reviewed_by",
            "reviewed_at",
            "bucket_snapshot_at",
            "calculation_cutoff_at",
            "calculated_at",
            "source_transaction_min_at",
            "source_transaction_max_at",
            "calculation_row_count",
            "matching_row_number",
            "matching_type",
            "matching_transaction_id",
            "matching_row_hash",
            "matching_transaction_at",
            "matching_running_saldo",
            "checkpoint_as_of_date",
            "refresh_mode",
        ):
            with self.subTest(column=column):
                self.assertIn(column, schema)
        for contract in (
            "PENDING_REVIEW",
            "MISMATCH",
            "review_keyboard",
            "staged rows retained",
            "purge_staged_rows",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, flow)

    def test_local_kestra_validation_config_is_ephemeral_and_secret_free(self):
        config = (
            REPOSITORY_ROOT / "config" / "kestra-validation.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("jdbc:h2:mem:kestra", config)
        self.assertRegex(config, r"(?m)^\s+type: h2$")
        self.assertNotIn("FINPAY_DB_PASSWORD", config)
        self.assertNotIn("basic-auth", config)

    def test_initdb_resolves_custom_docker_secret_files(self):
        script = (
            REPOSITORY_ROOT / "docker" / "initdb" / "00-create-databases.sh"
        ).read_text(encoding="utf-8")
        for value_name in (
            "FINPAY_DB_PASSWORD",
            "FINPAY_TXN_DB_PASSWORD",
            "SUPERSET_DB_PASSWORD",
            "LINKAJA_DB_PASSWORD",
            "GRIST_DB_PASSWORD",
            "FINPAY_READONLY_PASSWORD",
        ):
            with self.subTest(secret=value_name):
                self.assertIn(
                    f"resolve_secret {value_name} {value_name}_FILE",
                    script,
                )

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
            "| FinPay reporting |",
            "| LinkAja |",
            "| Integrator |",
            "finpay_topup_pipeline/**",
            "Preserve unrelated dirty or untracked work",
            "docs/agent-lanes.md",
        ):
            with self.subTest(boundary=required_boundary):
                self.assertIn(required_boundary, guide)


if __name__ == "__main__":
    unittest.main()
