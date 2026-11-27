"""统一 Git / SVN 版本控制工具模块（纯 Python 标准库，只读操作）。"""
from __future__ import annotations

import os
import subprocess
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

VALID_VCS_TYPES = ("git", "svn")
_DEFAULT_SKIP_DIRS = {".git", ".svn"}
_SVN_CERT_FAILURES = "unknown-ca,cn-mismatch,expired,not-yet-valid,other"


def detect_vcs_type(raw_url: str, explicit: str | None = None) -> str:
    """识别项目仓库类型：优先显式 vcs_type，其次按 URL 协议与路径特征自动推断。"""
    exp = (explicit or "").strip().lower()
    if exp in VALID_VCS_TYPES:
        return exp
    url = (raw_url or "").strip()
    lower = url.lower()
    if lower.startswith(("svn://", "svn+ssh://")):
        return "svn"
    if lower.startswith(("http://", "https://")):
        try:
            parsed = urllib.parse.urlparse(url)
            host = (parsed.hostname or "").lower()
            path = (parsed.path or "").lower()
            if host.startswith("svn.") or "/svn/" in path or path == "/svn":
                return "svn"
        except ValueError:
            pass
    return "git"


def validate_repo_url(raw_url: str, vcs_type: str = "git") -> str:
    """校验 Git / SVN 仓库地址，防止参数注入与 file:// 本地路径泄露。"""
    vtype = detect_vcs_type(raw_url, vcs_type)
    url = (raw_url or "").strip()
    if not url:
        raise ValueError(f"{'SVN' if vtype == 'svn' else 'Git'} 地址不能为空")
    if url.startswith("-") or any(ch.isspace() for ch in url):
        raise ValueError(f"仓库地址包含非法字符：{url}")
    lower = url.lower()
    if vtype == "svn":
        if lower.startswith(("http://", "https://", "svn://", "svn+ssh://")):
            return url
        raise ValueError("SVN 地址仅支持 http://、https://、svn:// 或 svn+ssh:// 协议")
    if lower.startswith(("http://", "https://", "ssh://")) or url.startswith("git@"):
        return url
    raise ValueError("Git 地址仅支持 http://、https://、ssh:// 或 git@ 协议")


def compose_svn_url(base_url: str, branch: str = "") -> str:
    """拼接 SVN 基础地址与可选子路径（如 trunk / branches/v1）；子路径留空则直连基础地址。"""
    base = (base_url or "").strip().rstrip("/")
    sub = (branch or "").strip().strip("/")
    if not sub or sub == ".":
        return base
    if sub.startswith("-") or any(ch.isspace() for ch in sub) or ".." in sub.split("/"):
        raise ValueError(f"SVN 分支/子路径格式无效：{branch}")
    if base.lower().endswith("/" + sub.lower()):
        return base
    return f"{base}/{sub}"


def _host_port(parsed: urllib.parse.ParseResult) -> str:
    host = (parsed.hostname or "").lower()
    if not host:
        return ""
    if parsed.port:
        return f"{host}:{parsed.port}"
    return host


def _iter_credential_files(credential_file: Path | None = None) -> list[Path]:
    candidates: list[Path] = []
    if credential_file is not None:
        candidates.append(Path(credential_file))
    env_file = (os.environ.get("AUDIT_GIT_CREDENTIAL_FILE") or "").strip()
    if env_file:
        candidates.append(Path(env_file))
    candidates.append(Path("/run/secrets/git-credentials"))
    seen: set[str] = set()
    out: list[Path] = []
    for p in candidates:
        key = str(p)
        if key not in seen and p.is_file():
            seen.add(key)
            out.append(p)
    return out


def match_credential_for_url(
    url: str,
    credential_file: Path | None = None,
) -> tuple[str, str] | None:
    """从 git-credentials 风格凭据文件中按 scheme + host + 最长路径前缀匹配账号密码。"""
    try:
        target = urllib.parse.urlparse(url)
    except ValueError:
        return None
    if target.username is not None:
        return (
            urllib.parse.unquote(target.username),
            urllib.parse.unquote(target.password or ""),
        )
    t_scheme = (target.scheme or "").lower()
    t_host = _host_port(target)
    t_path = target.path or "/"
    if not t_scheme or not t_host:
        return None

    best_score = -1
    best_pair: tuple[str, str] | None = None
    for fpath in _iter_credential_files(credential_file):
        try:
            lines = fpath.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                p = urllib.parse.urlparse(line)
            except ValueError:
                continue
            if (p.scheme or "").lower() != t_scheme or _host_port(p) != t_host:
                continue
            if p.username is None:
                continue
            prefix = (p.path or "").rstrip("/")
            if prefix:
                if t_path != prefix and not t_path.startswith(prefix + "/"):
                    continue
            score = len(prefix)
            if score > best_score:
                best_score = score
                best_pair = (
                    urllib.parse.unquote(p.username),
                    urllib.parse.unquote(p.password or ""),
                )
    return best_pair


