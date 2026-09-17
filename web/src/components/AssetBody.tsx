import { Link } from "react-router-dom";
import { Box } from "lucide-react";
import { withToken, type Job } from "../api";
import { AssetNode, assetSource, readyCandidates } from "../canvas";
import { useT } from "../i18n";
import { StatusChip } from "./ui";
import type { NodeProps } from "./CanvasNode";

/** The 3D asset node: one candidate of the generate node wired into it, turned into an asset.
 *
 * Its input is a single wire, and it only makes sense from a *generate* node - that is where the image job and its
 * candidates come from, and `POST /v1/asset-jobs` needs both. A wire from an image node is type-legal (it carries
 * an image) but has no job behind it, so the node says what it needs instead of failing at the server.
 */
export default function AssetBody(props: NodeProps) {
  const { node, graph, jobs, onChange, onMakeAsset } = props;
  const t = useT();
  // CanvasNode only renders this for an asset node; the cast keeps the body free of narrowing noise.
  const asset = node as AssetNode;
  const src = assetSource(graph, asset, jobs);
  const job: Job | null = asset.jobId ? jobs[asset.jobId] || null : null;
  const candidates = src ? readyCandidates(jobs[src.jobId]) : [];
  const chosen = src ? src.file : null;

  return (
    <>
      {!src && <div className="callout danger small">{t("canvas.assetNeedsGenerate")}</div>}

      {src && candidates.length > 0 && (
        <div className="field">
          <label>{t("canvas.assetPick")}</label>
          <div className="canvas-results">
            {candidates.map((c) => (
              <div key={c.file} className={"canvas-result" + (c.file === chosen ? " is-output" : "")}>
                <button className="canvas-result-img" onClick={() => onChange(asset.id, { candidate: c.file })}
                        title={c.file}>
                  <img src={withToken(c.thumb || c.url || "")} alt="" loading="lazy" />
                </button>
              </div>
            ))}
          </div>
          <div className="hint">{t("canvas.assetFrom", { n: candidates.length })}</div>
        </div>
      )}

      <button className="btn primary block" disabled={!src} onClick={() => onMakeAsset(asset.id)}>
        <Box size={14} /> {t("canvas.make3D")}
      </button>

      {asset.error && <div className="callout danger small">{t("canvas.submitFailed")}: {asset.error}</div>}
      {job && (
        <>
          <div className="row tight"><StatusChip job={job} /></div>
          <Link className="link small" to={`/assets/${job.id}`}>{t("canvas.openJob")}</Link>
        </>
      )}
    </>
  );
}
