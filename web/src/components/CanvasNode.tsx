import { memo, useRef } from "react";
import { GripVertical, ImagePlus, Trash2, Upload } from "lucide-react";
import { withToken, type Job } from "../api";
import {
  CanvasGraph, CanvasNode as Node, CanvasReference, NODE_HEAD, NODE_WIDTH, PORTS, PORT_DOT, PORT_INSET,
  PORT_ROW, PORT_TOP, inputEdges, outputEdges, referenceReady,
} from "../canvas";
import { useT } from "../i18n";
import { StatusChip } from "./ui";
import GenerateBody from "./GenerateBody";
import AssetBody from "./AssetBody";

/** Every callback takes the node id rather than closing over it, so the page can hand out one stable set of
    handlers and a pan (which re-renders the page on every frame) does not re-render every node. */
export interface NodeHandlers {
  onDragStart: (id: string, e: React.PointerEvent<HTMLElement>) => void;
  onChange: (id: string, patch: Partial<Node>) => void;
  onRemove: (id: string) => void;
  /** Pointer-down on an *output* dot: the start of a wire. */
  onPortDown: (id: string, port: string, e: React.PointerEvent<HTMLElement>) => void;
  onUpload: (id: string, file: File) => void;
  onPick: (id: string) => void;
  onGenerate: (id: string) => void;
  onOpenResult: (id: string, index: number) => void;
  onSelectOutput: (id: string, file: string) => void;
  onMakeAsset: (id: string) => void;
  onMoveRef: (id: string, edgeId: string, delta: number) => void;
  onRemoveRef: (id: string, edgeId: string) => void;
}

interface Props extends NodeHandlers {
  node: Node;
  /** The whole graph: a node reads its own inputs out of it rather than being handed pre-resolved objects, which
      is what keeps every prop here stable while the view pans. */
  graph: CanvasGraph;
  jobs: Record<string, Job>;
  /** /capabilities, so every option and range comes from the running server rather than a table here. */
  caps: any;
  maxRefs: number;
  varMin: number;
  varMax: number;
  /** The style in force when no style node is wired in, so the port can say what it will fall back to. */
  fallbackStyle: string;
  busy: boolean;
}

/** What every body receives. Exported so the two large bodies live in their own files; the import back here is
    type-only, so the runtime module graph stays acyclic. */
export type NodeProps = Props;

/** One node: a frame, its typed port rows, and a body that depends on the kind.
 *
 * The frame owns the geometry the wires are drawn from - the same NODE_HEAD / PORT_ROW / PORT_TOP constants
 * `canvas.ts` computes the anchors with, applied as inline sizes so a CSS change cannot silently move a wire off
 * its dot. */
function CanvasNode(props: Props) {
  const { node, graph, jobs, caps, busy, onDragStart, onChange, onRemove, onPortDown } = props;
  const t = useT();
  const job = node.kind === "generate" && node.jobId ? jobs[node.jobId] || null : null;
  const styleName = (id: string) => caps?.styles?.[id]?.label || id;

  const body =
    node.kind === "prompt" ? <PromptBody {...props} />
    : node.kind === "style" ? <StyleBody {...props} />
    : node.kind === "image" ? <ImageBody {...props} />
    : node.kind === "generate" ? <GenerateBody {...props} />
    : <AssetBody {...props} />;

  return (
    <div className="canvas-node" data-node-id={node.id}
         style={{ left: node.x, top: node.y, width: NODE_WIDTH[node.kind] }}>
      <div className="canvas-node-head" style={{ height: NODE_HEAD }}
           onPointerDown={(e) => onDragStart(node.id, e)}>
        <GripVertical size={14} className="grip" />
        <span className="chip sm">{t(`canvas.node.${node.kind}`)}</span>
        <div className="row tight" style={{ marginLeft: "auto" }}>
          {job && <StatusChip job={job} />}
          <button className="btn ghost icon sm" onClick={() => onRemove(node.id)}
                  title={t("canvas.nodeDelete")} aria-label={t("canvas.nodeDelete")}>
            <Trash2 size={13} />
          </button>
        </div>
      </div>

      <div className="canvas-ports" style={{ paddingTop: PORT_TOP }}>
        {PORTS[node.kind].in.map((p) => {
          const n = inputEdges(graph, node.id, p.key).length;
          // A required input with nothing wired in is a state the user has to see, and a style falling back to the
          // settings default is a decision on their behalf - so both are said on the port itself.
          const state = n ? (p.many ? t("canvas.portWired", { n }) : t("canvas.portConnected"))
            : p.required ? t("canvas.portRequired")
            : p.type === "style" ? t("canvas.portStyleDefault", { name: styleName(props.fallbackStyle) })
            : p.many ? t("canvas.portOptional") : "";
          return (
            <div key={"in" + p.key} className={"canvas-port in" + (p.required && !n ? " required" : "")}
                 style={{ height: PORT_ROW }} data-port-in={p.key}>
              <span className="canvas-port-dot" style={{ left: PORT_INSET - PORT_DOT / 2 }} />
              <span className="canvas-port-label">{t(p.label)}</span>
              <span className="canvas-port-state small muted">{state}</span>
            </div>
          );
        })}
        {PORTS[node.kind].out.map((p) => {
          const n = outputEdges(graph, node.id, p.key).length;
          return (
            <div key={"out" + p.key} className="canvas-port out" style={{ height: PORT_ROW }}
                 data-port-out={p.key} onPointerDown={(e) => onPortDown(node.id, p.key, e)}>
              <span className="canvas-port-state small muted">{n ? t("canvas.portWired", { n }) : ""}</span>
              <span className="canvas-port-label">{t(p.label)}</span>
              <span className="canvas-port-dot" style={{ right: PORT_INSET - PORT_DOT / 2 }} />
            </div>
          );
        })}
      </div>

      <div className="canvas-node-body">{body}</div>
    </div>
  );
}

