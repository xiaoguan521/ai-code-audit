"""零外部依赖 SMTP 邮件投递引擎（支持 SSL/STARTTLS、按负责人聚合及 .docx/.md/.zip 附件）。"""
from __future__ import annotations

import html
import io
import json
import re
import smtplib
import ssl
import time
import zipfile
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

import docx_export
import platform_db
from adapters import summary as summary_adapter


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n]+', "_", str(name or "").strip()).strip("_")
    return cleaned or "report"


def _open_smtp(cfg: dict, timeout_sec: int = 15):
    host = str(cfg.get("host") or "").strip()
    port = int(cfg.get("port") or 465)
    security = str(cfg.get("security") or "ssl").strip().lower()
    if not host:
        raise ValueError("未配置 SMTP 服务器地址")
    if security == "ssl":
        ctx = ssl.create_default_context()
        client = smtplib.SMTP_SSL(host, port, timeout=timeout_sec, context=ctx)
    else:
        client = smtplib.SMTP(host, port, timeout=timeout_sec)
        client.ehlo()
        if security == "starttls":
            ctx = ssl.create_default_context()
            client.starttls(context=ctx)
            client.ehlo()
    username = str(cfg.get("username") or "").strip()
    password = str(cfg.get("password") or "")
    if username and password:
        client.login(username, password)
    return client


def _build_message(cfg: dict, *, to_email: str, to_name: str, subject: str,
                   plain_body: str, html_body: str,
                   attachments: list[tuple[str, bytes, str, str]]) -> EmailMessage:
    msg = EmailMessage()
    from_email = str(cfg.get("from_email") or cfg.get("username") or "").strip()
    from_name = str(cfg.get("from_name") or "八性代码质量审查平台").strip()
    if not from_email:
        raise ValueError("未配置发件人邮箱")
    if "@" in from_email:
        local, domain = from_email.split("@", 1)
        msg["From"] = str(Address(display_name=from_name, username=local, domain=domain))
    else:
        msg["From"] = formataddr((from_name, from_email))
    msg["To"] = formataddr((to_name, to_email)) if to_name else to_email
    msg["Subject"] = subject
    msg.set_content(plain_body, subtype="plain", charset="utf-8")
    msg.add_alternative(html_body, subtype="html", charset="utf-8")
    for fname, raw_bytes, maintype, subtype in attachments:
        msg.add_attachment(raw_bytes, maintype=maintype, subtype=subtype, filename=fname)
    return msg


