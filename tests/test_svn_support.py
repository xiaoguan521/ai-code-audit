"""SVN 仓库支持单元测试：地址识别、凭据匹配脱敏、探测、同步/增量 diff、代码地图与平台 DB/API。"""
from __future__ import annotations

import datetime as dt
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "runner"))

import code_map  # noqa: E402
import facts  # noqa: E402
import platform_db  # noqa: E402
import run_daily  # noqa: E402
import vcs  # noqa: E402
import web_server  # noqa: E402


SAMPLE_SVN_URL = "https://svn.sjgjj.cn/svn/CustomerProject/YUN1.0/ShineYueYUN1.0/Working/SJPT/4.Src"

SAMPLE_SVN_INFO_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<info>
<entry path="4.Src" revision="4821" kind="dir">
<url>{SAMPLE_SVN_URL}</url>
<commit revision="4819">
<author>zhangsan</author>
<date>2026-10-15T08:30:00.000000Z</date>
</commit>
</entry>
</info>
"""

SAMPLE_SVN_LOG_XML = """<?xml version="1.0" encoding="UTF-8"?>
<log>
<logentry revision="4819">
<author>zhangsan</author>
<date>2026-10-15T08:30:00.000000Z</date>
<msg>fix: 修复社保接口超时问题</msg>
</logentry>
</log>
"""

SAMPLE_SVN_DIFF_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<diff>
<paths>
<path item="modified" props="none" kind="file">{SAMPLE_SVN_URL}/src/main/java/App.java</path>
<path item="modified" props="none" kind="dir">{SAMPLE_SVN_URL}/src/main/java</path>
<path item="added" props="none" kind="file">{SAMPLE_SVN_URL}/pom.xml</path>
</paths>
</diff>
"""


