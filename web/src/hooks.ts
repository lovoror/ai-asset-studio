import { useCallback, useEffect, useRef, useState } from "react";
import { api, Job } from "./api";
import { useStore } from "./store";

/** Load a job and keep it fresh: refetch on every SSE event for it, plus a slow poll while it is active. */
export function useJob(id: string | undefined, events = 30) {
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);
  const tick = useStore((s) => s.tick);
  const lastEvent = useStore((s) => s.lastEvent);
  const busy = useRef(false);
  const load = useCallback(async () => {
    if (!id || busy.current) return;
    busy.current = true;
    try {
      setJob(await api.job(id, events));
      setError(null);
    } catch (e: any) {
      setError(e.message || String(e));
    } finally {
      busy.current = false;
    }
  }, [id, events]);
  useEffect(() => {
    load();
  }, [load]);
  useEffect(() => {
    if (lastEvent && (lastEvent.job_id === id || (job && job.kind === "image"))) load();
  }, [tick]);
  useEffect(() => {
    if (!job || !["running", "queued", "held"].includes(job.status)) return;
    const t = setInterval(load, job.status === "running" ? 4000 : 10000);
    return () => clearInterval(t);
  }, [job?.status, load]);
  return { job, error, reload: load };
}

/** Re-run a loader on SSE ticks (debounced) and on an interval. */
export function useLive(loader: () => Promise<void> | void, intervalMs = 8000) {
  const tick = useStore((s) => s.tick);
  useEffect(() => {
    const t = setTimeout(() => loader(), 250);
    return () => clearTimeout(t);
  }, [tick]);
  useEffect(() => {
    loader();
    const t = setInterval(loader, intervalMs);
    return () => clearInterval(t);
  }, []);
}
