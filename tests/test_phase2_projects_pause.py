"""Phase 2 回归测试：项目管理 CRUD、Git 分支探测、代码量/长期未维护预警与优雅暂停恢复。"""
from __future__ import annotations

import csv
import datetime as dt
import http.cookiejar
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runner"))

import platform_db  # noqa: E402
import run_daily  # noqa: E402
import scoring  # noqa: E402
import web_server  # noqa: E402


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


class RepoMetricsAndPauseTests(unittest.TestCase):
    def test_validate_git_url_rejects_unsafe(self) -> None:
        self.assertEqual(platform_db.validate_git_url("https://git.example.com/a/b.git"), "https://git.example.com/a/b.git")
        self.assertEqual(platform_db.validate_git_url("git@git.example.com:a/b.git"), "git@git.example.com:a/b.git")
        for bad in ("", "file:///etc/passwd", "--upload-pack=sh", "https://bad host/a.git", "/local/path"):
            with self.assertRaises(ValueError):
                platform_db.validate_git_url(bad)

    def test_collect_repo_metrics_and_stale_levels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            env = dict(os.environ)
            old_date = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=200)).isoformat(timespec="seconds")
            env.update({
                "GIT_AUTHOR_NAME": "Tester",
                "GIT_AUTHOR_EMAIL": "tester@shineyue.com",
                "GIT_COMMITTER_NAME": "Tester",
                "GIT_COMMITTER_EMAIL": "tester@shineyue.com",
                "GIT_AUTHOR_DATE": old_date,
                "GIT_COMMITTER_DATE": old_date,
            })
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
            (repo / "App.java").write_text("public class App {\n  void run() {}\n}\n", encoding="utf-8")
            (repo / "index.ts").write_text("export const x = 1;\nexport const y = 2;\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env)
            subprocess.run(["git", "commit", "-q", "-m", "legacy commit"], cwd=repo, check=True, env=env)

            m = platform_db.collect_repo_metrics(repo)
            self.assertEqual(m["loc_total"], 5)
            self.assertEqual(m["file_count"], 2)
            self.assertEqual(m["languages"]["Java"], 3)
            self.assertEqual(m["languages"]["TypeScript"], 2)
            self.assertEqual(m["last_commit_author"], "Tester")
            self.assertEqual(m["last_commit_subject"], "legacy commit")
            days, level = platform_db.compute_stale_info(m["last_commit_at"], warn_days=90, critical_days=180)
            self.assertIsNotNone(days)
            assert days is not None
            self.assertGreaterEqual(days, 199)
            self.assertEqual(level, "critical")

            # 验证阈值切换
            days2, level2 = platform_db.compute_stale_info(m["last_commit_at"], warn_days=150, critical_days=250)
            self.assertEqual(level2, "warning")
            self.assertEqual(days2, days)

    def test_wait_while_paused_blocks_until_resumed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outdir = Path(tmp) / "output"
            web_server._set_pause_state(outdir, True, actor={"username": "admin"}, reason="gpu busy")
            resumed_at: list[float] = []

            def worker() -> None:
                run_daily._wait_while_paused(outdir, {"_pause_poll_sec": 0.05}, "demo")
                resumed_at.append(time.monotonic())

            t0 = time.monotonic()
            th = threading.Thread(target=worker, daemon=True)
            th.start()
            time.sleep(0.18)
            self.assertEqual(resumed_at, [])  # 暂停期间保持阻塞
            web_server._set_pause_state(outdir, False, actor={"username": "admin"})
            th.join(timeout=2.0)
            self.assertFalse(th.is_alive())
            self.assertEqual(len(resumed_at), 1)
            self.assertGreaterEqual(resumed_at[0] - t0, 0.15)


class Phase2WebIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.output_dir = self.root / "output"
        self.workdir = self.root / "work"
        self.projects_csv = self.root / "projects.csv"
        self.settings_path = self.root / "settings.json"
        self.settings_path.write_text(
            json.dumps({"output_dir": str(self.output_dir), "workdir": str(self.workdir)}),
            encoding="utf-8",
        )
        with self.projects_csv.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(platform_db.PROJECT_CSV_FIELDS))
            w.writeheader()
            w.writerow({
                "project_id": "proj-a", "project_name": "项目A", "department": "研发一部",
                "owner": "王秀丽", "git_url": "https://git.example.com/a.git", "branch": "main", "status": "在研",
            })

        # 在 workdir/proj-a/repo 建一个真实本地 git 仓（模拟 120 天前提交）供 refresh-metrics 扫描
        repo = self.workdir / "proj-a" / "repo"
        repo.mkdir(parents=True)
        old_iso = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=120)).isoformat(timespec="seconds")
        env = dict(os.environ, GIT_AUTHOR_NAME="Wang", GIT_AUTHOR_EMAIL="w@shineyue.com",
                   GIT_COMMITTER_NAME="Wang", GIT_COMMITTER_EMAIL="w@shineyue.com",
                   GIT_AUTHOR_DATE=old_iso, GIT_COMMITTER_DATE=old_iso)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
        (repo / "main.py").write_text("print('hi')\nprint('ok')\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", "old work"], cwd=repo, check=True, env=env)

        self.db = platform_db.PlatformDB(self.output_dir / "state" / "platform.db")
        self.db.seed_projects_from_csv(self.projects_csv, web_server._managed_projects_csv(self.output_dir))
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

    def test_admin_project_crud_metrics_csv_and_pause(self) -> None:
        mgr = _Client(self.base_url)
        self.assertEqual(mgr.request("POST", "/api/auth/login", {"username": "mgr", "password": "AdminPass123"})[0], 200)

        # 1. 新增项目并同步 projects.managed.csv
        code, res = mgr.request("POST", "/api/projects", {
            "project_id": "proj-new",
            "project_name": "新项目",
            "department": "平台部",
            "owner": "李力丁",
            "git_url": "https://git.example.com/new.git",
            "branch": "develop",
            "status": "在研",
        })
        self.assertEqual(code, 201)
        managed_csv = web_server._managed_projects_csv(self.output_dir)
        self.assertTrue(managed_csv.is_file())
        self.assertIn("proj-new", managed_csv.read_text(encoding="utf-8"))

        # 2. 更新分支与状态
        code, upd = mgr.request("POST", "/api/projects/proj-new/update", {"branch": "release", "status": "停用"})
        self.assertEqual(code, 200)
        self.assertEqual(upd["project"]["branch"], "release")
        self.assertEqual(upd["project"]["status"], "停用")

        # 3. Git 探测接口（mock subprocess）并持久化项目健康状态 + 批量校验接口
        fake_proc = mock.Mock(returncode=0, stdout="ref: refs/heads/main\tHEAD\nabc\trefs/heads/main\ndef\trefs/heads/release\n", stderr="")
        with mock.patch("platform_db.subprocess.run", return_value=fake_proc):
            code, probe = mgr.request("POST", "/api/projects/probe-git", {
                "git_url": "https://git.example.com/new.git", "branch": "release", "project_id": "proj-new",
            })
        self.assertEqual(code, 200)
        self.assertTrue(probe["ok"])
        self.assertTrue(probe["branch_exists"])
        self.assertEqual(probe["branches"], ["main", "release"])

        # 批量校验：模拟 proj-a 分支缺失，验证 git_health == 'error'
        missing_branch_proc = mock.Mock(returncode=0, stdout="ref: refs/heads/master\tHEAD\nabc\trefs/heads/master\n", stderr="")
        with mock.patch("platform_db.subprocess.run", return_value=missing_branch_proc):
            code, pall = mgr.request("POST", "/api/projects/probe-all", {"only_active": True})
        self.assertEqual(code, 200)
        self.assertEqual(pall["failed"], 1)
        code, projs_chk = mgr.request("GET", "/api/projects")
        pa_chk = next(p for p in projs_chk["projects"] if p["project_id"] == "proj-a")
        self.assertEqual(pa_chk["git_health"], "error")
        self.assertIn("未找到分支", pa_chk["git_health_reason"])

        # 4. 刷新代码量与最后提交时间 + 长期未维护阈值
        code, ref = mgr.request("POST", "/api/projects/refresh-metrics", {})
        self.assertEqual(code, 200)
        self.assertEqual(ref["updated"], 1)

        code, projs = mgr.request("GET", "/api/projects")
        self.assertEqual(code, 200)
        pa = next(p for p in projs["projects"] if p["project_id"] == "proj-a")
        self.assertEqual(pa["loc_total"], 2)
        self.assertEqual(pa["stale_level"], "warning")  # 120d >= 90d

        # 调整阈值 60 / 100 -> 120 天变为 critical
        code, th = mgr.request("POST", "/api/projects/stale-thresholds", {"warn_days": 60, "critical_days": 100})
        self.assertEqual(code, 200)
        self.assertEqual(th["stale_thresholds"], {"warn_days": 60, "critical_days": 100})
        code, ov = mgr.request("GET", "/api/overview")
        pa_ov = next(r for r in ov["rows"] if r["project_id"] == "proj-a")
        self.assertEqual(pa_ov["stale_level"], "critical")

        # 5. CSV 批量导入与删除项目
        csv_body = "project_id,project_name,department,owner,git_url,branch,status\nproj-csv,CSV导入项目,测试部,康特,https://git.example.com/csv.git,main,在研\n"
        code, imp = mgr.request("POST", "/api/projects/import", {"csv_text": csv_body, "mode": "upsert"})
        self.assertEqual(code, 200)
        self.assertEqual(imp["created"], 1)
        self.assertEqual(mgr.request("POST", "/api/projects/proj-csv/delete")[0], 200)

        # 6. 暂停 / 继续扫描状态同步到 progress 接口
        code, p1 = mgr.request("POST", "/api/scans/pause", {"reason": "模型高峰"})
        self.assertEqual(code, 200)
        self.assertTrue(p1["paused"])
        code, prog = mgr.request("GET", "/api/scans/progress")
        self.assertEqual(code, 200)
        self.assertTrue(prog["pause_state"]["paused"])
        self.assertEqual(prog["pause_state"]["paused_by"], "mgr")

        code, p2 = mgr.request("POST", "/api/scans/resume")
        self.assertEqual(code, 200)
        self.assertFalse(p2["paused"])

    def test_normal_user_cannot_modify_projects_or_pause(self) -> None:
        user = _Client(self.base_url)
        self.assertEqual(user.request("POST", "/api/auth/login", {"username": "wangxiuli", "password": "UserPass123"})[0], 200)

        for method, path, body in (
            ("POST", "/api/projects", {"project_id": "x", "git_url": "https://a.com/x.git"}),
            ("POST", "/api/projects/proj-a/update", {"branch": "dev"}),
            ("POST", "/api/projects/proj-a/delete", None),
            ("POST", "/api/projects/probe-git", {"git_url": "https://a.com/x.git"}),
            ("POST", "/api/projects/probe-all", {}),
            ("POST", "/api/projects/import", {"csv_text": "project_id\nx\n"}),
            ("POST", "/api/projects/refresh-metrics", {}),
            ("POST", "/api/projects/stale-thresholds", {"warn_days": 30, "critical_days": 60}),
            ("POST", "/api/scans/pause", {}),
            ("POST", "/api/scans/resume", {}),
        ):
            code, _ = user.request(method, path, body)
            self.assertEqual(code, 403, f"expected 403 for {method} {path}")

        # 普通用户仍可只读查看自己项目的指标和暂停状态
        code, projs = user.request("GET", "/api/projects")
        self.assertEqual(code, 200)
        self.assertEqual([p["project_id"] for p in projs["projects"]], ["proj-a"])
        code, pstat = user.request("GET", "/api/scans/pause")
        self.assertEqual(code, 200)
        self.assertFalse(pstat["paused"])


if __name__ == "__main__":
    unittest.main()