class SvnSupportUnitTests(unittest.TestCase):
    def test_detect_validate_and_compose_svn_url(self) -> None:
        self.assertEqual(vcs.detect_vcs_type(SAMPLE_SVN_URL), "svn")
        self.assertEqual(vcs.detect_vcs_type("svn://10.0.0.10/repo/proj"), "svn")
        self.assertEqual(vcs.detect_vcs_type("https://git.sjgjj.cn/team/app.git"), "git")
        self.assertEqual(vcs.detect_vcs_type("https://code.internal/legacy", "svn"), "svn")

        self.assertEqual(vcs.validate_repo_url(SAMPLE_SVN_URL, "svn"), SAMPLE_SVN_URL)
        self.assertEqual(vcs.compose_svn_url(SAMPLE_SVN_URL, ""), SAMPLE_SVN_URL)
        self.assertEqual(vcs.compose_svn_url("https://svn.sjgjj.cn/svn/app/", "trunk"), "https://svn.sjgjj.cn/svn/app/trunk")

        with self.assertRaises(ValueError):
            vcs.validate_repo_url("file:///etc/passwd", "svn")
        with self.assertRaises(ValueError):
            vcs.compose_svn_url(SAMPLE_SVN_URL, "../secret")

    def test_credential_matching_and_secret_redaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cred = Path(tmp) / "git-credentials"
            cred.write_text(
                "https://generic:oldpass@svn.sjgjj.cn\n"
                "https://audit_bot:P%40ssword!@svn.sjgjj.cn/svn/CustomerProject\n"
                "svn://svnuser:s3cret@10.0.0.10:3690/proj\n",
                encoding="utf-8",
            )
            matched = vcs.match_credential_for_url(SAMPLE_SVN_URL, credential_file=cred)
            self.assertEqual(matched, ("audit_bot", "P@ssword!"))

            cmd, secret = vcs.svn_base_cmd(SAMPLE_SVN_URL, credential_file=cred)
            self.assertEqual(secret, "P@ssword!")
            self.assertIn("--non-interactive", cmd)
            self.assertIn("--no-auth-cache", cmd)
            self.assertTrue(any(a.startswith("--trust-server-cert-failures=") for a in cmd))
            self.assertIn("audit_bot", cmd)
            self.assertEqual(
                vcs.redact_secret("failed with P@ssword! and P%40ssword!", secret),
                "failed with *** and ***",
            )

            svn_proto = vcs.match_credential_for_url("svn://10.0.0.10:3690/proj/trunk", credential_file=cred)
            self.assertEqual(svn_proto, ("svnuser", "s3cret"))

    def test_probe_svn_direct_dir_and_standard_branches(self) -> None:
        def fake_direct(cmd, **kwargs):
            if "info" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=SAMPLE_SVN_INFO_XML, stderr="")
            if "list" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="src/\npom.xml\n", stderr="")
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unexpected")

        with mock.patch("vcs.subprocess.run", side_effect=fake_direct):
            res = platform_db.probe_git_remote(SAMPLE_SVN_URL, branch="", vcs_type="svn")
        self.assertTrue(res["ok"])
        self.assertEqual(res["vcs_type"], "svn")
        self.assertEqual(res["revision"], "r4819")
        self.assertEqual(res["branches"], [])
        self.assertTrue(res["branch_exists"])

        def fake_std(cmd, **kwargs):
            if "info" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=SAMPLE_SVN_INFO_XML, stderr="")
            if "list" in cmd and cmd[-1].endswith("/branches"):
                return subprocess.CompletedProcess(cmd, 0, stdout="release-1.0/\nhotfix-2/\n", stderr="")
            if "list" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="trunk/\nbranches/\ntags/\n", stderr="")
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

        with mock.patch("vcs.subprocess.run", side_effect=fake_std):
            res2 = platform_db.probe_git_remote("https://svn.sjgjj.cn/svn/app", branch="trunk", vcs_type="svn")
        self.assertTrue(res2["ok"])
        self.assertEqual(res2["default_branch"], "trunk")
        self.assertEqual(res2["branches"], ["branches/hotfix-2", "branches/release-1.0", "trunk"])

    def test_svn_sync_changed_files_code_map_facts_and_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"

            def fake_run(cmd, cwd=None, timeout=1800, env=None):
                if cmd[0] == "svn" and "checkout" in cmd:
                    target = Path(cmd[-1])
                    (target / ".svn").mkdir(parents=True, exist_ok=True)
                    (target / "src").mkdir(parents=True, exist_ok=True)
                    (target / "src" / "App.java").write_text("public class App {\n  // TODO check\n}\n", encoding="utf-8")
                    (target / "pom.xml").write_text("<project/>\n", encoding="utf-8")
                    return subprocess.CompletedProcess(cmd, 0, stdout="Checked out revision 4819.\n", stderr="")
                if cmd[0] == "svn" and "diff" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout=SAMPLE_SVN_DIFF_XML, stderr="")
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            def fake_vcs_subproc(cmd, **kwargs):
                if "info" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout=SAMPLE_SVN_INFO_XML, stderr="")
                if "log" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout=SAMPLE_SVN_LOG_XML, stderr="")
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            proj = {"project_id": "sjpt-svn", "vcs_type": "svn", "git_url": SAMPLE_SVN_URL, "branch": ""}
            snap = dt.datetime(2026, 10, 20, 0, 0, tzinfo=dt.timezone.utc)
            with mock.patch("run_daily.run", side_effect=fake_run), \
                 mock.patch("vcs.subprocess.run", side_effect=fake_vcs_subproc):
                rev = run_daily.sync_repo(proj, repo, depth=50, snapshot_at=snap)
                self.assertEqual(rev, "r4819")

                # code_map & facts 跳过 .svn 正常索引
                cm = code_map.build(repo, rev, scope_out=[])
                self.assertEqual(cm["tracked_files"], 2)
                self.assertIn("pom.xml", cm["manifests"])
                self.assertIn("src/App.java", facts._tracked_files(repo))

                # 活跃度与代码量统计
                metrics = platform_db.collect_repo_metrics(repo)
                self.assertEqual(metrics["last_commit_author"], "zhangsan")
                self.assertEqual(metrics["last_commit_at"], "2026-10-15T08:30:00.000000Z")
                self.assertEqual(metrics["last_commit_subject"], "fix: 修复社保接口超时问题")
                self.assertGreaterEqual(metrics["loc_total"], 3)

                # 增量变更文件
                diff_files = run_daily.changed_files(repo, "r4800", "r4819")
                self.assertEqual(diff_files, ["pom.xml", "src/main/java/App.java"])

    def test_db_persistence_and_git_health_for_svn_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = platform_db.PlatformDB(Path(tmp) / "platform.db")
            managed_csv = Path(tmp) / "projects.managed.csv"
            created = db.upsert_project(
                {
                    "project_id": "sjpt-4src",
                    "project_name": "社保平台源码",
                    "department": "核心研发部",
                    "owner": "张三",
                    "git_url": SAMPLE_SVN_URL,
                    "branch": "",
                },
                create_only=True,
                managed_csv=managed_csv,
            )
            self.assertEqual(created["vcs_type"], "svn")
            self.assertEqual(created["branch"], "")
            self.assertIn(",svn,", managed_csv.read_text(encoding="utf-8"))

            # SVN 允许 branch 为空，不应被误判为 error
            gh, reason = web_server._compute_git_health(created, None, None)
            self.assertEqual(gh, "unknown")
            self.assertEqual(reason, "")

            # 支持新增 svn 协议和 https://svn.sjgjj.cn 凭据
            rt_creds = Path(tmp) / "git-credentials.runtime"
            c1 = db.upsert_git_credential(
                {"label": "内网SVN", "protocol": "https", "host": "svn.sjgjj.cn", "path_prefix": "/svn",
                 "username": "audit", "secret": "pass123"},
                runtime_file=rt_creds,
            )
            c2 = db.upsert_git_credential(
                {"label": "SVN原生协议", "protocol": "svn", "host": "10.0.0.10:3690",
                 "username": "audit2", "secret": "pass456"},
                runtime_file=rt_creds,
            )
            self.assertEqual(c1["protocol"], "https")
            self.assertEqual(c2["protocol"], "svn")
            self.assertEqual(vcs.match_credential_for_url(SAMPLE_SVN_URL, credential_file=rt_creds), ("audit", "pass123"))


if __name__ == "__main__":
    unittest.main()
