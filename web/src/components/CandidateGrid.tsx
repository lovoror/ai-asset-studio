import { Check, Maximize2, Box } from "lucide-react";
import { Candidate, Job, withToken } from "../api";
import { useFmt, useT } from "../i18n";

interface Props {
  job: Job;
  selected: Set<string>;
  onToggle: (file: string) => void;
  /** Open the lightbox at this candidate. Clicking the picture browses; the corner box selects. */
  onOpen: (index: number) => void;
  expected: number;
}

/** Variations grid: placeholders while generating, clickable cards when ready, badges for ones already turned into 3D. */
export default function CandidateGrid({ job, selected, onToggle, onOpen, expected }: Props) {
  const t = useT();
  const { status: statusText } = useFmt();
  const cands = job.candidates || [];
  const made = new Map<string, Job>();
  (job.children || []).forEach((c) => c.candidate && made.set(c.candidate, c));
  const placeholders = Math.max(0, expected - cands.length);
  const running = job.status === "running" || job.status === "queued";
  return (
    <div className="cands">
      {cands.map((c, i) => {
        const sel = selected.has(c.file);
        const child = made.get(c.file);
        const label = t("cand.variation", { n: c.file.replace(".png", "").replace("cand_", "") });
        return (
          <div
            key={c.file}
            className={"cand" + (sel ? " selected" : "")}
            role="group"
            aria-label={label}
          >
            <button className="cand-open" onClick={() => onOpen(i)} title={t("cand.open")}>
              <img src={withToken(c.thumb || c.url)} alt={c.file} loading="lazy" />
              <span className="cand-zoom" aria-hidden="true"><Maximize2 size={14} /></span>
            </button>
            {/* The selection control is this box, not the picture: clicking the picture opens it large. */}
            <button
              className="check"
              role="checkbox"
              aria-checked={sel}
              aria-label={sel ? t("common.deselect") : t("common.select")}
              title={sel ? t("common.deselect") : t("common.select")}
              disabled={c.partial}
              onClick={(e) => { e.stopPropagation(); if (!c.partial) onToggle(c.file); }}
            >
              {sel && <Check size={16} />}
            </button>
            {child && (
              <span className={"chip sm made " + (child.status === "completed" ? "ok" : child.status === "failed" ? "danger" : "info")}>
                <Box size={12} /> {child.status === "completed" ? t("cand.ready") : child.status === "held" ? t("cand.inReview") : statusText(child.status)}
              </span>
            )}
            <div className="meta">
              <button className="cand-label" onClick={() => onOpen(i)}>{label}{c.seed != null ? t("cand.seed", { n: c.seed }) : ""}</button>
              {c.score != null && (
                <span className={"chip " + (c.score >= 0.8 ? "ok" : c.score >= 0.5 ? "warn" : "danger")} title={Array.isArray(c.reasons) ? c.reasons.join("; ") : String(c.reasons || t("cand.framingPassed"))}>
                  {t("cand.framing", { n: Math.round(c.score * 100) })}
                </span>
              )}
            </div>
          </div>
        );
      })}
      {running &&
        Array.from({ length: placeholders }).map((_, i) => (
          <div key={"ph" + i} className="cand" style={{ cursor: "default" }}>
            <div className="ph skeleton" style={{ borderRadius: 0 }}>
              <span className="small">{cands.length + i === cands.length ? t("cand.generating") : t("cand.queued")}</span>
            </div>
            <div className="meta"><span>{t("cand.variation", { n: cands.length + i })}</span></div>
          </div>
        ))}
    </div>
  );
}
