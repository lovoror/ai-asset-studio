import { Job, fmtDuration, stageLabel } from "../api";
import { Progress } from "./ui";

const STAGES = ["reference", "pixal3d", "blender", "validate", "package"] as const;
const ORDER: Record<string, number> = { queued: -1, held: -1, reference: 0, pixal3d: 1, blender: 2, validate: 3, package: 4, done: 5 };

/** Stage stepper + live progress + last event line. Works for image jobs (reference only) and asset jobs. */
export default function JobProgress({ job, compact }: { job: Job; compact?: boolean }) {
  const stages = job.kind === "image" ? (["reference", "package"] as const) : STAGES;
  const cur = ORDER[job.stage] ?? -1;
  const last = job.events?.[job.events.length - 1];
  return (
    <div className="stack" style={{ gap: 8 }}>
      {!compact && (
        <div className="row" style={{ gap: 6 }}>
          {stages.map((s) => {
            const idx = ORDER[s];
            const done = job.status === "completed" || cur > idx;
            const active = job.status === "running" && cur === idx;
            const t = job.timings?.[s];
            return (
              <span key={s} className={"chip " + (done ? "ok" : active ? "accent" : "")}>
                {stageLabel[s]}{t != null && done ? ` · ${fmtDuration(t)}` : ""}
              </span>
            );
          })}
        </div>
      )}
      <Progress job={job} />
      {job.status === "running" && last && <div className="small muted" style={{ whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{last.message}</div>}
      {job.status === "failed" && job.error && "message" in job.error && (
        <div className="chip danger" style={{ whiteSpace: "normal" }}>Failed in {job.error.stage}: {String(job.error.message).slice(0, 300)}</div>
      )}
    </div>
  );
}
