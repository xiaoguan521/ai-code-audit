"""Phase 1 管理端回归测试：用户体系、拼音邮箱生成、RBAC 权限隔离与审计日志。"""
from __future__ import annotations

import csv
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
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runner"))

import platform_db  # noqa: E402
import scoring  # noqa: E402
import web_server  # noqa: E402


SAMPLE_REPORT = """# 项目代码审查报告

## 项目概览

| 字段 | 值 |
|---|---|
| 项目名称 | 示例项目 |
| 综合得分 | 85.0 |

## 八性评分明细

| 维度 | 得分 |
|---|---|
| 1. 安全性 | 88 |
| 2. 健壮性 | 82 |
| 3. 性能与效率 | 85 |
| 4. 可读性 | 86 |
| 5. 可维护性 | 84 |
| 6. 可扩展性 | 83 |
| 7. 可测试性 | 80 |
| 8. 规范性 | 90 |

## 问题清单

### [P1] 示例问题
- 位置：`src/app.py:10`
- 描述：示例
"""


class PinyinAndDbTests(unittest.TestCase):
    def test_name_to_pinyin_and_surname_polyphones(self) -> None:
        self.assertEqual(platform_db.name_to_pinyin("王秀丽"), "wangxiuli")
        self.assertEqual(platform_db.name_to_pinyin("李力丁"), "liliding")
        self.assertEqual(platform_db.name_to_pinyin("单涛"), "shantao")
        self.assertEqual(platform_db.name_to_pinyin("樊涌杰"), "fanyongjie")
        self.assertEqual(platform_db.name_to_pinyin("莫明鑫"), "momingxin")

    def test_multi_owner_matching_and_no_email_hijack(self) -> None:
        proj = {"project_id": "p1", "owner": "李力丁、康特"}
        u_li = {"role": "user", "display_name": "李力丁", "username": "liliding", "email": "liliding@shineyue.com"}
        u_kang = {"role": "user", "display_name": "康特", "username": "kangte", "email": "kangte@shineyue.com"}
        # 恶意用户把自己的邮箱改成 liliding@shineyue.com，不得越权匹配李力丁的项目
        u_attacker = {"role": "user", "display_name": "王秀丽", "username": "wangxiuli", "email": "liliding@shineyue.com"}
        self.assertTrue(platform_db.user_owns_project(u_li, proj))
        self.assertTrue(platform_db.user_owns_project(u_kang, proj))
        self.assertFalse(platform_db.user_owns_project(u_attacker, proj))

    def test_db_bootstrap_crud_and_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = platform_db.PlatformDB(Path(tmp) / "platform.db")
            init = db.ensure_initial_superadmin(legacy_token="Sy123456")
            self.assertIsNotNone(init)
            uname, pwd = init  # type: ignore[misc]
            self.assertEqual(uname, "admin")
            self.assertEqual(pwd, "Sy123456")
            # 再次调用不重复创建
            self.assertIsNone(db.ensure_initial_superadmin(legacy_token="Sy123456"))

            admin = db.authenticate("admin", "Sy123456")
            self.assertIsNotNone(admin)
            assert admin is not None
            self.assertEqual(admin["role"], "superadmin")
            self.assertTrue(admin["must_change_password"])

            # 禁止删除或降级最后一个超级管理员
            with self.assertRaises(ValueError):
                db.update_user(admin["id"], role="admin")
            with self.assertRaises(ValueError):
                db.update_user(admin["id"], is_active=False)
            with self.assertRaises(ValueError):
                db.delete_user(admin["id"])

            # 预览批量生成：格式 <姓名全拼>@shineyue.com，自动拆分多负责人与去重
            preview = db.preview_users_from_owners(
                ["王秀丽", "李力丁、康特", "王秀丽"], email_domain="shineyue.com"
            )
            by_name = {item["display_name"]: item for item in preview}
            self.assertEqual(set(by_name.keys()), {"王秀丽", "李力丁", "康特"})
            self.assertEqual(by_name["王秀丽"]["username"], "wangxiuli")
            self.assertEqual(by_name["王秀丽"]["email"], "wangxiuli@shineyue.com")
            self.assertFalse(by_name["王秀丽"]["exists"])

            created = db.create_user(
                username="wangxiuli",
                display_name="王秀丽",
                role="user",
                password="user1234",
                email="wangxiuli@shineyue.com",
            )
            self.assertEqual(created["role"], "user")
            preview2 = db.preview_users_from_owners(["王秀丽"], email_domain="shineyue.com")
            self.assertTrue(preview2[0]["exists"])


