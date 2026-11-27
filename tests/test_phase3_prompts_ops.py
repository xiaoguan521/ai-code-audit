"""Phase 3 回归测试：提示词提取/热挂载/回滚 + 超级管理员模型/环境变量/Git 账号凭据隔离。"""
from __future__ import annotations

import http.cookiejar
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runner"))
sys.path.insert(0, str(ROOT / "docker"))

import compose_entrypoint  # noqa: E402
import platform_db  # noqa: E402
import run_daily  # noqa: E402
import scoring  # noqa: E402
import web_server  # noqa: E402
from adapters import harness  # noqa: E402


class _Client:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(req, timeout=5) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8", errors="replace")
            finally:
                exc.close()
            try:
                parsed = json.loads(raw) if raw else {}
            except ValueError:
                parsed = {"raw": raw}
            return exc.code, parsed


class Phase3WiringTests(unittest.TestCase):
    def test_harness_mounts_skill_override_and_compose_applies_runtime_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            state_dir = data_dir / "output" / "state"
            prompts_dir = state_dir / "prompts"
            prompts_dir.mkdir(parents=True, exist_ok=True)

            skill_file = prompts_dir / "SKILL.md"
            skill_file.write_text("# Custom Skill\n" + ("rule\n" * 80), encoding="utf-8")
            (prompts_dir / "dispatch_extra.txt").write_text("重点核查越权与硬编码IP", encoding="utf-8")
            platform_db.write_env_file(state_dir / "model.runtime.env", {
                "OPENAI_BASE_URL": "http://vllm:8000/v1",
                "OPENAI_MODEL": "qwen3.8",
                "OPENAI_API_KEY": "EMPTY",
            })
            (state_dir / "runtime_overrides.json").write_text(json.dumps({
                "max_parallel": 8,
                "max_model_concurrency": 4,
                "timeout_small_sec": 1800,
                "fast_mode": True,
                "scanner_image": "harbor.local/scanner:v2",
            }), encoding="utf-8")

            settings = compose_entrypoint.configure_settings(
                {"agent": {"type": "deepseek-harness"}, "summary": {"enabled": True}},
                {"AUDIT_DATA_DIR": str(data_dir), "AUDIT_MODEL_NETWORK": "ai-net"},
            )
            self.assertEqual(settings["max_parallel"], 8)
            self.assertEqual(settings["max_model_concurrency"], 4)
            self.assertEqual(settings["scale_timeouts"]["small"], 1800)
            self.assertTrue(settings["fast_mode"])
            self.assertEqual(settings["agent"]["image"], "harbor.local/scanner:v2")
            self.assertEqual(settings["agent"]["env_file"], str(state_dir / "model.runtime.env"))
            self.assertEqual(settings["agent"]["skill_override_file"], str(skill_file))

            # 验证 harness._docker_command 热挂载了自定义 SKILL.md
            cmd, _ = harness._docker_command(settings["agent"], "/tmp/repo", "/tmp/ctx")
            self.assertIn(f"{skill_file}:/opt/audit/skills/eight-quality-code-audit/SKILL.md:ro", cmd)

            # 验证 build_prompt 拼接了管理员补充提示词
            prompt = run_daily.build_prompt(
                {"project_id": "p1", "project_name": "项目1", "branch": "main"},
                "abc1234567",
                {"output_dir": str(data_dir / "output"), "fast_mode": True},
                "full",
            )
            self.assertIn("管理员补充审查要求：", prompt)
            self.assertIn("重点核查越权与硬编码IP", prompt)


