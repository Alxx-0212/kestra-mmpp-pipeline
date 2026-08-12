import os
import shutil
import stat
import subprocess
import tempfile
import time
import tomllib
import unittest
import uuid
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPOSITORY_ROOT / "scripts" / "codex-lanes"
PROJECT_CODEX = REPOSITORY_ROOT / ".codex"


class CodexProjectConfigurationTest(unittest.TestCase):
    def test_project_config_enables_bounded_multi_agent_support(self):
        config = tomllib.loads((PROJECT_CODEX / "config.toml").read_text())

        self.assertIs(config["features"]["multi_agent"], True)
        self.assertEqual(
            config["agents"]["max_concurrent_threads_per_session"],
            2,
        )

    def test_custom_agent_profiles_are_scoped_and_secret_free(self):
        expected = {
            "finpay-agent": "workspace-write",
            "linkaja-agent": "workspace-write",
            "integration-reviewer": "read-only",
        }
        profiles = {}
        for profile_path in sorted((PROJECT_CODEX / "agents").glob("*.toml")):
            profile = tomllib.loads(profile_path.read_text())
            profiles[profile["name"]] = profile
            self.assertTrue(profile.get("description", "").strip())
            self.assertTrue(profile["developer_instructions"].strip())
            serialized = profile_path.read_text().lower()
            self.assertNotIn("dangerously-bypass", serialized)
            self.assertNotIn("password", serialized)
            self.assertNotIn("api_key", serialized)

        self.assertEqual(
            {name: profile["sandbox_mode"] for name, profile in profiles.items()},
            expected,
        )

    def test_launcher_is_executable_and_has_valid_bash_syntax(self):
        self.assertTrue(LAUNCHER.stat().st_mode & stat.S_IXUSR)
        subprocess.run(["bash", "-n", str(LAUNCHER)], check=True)


