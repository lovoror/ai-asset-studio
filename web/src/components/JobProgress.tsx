import { Check } from "lucide-react";
import { Job } from "../api";
import { useFmt, useT } from "../i18n";
import { Progress } from "./ui";

const STAGES = ["reference", "pixal3d", "blender", "validate", "package"] as const;
const ORDER: Record<string, number> = { queued: -1, held: -1, reference: 0, pixal3d: 1, blender: 2, validate: 3, package: 4, done: 5 };

/** Stage stepper + live progress + last event line. Works for image jobs (reference only) and asset jobs. */
export default function JobProgress({ job, compact }: { job: Job; compact?: boolean }) {
  const t = useT();
  const { stage: stageText, dur } = useFmt();
  const stages = job.kind === "image" ? (["reference", "package"] as const) : STAGES;
  const cur = ORDER[job.stage] ?? -1;
  const last = job.events?.[job.events.length - 1];
  return (
    <div className="stack tight">
      {!compact && (
        <div className="steps">
          {stages.map((s, i) => {
            const idx = ORDER[s];
            const done = job.status === "completed" || cur > idx;
            const active = job.status === "running" && cur === idx;
            const time = job.timings?.[s];
            return (
              <span key={s} className={"step" + (done ? " done" : active ? " active" : "")}>
                <span className="step-dot">{done ? <Check size={11} /> : i + 1}</span>
                <span>{stageText(s)}{time != null && done ? ` · ${dur(time)}` : ""}</span>
              </span>
            );
          })}
        </div>
      )}
      <Progress job={job} />
      {job.status === "running" && last && <div className="small muted truncate">{last.message}</div>}
      {job.status === "failed" && job.error && "message" in job.error && (
        <div className="chip danger" style={{ height: "auto", padding: "5px 10px", whiteSpace: "normal", lineHeight: 1.45 }}>
          {t("job.failedIn", { stage: stageText(String(job.error.stage)), msg: String(job.error.message).slice(0, 300) })}
        </div>
      )}
    </div>
  );
}
