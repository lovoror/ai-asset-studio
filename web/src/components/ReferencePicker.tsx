import { useEffect, useState } from "react";
import { Images } from "lucide-react";
import { api, Candidate, Job, withToken } from "../api";
import { candidatePath, clampLabel } from "../canvas";
import { useT } from "../i18n";
import { Modal, Spinner } from "./ui";

/** A reference that already exists on the server: exactly the `job_id` + `file` pair `ReferenceImage` wants, plus a
    thumbnail so the node has something to draw before the card is generated. */
export interface PickedReference { job_id: string; file: string; label: string; url: string }

export interface PickerSource { key: string; label: string; job: Job }

interface Props {
  /** Results of the other generate nodes on the canvas, in node order. */
  sources: PickerSource[];
  onPick: (ref: PickedReference) => void;
  onClose: () => void;
}

/** Choose an existing image as a reference: from the results already on this canvas, or from the library. */
export default function ReferencePicker({ sources, onPick, onClose }: Props) {
  const t = useT();
  const [lib, setLib] = useState<Job[] | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    api.library({ kind: "image", limit: 24 })
      .then((r) => setLib(r.items))
      .catch(() => { setFailed(true); setLib([]); });
  }, []);

  // A candidate still being written has no file under artifacts/ yet, so it could not be referenced: `partial`
  // marks exactly those.
  const ready = (j: Job): Candidate[] => (j.kind === "image" ? (j.candidates || []).filter((c) => !c.partial) : []);
  const groups: { key: string; label: string; job: Job }[] = [
    ...sources.filter((s) => ready(s.job).length),
    ...(lib || []).filter((j) => ready(j).length).map((j) => ({ key: j.id, label: j.title || j.id, job: j })),
  ];
  const empty = !groups.length && !failed && lib !== null;

  const pick = (job: Job, c: Candidate, label: string) => onPick({
    job_id: job.id,
    file: candidatePath(c.file),
    label: clampLabel(`${label} · ${c.file}`),
    url: c.thumb || c.url,
  });

  return (
    <Modal title={t("canvas.pickerTitle")} onClose={onClose} width={720}
           footer={<button className="btn" onClick={onClose}>{t("common.cancel")}</button>}>
      <div className="stack loose">
        <div className="small muted">{t("canvas.pickerHint")}</div>
        {failed && <div className="callout">{t("canvas.pickerFailed")}</div>}
        {lib === null && !failed && <div className="row tight"><Spinner /> <span className="small muted">{t("canvas.pickerLoading")}</span></div>}
        {empty && <div className="empty"><div className="empty-icon"><Images size={22} /></div><h3>{t("canvas.pickerNone")}</h3><p>{t("canvas.pickerNoneHint")}</p></div>}

        {groups.map((g) => (
          <div key={g.key} className="canvas-pick-group">
            <div className="canvas-pick-head">
              <b>{g.label}</b>
              <span className="small muted">{t("canvas.pickerResults", { n: ready(g.job).length })}</span>
            </div>
            <div className="canvas-pick-grid">
              {ready(g.job).map((c) => (
                <button key={c.file} className="canvas-pick" onClick={() => pick(g.job, c, g.label)} title={c.file}>
                  <img src={withToken(c.thumb || c.url)} alt="" loading="lazy" />
                  {c.seed != null && <span className="canvas-pick-seed">{c.seed}</span>}
                </button>
              ))}
            </div>
          </div>
        ))}
      </div>
    </Modal>
  );
}