/** The idea. The extras are behind a disclosure because a node has to stay small enough to see several at once. */
function PromptBody({ node, onChange }: Props) {
  const t = useT();
  if (node.kind !== "prompt") return null;
  return (
    <>
      <textarea
        className="input"
        rows={3}
        value={node.prompt}
        onChange={(e) => onChange(node.id, { prompt: e.target.value })}
        placeholder={t("create.promptPlaceholder")}
      />
      <button type="button" className="canvas-more" onClick={() => onChange(node.id, { more: !node.more })}>
        {t("canvas.more")}
      </button>
      {node.more && (
        <>
          <div className="field">
            <label>{t("canvas.materials")}</label>
            <input className="input" maxLength={300} value={node.materials}
                   onChange={(e) => onChange(node.id, { materials: e.target.value })} />
          </div>
          <div className="field">
            <label>{t("canvas.palette")}</label>
            <input className="input" value={node.palette} placeholder={t("canvas.paletteHint")}
                   onChange={(e) => onChange(node.id, { palette: e.target.value })} />
          </div>
          <div className="field">
            <label>{t("canvas.avoid")}</label>
            <input className="input" maxLength={300} value={node.avoid}
                   onChange={(e) => onChange(node.id, { avoid: e.target.value })} />
          </div>
        </>
      )}
    </>
  );
}

/** The look. Separate from the prompt because the same wording is routinely wanted under a different style, and
    vice versa - which is exactly what the user complained the single card made impossible. */
function StyleBody({ node, caps, onChange }: Props) {
  const t = useT();
  if (node.kind !== "style") return null;
  const styles: [string, any][] = Object.entries(caps?.styles || {});
  // Before /capabilities answers there is nothing to choose from, and an empty select would silently change the
  // style to nothing: show the one in force until the real list arrives.
  const list: [string, any][] = styles.length ? styles : [[node.style || "mobile_factory", null]];
  return (
    <div className="field">
      <select className="input" value={node.style} onChange={(e) => onChange(node.id, { style: e.target.value })}>
        {list.map(([sid, s]) => (
          <option key={sid} value={sid}>{s?.label || sid}</option>
        ))}
      </select>
      <div className="hint">{t("canvas.styleNodeHint")}</div>
    </div>
  );
}

/** One picture: an upload, or a result of a job the control plane already has. */
function ImageBody({ node, onUpload, onPick }: Props) {
  const t = useT();
  const fileRef = useRef<HTMLInputElement | null>(null);
  if (node.kind !== "image") return null;
  const ref: CanvasReference = node.ref;
  const ready = referenceReady(ref);
  return (
    <>
      <div className={"canvas-ref wide" + (ready ? "" : " missing")}>
        <RefThumb r={ref} />
      </div>
      <div className="row tight">
        <button className="btn sm" onClick={() => fileRef.current?.click()}>
          <Upload size={13} /> {t("canvas.upload")}
        </button>
        <button className="btn sm" onClick={() => onPick(node.id)}>
          <ImagePlus size={13} /> {t("canvas.pick")}
        </button>
      </div>
      {ref.label && <div className="small muted">{ref.label}</div>}
      {/* A node with no picture yet and a node whose bytes the browser could not keep are different states: the
          first needs a picture, the second needs re-attaching, and saying "re-attach" to the first is nonsense. */}
      {!ready && (ref.dropped
        ? <div className="callout danger small">{t("canvas.referenceDropped", { n: 1 })}</div>
        : <div className="hint">{t("canvas.imageEmpty")}</div>)}
      <input ref={fileRef} type="file" accept="image/png,image/jpeg" hidden
             onChange={(e) => {
               const f = e.target.files?.[0];
               if (f) onUpload(node.id, f);
               e.target.value = "";
             }} />
    </>
  );
}

/** A slot's picture: an upload draws from its own bytes, a job reference from the URL the API served it on. */
function RefThumb({ r }: { r: CanvasReference }) {
  if (r.image_b64) return <img src={`data:image/png;base64,${r.image_b64}`} alt="" />;
  if (r.url) return <img src={withToken(r.url)} alt="" />;
  return <span className="canvas-ref-none">?</span>;
}

// The canvas re-renders on every pan frame; every prop here is stable while a node is untouched, so memo pays.
export default memo(CanvasNode);