class Phase3WebIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.output_dir = self.root / "output"
        self.projects_csv = self.root / "projects.csv"
        self.projects_csv.write_text("project_id,project_name,owner,git_url,branch,status\n", encoding="utf-8")
        self.settings_path = self.root / "settings.json"
        self.settings_path.write_text(json.dumps({"output_dir": str(self.output_dir)}), encoding="utf-8")

        self.db = platform_db.PlatformDB(self.output_dir / "state" / "platform.db")
        self.db.ensure_initial_superadmin(legacy_token="SuperPass123")
        self.db.create_user(
            username="mgr", display_name="管理员", role="admin",
            password="AdminPass123", email="mgr@shineyue.com", must_change_password=False,
        )
        self.db.create_user(
            username="wangxiuli", display_name="王秀丽", role="user",
            password="UserPass123", email="wangxiuli@shineyue.com", must_change_password=False,
        )

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web_server.AuditHandler)
        self.server.cfg = {  # type: ignore[attr-defined]
            "output_dir": self.output_dir,
            "settings_path": self.settings_path,
            "projects_csv": self.projects_csv,
            "scan_statuses": ["在研"],
            "checklist": scoring.load_checklist(),
            "dist_dir": self.root / "no-dist",
            "auth_token": "",
            "db": self.db,
        }
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    def test_prompt_management_for_admin_and_blocked_for_user(self) -> None:
        user = _Client(self.base_url)
        self.assertEqual(user.request("POST", "/api/auth/login", {"username": "wangxiuli", "password": "UserPass123"})[0], 200)
        self.assertEqual(user.request("GET", "/api/prompts")[0], 403)
        self.assertEqual(user.request("POST", "/api/prompts/dispatch_extra/save", {"content": "hack"})[0], 403)

        mgr = _Client(self.base_url)
        self.assertEqual(mgr.request("POST", "/api/auth/login", {"username": "mgr", "password": "AdminPass123"})[0], 200)
        code, res = mgr.request("GET", "/api/prompts")
        self.assertEqual(code, 200)
        by_key = {p["key"]: p for p in res["prompts"]}
        self.assertGreater(len(by_key["skill_md"]["content"]), 500)

        # 保存两次 dispatch_extra，再回滚至 v1，再恢复默认
        code, r1 = mgr.request("POST", "/api/prompts/dispatch_extra/save", {"content": "规则版本一", "note": "v1"})
        self.assertEqual(code, 200)
        v1_id = next(p for p in r1["prompts"] if p["key"] == "dispatch_extra")["versions"][0]["id"]
        extra_file = self.output_dir / "state" / "prompts" / "dispatch_extra.txt"
        self.assertEqual(extra_file.read_text(encoding="utf-8"), "规则版本一")

        code, _ = mgr.request("POST", "/api/prompts/dispatch_extra/save", {"content": "规则版本二", "note": "v2"})
        self.assertEqual(code, 200)
        self.assertEqual(extra_file.read_text(encoding="utf-8"), "规则版本二")

        code, _ = mgr.request("POST", "/api/prompts/dispatch_extra/rollback", {"version_id": v1_id})
        self.assertEqual(code, 200)
        self.assertEqual(extra_file.read_text(encoding="utf-8"), "规则版本一")

        code, _ = mgr.request("POST", "/api/prompts/dispatch_extra/reset")
        self.assertEqual(code, 200)
        self.assertFalse(extra_file.exists())

    def test_ops_config_and_git_credentials_superadmin_only(self) -> None:
        # 管理员和普通用户都禁止访问运维配置（模型 / 环境变量 / Git 账号）
        for uname, pwd in (("mgr", "AdminPass123"), ("wangxiuli", "UserPass123")):
            cli = _Client(self.base_url)
            self.assertEqual(cli.request("POST", "/api/auth/login", {"username": uname, "password": pwd})[0], 200)
            for method, path, body in (
                ("GET", "/api/ops/config", None),
                ("POST", "/api/ops/model", {"OPENAI_MODEL": "x"}),
                ("POST", "/api/ops/test-model", {}),
                ("POST", "/api/ops/runtime", {"max_parallel": 4}),
                ("POST", "/api/ops/git-credentials", {"host": "git.a.com", "username": "u", "secret": "s"}),
            ):
                code, _ = cli.request(method, path, body)
                self.assertEqual(code, 403, f"{uname} should get 403 on {method} {path}")

        # 超级管理员操作模型配置、运行参数与 Git 凭据
        sup = _Client(self.base_url)
        self.assertEqual(sup.request("POST", "/api/auth/login", {"username": "admin", "password": "SuperPass123"})[0], 200)

        code, m1 = sup.request("POST", "/api/ops/model", {
            "OPENAI_BASE_URL": "http://10.0.1.5:8000/v1",
            "OPENAI_MODEL": "qwen3.8",
            "OPENAI_API_KEY": "sk-super-secret-9988",
            "OPENAI_MAX_OUTPUT_TOKENS": "8192",
        })
        self.assertEqual(code, 200)
        # 响应不泄露明文密钥
        self.assertNotIn("sk-super-secret-9988", json.dumps(m1))
        self.assertTrue(m1["model"]["OPENAI_API_KEY_SET"])
        self.assertIn("9988", m1["model"]["OPENAI_API_KEY_MASKED"])

        # 仅改模型名、留空 API Key 时保留原密钥
        code, _ = sup.request("POST", "/api/ops/model", {"OPENAI_MODEL": "qwen3.8-turbo", "OPENAI_API_KEY": ""})
        self.assertEqual(code, 200)
        parsed_env = platform_db.parse_env_file(self.output_dir / "state" / "model.runtime.env")
        self.assertEqual(parsed_env["OPENAI_MODEL"], "qwen3.8-turbo")
        self.assertEqual(parsed_env["OPENAI_API_KEY"], "sk-super-secret-9988")

        # 运行时并发与超时配置
        code, rt = sup.request("POST", "/api/ops/runtime", {
            "max_parallel": 8, "max_model_concurrency": 4, "fast_mode": True,
        })
        self.assertEqual(code, 200)
        self.assertEqual(rt["runtime"]["max_parallel"], 8)
        self.assertTrue(rt["runtime_overridden"])

        # Git 仓库账号凭据 CRUD 与运行时文件导出（含特殊字符 URL 编码 + 密钥脱敏）
        code, gc = sup.request("POST", "/api/ops/git-credentials", {
            "label": "内网 GitLab",
            "protocol": "https",
            "host": "git.shineyue.com",
            "path_prefix": "/core",
            "username": "bot@co",
            "secret": "p@ss#1234",
        })
        self.assertEqual(code, 201)
        self.assertNotIn("p@ss#1234", json.dumps(gc))
        cid = gc["credential"]["id"]
        rt_file = self.output_dir / "state" / "git-credentials.runtime"
        self.assertTrue(rt_file.is_file())
        self.assertIn("https://bot%40co:p%40ss%231234@git.shineyue.com/core", rt_file.read_text(encoding="utf-8"))

        # 编辑留空 secret 保留原密码
        code, _ = sup.request("POST", f"/api/ops/git-credentials/{cid}/update", {
            "label": "内网 GitLab 只读",
            "protocol": "https",
            "host": "git.shineyue.com",
            "path_prefix": "/core",
            "username": "bot@co",
            "secret": "",
            "is_active": True,
        })
        self.assertEqual(code, 200)
        self.assertIn("p%40ss%231234", rt_file.read_text(encoding="utf-8"))

        # 删除后凭据文件清空
        code, _ = sup.request("POST", f"/api/ops/git-credentials/{cid}/delete")
        self.assertEqual(code, 200)
        self.assertFalse(rt_file.exists())

        # 审计日志绝不包含明文密钥或 Git 密码
        code, logs = sup.request("GET", "/api/audit-logs?limit=50")
        self.assertEqual(code, 200)
        dumped_logs = json.dumps(logs, ensure_ascii=False)
        self.assertNotIn("sk-super-secret-9988", dumped_logs)
        self.assertNotIn("p@ss#1234", dumped_logs)


if __name__ == "__main__":
    unittest.main()
