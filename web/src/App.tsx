import { useEffect } from "react";
import { NavLink, Route, Routes } from "react-router-dom";
import { Box, Images, Library, ListOrdered, Settings as SettingsIcon, Sparkles, Sun, Moon, Monitor } from "lucide-react";
import { useStore } from "./store";
import { Toasts } from "./components/ui";
import Create from "./pages/Create";
import Session from "./pages/Session";
import Queue from "./pages/Queue";
import LibraryPage from "./pages/Library";
import Asset from "./pages/Asset";
import SettingsPage from "./pages/Settings";

export default function App() {
  const { theme, setTheme, startEvents, loadSettings, queueCounts } = useStore();
  useEffect(() => {
    startEvents();
    loadSettings().catch(() => {});
  }, []);
  const active = queueCounts.running + queueCounts.queued + queueCounts.held;
  const nextTheme: Record<string, "system" | "light" | "dark"> = { system: "light", light: "dark", dark: "system" };
  const ThemeIcon = theme === "dark" ? Moon : theme === "light" ? Sun : Monitor;
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="logo"><Box size={16} /></span> asset-studio
        </div>
        <nav className="nav stack" style={{ gap: 2 }}>
          <NavLink to="/" end><Sparkles size={16} /> Create</NavLink>
          <NavLink to="/queue"><ListOrdered size={16} /> Queue {active > 0 && <span className="count">{active}</span>}</NavLink>
          <NavLink to="/library"><Library size={16} /> Library</NavLink>
          <NavLink to="/library?kind=image"><Images size={16} /> Variations</NavLink>
          <NavLink to="/settings"><SettingsIcon size={16} /> Settings</NavLink>
        </nav>
        <div className="spacer" />
        <button className="btn ghost sm" onClick={() => setTheme(nextTheme[theme])} title={`Theme: ${theme}`}>
          <ThemeIcon size={15} /> {theme === "system" ? "System theme" : theme === "dark" ? "Dark" : "Light"}
        </button>
        <div className="faint small" style={{ padding: "6px 10px" }}>local · RTX 5090</div>
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