def _pack_attachments_if_needed(
    raw_items: list[tuple[str, bytes, str, str]],
    *,
    zip_name: str,
    max_attach_mb: int = 15,
) -> list[tuple[str, bytes, str, str]]:
    """附件数量过多（>6）或总大小偏大时自动打包为单个 ZIP，避免被企业邮箱网关拦截。"""
    if not raw_items:
        return []
    total_size = sum(len(b) for _, b, _, _ in raw_items)
    limit_bytes = max(1, int(max_attach_mb or 15)) * 1024 * 1024
    if len(raw_items) <= 6 and total_size <= min(limit_bytes, 10 * 1024 * 1024):
        return raw_items
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        seen: set[str] = set()
        for fname, content, _, _ in raw_items:
            arc = fname
            idx = 2
            while arc in seen:
                arc = f"{idx}_{fname}"
                idx += 1
            seen.add(arc)
            zf.writestr(arc, content)
    zip_bytes = buf.getvalue()
    if len(zip_bytes) > limit_bytes:
        # 极端情况超过单封邮件上限，仍只保留前若干个不超限的报告打包
        buf2 = io.BytesIO()
        acc = 0
        with zipfile.ZipFile(buf2, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for fname, content, _, _ in raw_items:
                if acc + len(content) > limit_bytes * 0.9:
                    break
                zf.writestr(fname, content)
                acc += len(content)
        zip_bytes = buf2.getvalue()
    return [(zip_name, zip_bytes, "application", "zip")]


def _collect_project_attachments(
    projects: list[dict],
    rep_dir: Path,
    date_str: str,
    attachment_format: str,
) -> list[tuple[str, bytes, str, str]]:
    items: list[tuple[str, bytes, str, str]] = []
    for p in projects:
        if p.get("status") != "ok":
            continue
        pid = str(p.get("project_id") or "")
        if not pid:
            continue
        md_cand = Path(str(p.get("report_path") or ""))
        if not md_cand.is_file():
            md_cand = rep_dir / f"{pid}.md"
        if not md_cand.is_file():
            continue
        try:
            md_text = md_cand.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        pname = _safe_filename(p.get("project_name") or pid)
        base = f"{date_str}_{pname}_{pid}"
        if attachment_format in ("docx", "both"):
            docx_bytes = docx_export.markdown_to_docx_bytes(md_text, title=str(p.get("project_name") or pid))
            items.append((
                f"{base}.docx",
                docx_bytes,
                "application",
                "vnd.openxmlformats-officedocument.wordprocessingml.document",
            ))
        if attachment_format in ("md", "both"):
            items.append((f"{base}.md", md_text.encode("utf-8"), "text", "markdown"))
    return items


def _is_risky_project(p: dict) -> bool:
    if p.get("status") != "ok":
        return True
    sc = p.get("report_scores") or {}
    if int(sc.get("security_p0") or 0) > 0:
        return True
    total = sc.get("total")
    if isinstance(total, (int, float)) and float(total) < 7.0:
        return True
    grade = str(sc.get("grade") or "")
    if "高风险" in grade or "明显风险" in grade:
        return True
    return False


def _render_owner_email(user: dict, projects: list[dict], date_str: str, run_id: str,
                        portal_url: str, attachment_names: list[str]) -> tuple[str, str, str]:
    ok_n = sum(1 for p in projects if p.get("status") == "ok")
    err_n = len(projects) - ok_n
    p0_total = sum(int((p.get("report_scores") or {}).get("security_p0") or 0) for p in projects)
    subject = f"[代码质量审查] {date_str} 您负责的 {len(projects)} 个项目审查结果"
    if err_n > 0 or p0_total > 0:
        subject += f"（失败 {err_n} / 安全P0 {p0_total}）"

    plain_lines = [
        f"{user.get('display_name') or user.get('username')}，您好：",
        f"批次 {date_str} / {run_id} 已完成，共涉及您负责的 {len(projects)} 个项目（成功 {ok_n}，失败 {err_n}）：",
        "",
    ]
    rows_html = []
    for p in projects:
        pid = str(p.get("project_id") or "")
        pname = str(p.get("project_name") or pid)
        st = p.get("status")
        sc = p.get("report_scores") or {}
        total = sc.get("total")
        sec = sc.get("security_score")
        grade = sc.get("grade") or ("扫描失败" if st != "ok" else "—")
        p0 = int(sc.get("security_p0") or 0)
        total_s = f"{float(total):.1f}" if isinstance(total, (int, float)) else "—"
        sec_s = f"{float(sec):.1f}" if isinstance(sec, (int, float)) else "—"
        link = f"{portal_url}/#/reports?date={date_str}&run={run_id}&target={pid}" if (portal_url and st == "ok") else ""
        plain_lines.append(f"- {pname} ({pid}) | 状态: {st} | 总分: {total_s} | 安全分: {sec_s} | 档位: {grade} | 安全P0: {p0}")
        if link:
            plain_lines.append(f"  报告链接: {link}")
        name_cell = f'<a href="{html.escape(link)}" style="color:#2563eb;text-decoration:none;font-weight:600;">{html.escape(pname)}</a>' if link else f"<b>{html.escape(pname)}</b>"
        color = "#16a34a" if st == "ok" else "#dc2626"
        rows_html.append(
            f"<tr>"
            f"<td style='padding:8px 10px;border-bottom:1px solid #e5e7eb;'>{name_cell}<br><span style='color:#6b7280;font-size:12px;'>{html.escape(pid)}</span></td>"
            f"<td style='padding:8px 10px;border-bottom:1px solid #e5e7eb;color:{color};font-weight:600;'>{'成功' if st == 'ok' else '失败'}</td>"
            f"<td style='padding:8px 10px;border-bottom:1px solid #e5e7eb;text-align:right;font-weight:700;'>{html.escape(total_s)}</td>"
            f"<td style='padding:8px 10px;border-bottom:1px solid #e5e7eb;text-align:right;'>{html.escape(sec_s)}</td>"
            f"<td style='padding:8px 10px;border-bottom:1px solid #e5e7eb;'>{html.escape(str(grade))}</td>"
            f"<td style='padding:8px 10px;border-bottom:1px solid #e5e7eb;text-align:right;color:{'#dc2626' if p0 > 0 else '#374151'};'>{p0}</td>"
            f"</tr>"
        )
    if attachment_names:
        plain_lines.append("")
        plain_lines.append("附件列表：" + "、".join(attachment_names))

    html_body = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;color:#111827;max-width:760px;">
      <div style="background:linear-gradient(120deg,#0f172a,#1d4ed8);color:#fff;padding:18px 22px;border-radius:10px 10px 0 0;">
        <div style="font-size:16px;font-weight:700;">八性代码质量审查平台 · 负责人结果通知</div>
        <div style="font-size:12px;opacity:0.85;margin-top:4px;">批次：{html.escape(date_str)} / {html.escape(run_id)} · 共 {len(projects)} 个项目（成功 {ok_n} / 失败 {err_n}）</div>
      </div>
      <div style="border:1px solid #e5e7eb;border-top:none;padding:18px 22px;border-radius:0 0 10px 10px;background:#fff;">
        <p style="margin-top:0;">{html.escape(user.get('display_name') or user.get('username') or '')}，您好：本批次您负责的项目审查结果汇总如下，完整报告已随附件发送。</p>
        <table style="width:100%;border-collapse:collapse;font-size:13px;">
          <thead>
            <tr style="background:#f9fafb;text-align:left;color:#4b5563;">
              <th style="padding:8px 10px;border-bottom:1px solid #e5e7eb;">项目</th>
              <th style="padding:8px 10px;border-bottom:1px solid #e5e7eb;">状态</th>
              <th style="padding:8px 10px;border-bottom:1px solid #e5e7eb;text-align:right;">总分</th>
              <th style="padding:8px 10px;border-bottom:1px solid #e5e7eb;text-align:right;">安全分</th>
              <th style="padding:8px 10px;border-bottom:1px solid #e5e7eb;">档位</th>
              <th style="padding:8px 10px;border-bottom:1px solid #e5e7eb;text-align:right;">安全P0</th>
            </tr>
          </thead>
          <tbody>{''.join(rows_html)}</tbody>
        </table>
        {f'<p style="font-size:12px;color:#4b5563;margin-top:14px;">随信附件：{html.escape("、".join(attachment_names))}</p>' if attachment_names else ''}
        {f'<p style="margin-top:14px;"><a href="{html.escape(portal_url)}" style="display:inline-block;background:#2563eb;color:#fff;padding:8px 16px;border-radius:6px;text-decoration:none;font-size:13px;">前往管理端查看详情</a></p>' if portal_url else ''}
      </div>
    </div>
    """
    return subject, "\n".join(plain_lines), html_body


def send_test_email(db: platform_db.PlatformDB, to_email: str, *, override_cfg: dict | None = None) -> dict:
    """发送测试邮件（附带示例 Word 报告 .docx），验证 SMTP 连通性与附件投递。"""
    target = (to_email or "").strip()
    if not platform_db.EMAIL_RE.match(target):
        raise ValueError(f"测试收件人邮箱格式无效：{target}")
    cfg = db.get_smtp_config(include_password=True)
    if override_cfg:
        for k, v in override_cfg.items():
            if k == "password" and not v:
                continue
            if v is not None:
                cfg[k] = v
    sample_md = (
        "# 八性代码质量审查平台 · 测试报告\n\n"
        "## 评分总览\n\n"
        "| 维度 | 得分 | 状态 |\n|---|---|---|\n"
        "| 安全性 | 9.0 | 正常 |\n| 可维护性 | 8.5 | 正常 |\n\n"
        "本文件为 SMTP 附件投递测试自动生成的 Word 报告示例。\n"
    )
    sample_docx = docx_export.markdown_to_docx_bytes(sample_md, title="SMTP 附件投递测试报告")
    subject = "[代码质量审查] SMTP 邮件与附件投递测试"
    attachments = [(
        "测试报告示例.docx",
        sample_docx,
        "application",
        "vnd.openxmlformats-officedocument.wordprocessingml.document",
    )]
    msg = _build_message(
        cfg,
        to_email=target,
        to_name="",
        subject=subject,
        plain_body="如果您收到此邮件且附件「测试报告示例.docx」可正常打开，说明平台 SMTP 邮件投递配置成功。",
        html_body="<p>如果您收到此邮件且附件 <b>测试报告示例.docx</b> 可正常打开，说明平台 SMTP 邮件与附件投递配置成功。</p>",
        attachments=attachments,
    )
    t0 = time.perf_counter()
    try:
        with _open_smtp(cfg) as client:
            client.send_message(msg)
        elapsed_ms = round((time.perf_counter() - t0) * 1000)
        db.record_email_log(
            run_date="", run_id="test", recipient_email=target, recipient_name="测试收件人",
            recipient_type="test", project_ids=[], subject=subject,
            attachment_names=["测试报告示例.docx"], status="sent",
        )
        return {"ok": True, "elapsed_ms": elapsed_ms, "error": ""}
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - t0) * 1000)
        err = f"{type(exc).__name__}: {exc}"
        db.record_email_log(
            run_date="", run_id="test", recipient_email=target, recipient_name="测试收件人",
            recipient_type="test", project_ids=[], subject=subject,
            attachment_names=["测试报告示例.docx"], status="failed", error=err,
        )
        return {"ok": False, "elapsed_ms": elapsed_ms, "error": err}


def dispatch_batch_emails(
    output_dir: Path,
    date_str: str,
    run_id: str,
    db: platform_db.PlatformDB,
    *,
    force: bool = False,
    only_email: str | None = None,
    only_type: str | None = None,
) -> dict:
    """按项目负责人聚合发送本批次审查结果邮件（含 .docx/.md 附件）及管理层 Word 汇总邮件。"""
    cfg = db.get_smtp_config(include_password=True)
    if not force and not cfg.get("enabled"):
        return {"ok": False, "skipped_reason": "SMTP 未启用", "sent": 0, "failed": 0, "skipped": 0, "details": []}
    if not cfg.get("host"):
        return {"ok": False, "skipped_reason": "SMTP 服务器未配置", "sent": 0, "failed": 0, "skipped": 0, "details": []}

    run_file = (
        output_dir / "results" / date_str / "_run.json"
        if run_id == "default"
        else output_dir / "results" / date_str / run_id / "_run.json"
    )
    if not run_file.is_file():
        return {"ok": False, "skipped_reason": "未找到该批次运行记录", "sent": 0, "failed": 0, "skipped": 0, "details": []}
    run_data = json.loads(run_file.read_text(encoding="utf-8"))
    rep_dir = output_dir / "reports" / date_str if run_id == "default" else output_dir / "reports" / date_str / run_id

    # 补全项目的 owner/department/project_name 元信息
    catalog = {p["project_id"]: p for p in db.list_projects()}
    projects: list[dict] = []
    for raw in run_data.get("projects") or []:
        rec = dict(raw)
        meta = catalog.get(str(rec.get("project_id") or ""), {})
        for k in ("project_name", "department", "owner", "weights"):
            if not rec.get(k) and meta.get(k):
                rec[k] = meta[k]
        projects.append(rec)

    users = [u for u in db.list_users() if u.get("is_active") and str(u.get("email") or "").strip()]
    att_fmt = str(cfg.get("attachment_format") or "docx")
    max_mb = int(cfg.get("max_attach_mb") or 15)
    portal_url = str(cfg.get("portal_url") or "").strip().rstrip("/")
    target_email_lower = (only_email or "").strip().lower()

    # 构建待发队列：[(recipient_email, recipient_name, recipient_type, pids, subject, msg, att_names)]
    queue: list[tuple[str, str, str, list[str], str, EmailMessage, list[str]]] = []
    skipped = 0

    if only_type in (None, "", "owner"):
        for u in users:
            u_email = str(u.get("email") or "").strip()
            if target_email_lower and u_email.lower() != target_email_lower:
                continue
            owned = [p for p in projects if platform_db.owner_matches_user(u, p.get("owner"))]
            if not owned:
                continue
            mode = str(u.get("email_notify") or "batch")
            if not target_email_lower:
                if mode == "off":
                    skipped += 1
                    continue
                if mode == "risk_only" and not any(_is_risky_project(p) for p in owned):
                    skipped += 1
                    continue
            raw_atts = _collect_project_attachments(owned, rep_dir, date_str, att_fmt)
            packed = _pack_attachments_if_needed(
                raw_atts,
                zip_name=f"{date_str}_{_safe_filename(u.get('display_name') or u.get('username') or 'reports')}.zip",
                max_attach_mb=max_mb,
            )
            att_names = [a[0] for a in packed]
            subject, plain_body, html_body = _render_owner_email(u, owned, date_str, run_id, portal_url, att_names)
            msg = _build_message(
                cfg, to_email=u_email, to_name=str(u.get("display_name") or ""),
                subject=subject, plain_body=plain_body, html_body=html_body, attachments=packed,
            )
            queue.append((u_email, str(u.get("display_name") or u.get("username") or ""), "owner",
                          [str(p.get("project_id") or "") for p in owned], subject, msg, att_names))

    # 管理层整体汇总 Word 抄送邮件
    cc_raw = str(cfg.get("summary_cc_emails") or "").strip()
    cc_emails = [e.strip() for e in re.split(r"[;,，、\s]+", cc_raw) if e.strip()]
    ok_projects = [p for p in projects if p.get("status") == "ok"]
    if only_type in (None, "", "summary") and cc_emails and ok_projects:
        summary_md_file = rep_dir / "_summary.md"
        if summary_md_file.is_file():
            summary_md = summary_md_file.read_text(encoding="utf-8", errors="replace")
        else:
            gen = summary_adapter.generate_batch(projects, rep_dir, {"summary": {"enabled": False}}, run_id)
            summary_md = str(gen.get("text") or "")
        summary_docx = docx_export.markdown_to_docx_bytes(summary_md, title="代码检查分析报告汇总")
        s_fname = f"代码检查分析报告汇总_{date_str}_{run_id}.docx"
        s_subject = f"[代码质量审查汇总] {date_str} 批次整体报告（成功 {len(ok_projects)} / 共 {len(projects)}）"
        s_plain = f"批次 {date_str} / {run_id} 已完成（成功 {len(ok_projects)} / 共 {len(projects)}），整体汇总 Word 报告见附件。"
        s_html = (
            f"<p>批次 <b>{html.escape(date_str)} / {html.escape(run_id)}</b> 已完成（成功 {len(ok_projects)} / 共 {len(projects)}）。</p>"
            f"<p>管理层整体汇总报告 <b>{html.escape(s_fname)}</b> 已随附件发送。</p>"
            + (f'<p><a href="{html.escape(portal_url)}/#/reports?date={date_str}&run={run_id}&target=_summary">点击查看在线汇总报告</a></p>' if portal_url else "")
        )
        s_att = [(s_fname, summary_docx, "application", "vnd.openxmlformats-officedocument.wordprocessingml.document")]
        for cc in cc_emails:
            if target_email_lower and cc.lower() != target_email_lower:
                continue
            msg = _build_message(
                cfg, to_email=cc, to_name="管理层汇总",
                subject=s_subject, plain_body=s_plain, html_body=s_html, attachments=s_att,
            )
            queue.append((cc, "管理层汇总", "summary", [str(p.get("project_id") or "") for p in projects],
                          s_subject, msg, [s_fname]))

    if not queue:
        return {"ok": True, "sent": 0, "failed": 0, "skipped": skipped, "details": []}

    sent, failed = 0, 0
    details: list[dict] = []
    try:
        with _open_smtp(cfg) as client:
            for to_email, to_name, rtype, pids, subject, msg, att_names in queue:
                try:
                    client.send_message(msg)
                    sent += 1
                    log_rec = db.record_email_log(
                        run_date=date_str, run_id=run_id, recipient_email=to_email,
                        recipient_name=to_name, recipient_type=rtype, project_ids=pids,
                        subject=subject, attachment_names=att_names, status="sent",
                    )
                    details.append(log_rec)
                except Exception as exc:
                    failed += 1
                    log_rec = db.record_email_log(
                        run_date=date_str, run_id=run_id, recipient_email=to_email,
                        recipient_name=to_name, recipient_type=rtype, project_ids=pids,
                        subject=subject, attachment_names=att_names, status="failed",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    details.append(log_rec)
    except Exception as conn_exc:
        err_msg = f"SMTP 连接/认证失败：{type(conn_exc).__name__}: {conn_exc}"
        for to_email, to_name, rtype, pids, subject, _msg, att_names in queue:
            failed += 1
            log_rec = db.record_email_log(
                run_date=date_str, run_id=run_id, recipient_email=to_email,
                recipient_name=to_name, recipient_type=rtype, project_ids=pids,
                subject=subject, attachment_names=att_names, status="failed",
                error=err_msg,
            )
            details.append(log_rec)

    return {"ok": failed == 0, "sent": sent, "failed": failed, "skipped": skipped, "details": details}
