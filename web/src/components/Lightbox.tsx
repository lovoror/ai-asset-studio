import { ReactNode, useCallback, useEffect } from "react";
import { ChevronLeft, ChevronRight, X } from "lucide-react";
import { useT } from "../i18n";

export interface LightboxItem {
  /** Full-resolution image: this is what the lightbox is for. */
  url: string;
  /** Optional small version, shown behind `url` while it loads so browsing never shows a blank frame. */
  preview?: string;
  /** Shown bottom-left, e.g. the file name. */
  caption?: string;
  /** Extra detail after the caption, e.g. the seed. */
  note?: string;
}

/** Full-screen image viewer: as large as the viewport allows, browse with ←/→, close with Esc or the backdrop.
 *
 * Deliberately not a `Modal`: this is for looking at a picture, so the picture gets the whole window and the chrome
 * is confined to a thin bar. The optional `action` renders next to that bar for the *current* image, which is how
 * the candidate grid keeps its select/deselect button while gaining browsing.
 */
export default function Lightbox({ items, index, onClose, onIndex, action }: {
  items: LightboxItem[];
  index: number;
  onClose: () => void;
  onIndex: (index: number) => void;
  action?: (index: number) => ReactNode;
}) {
  const t = useT();
  const count = items.length;
  const step = useCallback((delta: number) => {
    if (count < 2) return;
    onIndex((index + delta + count) % count);
  }, [count, index, onIndex]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.preventDefault(); onClose(); }
      else if (e.key === "ArrowLeft") { e.preventDefault(); step(-1); }
      else if (e.key === "ArrowRight") { e.preventDefault(); step(1); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, step]);

  const item = items[index];
  if (!item) return null;

  return (
    <div className="lightbox" role="dialog" aria-modal="true" aria-label={item.caption || t("asset.preview")}
         onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="lightbox-stage" style={item.preview ? { backgroundImage: `url("${item.preview}")` } : undefined}>
        <img className="lightbox-img" src={item.url} alt={item.caption || ""} onMouseDown={(e) => e.stopPropagation()} />
      </div>
      <button className="lightbox-btn lightbox-close" onClick={onClose} aria-label={t("common.close")} title={t("common.close")}>
        <X size={18} />
      </button>
      {count > 1 && (
        <>
          <button className="lightbox-btn lightbox-nav prev" onClick={() => step(-1)} aria-label={t("lightbox.prev")} title={t("lightbox.prev")}>
            <ChevronLeft size={22} />
          </button>
          <button className="lightbox-btn lightbox-nav next" onClick={() => step(1)} aria-label={t("lightbox.next")} title={t("lightbox.next")}>
            <ChevronRight size={22} />
          </button>
        </>
      )}
      <div className="lightbox-bar">
        {item.caption && <span className="lightbox-caption">{item.caption}{item.note ? ` · ${item.note}` : ""}</span>}
        {action?.(index)}
        <span className="lightbox-count">{index + 1} / {count}</span>
        <span className="lightbox-hint">{t("lightbox.hint")}</span>
      </div>
    </div>
  );
}
