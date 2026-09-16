import { Fragment, ReactNode, useEffect, useState } from "react";
import { Activity, Cpu, KeyRound, Languages, Moon, Palette, Settings2, Sun, Monitor } from "lucide-react";
import { api, TOKEN_KEY } from "../api";
import { LANGS, useT } from "../i18n";
import { useStore } from "../store";
import BackendSettings from "../components/BackendSettings";
import { CardHead, Skeleton } from "../components/ui";

function Row({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <div className="settings-row">
      <div className="settings-row-label">
        <b>{label}</b>
        {hint && <span className="small muted">{hint}</span>}
      </div>
      <div className="settings-row-control">{children}</div>
    </div>
  );
}

export default function SettingsPage() {
  const { settings, saveSettings, theme, setTheme, lang, setLang, toast, loadSettings } = useStore();
  const t = useT();
  const [health, setHealth] = useState<any>(null);
  const [caps, setCaps] = useState<any>(null);
  const [tok, setTok] = useState<string>(() => {
    try { return localStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; }
  });
  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth({ ok: false }));
    api.capabilities().then(setCaps).catch(() => {});
    loadSettings().catch(() => {});
  }, []);
  const save = async (p: Record<string, unknown>) => {
    try {
      await saveSettings(p);
      toast(t("common.saved"));
    } catch (e: any) {
      toast(e.message, true);
    }
  };
  const saveToken = () => {
    try {
      tok ? localStorage.setItem(TOKEN_KEY, tok) : localStorage.removeItem(TOKEN_KEY);
      toast(t("settings.tokenSaved"));
    } catch {}
  };
  const g = caps?.gpu?.available ? caps.gpu.gpus[0] : null;
  return (
    <>
      <div className="page-head"><div><h1>{t("settings.title")}</h1><p>{t("settings.subtitle")}</p></div></div>
      <div className="grid wide" style={{ alignItems: "start" }}>
        <div className="stack loose">
          <div className="card pad stack">
            <CardHead icon={<Settings2 size={15} />} title={t("settings.workflow")} />
            {settings ? (
              <>
                <label className="switch" style={{ alignItems: "flex-start" }}>
                  <input type="checkbox" checked={settings.auto_process} onChange={(e) => save({ auto_process: e.target.checked })} />
                  <span className="switch-text">
                    <b>{t("settings.autoProcess")}</b>
                    <span className="small muted">{t("settings.autoProcessHint")}</span>
                  </span>
                </label>
                <div className="field">
                  <label>{t("settings.defaultImageModel")}</label>
                  <select className="input" value={settings.default_image_model} onChange={(e) => save({ default_image_model: e.target.value })}>
                    {(caps?.image_models || []).map((m: any) => <option key={m.id} value={m.id} disabled={m.available === false}>{m.label}{m.available === false ? t("settings.unavailableSuffix") : ""} · ~{m.est_s < 60 ? m.est_s + " s" : Math.round(m.est_s / 60) + " min"}/image</option>)}
                  </select>
                </div>
                <div>
                  <Row label={t("settings.defaultVariations")}>
                    <select className="input" value={settings.default_variations} onChange={(e) => save({ default_variations: Number(e.target.value) })}>{[1, 2, 3, 4, 6, 8].map((n) => <option key={n} value={n}>{n}</option>)}</select>
                  </Row>
                  <Row label={t("settings.defaultQuality")}>
                    <select className="input" value={settings.default_quality} onChange={(e) => save({ default_quality: e.target.value })}><option value="balanced">{t("settings.qualityBalanced")}</option><option value="quality">{t("settings.qualityQuality")}</option></select>
                  </Row>
                  <Row label={t("settings.defaultStyle")}>
                    <select className="input" value={settings.default_style} onChange={(e) => save({ default_style: e.target.value })}>{Object.entries(caps?.styles || {}).map(([id, s]: any) => <option key={id} value={id}>{s.label || id}</option>)}</select>
                  </Row>
                  <Row label={t("settings.defaultTriangles")}>
                    <select className="input" value={settings.default_target_triangles} onChange={(e) => save({ default_target_triangles: Number(e.target.value) })}>{[3000, 6000, 8000, 12000, 20000, 30000, 50000].map((n) => <option key={n} value={n}>{n.toLocaleString()}</option>)}</select>
                  </Row>
                  <Row label={t("settings.defaultTexture")}>
                    <select className="input" value={settings.default_texture_size} onChange={(e) => save({ default_texture_size: Number(e.target.value) })}>{[512, 1024, 2048, 4096].map((n) => <option key={n} value={n}>{t("common.px", { n })}</option>)}</select>
                  </Row>
                </div>
              </>
            ) : <Skeleton h={120} />}
          </div>
          <BackendSettings />
        </div>

        <div className="stack loose">
          <div className="card pad stack">
            <CardHead icon={<Palette size={15} />} title={t("settings.appearance")} />
            <Row label={t("settings.theme")}>
              <div className="segmented">
                {([["system", Monitor, t("app.themeSystem")], ["light", Sun, t("app.themeLight")], ["dark", Moon, t("app.themeDark")]] as const).map(([id, Icon, label]) => (
                  <button key={id} className={theme === id ? "active" : ""} onClick={() => setTheme(id)} title={label} aria-pressed={theme === id}>
                    <Icon size={14} /> {label}
                  </button>
                ))}
              </div>
            </Row>
            <Row label={t("settings.language")}>
              <div className="segmented">
                {LANGS.map((l) => (
                  <button key={l.id} className={lang === l.id ? "active" : ""} onClick={() => setLang(l.id)} aria-pressed={lang === l.id}>{l.label}</button>
                ))}
              </div>
            </Row>
          </div>

          <div className="card pad stack">
            <CardHead icon={<Activity size={15} />} title={t("settings.service")} />
            {health ? (
              <dl className="kv rows">
                <dt>{t("settings.api")}</dt><dd>{health.ok ? <span className="chip ok">{t("settings.healthy")}</span> : <span className="chip danger">{t("settings.problem")}</span>} <span className="faint small">v{health.version}</span></dd>
                {health.runners && Object.entries(health.runners).map(([k, v]: any) => (
                  <Fragment key={k}>
                    <dt>{t("settings.worker", { name: k })}</dt>
                    <dd>{v.ok ? <span className="chip ok">{v.busy ? t("settings.busy") : t("settings.idle")}</span> : <span className="chip danger">{t("settings.down")}</span>}</dd>
                  </Fragment>
                ))}
                {g && <><dt><Cpu size={13} style={{ verticalAlign: -2 }} /> {t("settings.gpu")}</dt><dd>{g.name} · {t("settings.gpuUsed", { used: Math.round(g.used_mib / 1024), total: Math.round(g.total_mib / 1024) })}</dd></>}
                <dt>{t("settings.queue")}</dt><dd>{health.queue ? t("settings.queueLine", { running: health.queue.running ? 1 : 0, queued: health.queue.queued, held: health.queue.held }) : "–"}</dd>
                <dt>{t("settings.models")}</dt><dd className="small">{caps?.dependencies ? t("settings.pinnedOffline") : "–"} {caps?.features?.multiview_checkpoints_cached ? t("settings.mvCached") : ""}</dd>
              </dl>
            ) : <Skeleton h={80} />}
          </div>

          <div className="card pad stack">
            <CardHead icon={<KeyRound size={15} />} title={t("settings.apiToken")} />
            <div className="small muted">{t("settings.apiTokenHint")}</div>
            <div className="row tight" style={{ flexWrap: "nowrap" }}>
              <input className="input" type="password" value={tok} onChange={(e) => setTok(e.target.value)} placeholder={t("settings.apiTokenPlaceholder")} />
              <button className="btn" onClick={saveToken}>{t("common.save")}</button>
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
