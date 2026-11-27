"""Phase 4 回归测试：SMTP 配置隔离、按负责人聚合发信（含 .docx/.zip 附件）、投递日志与重试。"""
from __future__ import annotations

import http.cookiejar
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from email.message import EmailMessage
from http.server import ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runner"))

import mailer  # noqa: E402
import platform_db  # noqa: E402
import scoring  # noqa: E402
import web_server  # noqa: E402


SAMPLE_MD = """# 项目审查报告

## 评分总览

| 维度 | 得分 |
|---|---|
| 1. 功能适合性 | 8.5 |
| 6. 安全性 | 8.8 |
"""


class _FakeSmtp:
    def __init__(self, sent_box: list[EmailMessage], fail_once: bool = False) -> None:
        self.sent_box = sent_box
        self.fail_once = fail_once
        self._failed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def send_message(self, msg: EmailMessage) -> None:
        if self.fail_once and not self._failed:
            self._failed = True
            raise RuntimeError("transient smtp 421")
        self.sent_box.append(msg)


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


class MailerPackingTests(unittest.TestCase):
    def test_pack_more_than_six_attachments_into_zip(self) -> None:
        raw = [(f"r{i}.docx", b"PK\x03\x04dummy" * 10, "application", "octet-stream") for i in range(8)]
        packed = mailer._pack_attachments_if_needed(raw, zip_name="bundle.zip", max_attach_mb=15)
        self.assertEqual(len(packed), 1)
        fname, zbytes, maintype, subtype = packed[0]
        self.assertEqual((fname, maintype, subtype), ("bundle.zip", "application", "zip"))
        with zipfile.ZipFile(BytesIO(zbytes)) as zf:
            self.assertEqual(len(zf.namelist()), 8)