def redact_secret(text: str, secret: str = "") -> str:
    cleaned = text or ""
    if secret:
        variants = {
            secret,
            urllib.parse.quote(secret, safe=""),
            urllib.parse.quote(secret, safe="!~*'()-_."),
        }
        for v in sorted(variants, key=len, reverse=True):
            if v:
                cleaned = cleaned.replace(v, "***")
    return cleaned


def svn_base_cmd(
    url: str,
    credential_file: Path | None = None,
) -> tuple[list[str], str]:
    """构造非交互、不缓存凭据、兼容内网自签证书的 svn 命令前缀，并返回需脱敏的密码。"""
    cmd = ["svn", "--non-interactive", "--no-auth-cache"]
    if (url or "").strip().lower().startswith("https://"):
        cmd.append(f"--trust-server-cert-failures={_SVN_CERT_FAILURES}")
    cred = match_credential_for_url(url, credential_file=credential_file)
    secret = ""
    if cred:
        user, pwd = cred
        if user:
            cmd.extend(["--username", user])
        if pwd:
            cmd.extend(["--password", pwd])
            secret = pwd
    return cmd, secret


def is_svn_working_copy(repo: Path) -> bool:
    return (repo / ".svn").exists()


def list_working_copy_files(repo: Path, subdir: str = "") -> list[str]:
    """遍历本地工作副本文件（跳过 .git / .svn），返回 POSIX 相对路径列表。"""
    base = (repo / subdir) if subdir else repo
    if not base.exists():
        return []
    root = repo.resolve()
    files: list[str] = []
    for cur_dir, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in _DEFAULT_SKIP_DIRS)
        cur_path = Path(cur_dir)
        for fname in sorted(filenames):
            fpath = cur_path / fname
            try:
                if fpath.is_symlink() or not fpath.is_file():
                    continue
                rel = fpath.resolve().relative_to(root).as_posix()
                files.append(rel)
            except (OSError, ValueError):
                continue
    return sorted(files)


def list_repo_files(repo: Path, subdir: str = "") -> list[str]:
    """统一列出 Git 或 SVN 仓库中的受控文件（POSIX 相对路径）。"""
    if (repo / ".git").exists():
        cmd = ["git", "ls-files", "-z"]
        if subdir:
            cmd.extend(["--", subdir])
        proc = subprocess.run(cmd, cwd=repo, capture_output=True, timeout=120)
        if proc.returncode == 0:
            return [part.decode("utf-8", "surrogateescape") for part in proc.stdout.split(b"\0") if part]
        raise RuntimeError("无法读取 Git 跟踪文件清单")
    if is_svn_working_copy(repo):
        return list_working_copy_files(repo, subdir=subdir)
    return list_working_copy_files(repo, subdir=subdir)


