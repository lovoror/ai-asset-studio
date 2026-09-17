import { memo } from "react";
import { CanvasEdge, CanvasGraph, edgeBadge, edgeMid, edgePath, nodeById, portAnchor } from "../canvas";
import { useT } from "../i18n";

/** A wire being dragged: from a node's output port, to wherever the pointer is (world coordinates). */
export interface PendingWire { from: string; fromPort: string; x: number; y: number }

interface Props {
  graph: CanvasGraph;
  selected: string | null;
  pending: PendingWire | null;
  onSelect: (id: string | null) => void;
  onDelete: (id: string) => void;
}

/** Room around the outermost anchor so a wire's curve is never clipped by its own svg's box. */
const PAD = 80;

/** The wires, as one svg in world coordinates.
 *
 * Its own box is the bounding box of the endpoints and it is placed at that box's corner, with the drawing shifted
 * back by the same amount: that keeps every path in plain world coordinates (so an anchor is exactly
 * `portAnchor`) while the svg still has a real viewport to paint in, instead of relying on `overflow: visible`
 * behaving the same in every browser. `non-scaling-stroke` keeps a wire hairline at any zoom.
 */
function CanvasEdges({ graph, selected, pending, onSelect, onDelete }: Props) {
  const t = useT();
  const wires = graph.edges.flatMap((edge) => {
    const from = nodeById(graph, edge.from);
    const to = nodeById(graph, edge.to);
    if (!from || !to) return [];
    return [{
      edge,
      a: portAnchor(from, "out", edge.fromPort),
      b: portAnchor(to, "in", edge.toPort),
      badge: edgeBadge(graph, edge),
    }];
  });
  const pendNode = pending ? nodeById(graph, pending.from) : null;
  const pendA = pendNode && pending ? portAnchor(pendNode, "out", pending.fromPort) : null;
  const tip = pending ? { x: pending.x, y: pending.y } : null;
  if (!wires.length && !(pendA && tip)) return null;

  const pts = [...wires.flatMap((w) => [w.a, w.b]), ...(tip ? [tip] : [])];
  const x0 = Math.min(...pts.map((p) => p.x)) - PAD;
  const y0 = Math.min(...pts.map((p) => p.y)) - PAD;
  const x1 = Math.max(...pts.map((p) => p.x)) + PAD;
  const y1 = Math.max(...pts.map((p) => p.y)) + PAD;

  return (
    <svg className="canvas-edges" style={{ left: x0, top: y0, width: x1 - x0, height: y1 - y0 }}
         viewBox={`0 0 ${x1 - x0} ${y1 - y0}`}>
      <g transform={`translate(${-x0} ${-y0})`}>
        {wires.map(({ edge, a, b, badge }) => {
          const d = edgePath(a, b);
          const mid = edgeMid(a, b);
          const isSel = edge.id === selected;
          return (
            <g key={edge.id} className={isSel ? "is-selected" : ""}>
              {/* An invisible fat stroke on top: a 2px hairline is not a click target. */}
              <path className="canvas-wire-hit" d={d} data-edge={edge.id} vectorEffect="non-scaling-stroke"
                    onPointerDown={(e) => { e.stopPropagation(); onSelect(edge.id); }} />
              <path className={"canvas-wire" + (isSel ? " is-selected" : "")} d={d} vectorEffect="non-scaling-stroke" />
              {badge && (
                <>
                  <circle className="canvas-edge-badge" cx={mid.x} cy={mid.y} r={9} />
                  <text className="canvas-edge-badge-text" x={mid.x} y={mid.y}>{badge}</text>
                </>
              )}
              {isSel && (
                <g className="canvas-edge-x" onPointerDown={(e) => { e.stopPropagation(); onDelete(edge.id); }}>
                  <title>{t("canvas.edgeDelete")}</title>
                  <circle cx={mid.x} cy={mid.y - (badge ? 20 : 0)} r={9} />
                  <text x={mid.x} y={mid.y - (badge ? 20 : 0)}>×</text>
                </g>
              )}
            </g>
          );
        })}
        {pendA && tip && <path className="canvas-wire pending" d={edgePath(pendA, tip)}
                              vectorEffect="non-scaling-stroke" />}
      </g>
    </svg>
  );
}

export default memo(CanvasEdges);
