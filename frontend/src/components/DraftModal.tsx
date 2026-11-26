import { useMemo, useState } from "react";
import { Check, Code2, Copy, Download, Eye, Printer } from "lucide-react";
import { downloadMarkdown, exportHtmlToPdf, renderMarkdownWithToc } from "../utils/markdown";

interface Props {
  title: string;
  markdown: string;
  filenameBase?: string;
  onClose: () => void;
}

export function DraftModal({ title, markdown, filenameBase, onClose }: Props) {
  const [mode, setMode] = useState<"rendered" | "raw">("rendered");
  const [copied, setCopied] = useState(false);

  const html = useMemo(() => renderMarkdownWithToc(markdown).html, [markdown]);
  const exportName = filenameBase || title;

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(markdown);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      // ignore clipboard errors on non-HTTPS origins
    }
  };

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.48)",
        display: "grid",
        placeItems: "center",
        padding: 20,
        zIndex: 60,
      }}
      onClick={onClose}
    >
      <div
        className="card"
        style={{
          width: "min(980px, 95vw)",
          maxHeight: "88vh",
          display: "flex",
          flexDirection: "column",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="card-title" style={{ flexWrap: "wrap" }}>
          <span>{title}</span>
          <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
            <button
              type="button"
              className={`btn ${mode === "rendered" ? "btn-primary" : ""}`}
              style={{ padding: "4px 10px", fontSize: 12 }}
              onClick={() => setMode("rendered")}
            >
              <Eye size={13} /> 渲染视图
            </button>
            <button
              type="button"
              className={`btn ${mode === "raw" ? "btn-primary" : ""}`}
              style={{ padding: "4px 10px", fontSize: 12 }}
              onClick={() => setMode("raw")}
            >
              <Code2 size={13} /> 原文
            </button>
            <button
              type="button"
              className="btn"
              style={{ padding: "4px 10px", fontSize: 12 }}
              onClick={() => downloadMarkdown(exportName, markdown)}
            >
              <Download size={13} /> 下载 Markdown
            </button>
            <button
              type="button"
              className="btn"
              style={{ padding: "4px 10px", fontSize: 12 }}
              onClick={() => exportHtmlToPdf(exportName, html)}
            >
              <Printer size={13} /> 导出 PDF
            </button>
            <button
              type="button"
              className="btn"
              style={{ padding: "4px 10px", fontSize: 12 }}
              onClick={() => void handleCopy()}
            >
              {copied ? <Check size={13} /> : <Copy size={13} />}
              {copied ? "已复制" : "复制"}
            </button>
            <button type="button" className="btn" style={{ padding: "4px 10px", fontSize: 12 }} onClick={onClose}>
              关闭
            </button>
          </div>
        </div>
        {mode === "rendered" ? (
          <div
            className="markdown-body"
            style={{
              overflowY: "auto",
              maxHeight: "74vh",
              padding: "4px 6px",
            }}
            dangerouslySetInnerHTML={{ __html: html }}
          />
        ) : (
          <pre className="log-box" style={{ maxHeight: "74vh", margin: 0 }}>
            {markdown}
          </pre>
        )}
      </div>
    </div>
  );
}
