import { useEffect } from "react";
import { AlertTriangle, Info, Trash2, X } from "lucide-react";

export interface ConfirmDialogProps {
  open: boolean;
  tone?: "danger" | "warning" | "info";
  title: string;
  description: string;
  previewItems?: string[];
  footnote?: string;
  error?: string | null;
  confirmLabel?: string;
  cancelLabel?: string;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  open,
  tone = "warning",
  title,
  description,
  previewItems,
  footnote,
  error,
  confirmLabel = "确认执行",
  cancelLabel = "取消",
  busy = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) onCancel();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, busy, onCancel]);

  if (!open) return null;

  const shownItems = (previewItems || []).slice(0, 6);
  const moreCount = (previewItems?.length || 0) - shownItems.length;

  return (
    <div
      className="modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby="confirm-dialog-title"
      onClick={() => {
        if (!busy) onCancel();
      }}
    >
      <div className="modal-dialog confirm-dialog" onClick={(e) => e.stopPropagation()}>
        <div className="confirm-head">
          <div className={`confirm-icon-badge ${tone}`}>
            {tone === "danger" ? (
              <Trash2 size={18} />
            ) : tone === "info" ? (
              <Info size={18} />
            ) : (
              <AlertTriangle size={18} />
            )}
          </div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div id="confirm-dialog-title" className="confirm-title">
              {title}
            </div>
            <p className="confirm-desc">{description}</p>
          </div>
          <button
            type="button"
            className="confirm-close-btn"
            aria-label="关闭弹窗"
            disabled={busy}
            onClick={onCancel}
          >
            <X size={16} />
          </button>
        </div>

        {shownItems.length > 0 && (
          <div className="confirm-preview-list">
            {shownItems.map((item, idx) => (
              <span key={`${item}-${idx}`} className="confirm-preview-chip">
                {item}
              </span>
            ))}
            {moreCount > 0 && (
              <span className="confirm-preview-chip muted">等共 {previewItems?.length} 项</span>
            )}
          </div>
        )}

        {footnote && <div className="confirm-footnote">{footnote}</div>}

        {error && (
          <div
            style={{
              fontSize: 12.5,
              padding: "8px 10px",
              borderRadius: 8,
              border: "1px solid var(--critical)",
              background: "color-mix(in srgb, var(--critical) 10%, transparent)",
              color: "var(--critical)",
              lineHeight: 1.45,
            }}
          >
            {error}
          </div>
        )}

        <div className="confirm-footer">
          <button type="button" className="btn" disabled={busy} onClick={onCancel}>
            {cancelLabel}
          </button>
          <button
            type="button"
            className={`btn ${tone === "danger" ? "btn-danger-solid" : "btn-primary"}`}
            disabled={busy}
            onClick={onConfirm}
          >
            {busy ? "处理中…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
