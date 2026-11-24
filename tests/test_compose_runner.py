"""验证 Compose 调度配置、多个仓库和只读 Git 凭据，不需要 Docker daemon。"""
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "docker"))
from test_runner import valid_result  # noqa: E402
from audit_fixture import markdown_from_result  # noqa: E402
import compose_entrypoint  # noqa: E402


class ComposeRunnerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def git(self, repo, *args):
        result = subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=Audit Test", "-c", "user.email=audit@example.com", *args],
            check=True, capture_output=True, text=True,
        )
        return result.stdout.strip()

    def test_compose_can_select_harness_without_dimension_split(self):
        settings = {"agent": {"type": "deepseek-harness", "strategy": "dimensions"}}
        configured = compose_entrypoint.configure_settings(settings, {
            "AUDIT_DATA_DIR": str(self.root / "data"),
            "AUDIT_MODEL_NETWORK": "harness-net",
            "AUDIT_AGENT_TYPE": "deepseek-harness",
            "AUDIT_SCANNER_IMAGE": "harbor.example/deepseek-harness:2026.11.23",
            "AUDIT_MODEL_ENV_PATH": "/run/secrets/deepseek.env",
            "AUDIT_HARNESS_ARGS": '["headless", "--json"]',
        })
        agent = configured["agent"]
        self.assertEqual(agent["type"], "deepseek-harness")
        self.assertEqual(agent["strategy"], "single")
        self.assertEqual(agent["launcher"], "docker")
        self.assertEqual(agent["image"], "harbor.example/deepseek-harness:2026.11.23")
        self.assertEqual(agent["network"], "harness-net")
        self.assertEqual(agent["args"], ["headless", "--json"])

        defaults = compose_entrypoint.configure_settings(
            {"agent": {"type": "deepseek-harness", "strategy": "dimensions"}},
            {"AUDIT_DATA_DIR": str(self.root / "data"),
             "AUDIT_MODEL_NETWORK": "harness-net",
             "AUDIT_AGENT_TYPE": "deepseek-harness",
             "AUDIT_SCANNER_IMAGE": "harness:test"},
        )["agent"]
        self.assertEqual(defaults["args"], [
            "headless", "--json", "--permission-mode", "read-only", "--approval-policy", "never",
        ])

    def test_compose_rejects_shell_harness_args(self):
        with self.assertRaisesRegex(ValueError, "JSON 字符串数组"):
            compose_entrypoint.configure_settings(
                {"agent": {}},
                {"AUDIT_DATA_DIR": str(self.root / "data"),
                 "AUDIT_MODEL_NETWORK": "harness-net",
                 "AUDIT_AGENT_TYPE": "deepseek-harness",
                 "AUDIT_HARNESS_ARGS": "dsh headless --json"},
            )

    def test_compose_rejects_old_agent_backend(self):
        with self.assertRaisesRegex(ValueError, "deepseek-harness"):
            compose_entrypoint.configure_settings(
                {"agent": {}},
                {"AUDIT_DATA_DIR": str(self.root / "data"), "AUDIT_MODEL_NETWORK": "harness-net",
                 "AUDIT_AGENT_TYPE": "pi-docker"},
            )

    def test_compose_configures_project_and_model_concurrency(self):
        configured = compose_entrypoint.configure_settings(
            {"agent": {"type": "deepseek-harness"}},
            {"AUDIT_DATA_DIR": str(self.root / "data"), "AUDIT_MODEL_NETWORK": "harness-net",
             "AUDIT_MAX_PARALLEL": "4", "AUDIT_MAX_MODEL_CONCURRENCY": "2"},
        )
        self.assertEqual(configured["max_parallel"], 4)
        self.assertEqual(configured["max_model_concurrency"], 2)
        with self.assertRaisesRegex(ValueError, "AUDIT_MAX_PARALLEL"):
            compose_entrypoint.configure_settings(
                {"agent": {"type": "deepseek-harness"}},
                {"AUDIT_DATA_DIR": str(self.root / "data"), "AUDIT_MODEL_NETWORK": "harness-net",
                 "AUDIT_MAX_PARALLEL": "0"},
            )

    def test_compose_can_enable_isolated_summary_with_harness_defaults(self):
        configured = compose_entrypoint.configure_settings(
            {"agent": {"type": "deepseek-harness"}, "summary": {"enabled": False}},
            {"AUDIT_DATA_DIR": str(self.root / "data"),
             "AUDIT_MODEL_NETWORK": "harness-net",
             "AUDIT_AGENT_TYPE": "deepseek-harness",
             "AUDIT_SCANNER_IMAGE": "harness:test",
             "AUDIT_MODEL_ENV_PATH": "/run/secrets/model.env",
             "AUDIT_SUMMARY_ENABLED": "true"},
        )
        summary_agent = configured["summary"]["agent"]
        self.assertTrue(configured["summary"]["enabled"])
        self.assertEqual(summary_agent["type"], "deepseek-harness")
        self.assertEqual(summary_agent["strategy"], "single")
        self.assertEqual(summary_agent["image"], "harness:test")
        self.assertEqual(summary_agent["network"], "harness-net")
        self.assertEqual(summary_agent["env_file"], "/run/secrets/model.env")

    def make_repo(self, name):
        repo = self.root / name
        repo.mkdir()
        self.git(repo, "init", "-q", "-b", "main")
        (repo / "app.py").write_text(f"# {name} main\n", encoding="utf-8")
        self.git(repo, "add", "app.py")
        self.git(repo, "commit", "-qm", "main")
        main_sha = self.git(repo, "rev-parse", "HEAD")
        self.git(repo, "checkout", "-qb", "develop")
        (repo / "app.py").write_text(f"# {name} develop\n", encoding="utf-8")
        self.git(repo, "commit", "-qam", "develop")
        dev_sha = self.git(repo, "rev-parse", "HEAD")
        self.git(repo, "checkout", "-q", "main")
        return repo, main_sha, dev_sha

    def test_compose_pull_target_and_runner_use_the_same_scanner_image(self):
        if not shutil.which("docker"):
            self.skipTest("Docker CLI 未安装")
        probe = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True)
        if probe.returncode:
            self.skipTest("Docker Compose 不可用")
        image = "registry.example/scanner:custom"
        runner_image = "registry.example/runner:custom"
        env = dict(os.environ, AUDIT_DATA_DIR=str(self.root / "data"), AUDIT_SCANNER_IMAGE=image,
                   AUDIT_RUNNER_IMAGE=runner_image)
        result = subprocess.run(["docker", "compose", "--profile", "images", "config", "--format", "json"],
                                cwd=ROOT, env=env, capture_output=True, text=True, check=True)
        services = json.loads(result.stdout)["services"]
        self.assertEqual(services["scanner-image"]["image"], image)
        self.assertEqual(services["audit"]["environment"]["AUDIT_SCANNER_IMAGE"], image)
        self.assertEqual(services["audit"]["image"], runner_image)
        self.assertEqual(services["scanner-image"]["profiles"], ["images"])

    def test_three_project_branches_pass_host_paths_to_docker(self):
        first, first_main, first_dev = self.make_repo("first")
        second, second_main, _ = self.make_repo("second")
        projects = self.root / "projects.csv"
        with projects.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["project_id", "project_name", "git_url", "branch", "status"])
            writer.writeheader()
            for pid, repo, branch in [("first-main", first, "main"), ("first-dev", first, "develop"),
                                      ("second-main", second, "main")]:
                writer.writerow({"project_id": pid, "project_name": pid, "git_url": repo.as_uri(),
                                 "branch": branch, "status": "在研"})

        settings = json.loads((ROOT / "config/settings.json").read_text(encoding="utf-8"))
        settings.update({"prescan_tools": [], "max_parallel": 1})
        settings["summary"]["enabled"] = False
        settings["agent"].update(type="deepseek-harness", strategy="single")
        settings_file = self.root / "settings.json"
        settings_file.write_text(json.dumps(settings), encoding="utf-8")
        model_file = self.root / "model.env"
        model_file.write_text("OPENAI_API_KEY=dummy-secret\n", encoding="utf-8")
        log_file = self.root / "docker-calls.jsonl"
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        fake_docker = bin_dir / "docker"
        fake_docker.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "with open(os.environ['FAKE_DOCKER_LOG'], 'a', encoding='utf-8') as f:\n"
            "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "print(os.environ['FAKE_DOCKER_EVENTS'])\n",
            encoding="utf-8",
        )
        fake_docker.chmod(0o755)
        data_dir = self.root / "host-data"
        data_dir.mkdir()
        output = valid_result()
        for item in output["items"]:
            item["evidence"] = [{"path": "app.py", "start_line": 1}]
        env = dict(os.environ, PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
                   AUDIT_DATA_DIR=str(data_dir), AUDIT_MODEL_NETWORK="test-model-net",
                   AUDIT_SCANNER_IMAGE="example.test/scanner:test", AUDIT_MODEL_ENV_PATH=str(model_file),
                   AUDIT_SETTINGS_FILE=str(settings_file), AUDIT_PROJECTS_FILE=str(projects),
                   FAKE_DOCKER_LOG=str(log_file),
                   FAKE_DOCKER_EVENTS=json.dumps({"type": "final", "reason": {"kind": "completed"},
                                                 "text": markdown_from_result(output)}, ensure_ascii=False))
        result = subprocess.run([sys.executable, str(ROOT / "docker/compose_entrypoint.py"),
                                 "--date", "2026-11-21", "--force-full"],
                                env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        runlog = json.loads(next((data_dir / "output/results/2026-11-21").glob("*/_run.json")).read_text(encoding="utf-8"))
        actual = {p["project_id"]: (p["status"], p["commit"]) for p in runlog["projects"]}
        self.assertEqual(actual, {"first-main": ("ok", first_main), "first-dev": ("ok", first_dev),
                                  "second-main": ("ok", second_main)})
        calls = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(calls), 3)
        for pid, args in zip(("first-main", "first-dev", "second-main"), calls):
            self.assertIn(f"{data_dir}/work/{pid}/repo:/workspace/repo:ro", args)
            self.assertIn(f"{data_dir}/work/{pid}/context:/workspace/context:ro", args)
            self.assertEqual(args[args.index("--network") + 1], "test-model-net")
            self.assertEqual(args[args.index("--env-file") + 1], str(model_file))
            image_index = args.index("example.test/scanner:test")
            self.assertEqual(args[image_index + 1:image_index + 3], ["dsh", "headless"])
            self.assertIn("YAML front matter", args[-1])
            self.assertNotIn("OUTPUT_FORMAT: markdown", args[-1])
            self.assertNotIn("dummy-secret", " ".join(args))

    def test_git_credentials_are_selected_per_repository_and_never_rewritten(self):
        credentials = self.root / "credentials"
        original = ("https://alice:token-one@git.example.com/team/one.git\n"
                    "https://bob:token-two@git.example.com/team/two.git\n")
        credentials.write_text(original, encoding="utf-8")
        helper = ROOT / "docker/git-credential-readonly.sh"
        env = dict(os.environ, AUDIT_GIT_CREDENTIAL_FILE=str(credentials), GIT_TERMINAL_PROMPT="0")
        for repo, user, token in (("one", "alice", "token-one"), ("two", "bob", "token-two")):
            request = f"protocol=https\nhost=git.example.com\npath=team/{repo}.git\n\n"
            result = subprocess.run(["git", "-c", "credential.helper=", "-c", f"credential.helper={helper}",
                                     "-c", "credential.useHttpPath=true", "credential", "fill"],
                                    input=request, env=env, capture_output=True, text=True, check=True)
            self.assertIn(f"username={user}\n", result.stdout)
            self.assertIn(f"password={token}\n", result.stdout)
        subprocess.run([str(helper), "store"], input="protocol=https\nhost=git.example.com\n\n",
                       env=env, capture_output=True, text=True, check=True)
        self.assertEqual(credentials.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