class _HttpClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, method: str, path: str, body: dict | None = None,
                headers: dict[str, str] | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
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


class WebRbacIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.output_dir = self.root / "output"
        self.projects_csv = self.root / "projects.csv"
        self.settings_path = self.root / "settings.json"
        self.settings_path.write_text(json.dumps({"output_dir": str(self.output_dir)}), encoding="utf-8")

        # 构造两个项目：proj-a 属王秀丽，proj-b 属李力丁、康特
        with self.projects_csv.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["project_id", "project_name", "department", "owner", "repo_url", "branch", "status"])
            w.writeheader()
            w.writerow({
                "project_id": "proj-a", "project_name": "项目A", "department": "研发一部",
                "owner": "王秀丽", "repo_url": "https://example.com/a.git", "branch": "main", "status": "在研",
            })
            w.writerow({
                "project_id": "proj-b", "project_name": "项目B", "department": "研发二部",
                "owner": "李力丁、康特", "repo_url": "https://example.com/b.git", "branch": "main", "status": "在研",
            })

        # 构造一次历史批次与报告
        run_dir = self.output_dir / "results" / "2026-11-27" / "100000"
        rep_dir = self.output_dir / "reports" / "2026-11-27" / "100000"
        run_dir.mkdir(parents=True, exist_ok=True)
        rep_dir.mkdir(parents=True, exist_ok=True)
        (rep_dir / "proj-a.md").write_text(SAMPLE_REPORT, encoding="utf-8")
        (rep_dir / "proj-b.md").write_text(SAMPLE_REPORT, encoding="utf-8")
        (rep_dir / "_summary.md").write_text("# 全局汇总\n", encoding="utf-8")
        (run_dir / "_run.json").write_text(json.dumps({
            "date": "2026-11-27",
            "run_id": "100000",
            "status": "ok",
            "started_at": "2026-11-27T10:00:00",
            "finished_at": "2026-11-27T10:10:00",
            "projects": [
                {"project_id": "proj-a", "project_name": "项目A", "owner": "王秀丽", "status": "ok",
                 "report_path": str(rep_dir / "proj-a.md"), "report_scores": {"total": 85.0, "security_score": 88.0, "grade": "良好"}},
                {"project_id": "proj-b", "project_name": "项目B", "owner": "李力丁、康特", "status": "ok",
                 "report_path": str(rep_dir / "proj-b.md"), "report_scores": {"total": 72.0, "security_score": 70.0, "grade": "合格"}},
            ],
        }, ensure_ascii=False), encoding="utf-8")

        self.db = platform_db.PlatformDB(self.output_dir / "state" / "platform.db")
        self.db.ensure_initial_superadmin(legacy_token="InitSuper123")
        self.db.create_user(
            username="mgr", display_name="管理员甲", role="admin",
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
            "auth_token": "ApiTokenSecret",
            "db": self.db,
        }
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    def test_unauthenticated_rejected_and_api_token_works(self) -> None:
        anon = _HttpClient(self.base_url)
        code, _ = anon.request("GET", "/api/overview")
        self.assertEqual(code, 401)
        code, status = anon.request("GET", "/api/auth/status")
        self.assertEqual(code, 200)
        self.assertTrue(status["auth_required"])
        self.assertFalse(status["authenticated"])

        # Bearer API Token 兼容脚本访问
        code, ov = anon.request("GET", "/api/overview", headers={"Authorization": "Bearer ApiTokenSecret"})
        self.assertEqual(code, 200)
        self.assertEqual(len(ov["rows"]), 2)

    def test_normal_user_isolation_and_self_scan(self) -> None:
        user_cli = _HttpClient(self.base_url)
        code, login_res = user_cli.request("POST", "/api/auth/login", {"username": "wangxiuli", "password": "UserPass123"})
        self.assertEqual(code, 200)
        self.assertEqual(login_res["user"]["role"], "user")

        # 仅看到自己的项目 proj-a
        code, ov = user_cli.request("GET", "/api/overview")
        self.assertEqual(code, 200)
        self.assertEqual([r["project_id"] for r in ov["rows"]], ["proj-a"])

        code, projs = user_cli.request("GET", "/api/projects")
        self.assertEqual(code, 200)
        self.assertEqual([p["project_id"] for p in projs["projects"]], ["proj-a"])

        code, run_detail = user_cli.request("GET", "/api/runs/2026-11-27/100000")
        self.assertEqual(code, 200)
        self.assertEqual([p["project_id"] for p in run_detail["projects"]], ["proj-a"])
        self.assertFalse(run_detail["has_summary"])

        # 可读本人报告，不可读他人报告或全局汇总
        code, _ = user_cli.request("GET", "/api/reports/2026-11-27/100000/proj-a")
        self.assertEqual(code, 200)
        code, _ = user_cli.request("GET", "/api/reports/2026-11-27/100000/proj-b")
        self.assertEqual(code, 403)
        code, _ = user_cli.request("GET", "/api/reports/2026-11-27/100000/_summary")
        self.assertEqual(code, 403)

        # 不可访问用户管理、审计日志、定时任务
        for endpoint in ("/api/users", "/api/audit-logs", "/api/schedule"):
            code, _ = user_cli.request("GET", endpoint)
            self.assertEqual(code, 403, f"expected 403 on {endpoint}")

        # 个人资料修改：可改邮箱，但不可篡改 display_name 越权访问李力丁的项目
        code, prof = user_cli.request("POST", "/api/me/profile", {
            "display_name": "李力丁",
            "email": "wangxiuli@shineyue.com",
            "email_notify": "risk_only",
        })
        self.assertEqual(code, 200)
        self.assertEqual(prof["user"]["display_name"], "王秀丽")
        self.assertEqual(prof["user"]["email_notify"], "risk_only")

        # 普通用户触发扫描限制：禁止全量/他人/summary_only，允许触发本人项目
        code, _ = user_cli.request("POST", "/api/scans", {"fast": True})
        self.assertEqual(code, 403)
        code, _ = user_cli.request("POST", "/api/scans", {"only": ["proj-b"]})
        self.assertEqual(code, 403)
        code, _ = user_cli.request("POST", "/api/scans", {"only": ["proj-a"], "summary_only": True})
        self.assertEqual(code, 403)

        with mock.patch.object(web_server, "_start_scan_task", return_value={"task_id": "t-own-1", "status": "running", "command": "mock"}) as m_start:
            code, scan_res = user_cli.request("POST", "/api/scans", {"only": ["proj-a"], "fast": True})
            self.assertEqual(code, 202)
            self.assertEqual(scan_res["task_id"], "t-own-1")
            m_start.assert_called_once()

    def test_admin_vs_superadmin_permissions_and_batch_create(self) -> None:
        # 管理员：可看全局总览/汇总/定时，可管理普通用户（user），禁止操作管理员/超管或看审计日志
        mgr_cli = _HttpClient(self.base_url)
        code, _ = mgr_cli.request("POST", "/api/auth/login", {"username": "mgr", "password": "AdminPass123"})
        self.assertEqual(code, 200)

        code, ov = mgr_cli.request("GET", "/api/overview")
        self.assertEqual(code, 200)
        self.assertEqual(len(ov["rows"]), 2)
        code, _ = mgr_cli.request("GET", "/api/reports/2026-11-27/100000/_summary")
        self.assertEqual(code, 200)
        code, _ = mgr_cli.request("GET", "/api/schedule")
        self.assertEqual(code, 200)
        code, owners = mgr_cli.request("GET", "/api/users?scope=owners")
        self.assertEqual(code, 200)
        self.assertGreaterEqual(len(owners["users"]), 2)
        code, all_users = mgr_cli.request("GET", "/api/users")
        self.assertEqual(code, 200)
        users_by_name = {u["username"]: u for u in all_users["users"]}
        code, _ = mgr_cli.request("GET", "/api/audit-logs")
        self.assertEqual(code, 403)

        # 管理员创建/编辑/重置/删除普通用户均应成功
        code, created_u = mgr_cli.request("POST", "/api/users", {
            "username": "tempuser", "display_name": "临时负责人", "role": "user",
            "password": "TempPass123", "email": "tempuser@shineyue.com",
        })
        self.assertEqual(code, 201)
        temp_uid = created_u["user"]["id"]
        code, _ = mgr_cli.request("POST", f"/api/users/{temp_uid}/update", {"email": "temp2@shineyue.com"})
        self.assertEqual(code, 200)
        # 禁止将普通用户提权为管理员
        code, _ = mgr_cli.request("POST", f"/api/users/{temp_uid}/update", {"role": "admin"})
        self.assertEqual(code, 403)
        # 禁止新建管理员或超管
        code, _ = mgr_cli.request("POST", "/api/users", {
            "username": "badadmin", "display_name": "越权管理员", "role": "admin",
            "password": "TempPass123", "email": "badadmin@shineyue.com",
        })
        self.assertEqual(code, 403)
        # 禁止修改/重置/删除超管账号
        super_uid = users_by_name["admin"]["id"]
        self.assertEqual(mgr_cli.request("POST", f"/api/users/{super_uid}/update", {"display_name": "改名"})[0], 403)
        self.assertEqual(mgr_cli.request("POST", f"/api/users/{super_uid}/reset-password", {"new_password": "HackPass123"})[0], 403)
        self.assertEqual(mgr_cli.request("POST", f"/api/users/{super_uid}/delete")[0], 403)
        # 管理员可正常删除普通用户
        self.assertEqual(mgr_cli.request("POST", f"/api/users/{temp_uid}/delete")[0], 200)
        # 管理员批量生成时禁止混入 admin 角色
        code, _ = mgr_cli.request("POST", "/api/users/batch-create", {
            "items": [{"username": "badmgr", "display_name": "坏管理", "email": "badmgr@shineyue.com", "role": "admin"}],
            "default_password": "InitUser123",
        })
        self.assertEqual(code, 403)

        # 超级管理员：可预览并批量从项目负责人生成账号、查看审计日志
        sup_cli = _HttpClient(self.base_url)
        code, _ = sup_cli.request("POST", "/api/auth/login", {"username": "admin", "password": "InitSuper123"})
        self.assertEqual(code, 200)

        code, prev = sup_cli.request("POST", "/api/users/preview-from-projects", {"email_domain": "shineyue.com"})
        self.assertEqual(code, 200)
        to_create = [x for x in prev["items"] if not x["exists"]]
        self.assertEqual({x["username"] for x in to_create}, {"liliding", "kangte"})
        self.assertTrue(all(x["email"].endswith("@shineyue.com") for x in to_create))

        code, batch = sup_cli.request("POST", "/api/users/batch-create", {
            "items": to_create,
            "default_password": "InitUser123",
        })
        self.assertEqual(code, 200)
        self.assertEqual(len(batch["created"]), 2)

        code, logs = sup_cli.request("GET", "/api/audit-logs?limit=20")
        self.assertEqual(code, 200)
        actions = [entry["action"] for entry in logs["logs"]]
        self.assertIn("batch_create_users", actions)

    def test_delete_run_permissions_and_state_rollback(self) -> None:
        # 新增一个由 wangxiuli 触发的测试批次（比 100000 更新），并将 state/proj-a.json 指向它
        test_run_dir = self.output_dir / "results" / "2026-11-27" / "110000-test"
        test_rep_dir = self.output_dir / "reports" / "2026-11-27" / "110000-test"
        test_run_dir.mkdir(parents=True, exist_ok=True)
        test_rep_dir.mkdir(parents=True, exist_ok=True)
        (test_rep_dir / "proj-a.md").write_text(SAMPLE_REPORT, encoding="utf-8")
        (test_run_dir / "_run.json").write_text(json.dumps({
            "date": "2026-11-27",
            "run_id": "110000-test",
            "status": "complete",
            "triggered_by": "wangxiuli",
            "trigger": "manual",
            "started_at": "2026-11-27T11:00:00",
            "finished_at": "2026-11-27T11:02:00",
            "projects": [
                {"project_id": "proj-a", "project_name": "项目A", "owner": "王秀丽", "status": "ok",
                 "commit": "newcommit999", "report_path": str(test_rep_dir / "proj-a.md"),
                 "report_scores": {"total": 91.0, "security_score": 92.0, "grade": "优秀"}},
            ],
        }, ensure_ascii=False), encoding="utf-8")

        state_file = self.output_dir / "state" / "proj-a.json"
        state_file.write_text(json.dumps({
            "last_commit": "newcommit999",
            "last_report": str(test_rep_dir / "proj-a.md"),
            "last_scan_date": "2026-11-27",
            "report_mode": "standard",
            "scores": {"total": 91.0, "security_score": 92.0, "grade": "优秀"},
        }, ensure_ascii=False), encoding="utf-8")

        # 再构造一个运行中批次（即使是超管也不能直接删除）
        running_dir = self.output_dir / "results" / "2026-11-27" / "120000-run"
        running_dir.mkdir(parents=True, exist_ok=True)
        (running_dir / "_run.json").write_text(json.dumps({
            "date": "2026-11-27",
            "run_id": "120000-run",
            "status": "running",
            "triggered_by": "wangxiuli",
            "started_at": "2026-11-27T12:00:00",
            "projects": [],
            "pending_projects": ["proj-a"],
        }, ensure_ascii=False), encoding="utf-8")

        user_cli = _HttpClient(self.base_url)
        self.assertEqual(user_cli.request("POST", "/api/auth/login", {"username": "wangxiuli", "password": "UserPass123"})[0], 200)

        # 普通用户看列表：自己的完成批次 can_delete=True，系统/他人批次 100000 can_delete=False，运行中 can_delete=False
        code, runs_res = user_cli.request("GET", "/api/runs")
        self.assertEqual(code, 200)
        by_id = {r["run_id"]: r for r in runs_res["runs"]}
        self.assertTrue(by_id["110000-test"]["can_delete"])
        self.assertFalse(by_id["100000"]["can_delete"])
        self.assertFalse(by_id["120000-run"]["can_delete"])

        # 普通用户尝试删除非自己触发的批次 100000 -> 403
        code, _ = user_cli.request("POST", "/api/runs/2026-11-27/100000/delete")
        self.assertEqual(code, 403)

        # 普通用户尝试删除运行中批次 -> 409
        code, _ = user_cli.request("POST", "/api/runs/2026-11-27/120000-run/delete")
        self.assertEqual(code, 409)

        # 普通用户删除自己触发的测试批次 -> 200，目录清除，且 state/proj-a.json 自动回退到 100000 批次
        code, del_res = user_cli.request("POST", "/api/runs/2026-11-27/110000-test/delete")
        self.assertEqual(code, 200)
        self.assertIn("proj-a", del_res["rebuilt_states"])
        self.assertFalse(test_run_dir.exists())
        self.assertFalse(test_rep_dir.exists())
        rolled = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(Path(rolled["last_report"]).resolve(), (self.output_dir / "reports" / "2026-11-27" / "100000" / "proj-a.md").resolve())
        self.assertEqual(rolled["scores"]["total"], 85.0)

        # 管理员可以删除历史/他人批次 100000 -> 200，无更早历史则 state/proj-a.json 被清理；但仍不能删 running 批次
        mgr_cli = _HttpClient(self.base_url)
        self.assertEqual(mgr_cli.request("POST", "/api/auth/login", {"username": "mgr", "password": "AdminPass123"})[0], 200)
        self.assertEqual(mgr_cli.request("POST", "/api/runs/2026-11-27/120000-run/delete")[0], 409)
        code, del2 = mgr_cli.request("POST", "/api/runs/2026-11-27/100000/delete")
        self.assertEqual(code, 200)
        self.assertIn("proj-a", del2["cleared_states"])
        self.assertFalse(state_file.exists())


if __name__ == "__main__":
    unittest.main()