class Phase4EmailIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.output_dir = self.root / "output"
        self.projects_csv = self.root / "projects.csv"
        self.projects_csv.write_text("project_id,project_name,department,owner,git_url,branch,status\n", encoding="utf-8")
        self.settings_path = self.root / "settings.json"
        self.settings_path.write_text(json.dumps({"output_dir": str(self.output_dir)}), encoding="utf-8")

        self.db = platform_db.PlatformDB(self.output_dir / "state" / "platform.db")
        self.db.ensure_initial_superadmin(legacy_token="SuperPass123")
        self.db.create_user(
            username="mgr", display_name="管理员甲", role="admin",
            password="AdminPass123", email="mgr@shineyue.com", must_change_password=False,
        )
        self.db.create_user(
            username="wangxiuli", display_name="王秀丽", role="user",
            password="UserPass123", email="wangxiuli@shineyue.com",
            must_change_password=False, email_notify="batch",
        )
        self.db.create_user(
            username="liliding", display_name="李力丁", role="user",
            password="UserPass123", email="liliding@shineyue.com",
            must_change_password=False, email_notify="risk_only",
        )
        self.db.create_user(
            username="kangte", display_name="康特", role="user",
            password="UserPass123", email="kangte@shineyue.com",
            must_change_password=False, email_notify="off",
        )

        # 构造一个含 3 个项目的批次：王秀丽有 2 个项目，李力丁/康特共有 1 个健康项目
        run_dir = self.output_dir / "results" / "2026-11-27" / "100000"
        rep_dir = self.output_dir / "reports" / "2026-11-27" / "100000"
        run_dir.mkdir(parents=True, exist_ok=True)
        rep_dir.mkdir(parents=True, exist_ok=True)
        for pid in ("p-w1", "p-w2", "p-lk"):
            (rep_dir / f"{pid}.md").write_text(SAMPLE_MD, encoding="utf-8")
        (rep_dir / "_summary.md").write_text("# 汇总报告\n\n整体良好。\n", encoding="utf-8")
        (run_dir / "_run.json").write_text(json.dumps({
            "date": "2026-11-27",
            "run_id": "100000",
            "status": "complete",
            "projects": [
                {"project_id": "p-w1", "project_name": "订单中心", "owner": "王秀丽", "status": "ok",
                 "report_path": str(rep_dir / "p-w1.md"), "report_scores": {"total": 8.2, "security_score": 8.5, "grade": "良好", "security_p0": 0}},
                {"project_id": "p-w2", "project_name": "支付网关", "owner": "王秀丽", "status": "ok",
                 "report_path": str(rep_dir / "p-w2.md"), "report_scores": {"total": 7.4, "security_score": 7.0, "grade": "基本合格需改进", "security_p0": 1}},
                {"project_id": "p-lk", "project_name": "基础组件", "owner": "李力丁、康特", "status": "ok",
                 "report_path": str(rep_dir / "p-lk.md"), "report_scores": {"total": 8.8, "security_score": 9.0, "grade": "良好", "security_p0": 0}},
            ],
        }, ensure_ascii=False), encoding="utf-8")

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

    def test_smtp_rbac_batch_aggregation_attachments_and_retry(self) -> None:
        # 普通用户与管理员无权配置 SMTP；普通用户无权看投递日志或触发批次群发
        mgr = _Client(self.base_url)
        self.assertEqual(mgr.request("POST", "/api/auth/login", {"username": "mgr", "password": "AdminPass123"})[0], 200)
        self.assertEqual(mgr.request("GET", "/api/ops/smtp")[0], 403)
        self.assertEqual(mgr.request("POST", "/api/ops/smtp", {"host": "smtp.x.com"})[0], 403)
        self.assertEqual(mgr.request("POST", "/api/ops/smtp/test", {"to_email": "a@shineyue.com"})[0], 403)

        user = _Client(self.base_url)
        self.assertEqual(user.request("POST", "/api/auth/login", {"username": "wangxiuli", "password": "UserPass123"})[0], 200)
        self.assertEqual(user.request("GET", "/api/email-logs")[0], 403)
        self.assertEqual(user.request("POST", "/api/runs/2026-11-27/100000/send-emails", {})[0], 403)

        # 超管配置 SMTP（密码脱敏且不入审计日志）
        sup = _Client(self.base_url)
        self.assertEqual(sup.request("POST", "/api/auth/login", {"username": "admin", "password": "SuperPass123"})[0], 200)
        code, smtp_res = sup.request("POST", "/api/ops/smtp", {
            "enabled": True,
            "host": "smtp.shineyue.com",
            "port": 465,
            "security": "ssl",
            "username": "audit@shineyue.com",
            "password": "SmtpSecret#8899",
            "from_email": "audit@shineyue.com",
            "attachment_format": "docx",
            "portal_url": "http://10.0.87.179:8787",
            "summary_cc_emails": "leader@shineyue.com",
        })
        self.assertEqual(code, 200)
        self.assertNotIn("SmtpSecret#8899", json.dumps(smtp_res))
        self.assertTrue(smtp_res["smtp"]["password_set"])

        # 测试邮件发送（带示例 .docx 附件）
        sent_box: list[EmailMessage] = []
        with mock.patch.object(mailer, "_open_smtp", return_value=_FakeSmtp(sent_box)):
            code, tres = sup.request("POST", "/api/ops/smtp/test", {"to_email": "ops@shineyue.com"})
        self.assertEqual(code, 200)
        self.assertTrue(tres["ok"])
        self.assertEqual(len(sent_box), 1)
        atts = [part.get_filename() for part in sent_box[0].iter_attachments()]
        self.assertEqual(atts, ["测试报告示例.docx"])

        # 管理员触发批次群发：
        # - 王秀丽 (batch) 聚合收到 1 封含 2 个 .docx 附件的邮件
        # - 李力丁 (risk_only, 项目健康) 跳过
        # - 康特 (off) 跳过
        # - 管理员甲 (非任何项目 owner) 不收负责人邮件
        # - 管理层 leader@shineyue.com 收到 1 封带汇总 Word 附件的邮件
        batch_box: list[EmailMessage] = []
        with mock.patch.object(mailer, "_open_smtp", return_value=_FakeSmtp(batch_box)):
            code, dres = mgr.request("POST", "/api/runs/2026-11-27/100000/send-emails", {})
        self.assertEqual(code, 200)
        self.assertEqual(dres["sent"], 2)
        self.assertEqual(dres["failed"], 0)
        self.assertEqual(dres["skipped"], 2)
        self.assertEqual(len(batch_box), 2)

        owner_msg = next(m for m in batch_box if "wangxiuli@shineyue.com" in m["To"])
        owner_atts = [part.get_filename() for part in owner_msg.iter_attachments()]
        self.assertEqual(len(owner_atts), 2)
        self.assertTrue(all(a and a.endswith(".docx") for a in owner_atts))

        summary_msg = next(m for m in batch_box if "leader@shineyue.com" in m["To"])
        summary_atts = [part.get_filename() for part in summary_msg.iter_attachments()]
        self.assertEqual(summary_atts, ["代码检查分析报告汇总_2026-11-27_100000.docx"])

        # 模拟一次发送失败 -> 日志记录 failed -> 点击重发成功
        fail_box: list[EmailMessage] = []
        with mock.patch.object(mailer, "_open_smtp", return_value=_FakeSmtp(fail_box, fail_once=True)):
            code, fres = mgr.request("POST", "/api/runs/2026-11-27/100000/send-emails", {"recipient_email": "wangxiuli@shineyue.com"})
        self.assertEqual(code, 200)
        self.assertEqual(fres["failed"], 1)
        failed_log_id = fres["details"][0]["id"]

        retry_box: list[EmailMessage] = []
        with mock.patch.object(mailer, "_open_smtp", return_value=_FakeSmtp(retry_box)):
            code, rres = mgr.request("POST", f"/api/email-logs/{failed_log_id}/retry")
        self.assertEqual(code, 200)
        self.assertEqual(rres["sent"], 1)
        self.assertEqual(len(retry_box), 1)

        # 审计日志中无 SMTP 明文密码
        code, logs = sup.request("GET", "/api/audit-logs?limit=50")
        self.assertEqual(code, 200)
        self.assertNotIn("SmtpSecret#8899", json.dumps(logs, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
