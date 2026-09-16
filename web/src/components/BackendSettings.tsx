import { useEffect, useState } from "react";
import { AlertTriangle, PlugZap, Server } from "lucide-react";
import { api, ProbeResult } from "../api";
import { useGate, useT } from "../i18n";
import { useStore } from "../store";
import { CardHead, Spinner } from "./ui";

const STAGES = ["image", "pixal3d", "blender"] as const;
const LANES = ["image", "three_d"] as const;

interface Form {
  image_kind: "local" | "comfyui";
  image_url: string;
  image_workflow: string;
  three_d_kind: "local" | "comfyui";
  three_d_url: string;
  three_d_workflow: string;
  worker_endpoints: Record<string, string>;
}

type Settings = Record<string, any>;

/** The form starts from the saved settings; the same function decides whether anything is unsaved. */
function formFrom(s: Settings): Form {
  return {
    image_kind: s.image_kind || "local",
    image_url: s.image_url || "",
    image_workflow: s.image_workflow || "",
    three_d_kind: s.three_d_kind || "comfyui",
    three_d_url: s.three_d_url || "",
    three_d_workflow: s.three_d_workflow || "",
    worker_endpoints: { ...(s.worker_endpoints || {}) },
  };
}
/** Only the addresses that are actually set count as a change, so typing then clearing is not "unsaved". */
function normEndpoints(e?: Record<string, string>): string {
  return JSON.stringify(Object.entries(e || {})
    .map(([k, v]) => [k, (v || "").trim()] as const)
    .filter(([, v]) => v)
    .sort(([a], [b]) => a.localeCompare(b)));
}
function isDirty(form: Form, s?: Settings | null): boolean {
  if (!s) return false;
  const base = formFrom(s);
  return form.image_kind !== base.image_kind || form.image_url !== base.image_url
    || form.image_workflow !== base.image_workflow || form.three_d_kind !== base.three_d_kind
    || form.three_d_url !== base.three_d_url || form.three_d_workflow !== base.three_d_workflow
    || normEndpoints(form.worker_endpoints) !== normEndpoints(base.worker_endpoints);
}

/** Settings for the addresses: which engine each GPU stage uses, and where each stage worker runs.
 *
 * Every row has a Test button that probes *the value in the box* (saved or not), and the ComfyUI probes are
 * issued through the stage worker - the machine that will actually talk to that server - so the answer means
 * something. Failures are also enforced: the API refuses to start a job whose stage server is down.
 */
