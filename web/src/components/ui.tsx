import { ReactNode, useEffect } from "react";
import { AlertCircle, Info, X } from "lucide-react";
import { Job } from "../api";
import { useFmt, useT } from "../i18n";
import { useStore } from "../store";

export function Toasts() {
  const toasts = useStore((s) => s.toasts);
  return (
    <div className="toasts" aria-live="polite">
      {toasts.map((t) => {
        const Icon = t.err ? AlertCircle : Info;
        return (
          <div key={t.id} className={"toast" + (t.err ? " err" : "")} role={t.err ? "alert" : "status"}>
            <Icon size={16} />
            <div className="toast-text">{t.text}</div>
          </div>
        );
      })}
    </div>
  );
}

/** Dialog shell: a fixed header, a scrolling body and (optionally) a footer outside the scroll area. */
export function Modal({ title, onClose, children, footer, width }: {
  title: string; onClose: () => void; children: ReactNode; footer?: ReactNode; width?: number;
}) {
  const t = useT();
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-bg" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={title}
           style={width ? { width: `min(${width}px, 100%)` } : undefined}>
        <div className="modal-head">
          <h2>{title}</h2>
          <button className="btn ghost icon sm" onClick={onClose} aria-label={t("common.close")}><X size={16} /></button>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  );
}

/** Title + optional hint + optional actions, for the top of a card. */
export function CardHead({ icon, title, hint, actions }: {
  icon?: ReactNode; title: ReactNode; hint?: ReactNode; actions?: ReactNode;
}) {
  return (
    <div className="card-head">
      <div>
        <h3>{icon}{title}</h3>
        {hint && <div className="hint">{hint}</div>}
      </div>
      {actions && <div className="row tight">{actions}</div>}
    </div>
  );
}

export function StatusChip({ job }: { job: Job }) {
  const { stage: stageText, status: statusText } = useFmt();
  const s = job.status;
  const cls = s === "completed" ? "ok" : s === "failed" ? "danger" : s === "running" ? "accent" : s === "cancelled" ? "warn" : s === "held" ? "info" : "";
  const text = s === "running" ? stageText(job.stage) : statusText(s);
  return (
    <span className={"chip " + cls}>
      {s === "running" && <span className="dot pulse" />}
      {text}
    </span>
  );
}

export function Progress({ job }: { job: Job }) {
  if (job.status !== "running") return null;
  const pct = Math.round((job.progress || 0) * 100);
  return (
    <div className={"progress" + (pct < 3 ? " indeterminate" : "")} title={`${pct}%`}
         role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
      <span style={{ width: `${Math.max(pct, 3)}%` }} />
    </div>
  );
}

export function Empty({ icon, title, hint, action }: { icon?: ReactNode; title: string; hint?: string; action?: ReactNode }) {
  return (
    <div className="empty">
      {icon && <div className="empty-icon">{icon}</div>}
      <h3>{title}</h3>
      {hint && <p>{hint}</p>}
      {action && <div className="empty-action">{action}</div>}
    </div>
  );
}

export const Skeleton = ({ h = 20, w = "100%" }: { h?: number; w?: string | number }) => <div className="skeleton" style={{ height: h, width: w }} />;

/** The inline spinner used inside a busy button. */
export const Spinner = () => <span className="spinner" aria-hidden="true" />;
