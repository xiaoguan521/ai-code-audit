"""专项集成测试：问题整改与误报豁免、批量改负责人/部门、磁盘观测与清理、SQLite 热备份下载。"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "runner"))

import platform_db  # noqa: E402
import web_server  # noqa: E402


SAMPLE_REPORT_MD = """# proj-a 八性代码质量审查报告

## 综合评估
- **总评分**：7.8 / 10
- **安全评分**：7.5 / 10

## 维度得分
| 维度 | 得分 | 覆盖率 | 判定统计 | 主要风险 |
| --- | --- | --- | --- | --- |
| 1. 架构合理性 | 8.0 | 100% | PASS 8 | 无 |
| 2. 功能正确性 | 8.0 | 100% | PASS 8 | 无 |
| 3. 健壮性 | 8.0 | 100% | PASS 8 | 无 |
| 4. 安全性 | 7.0 | 100% | FAIL 1, WARN 1 | 鉴权校验 |
| 5. 性能与资源 | 8.0 | 100% | PASS 8 | 无 |
| 6. 可维护性 | 8.0 | 100% | PASS 8 | 无 |
| 7. 可测试性 | 8.0 | 100% | PASS 8 | 无 |
| 8. 可观测性 | 8.0 | 100% | PASS 8 | 无 |

