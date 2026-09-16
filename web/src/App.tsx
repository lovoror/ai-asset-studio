import { useEffect } from "react";
import { Link, NavLink, Route, Routes, useLocation } from "react-router-dom";
import { Box, Images, Library, ListOrdered, Settings as SettingsIcon, Sparkles, Sun, Moon, Monitor } from "lucide-react";
import { useStore } from "./store";
import { useT } from "./i18n";
import { Toasts } from "./components/ui";
import Create from "./pages/Create";
import Session from "./pages/Session";
import Queue from "./pages/Queue";
import LibraryPage from "./pages/Library";
import Asset from "./pages/Asset";
import SettingsPage from "./pages/Settings";

const THEMES = [
  ["system", Monitor, "app.themeSystem"],
  ["light", Sun, "app.themeLight"],
  ["dark", Moon, "app.themeDark"],
] as const;

export default function App() {
  const { theme, setTheme, lang, startEvents, loadSettings, queueCounts } = useStore();
  const t = useT();
  const { pathname, search } = useLocation();
  useEffect(() => {
    startEvents();
    loadSettings().catch(() => {});
  }, []);
  useEffect(() => {
    document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  }, [lang]);
  const active = queueCounts.running + queueCounts.queued + queueCounts.held;
  // The two library entries share a pathname and differ only by query, and a detail page belongs to the section it
  // came from - neither of which NavLink's own matching can express, so they are decided here.
  const inVariations = pathname.startsWith("/sessions/") || (pathname === "/library" && search.includes("kind=image"));
  const inLibrary = pathname.startsWith("/assets/") || (pathname === "/library" && !search.includes("kind=image"));
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="logo"><Box size={17} /></span> <span className="name">asset-studio</span>
        </div>
        <nav className="nav">
          <NavLink to="/" end><Sparkles size={16} /> <span>{t("app.create")}</span></NavLink>
          <NavLink to="/queue">
            <ListOrdered size={16} /> <span>{t("app.queue")}</span>
            {active > 0 && <span className="count">{active}</span>}
          </NavLink>
          <Link to="/library" className={inLibrary ? "active" : undefined} aria-current={inLibrary ? "page" : undefined}>
            <Library size={16} /> <span>{t("app.library")}</span>
          </Link>
          <Link to="/library?kind=image" className={inVariations ? "active" : undefined} aria-current={inVariations ? "page" : undefined}>
            <Images size={16} /> <span>{t("app.variations")}</span>
          </Link>
          <NavLink to="/settings"><SettingsIcon size={16} /> <span>{t("app.settings")}</span></NavLink>
        </nav>
        <div className="spacer" />
        <div className="sb-foot">
          <div className="segmented icon-only" role="group" aria-label={t("settings.appearance")}>
            {THEMES.map(([id, Icon, key]) => (
              <button key={id} type="button" className={theme === id ? "active" : ""} onClick={() => setTheme(id)}
                      title={t(key)} aria-pressed={theme === id}>
                <Icon size={15} />
              </button>
            ))}
          </div>
          <div className="sb-note">{t("app.local")}</div>
        </div>
      </aside>
      <main className="main">
        <Routes>
          <Route path="/" element={<Create />} />
          <Route path="/sessions/:id" element={<Session />} />
          <Route path="/queue" element={<Queue />} />
          <Route path="/library" element={<LibraryPage />} />
          <Route path="/assets/:id" element={<Asset />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Routes>
      </main>
      <Toasts />
    </div>
  );
}
