import { Marked, Renderer } from "marked";

export interface TocItem {
  id: string;
  depth: number;
  text: string;
}

export function escapeHtml(raw: string): string {
  return raw.replace(/[&<>"']/g, (ch) => {
    switch (ch) {
      case "&":
        return "&amp;";
      case "<":
        return "&lt;";
      case ">":
        return "&gt;";
      case '"':
        return "&quot;";
      case "'":
        return "&#39;";
      default:
        return ch;
    }
  });
}

export function renderMarkdownWithToc(markdown: string): { html: string; toc: TocItem[] } {
  const toc: TocItem[] = [];
  const renderer = new Renderer();
  renderer.html = ({ text }) => escapeHtml(text);
  renderer.link = function (token) {
    const href = (token.href || "").trim();
    if (/^(javascript|vbscript|data):/i.test(href)) {
      return escapeHtml(token.text || href);
    }
    return Renderer.prototype.link.call(this, token);
  };
  renderer.image = function (token) {
    const href = (token.href || "").trim();
    if (/^(javascript|vbscript|data):/i.test(href)) {
      return escapeHtml(token.text || href);
    }
    return Renderer.prototype.image.call(this, token);
  };
  renderer.heading = function ({ tokens, depth, text }) {
    const inner = this.parser.parseInline(tokens);
    if (depth === 2 || depth === 3) {
      const id = `sec-${toc.length}`;
      toc.push({ id, depth, text: text.replace(/[`*_#]/g, "").trim() });
      return `<h${depth} id="${id}">${inner}</h${depth}>\n`;
    }
    return `<h${depth}>${inner}</h${depth}>\n`;
  };
  try {
    const instance = new Marked({ gfm: true, breaks: false, renderer });
    const html = instance.parse(markdown) as string;
    return { html, toc };
  } catch {
    return { html: `<pre>${escapeHtml(markdown)}</pre>`, toc: [] };
  }
}

export function sanitizeFilename(raw: string): string {
  return (
    raw
      .replace(/[\\/:*?"<>|\s]+/g, "_")
      .replace(/^_+|_+$/g, "")
      .slice(0, 100) || "report"
  );
}

export function downloadBlob(filename: string, content: string, mime: string) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function downloadMarkdown(filenameBase: string, markdown: string) {
  const base = sanitizeFilename(filenameBase);
  const fname = base.endsWith(".md") ? base : `${base}.md`;
  downloadBlob(fname, markdown, "text/markdown;charset=utf-8");
}

/**
 * 通过隔离的白色打印样式 iframe 导出整洁的 A4 矢量 PDF（支持中文、自动分页、表格跨页重复表头，
 * 不受当前页面暗黑主题或弹窗滚动条裁切影响）。
 */
export function exportHtmlToPdf(filenameBase: string, bodyHtml: string) {
  const safeTitle = sanitizeFilename(filenameBase);
  const iframe = document.createElement("iframe");
  iframe.style.position = "fixed";
  iframe.style.right = "0";
  iframe.style.bottom = "0";
  iframe.style.width = "0";
  iframe.style.height = "0";
  iframe.style.border = "0";
  iframe.style.visibility = "hidden";
  document.body.appendChild(iframe);

  const doc = iframe.contentDocument || iframe.contentWindow?.document;
  if (!doc || !iframe.contentWindow) {
    document.body.removeChild(iframe);
    window.print();
    return;
  }

  doc.open();
  doc.write(`<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<title>${escapeHtml(safeTitle)}</title>
<style>
  @page { size: A4; margin: 14mm 12mm; }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    color: #111827;
    background: #ffffff;
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", "Segoe UI", sans-serif;
    font-size: 11.5pt;
    line-height: 1.65;
    -webkit-print-color-adjust: exact;
    print-color-adjust: exact;
  }
  h1, h2, h3, h4 {
    color: #111827;
    break-after: avoid;
    page-break-after: avoid;
  }
  h1 { font-size: 18pt; border-bottom: 1.5px solid #d1d5db; padding-bottom: 6px; margin: 0 0 12px; }
  h2 { font-size: 14pt; border-bottom: 1px solid #e5e7eb; padding-bottom: 4px; margin: 18px 0 8px; }
  h3 { font-size: 12pt; margin: 14px 0 6px; }
  p, ul, ol { margin: 6px 0; }
  ul, ol { padding-left: 20px; }
  blockquote {
    margin: 8px 0;
    padding: 6px 12px;
    border-left: 3px solid #2563eb;
    background: #f8fafc;
    color: #374151;
  }
  code {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 9.5pt;
    background: #f3f4f6;
    padding: 1px 4px;
    border-radius: 3px;
    word-break: break-all;
  }
  pre {
    background: #f8fafc;
    border: 1px solid #e5e7eb;
    border-radius: 6px;
    padding: 8px 10px;
    white-space: pre-wrap;
    word-break: break-word;
    font-size: 9pt;
    line-height: 1.5;
    overflow: visible;
  }
  pre code { background: transparent; padding: 0; }
  table {
    width: 100%;
    border-collapse: collapse;
    margin: 8px 0 12px;
    font-size: 9.5pt;
  }
  thead { display: table-header-group; }
  tr { break-inside: avoid; page-break-inside: avoid; }
  th, td {
    border: 1px solid #d1d5db;
    padding: 5px 7px;
    text-align: left;
    vertical-align: top;
    word-break: break-word;
  }
  th { background: #f3f4f6; font-weight: 600; }
  a { color: #2563eb; text-decoration: none; }
  hr { border: 0; border-top: 1px solid #e5e7eb; margin: 12px 0; }
  .pdf-meta-banner {
    border: 1px solid #d1d5db;
    background: #f8fafc;
    border-radius: 6px;
    padding: 8px 12px;
    margin-bottom: 14px;
    font-size: 10pt;
    color: #374151;
  }
</style>
</head>
<body>${bodyHtml}</body>
</html>`);
  doc.close();

  const win = iframe.contentWindow;
  const prevTitle = document.title;
  let cleaned = false;
  const cleanup = () => {
    if (cleaned) return;
    cleaned = true;
    document.title = prevTitle;
    if (iframe.parentNode) {
      iframe.parentNode.removeChild(iframe);
    }
  };

  win.onafterprint = cleanup;
  document.title = safeTitle;
  setTimeout(() => {
    try {
      win.focus();
      win.print();
    } finally {
      // 某些浏览器在取消打印时不触发 iframe.onafterprint，兜底延迟清理
      setTimeout(cleanup, 60000);
    }
  }, 80);
}
