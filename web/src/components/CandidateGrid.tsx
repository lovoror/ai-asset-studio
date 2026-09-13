import { Check, Box } from "lucide-react";
import { Candidate, Job, withToken } from "../api";

interface Props {
  job: Job;
  selected: Set<string>;
  onToggle: (file: string) => void;
  expected: number;
  onOpen?: (c: Candidate) => void;
}

/** Variations grid: placeholders while generating, selectable cards when ready, badges for ones already turned into 3D. */
export default function CandidateGrid({ job, selected, onToggle, expected, onOpen }: Props) {
  const cands = job.candidates || [];
  const made = new Map<string, Job>();
  (job.children || []).forEach((c) => c.candidate && made.set(c.candidate, c));
  const placeholders = Math.max(0, expected - cands.length);
  const running = job.status === "running" || job.status === "queued";
  return (
    <div className="cands">
      {cands.map((c) => {
        const sel = selected.has(c.file);
        const child = made.get(c.file);
        return (
          <div
            key={c.file}
            className={"cand" + (sel ? " selected" : "")}
            role="checkbox"
            aria-checked={sel}
            tabIndex={0}
            onClick={() => !c.partial && onToggle(c.file)}
            onKeyDown={(e) => {
              if (e.key === " " || e.key === "Enter") {
                e.preventDefault();
                if (e.key === "Enter" && onOpen) onOpen(c);
                else if (!c.partial) onToggle(c.file);
              }
            }}
          >
            <img src={withToken(c.thumb || c.url)} alt={c.file} loading="lazy" />
            <span className="check">{sel && <Check size={16} />}</span>
            {child && (
              <span className={"chip made " + (child.status === "completed" ? "ok" : child.status === "failed" ? "danger" : "info")}>
                <Box size={12} /> {child.status === "completed" ? "3D ready" : child.status === "held" ? "in review" : child.status}
              </span>
            )}
            <div className="meta">
              <span>{c.file.replace(".png", "").replace("cand_", "variation ")}{c.seed != null ? ` · seed ${c.seed}` : ""}</span>
              {c.score != null && (
                <span className={"chip " + (c.score >= 0.8 ? "ok" : c.score >= 0.5 ? "warn" : "danger")} title={Array.isArray(c.reasons) ? c.reasons.join("; ") : String(c.reasons || "framing checks passed")}>
                  framing {Math.round(c.score * 100)}%
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
              <span className="small">{cands.length + i === cands.length ? "generating…" : "queued"}</span>
            </div>
            <div className="meta"><span>variation {cands.length + i}</span></div>
          </div>
        ))}
    </div>
  );
}
