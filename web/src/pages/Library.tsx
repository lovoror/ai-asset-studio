import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Archive, Box, Images, Search, Star } from "lucide-react";
import { api, Job, withToken } from "../api";
import { useLive } from "../hooks";
import { useFmt, useT } from "../i18n";
import { useStore } from "../store";
import { Empty, Progress, Skeleton, StatusChip } from "../components/ui";

const PAGE_SIZE = 120;

export default function LibraryPage() {
  const [params, setParams] = useSearchParams();
  const kind = params.get("kind") || "";
  const [q, setQ] = useState(params.get("q") || "");
  const [fav, setFav] = useState(false);
  const [archived, setArchived] = useState(false);
  const [items, setItems] = useState<Job[] | null>(null);
  const { toast } = useStore();
  const t = useT();
  const { ago } = useFmt();
  const load = async () => {
    try {
      const r = await api.library({ kind: kind || undefined, q: q || undefined, favorite: fav ? true : undefined, archived, limit: PAGE_SIZE });
      setItems(r.items);
    } catch (e: any) {
      toast(e.message, true);
      setItems([]);
    }
  };
  useLive(load, 10000);
  const setKind = (k: string) => {
    const p = new URLSearchParams(params);
    k ? p.set("kind", k) : p.delete("kind");
    setParams(p);
    setTimeout(load, 0);
  };
  const toggleFav = async (e: React.MouseEvent, j: Job) => {
    e.preventDefault();
    await api.patch(j.id, { favorite: !j.favorite });
    load();
  };
  const restore = async (e: React.MouseEvent, j: Job) => {
    e.preventDefault();
    await api.patch(j.id, { archived: false });
    load();
  };
  const visible = items ?? [];
  const href = (j: Job) => (j.kind === "image" ? `/sessions/${j.id}` : `/assets/${j.id}`);
  return (
    <>
      <div className="page-head">
        <div><h1>{t("library.title")}</h1><p>{t("library.subtitle")}</p></div>
      </div>

      <div className="toolbar" style={{ marginBottom: 18 }}>
        <div className="segmented">
          <button className={kind === "" ? "active" : ""} onClick={() => setKind("")}>{t("library.all")}</button>
          <button className={kind === "asset" ? "active" : ""} onClick={() => setKind("asset")}><Box size={13} /> {t("library.assets3d")}</button>
          <button className={kind === "image" ? "active" : ""} onClick={() => setKind("image")}><Images size={13} /> {t("library.variations")}</button>
        </div>
        <div className="input-wrap" style={{ flex: "1 1 220px", maxWidth: 320 }}>
          <Search size={15} />
          <input className="input" placeholder={t("library.searchPlaceholder")} value={q} aria-label={t("library.searchPlaceholder")}
                 onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && load()} />
        </div>
        <div className="row tight" style={{ marginLeft: "auto" }}>
          <button className={"btn sm" + (fav ? " primary" : "")} onClick={() => { setFav(!fav); setTimeout(load, 0); }}>
            <Star size={14} fill={fav ? "currentColor" : "none"} /> {t("library.favourites")}
          </button>
          <button className={"btn sm" + (archived ? " primary" : "")} onClick={() => { setArchived(!archived); setTimeout(load, 0); }}>
            <Archive size={14} /> {t("library.archived")}
          </button>
        </div>
      </div>

      {items === null ? (
        <div className="grid cards">
          {Array.from({ length: 8 }).map((_, i) => (
            <div key={i} className="card">
              <div className="skeleton" style={{ aspectRatio: "4 / 3", borderRadius: "var(--radius) var(--radius) 0 0" }} />
              <div className="stack tight" style={{ padding: "11px 13px 13px" }}>
                <Skeleton h={14} w="72%" />
                <Skeleton h={20} w="46%" />
              </div>
            </div>
          ))}
        </div>
      ) : visible.length === 0 ? (
        <Empty icon={<Images size={24} />} title={archived ? t("library.noArchived") : t("library.empty")} hint={t("library.emptyHint")}
               action={<Link className="btn primary" to="/">{t("app.create")}</Link>} />
      ) : (
        <div className="grid cards">
          {visible.map((j) => (
            <div key={j.id} className="card lib-card hover">
              <div className="thumb">
                <Link to={href(j)} className="thumb-link" tabIndex={-1} aria-hidden="true">
                  {j.thumb ? <img src={withToken(j.thumb)} alt="" loading="lazy" />
                           : j.kind === "image" ? <Images size={30} className="faint" /> : <Box size={30} className="faint" />}
                </Link>
                <button className={"btn ghost icon sm fav" + (j.favorite ? " on" : "")} onClick={(e) => toggleFav(e, j)}
                        title={t("common.favouriteTitle")} aria-label={t("common.favouriteTitle")}
                        style={{ color: j.favorite ? "var(--warn)" : undefined }}>
                  <Star size={14} fill={j.favorite ? "currentColor" : "none"} />
                </button>
              </div>
              <div className="body">
                <Link to={href(j)} className="title" title={j.title || j.request.prompt}>{j.title || j.request.prompt}</Link>
                <div className="row tight">
                  <StatusChip job={j} />
                  <span className="chip">{j.kind === "image"
                    ? t("library.images", { a: j.candidate_count ?? 0, b: j.settings?.reference?.candidates ?? "?" })
                    : t("library.assetMeta", { n: j.request.target_triangles?.toLocaleString?.() ?? "" })}</span>
                  {j.kind === "image" && (j.children_count || 0) > 0 && <span className="chip accent"><Box size={12} /> {j.children_count}</span>}
                </div>
                <Progress job={j} />
                <div className="row between">
                  <span className="small faint truncate">{ago(j.created_at)} · {j.request.style}</span>
                  {archived && <button className="btn sm ghost" onClick={(e) => restore(e, j)}>{t("common.restore")}</button>}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </>
  );
}
