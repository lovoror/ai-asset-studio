import { create } from "zustand";
import { api, LANG_KEY, Lang, Settings, withToken } from "./api";

type Theme = "system" | "light" | "dark";
interface Toast { id: number; text: string; err?: boolean }

interface State {
  theme: Theme;
  setTheme: (t: Theme) => void;
  lang: Lang;
  setLang: (l: Lang) => void;
  toasts: Toast[];
  toast: (text: string, err?: boolean) => void;
  settings: Settings | null;
  loadSettings: () => Promise<Settings>;
  saveSettings: (s: Partial<Settings>) => Promise<void>;
  // live updates: a monotonically increasing tick that pages subscribe to (bumped on every SSE job event)
  tick: number;
  lastEvent: { job_id: string; message: string; level: string } | null;
  startEvents: () => void;
  queueCounts: { running: number; queued: number; held: number };
}

let es: EventSource | null = null;
let seq = 0;

export const useStore = create<State>((set, get) => ({
  theme: ((): Theme => {
    try {
      const t = localStorage.getItem("as-theme");
      return t === "dark" || t === "light" ? t : "system";
    } catch {
      return "system";
    }
  })(),
  setTheme: (t) => {
    try {
      if (t === "system") localStorage.removeItem("as-theme");
      else localStorage.setItem("as-theme", t);
    } catch {}
    if (t === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = t;
    set({ theme: t });
  },
  lang: ((): Lang => {
    try {
      const stored = localStorage.getItem(LANG_KEY);
      if (stored === "zh" || stored === "en") return stored;
      // first visit: follow the browser, so a Chinese browser opens in Chinese
      return (navigator.language || "").toLowerCase().startsWith("zh") ? "zh" : "en";
    } catch {
      return "en";
    }
  })(),
  setLang: (l) => {
    try {
      localStorage.setItem(LANG_KEY, l);
    } catch {}
    document.documentElement.lang = l === "zh" ? "zh-CN" : "en";
    set({ lang: l });
  },
  toasts: [],
  toast: (text, err) => {
    const id = ++seq;
    set({ toasts: [...get().toasts, { id, text, err }] });
    setTimeout(() => set({ toasts: get().toasts.filter((t) => t.id !== id) }), err ? 7000 : 3500);
  },
  settings: null,
  loadSettings: async () => {
    const s = await api.settings();
    set({ settings: s });
    return s;
  },
  saveSettings: async (p) => {
    const s = await api.putSettings(p);
    set({ settings: s });
  },
  tick: 0,
  lastEvent: null,
  queueCounts: { running: 0, queued: 0, held: 0 },
  startEvents: () => {
    if (es) return;
    const open = () => {
      es = new EventSource(withToken("/v1/events"));
      es.addEventListener("job", (e) => {
        try {
          const d = JSON.parse((e as MessageEvent).data);
          set({ tick: get().tick + 1, lastEvent: d });
        } catch {}
      });
      es.onerror = () => {
        es?.close();
        es = null;
        setTimeout(open, 3000);
      };
    };
    open();
    const poll = async () => {
      try {
        const h = await api.health();
        set({ queueCounts: { running: h.queue.running ? 1 : 0, queued: h.queue.queued, held: h.queue.held } });
      } catch {}
    };
    poll();
    setInterval(poll, 5000);
  },
}));
