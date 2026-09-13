import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Archive, Box, Images, Search, Star } from "lucide-react";
import { api, Job, fmtAgo, withToken } from "../api";
import { useLive } from "../hooks";
import { useStore } from "../store";
import { Empty, Progress, StatusChip } from "../components/ui";

export default function LibraryPage() {
  const [params, setParams] = useSearchParams();
  const kind = params.get("kind") || "";
  const [q, setQ] = useState(params.get("q") || "");
  const [fav, setFav] = useState(false);
  const [archived, setArchived] = useState(false);
  const [items, setItems] = useState<Job[] | null>(null);
  const { toast } = useStore();
  const load = async () => {
    try {
      const r = await api.library({ kind: kind || undefined, q: q || undefined, favorite: fav ? true : undefined, archived, limit: 120 });
      setItems(r.items);
    } catch (e: any) {
      toast(e.message, true);
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
  return (
    <>
      <div className="page-head">
        <div><h1>Library</h1><p>Everything you've generated: image sessions and 3D assets, with previews and downloads.</p></div>
        <div className="row">
          <div className="tabs" style={{ margin: 0, borderBottom: 0 }}>
            <button className={kind === "" ? "active" : ""} onClick={() => setKind("")}>All</button>
            <button className={kind === "asset" ? "active" : ""} onClick={() => setKind("asset")}><Box size={13} /> 3D assets</button>
            <button className={kind === "image" ? "active" : ""} onClick={() => setKind("image")}><Images size={13} /> Variations</button>
          </div>
          <div className="row" style={{ gap: 6 }}>
            <Search size={15} className="faint" />
            <input className="input" style={{ width: 220 }} placeholder="Search prompts…" value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && load()} />
          </div>
          <button className={"btn sm" + (fav ? " primary" : "")} onClick={() => { setFav(!fav); setTimeout(load, 0); }}><Star size={14} /> Favourites</button>
          <button className={"btn sm" + (archived ? " primary" : "")} onClick={() => { setArchived(!archived); setTimeout(load, 0); }}><Archive size={14} /> Archived</button>
        </div>
      </div>
      {items && visible.length === 0 ? (
        <Empty icon={<Images size={36} className="faint" />} title={archived ? "No archived items" : "Your library is empty"} hint="Start on the Create page." action={<Link className="btn primary" to="/">Create</Link>} />
      ) : (
        <div className="grid cards">
          {visible.map((j) => (
            <Link key={j.id} to={j.kind === "image" ? `/sessions/${j.id}` : `/assets/${j.id}`} className="card lib-card">
              <div className="thumb">
                {j.thumb ? <img src={withToken(j.thumb)} alt="" loading="lazy" /> : j.kind === "image" ? <Images size={30} className="faint" /> : <Box size={30} className="faint" />}
              </div>
              <div className="body">
                <div className="row" style={{ justifyContent: "space-between", gap: 6 }}>
                  <div className="title" title={j.title || j.request.prompt}>{j.title || j.request.prompt}</div>
                  <button className="btn ghost icon sm" onClick={(e) => toggleFav(e, j)} title="Favourite" style={{ color: j.favorite ? "var(--warn)" : undefined }}>
                    <Star size={14} fill={j.favorite ? "currentColor" : "none"} />
                  </button>
                </div>
                <div className="row" style={{ gap: 6 }}>
                  <StatusChip job={j} />
                  <span className="chip">{j.kind === "image" ? `${j.candidate_count ?? 0}/${j.settings?.reference?.candidates ?? "?"} images` : `3D · ${j.request.target_triangles?.toLocaleString?.() ?? ""} tris`}</span>
                  {j.kind === "image" && (j.children_count || 0) > 0 && <span className="chip accent"><Box size={12} /> {j.children_count}</span>}
                </div>
                <Progress job={j} />
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <span className="small faint">{fmtAgo(j.created_at)} · {j.request.style}</span>
                  {archived && <button className="btn sm ghost" onClick={(e) => restore(e, j)}>Restore</button>}
                </div>
              </div>
            </Link>
          ))}
        </div>
      )}
    </>
  );
}