export default function BackendSettings() {
  const { settings, saveSettings, toast } = useStore();
  const t = useT();
  const gate = useGate();
  const [caps, setCaps] = useState<any>(null);
  const [ready, setReady] = useState<any>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, ProbeResult>>({});
  const [form, setForm] = useState<Form | null>(null);

  const refresh = () => api.readiness().then(setReady).catch(() => {});
  useEffect(() => {
    api.capabilities().then(setCaps).catch(() => {});
    refresh();
  }, []);
  useEffect(() => {
    if (settings && !form) setForm(formFrom(settings));
  }, [settings, form]);

  if (!form) return <div className="card pad"><div className="skeleton" style={{ height: 160 }} /></div>;

  const workflows: string[] = caps?.workflows || [];
  const ports: Record<string, number> = caps?.worker_ports || {};
  const dirty = isDirty(form, settings);
  const patch = (p: Partial<Form>) => setForm({ ...form, ...p });

  const test = async (key: string, body: { target: "worker" | "comfyui"; role: string; url?: string; workflow?: string }) => {
    setBusy(key);
    setResults((r) => ({ ...r, [key]: undefined as unknown as ProbeResult }));
    try {
      const res = await api.testBackend(body);
      setResults((r) => ({ ...r, [key]: res }));
    } catch (e: any) {
      setResults((r) => ({ ...r, [key]: { ok: false, target: body.target, detail: e.message || String(e) } }));
    } finally {
      setBusy(null);
    }
  };

  const save = async (p: Partial<Form>, msg?: string) => {
    try {
      await saveSettings(p as any);
      toast(msg || t("common.saved"));
      refresh();
    } catch (e: any) {
      toast(e.message, true);
    }
  };

  // Plain render helpers, not components: a component declared inside the render body gets a new type identity on
  // every render, and React would remount its subtree - which costs the address input its focus on every keystroke.
  const testButton = (k: string, onClick: () => void) => (
    <button className="btn" disabled={busy === k} onClick={onClick}>
      {busy === k ? <Spinner /> : <PlugZap size={14} />} {busy === k ? t("settings.testing") : t("settings.test")}
    </button>
  );

  const renderLane = (lane: (typeof LANES)[number], role: "image" | "pixal3d") => {
    const kind = lane === "image" ? form.image_kind : form.three_d_kind;
    const url = lane === "image" ? form.image_url : form.three_d_url;
    const workflow = lane === "image" ? form.image_workflow : form.three_d_workflow;
    const key = `lane-${lane}`;
    const res = results[key];
    // The saved state of this lane, as the API sees it right now - shown next to the Test result, which only
    // reports what was typed.
    const live = ready?.backends?.[lane];
    const stageRole = role === "image" ? "image" : "pixal3d";
    return (
      <div className="stack" key={lane}>
        <div className="row between">
          <b>{t(`lane.${lane}`)}</b>
          <div className="row tight">
            {kind === "comfyui" && live && (
              <span className={"chip " + (live.ok ? "ok" : "danger")}>
                <span className="dot" /> {live.ok ? t("settings.reachable") : t("settings.unreachable")}
              </span>
            )}
            <select className="input" style={{ width: 150 }} value={kind} aria-label={t(`lane.${lane}`)}
                    onChange={(e) => patch(lane === "image" ? { image_kind: e.target.value as any } : { three_d_kind: e.target.value as any })}>
              <option value="local">{t("settings.kindLocal")}</option>
              <option value="comfyui">{t("settings.kindComfy")}</option>
            </select>
          </div>
        </div>
        {kind === "comfyui" ? (
          <>
            <div className="row" style={{ gap: 8, alignItems: "flex-end" }}>
              <div className="field" style={{ flex: 1 }}>
                <label>{t("settings.address")}</label>
                <input className="input" value={url} placeholder={t("settings.addressPlaceholder")}
                       onChange={(e) => patch(lane === "image" ? { image_url: e.target.value } : { three_d_url: e.target.value })} />
              </div>
              {testButton(key, () => test(key, { target: "comfyui", role, url, workflow }))}
            </div>
            <div className="row" style={{ gap: 8, alignItems: "flex-end" }}>
              <div className="field" style={{ flex: 1 }}>
                <label>{t("settings.workflowFile")}</label>
                <select className="input" value={workflow}
                        onChange={(e) => patch(lane === "image" ? { image_workflow: e.target.value } : { three_d_workflow: e.target.value })}>
                  <option value="">{t("settings.noWorkflow")}</option>
                  {workflows.map((w) => <option key={w} value={w}>{w}</option>)}
                </select>
              </div>
              {testButton(key, () => test(key, { target: "comfyui", role, url, workflow }))}
            </div>
          </>
        ) : (
          <div className="small muted">{t("settings.kindLocal")} · {ready?.endpoints?.[stageRole] || "local"}</div>
        )}
        <ProbeLine res={res} note={!res && kind === "comfyui" ? live?.detail : undefined} />
      </div>
    );
  };

  return (
    <>
      <div className="card pad stack loose">
        <CardHead icon={<PlugZap size={15} />} title={t("settings.backends")} hint={t("settings.backendsHint")}
                  actions={dirty ? <span className="chip warn">{t("settings.unsaved")}</span> : undefined} />
        {renderLane("image", "image")}
        <div className="sep" style={{ margin: 0 }} />
        {renderLane("three_d", "pixal3d")}
        <div className="row end">
          <button className="btn primary" disabled={!dirty} onClick={() => save({
            image_kind: form.image_kind, image_url: form.image_url.trim(), image_workflow: form.image_workflow,
            three_d_kind: form.three_d_kind, three_d_url: form.three_d_url.trim(), three_d_workflow: form.three_d_workflow,
          })}>{t("common.save")}</button>
        </div>
      </div>

      <div className="card pad stack loose">
        <CardHead icon={<Server size={15} />} title={t("settings.workers")} hint={t("settings.workersHint")}
                  actions={normEndpoints(form.worker_endpoints) !== normEndpoints((settings || {}).worker_endpoints)
                    ? <span className="chip warn">{t("settings.unsaved")}</span> : undefined} />
        {STAGES.map((role) => {
          const raw = form.worker_endpoints[role] ?? ready?.endpoints?.[role] ?? "local";
          const key = `worker-${role}`;
          const live = ready?.workers?.[role];
          return (
            <div className="row" key={role} style={{ gap: 8, alignItems: "flex-end" }}>
              <div className="field" style={{ flex: 1 }}>
                <label>{t(`role.${role}`)}</label>
                <input className="input" value={raw} placeholder={`local · ${ports[role] ?? ""}`}
                       onChange={(e) => patch({ worker_endpoints: { ...form.worker_endpoints, [role]: e.target.value } })} />
              </div>
              {testButton(key, () => test(key, { target: "worker", role, url: raw }))}
            </div>
          );
        })}
        {STAGES.map((role) => results[`worker-${role}`] && (
          <ProbeLine key={role} res={results[`worker-${role}`]} lead={t(`role.${role}`)} />
        ))}
        <div className="row between">
          <span className="small faint">{t("settings.workersRestartHint")}</span>
          <div className="row tight">
            <button className="btn sm ghost" onClick={() => save({ worker_endpoints: {} }, t("settings.endpointsSaved"))}>
              {t("settings.resetEndpoints")}
            </button>
            <button className="btn primary" disabled={normEndpoints(form.worker_endpoints) === normEndpoints((settings || {}).worker_endpoints)}
                    onClick={() => save({ worker_endpoints: form.worker_endpoints }, t("settings.endpointsSaved"))}>
              {t("common.save")}
            </button>
          </div>
        </div>
      </div>

      {ready && !ready.create_image?.ready && (
        <div className="callout danger">
          <div className="callout-title"><AlertTriangle size={14} /> {t("gate.blockedTitle")}</div>
          <div className="small">{t("gate.blockedHint")}</div>
          <ul>
            {gate(ready.create_image.problems).map((s, i) => <li key={i}>{s}</li>)}
          </ul>
        </div>
      )}
    </>
  );
}

function ProbeLine({ res, note, lead }: { res?: ProbeResult; note?: string; lead?: string }) {
  const t = useT();
  if (!res) {
    if (!note) return null;
    return <div className="small faint">{lead ? `${lead}: ` : ""}{note}</div>;
  }
  return (
    <div className="small">
      {lead && <span className="faint">{lead}: </span>}
      <span className={"chip " + (res.ok ? "ok" : "danger")}>{res.ok ? t("settings.reachable") : t("settings.unreachable")}</span>{" "}
      <span className={res.ok ? "muted" : ""} style={{ color: res.ok ? undefined : "var(--danger)" }}>{res.detail}</span>
      {res.elapsed_ms != null && <span className="faint"> · {res.elapsed_ms} ms</span>}
    </div>
  );
}