## 风险问题清单
| ID | 等级 | 检查项 | 标题 | 证据 | 影响 | 修复建议 | 工作量 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| ISS-1 | P0 | 4.1 | 内网接口缺少鉴权注解 | `src/Api.java:42` | 越权访问风险 | 补充鉴权拦截器 | 0.5d |
| ISS-2 | P1 | 4.3 | 日志打印疑似敏感参数 | `src/Auth.java:88` | 信息泄露 | 脱敏后输出 | 0.5d |
"""


class _Client:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def request_raw(self, method: str, path: str, body: dict | None = None) -> tuple[int, bytes]:
        data = None
        headers: dict[str, str] = {}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=15) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as err:
            return err.code, err.read()

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        code, raw = self.request_raw(method, path, body)
        return code, (json.loads(raw.decode("utf-8")) if raw else {})


class OpsTriageStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.output_dir = self.root / "output"
        self.workdir = self.root / "workdir"
        self.output_dir.mkdir(parents=True)
        self.workdir.mkdir(parents=True)

        self.settings_path = self.root / "settings.json"
        self.settings_path.write_text(
            json.dumps({"workdir": str(self.workdir), "output_dir": str(self.output_dir)}),
            encoding="utf-8",
        )
        self.projects_csv = self.root / "projects.csv"
        self.projects_csv.write_text(
            "project_id,project_name,department,owner,vcs_type,git_url,branch,status\n"
            "proj-a,项目A,研发一部,王秀丽,git,https://git.example.com/a.git,main,在研\n"
            "proj-b,项目B,研发二部,李明,git,https://git.example.com/b.git,main,在研\n"
            "proj-stopped,停用项目,研发一部,王秀丽,git,https://git.example.com/old.git,main,停用\n",
            encoding="utf-8",
        )

        # 构造本地工作副本（含活跃、停用与孤儿目录）
        for name, size in (("proj-a", 1200), ("proj-stopped", 2400), ("proj-orphan", 3600)):
            d = self.workdir / name / "repo"
            d.mkdir(parents=True)
            (d / "blob.bin").write_bytes(b"x" * size)

        # 构造多个历史批次与报告，其中最新批次及 state 引用批次受保护，老批次可被清理
        self._seed_run("2025-01-01", "old1", include_report=True)
        self._seed_run("2025-01-02", "old2", include_report=True)
        self._seed_run("2026-11-20", "cur1", include_report=True)
        self._seed_run("2026-11-21", "cur2", include_report=True)
        self._seed_run("2026-11-22", "cur3", include_report=True)
        self._seed_run("2026-11-23", "cur4", include_report=True)
        self._seed_run("2026-11-24", "cur5", include_report=True)

        # state 引用 old2（应受保护），old1 应被 prune 清理
        state_dir = self.output_dir / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "proj-b.json").write_text(
            json.dumps({"last_report": str(self.output_dir / "reports" / "2025-01-02" / "old2" / "proj-a.md")}),
            encoding="utf-8",
        )

        self.db = platform_db.PlatformDB(state_dir / "platform.db")
        self.db.create_user(username="admin", password="SuperPassword123", display_name="超管", role="superadmin", email="admin@shineyue.com")
        self.db.create_user(username="mgr", password="AdminPass123", display_name="管理员", role="admin", email="mgr@shineyue.com")
        self.db.create_user(username="wangxiuli", password="UserPass123", display_name="王秀丽", role="user", email="wangxiuli@shineyue.com")
        self.db.seed_projects_from_csv(self.projects_csv, self.output_dir / "state" / "projects.managed.csv")

        checklist = json.loads((ROOT / "config" / "checklist.json").read_text(encoding="utf-8"))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web_server.AuditHandler)
        self.server.cfg = {  # type: ignore[attr-defined]
            "output_dir": self.output_dir,
            "projects_csv": self.projects_csv,
            "settings_path": self.settings_path,
            "checklist": checklist,
            "dist_dir": self.root / "empty-dist",
            "auth_token": "",
            "scan_statuses": ["在研"],
            "db": self.db,
        }
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _seed_run(self, date_str: str, run_id: str, include_report: bool = False) -> None:
        res_dir = self.output_dir / "results" / date_str / run_id
        rep_dir = self.output_dir / "reports" / date_str / run_id
        res_dir.mkdir(parents=True, exist_ok=True)
        rep_dir.mkdir(parents=True, exist_ok=True)
        if include_report:
            (rep_dir / "proj-a.md").write_text(SAMPLE_REPORT_MD, encoding="utf-8")
        run_meta = {
            "date": date_str,
            "run_id": run_id,
            "status": "complete",
            "projects": [
                {
                    "project_id": "proj-a",
                    "project_name": "项目A",
                    "department": "研发一部",
                    "owner": "王秀丽",
                    "status": "ok",
                    "report_scores": {"total": 7.8, "security_score": 7.5, "grade": "良好", "security_p0": 1},
                }
            ],
        }
        (res_dir / "_run.json").write_text(json.dumps(run_meta, ensure_ascii=False), encoding="utf-8")

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def test_issue_triage_exemption_and_rbac(self) -> None:
        # 稳定指纹忽略行号偏移
        fp1 = platform_db.compute_issue_fingerprint("4.1", "内网接口缺少鉴权注解", "`src/Api.java:42`")
        fp2 = platform_db.compute_issue_fingerprint("4.1", "内网接口缺少鉴权注解", "`src/Api.java:99-105`")
        self.assertEqual(fp1, fp2)

        user = _Client(self.base_url)
        self.assertEqual(user.request("POST", "/api/auth/login", {"username": "wangxiuli", "password": "UserPass123"})[0], 200)

        code, rep = user.request("GET", "/api/reports/2026-11-24/cur5/proj-a")
        self.assertEqual(code, 200)
        self.assertEqual(len(rep["issues"]), 2)
        iss1 = rep["issues"][0]
        self.assertEqual(iss1["triage_status"], "open")

        # 豁免未填原因应返回 400
        code, _ = user.request("POST", "/api/issues/triage", {
            "project_id": "proj-a",
            "fingerprint": iss1["fingerprint"],
            "status": "exempted",
            "reason": "",
        })
        self.assertEqual(code, 400)

        # 普通用户不能处置非本人负责项目
        code, _ = user.request("POST", "/api/issues/triage", {
            "project_id": "proj-b",
            "fingerprint": iss1["fingerprint"],
            "status": "planned",
            "reason": "排期",
        })
        self.assertEqual(code, 403)

        # 标记 ISS-1 为误报豁免
        code, saved = user.request("POST", "/api/issues/triage", {
            "project_id": "proj-a",
            "fingerprint": iss1["fingerprint"],
            "item_id": iss1["item_id"],
            "level": iss1["level"],
            "title": iss1["title"],
            "evidence_text": iss1["evidence_text"],
            "status": "exempted",
            "reason": "内网定时回调接口由网关统一鉴权",
        })
        self.assertEqual(code, 200)
        self.assertEqual(saved["triage"]["status"], "exempted")

        # 重新读取报告：有效 P0 降为 0，triage_summary.exempted == 1
        code, rep2 = user.request("GET", "/api/reports/2026-11-24/cur5/proj-a")
        self.assertEqual(code, 200)
        self.assertEqual(rep2["triage_summary"]["exempted"], 1)
        self.assertEqual(rep2["effective_issue_counts"]["P0"], 0)
        self.assertEqual(rep2["effective_issue_counts"]["P1"], 1)

        code, ov = user.request("GET", "/api/overview")
        self.assertEqual(code, 200)
        row_a = next(r for r in ov["rows"] if r["project_id"] == "proj-a")
        self.assertEqual(row_a["exempted_issues"], 1)

    def test_batch_update_owner_and_department(self) -> None:
        mgr = _Client(self.base_url)
        user = _Client(self.base_url)
        self.assertEqual(mgr.request("POST", "/api/auth/login", {"username": "mgr", "password": "AdminPass123"})[0], 200)
        self.assertEqual(user.request("POST", "/api/auth/login", {"username": "wangxiuli", "password": "UserPass123"})[0], 200)

        # 普通用户禁止批量修改
        code, _ = user.request("POST", "/api/projects/batch-update", {
            "project_ids": ["proj-a"], "owner": "张三", "owner_mode": "replace"
        })
        self.assertEqual(code, 403)

        # 追加负责人 + 修改部门
        code, res = mgr.request("POST", "/api/projects/batch-update", {
            "project_ids": ["proj-a", "proj-b"],
            "department": "平台研发部",
            "owner": "张三",
            "owner_mode": "append",
        })
        self.assertEqual(code, 200)
        self.assertEqual(res["updated"], 2)
        pa = self.db.get_project("proj-a")
        self.assertEqual(pa["department"], "平台研发部")
        self.assertEqual(pa["owner"], "王秀丽、张三")

        # 移除原负责人（模拟离职交接）
        code, res2 = mgr.request("POST", "/api/projects/batch-update", {
            "project_ids": ["proj-a"],
            "owner": "王秀丽",
            "owner_mode": "remove",
        })
        self.assertEqual(code, 200)
        self.assertEqual(self.db.get_project("proj-a")["owner"], "张三")

    def test_storage_overview_cleanup_and_hot_backup(self) -> None:
        sa = _Client(self.base_url)
        mgr = _Client(self.base_url)
        self.assertEqual(sa.request("POST", "/api/auth/login", {"username": "admin", "password": "SuperPassword123"})[0], 200)
        self.assertEqual(mgr.request("POST", "/api/auth/login", {"username": "mgr", "password": "AdminPass123"})[0], 200)

        # 非超管禁止访问存储与热备份接口
        self.assertEqual(mgr.request("GET", "/api/ops/storage")[0], 403)
        self.assertEqual(mgr.request_raw("GET", "/api/ops/db-backup")[0], 403)

        # 超管查看存储统计：应识别 proj-stopped 与 proj-orphan 为可清理仓库
        code, st = sa.request("GET", "/api/ops/storage")
        self.assertEqual(code, 200)
        reclaim_ids = {it["project_id"] for it in st["reclaimable_repos"]["items"]}
        self.assertEqual(reclaim_ids, {"proj-stopped", "proj-orphan"})
        self.assertEqual(st["runs_summary"]["total_runs"], 7)

        # 清理残留仓库：不删除活跃 proj-a
        code, cl = sa.request("POST", "/api/ops/storage/cleanup", {"action": "clean_inactive_repos"})
        self.assertEqual(code, 200)
        self.assertEqual(cl["result"]["removed_count"], 2)
        self.assertTrue((self.workdir / "proj-a").is_dir())
        self.assertFalse((self.workdir / "proj-stopped").exists())
        self.assertFalse((self.workdir / "proj-orphan").exists())

        # 保存保留策略并清理过期批次：保留最新 5 个，old1 清理，old2 因 state 引用被保护
        code, pol = sa.request("POST", "/api/ops/storage/cleanup", {
            "action": "save_policy", "keep_days": 30, "keep_latest": 5
        })
        self.assertEqual(code, 200)
        self.assertEqual(pol["storage"]["retention_policy"], {"keep_days": 30, "keep_latest": 5})

        code, pr = sa.request("POST", "/api/ops/storage/cleanup", {
            "action": "prune_runs", "keep_days": 30, "keep_latest": 5
        })
        self.assertEqual(code, 200)
        self.assertEqual(pr["result"]["removed_count"], 1)
        self.assertEqual(pr["result"]["skipped_protected"], 1)
        self.assertFalse((self.output_dir / "results" / "2025-01-01" / "old1").exists())
        self.assertTrue((self.output_dir / "results" / "2025-01-02" / "old2").is_dir())

        # 下载 SQLite 在线热备份并校验文件头与表完整性
        code, raw_db = sa.request_raw("GET", "/api/ops/db-backup")
        self.assertEqual(code, 200)
        self.assertTrue(raw_db.startswith(b"SQLite format 3\x00"))
        bak_path = self.root / "downloaded.db"
        bak_path.write_bytes(raw_db)
        conn = sqlite3.connect(str(bak_path))
        try:
            cnt = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
            self.assertEqual(cnt, 3)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