@unittest.skipUnless(
    all(shutil.which(command) for command in ("bash", "flock", "git")),
    "bash, flock, and git are required for launcher integration tests",
)
class CodexLaneLauncherTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="codex-lanes-test-")
        self.base = Path(self.temp_dir.name)
        self.repo = self.base / "repository with spaces"
        self.repo.mkdir()
        subprocess.run(
            ["git", "init", "-b", "integration/mmpp-next"],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "codex-lanes@example.invalid"],
            cwd=self.repo,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Codex Lanes Test"],
            cwd=self.repo,
            check=True,
        )

        (self.repo / "scripts").mkdir()
        shutil.copy2(LAUNCHER, self.repo / "scripts" / "codex-lanes")
        shutil.copytree(PROJECT_CODEX, self.repo / ".codex")
        (self.repo / "AGENTS.md").write_text("# Test repository\n")
        (self.repo / ".gitignore").write_text(".env\ndata/\n")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "test baseline"],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        )

        self.finpay_path = self.base / "finpay lane"
        self.linkaja_path = self.base / "linkaja lane"
        self.tmux_session = f"codex-lanes-test-{uuid.uuid4().hex[:12]}"
        self.environment = os.environ.copy()
        self.environment.update({
            "CODEX_LANES_FINPAY_PATH": str(self.finpay_path),
            "CODEX_LANES_LINKAJA_PATH": str(self.linkaja_path),
            "CODEX_LANES_TMUX_SESSION": self.tmux_session,
        })

    def tearDown(self):
        if shutil.which("tmux"):
            subprocess.run(
                ["tmux", "kill-session", "-t", self.tmux_session],
                capture_output=True,
            )
        self.temp_dir.cleanup()

    def run_launcher(self, *arguments, check=True):
        return subprocess.run(
            [str(self.repo / "scripts" / "codex-lanes"), *arguments],
            cwd=self.repo / "scripts",
            env=self.environment,
            check=check,
            capture_output=True,
            text=True,
        )

    def init_lanes(self):
        return self.run_launcher("init")

    def test_init_creates_independent_worktrees_without_ignored_state(self):
        (self.repo / ".env").write_text("SECRET=value\n")
        (self.repo / "data").mkdir()
        (self.repo / "data" / "financial.csv").write_text("private\n")

        result = self.init_lanes()

        self.assertIn("Created FinPay lane", result.stdout)
        self.assertEqual(
            subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=self.finpay_path,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            "agent/finpay-next",
        )
        self.assertEqual(
            subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=self.linkaja_path,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            "agent/linkaja-next",
        )
        self.assertFalse((self.finpay_path / ".env").exists())
        self.assertFalse((self.linkaja_path / "data").exists())

        (self.finpay_path / "finpay_pipeline").mkdir()
        (self.finpay_path / "finpay_pipeline" / "only_here.py").write_text("# lane\n")
        self.assertFalse(
            (self.linkaja_path / "finpay_pipeline" / "only_here.py").exists()
        )

    def test_init_refuses_a_dirty_baseline(self):
        (self.repo / "untracked.txt").write_text("dirty\n")

        result = self.run_launcher("init", check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("integration worktree must be clean", result.stderr)
        self.assertFalse(self.finpay_path.exists())
        self.assertFalse(self.linkaja_path.exists())

    def test_scope_check_accepts_lane_files_and_rejects_shared_files(self):
        self.init_lanes()
        (self.finpay_path / "finpay_pipeline").mkdir()
        (self.finpay_path / "finpay_pipeline" / "change.py").write_text("# allowed\n")

        accepted = self.run_launcher("check", "finpay")
        self.assertIn("finpay lane check passed", accepted.stdout)

        (self.finpay_path / "README.md").write_text("out of lane\n")
        rejected = self.run_launcher("check", "finpay", check=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("out-of-lane: README.md", rejected.stderr)

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required")
    def test_start_and_stop_manage_only_the_tmux_session(self):
        probe_session = f"{self.tmux_session}-probe"
        probe = subprocess.run(
            ["tmux", "new-session", "-d", "-s", probe_session, "sleep 5"],
            capture_output=True,
            text=True,
        )
        if probe.returncode != 0:
            self.skipTest(f"tmux server unavailable: {probe.stderr.strip()}")
        subprocess.run(
            ["tmux", "kill-session", "-t", probe_session],
            capture_output=True,
        )

        self.init_lanes()
        fake_codex = self.base / "fake-codex"
        launch_log = self.base / "codex-cwd.log"
        fake_codex.write_text(
            "#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"$PWD\" >>{launch_log}\n"
            "exec sleep 300\n"
        )
        fake_codex.chmod(0o755)
        self.environment["CODEX_LANES_CODEX_BIN"] = str(fake_codex)
        session_id = "12345678-1234-1234-1234-123456789abc"

        started = self.run_launcher(
            "start",
            "--linkaja-fork",
            session_id,
            check=False,
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertIn("LinkAja forked session", started.stdout)
        windows = subprocess.run(
            [
                "tmux",
                "list-windows",
                "-t",
                self.tmux_session,
                "-F",
                "#{window_name}|#{pane_current_path}",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        self.assertEqual(
            {line.split("|", 1)[0] for line in windows},
            {"integrator", "finpay", "linkaja"},
        )
        deadline = time.monotonic() + 2
        launched_directories = set()
        while time.monotonic() < deadline:
            if launch_log.exists():
                launched_directories = set(launch_log.read_text().splitlines())
            if len(launched_directories) == 3:
                break
            time.sleep(0.05)
        self.assertEqual(
            launched_directories,
            {str(self.repo), str(self.finpay_path), str(self.linkaja_path)},
        )

        stopped = self.run_launcher("stop", check=False)
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertIn("Worktrees and branches were kept", stopped.stdout)
        self.assertTrue(self.finpay_path.exists())
        self.assertTrue(self.linkaja_path.exists())
        self.assertNotEqual(
            subprocess.run(
                ["tmux", "has-session", "-t", self.tmux_session],
                capture_output=True,
            ).returncode,
            0,
        )

    def test_start_requires_an_explicit_linkaja_context_choice(self):
        result = self.run_launcher("start", check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--linkaja-picker", result.stderr)


if __name__ == "__main__":
    unittest.main()