def probe_svn_remote(
    repo_url: str,
    branch: str = "",
    timeout_sec: int = 15,
    credential_file: Path | None = None,
) -> dict:
    """执行只读 svn info --xml 校验 SVN 地址与可选子路径连通性；若根目录含 branches/ 则顺带列举分支。"""
    base_url = validate_repo_url(repo_url, "svn")
    target_url = compose_svn_url(base_url, branch)
    cmd_prefix, secret = svn_base_cmd(target_url, credential_file=credential_file)
    try:
        proc = subprocess.run(
            [*cmd_prefix, "info", "--xml", target_url],
            capture_output=True, text=True, timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False, "vcs_type": "svn", "branches": [], "default_branch": "",
            "branch_exists": False, "revision": "", "error": f"连接 SVN 仓库超时（>{timeout_sec}s）",
        }
    except OSError as exc:
        return {
            "ok": False, "vcs_type": "svn", "branches": [], "default_branch": "",
            "branch_exists": False, "revision": "", "error": f"当前环境未安装或无法调用 svn：{exc}",
        }

    if proc.returncode != 0:
        raw_err = redact_secret((proc.stderr or proc.stdout or "svn info 失败").strip(), secret)
        lines = [ln.strip() for ln in raw_err.splitlines() if ln.strip()]
        return {
            "ok": False, "vcs_type": "svn", "branches": [], "default_branch": "",
            "branch_exists": False, "revision": "", "error": " ".join(lines[:3])[:300],
        }

    revision = ""
    try:
        root_xml = ET.fromstring(proc.stdout)
        entry = root_xml.find("entry")
        if entry is not None:
            commit_el = entry.find("commit")
            rev_val = (commit_el.attrib.get("revision") if commit_el is not None else "") or entry.attrib.get("revision", "")
            if rev_val:
                revision = f"r{rev_val}"
    except ET.ParseError:
        pass

    # 若 base_url 恰好是标准 trunk/branches 结构，尝试列出候选子路径供下拉选择；
    # 若是直指源码目录（如 .../Working/SJPT/4.Src），则不把源码文件夹误当分支。
    branches: list[str] = []
    default_branch = ""
    try:
        ls_prefix, _ = svn_base_cmd(base_url, credential_file=credential_file)
        ls_proc = subprocess.run(
            [*ls_prefix, "list", base_url],
            capture_output=True, text=True, timeout=min(6, timeout_sec),
        )
        if ls_proc.returncode == 0:
            entries = {ln.strip().rstrip("/") for ln in ls_proc.stdout.splitlines() if ln.strip()}
            if "trunk" in entries:
                branches.append("trunk")
                default_branch = "trunk"
            if "branches" in entries:
                br_proc = subprocess.run(
                    [*ls_prefix, "list", f"{base_url.rstrip('/')}/branches"],
                    capture_output=True, text=True, timeout=min(6, timeout_sec),
                )
                if br_proc.returncode == 0:
                    for ln in br_proc.stdout.splitlines():
                        name = ln.strip().rstrip("/")
                        if name:
                            branches.append(f"branches/{name}")
    except (OSError, subprocess.TimeoutExpired):
        pass

    return {
        "ok": True,
        "vcs_type": "svn",
        "branches": sorted(set(branches)),
        "default_branch": default_branch,
        "branch_exists": True,
        "revision": revision,
        "error": "",
    }


def svn_working_copy_info(repo_dir: Path) -> dict:
    """离线读取本地 SVN 工作副本的 URL、修订号、最后提交时间与作者。"""
    out = {
        "url": "",
        "revision": "",
        "last_commit_at": None,
        "last_commit_author": "",
        "last_commit_subject": "",
    }
    if not is_svn_working_copy(repo_dir):
        return out
    try:
        proc = subprocess.run(
            ["svn", "info", "--xml"],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return out
        root_xml = ET.fromstring(proc.stdout)
        entry = root_xml.find("entry")
        if entry is None:
            return out
        url_el = entry.find("url")
        if url_el is not None and url_el.text:
            out["url"] = url_el.text.strip()
        commit_el = entry.find("commit")
        rev_val = ""
        if commit_el is not None:
            rev_val = commit_el.attrib.get("revision", "")
            author_el = commit_el.find("author")
            date_el = commit_el.find("date")
            if author_el is not None and author_el.text:
                out["last_commit_author"] = author_el.text.strip()[:80]
            if date_el is not None and date_el.text:
                out["last_commit_at"] = date_el.text.strip()
        if not rev_val:
            rev_val = entry.attrib.get("revision", "")
        if rev_val:
            out["revision"] = f"r{rev_val}"
    except (OSError, subprocess.TimeoutExpired, ET.ParseError):
        return out

    # 补充最后一次提交说明（需要连接服务端，超时自动降级为空，不阻塞）
    if out["url"]:
        try:
            cmd_prefix, _ = svn_base_cmd(out["url"])
            log_proc = subprocess.run(
                [*cmd_prefix, "log", "-l", "1", "--xml"],
                cwd=str(repo_dir), capture_output=True, text=True, timeout=5,
            )
            if log_proc.returncode == 0 and log_proc.stdout.strip():
                log_xml = ET.fromstring(log_proc.stdout)
                msg_el = log_xml.find(".//logentry/msg")
                if msg_el is not None and msg_el.text:
                    first_line = msg_el.text.strip().splitlines()[0].strip()
                    out["last_commit_subject"] = first_line[:140]
        except (OSError, subprocess.TimeoutExpired, ET.ParseError, IndexError):
            pass
    return out


def parse_svn_revision(raw: str | None) -> int | None:
    s = (raw or "").strip().lower()
    if s.startswith("r"):
        s = s[1:]
    if s.isdigit():
        return int(s)
    return None
