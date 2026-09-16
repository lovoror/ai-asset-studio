# Web portal

Open **http://127.0.0.1:8090** after `scripts\start.ps1`. The portal is a React app bundled into the `api` container
(no CDNs, works offline). Dark/light follows your OS; the sidebar button overrides it.

## Flow

1. **Create** – type a prompt (or pick an example / "Surprise me"), choose a style, optionally open *Advanced*
   (materials, palette, avoid-list, height, seed, title), pick how many variations (default 4, ~5 min each) →
   *Generate variations*. Only images are made at this point.
2. **Session** – variations appear as they finish. Each shows its seed and a *framing* score (technical hints only:
   clipping, margins, background plainness). Click to select (⌘/Ctrl+A selects all, Enter zooms) → *Make 3D*.
   The dialog sets quality (balanced 1024 / quality 1536), height, triangle budget, texture size, LODs, collision, and
   **Start now / Hold for review**. Your choice is remembered as the *process automatically* setting.
   *More variations* re-runs the same prompt with new seeds.
3. **Queue** – running / held / queued / recently failed. Held items wait until *Process* (or *Process all*); queued
   items can be put back on hold; anything can be cancelled; failed jobs can be retried. GPU usage is shown live.
4. **Library** – every session and asset with thumbnails, search, favourites, archive/restore. Asset page: in-browser
   3D viewer (optimized / LODs / collision / master), preview renders, stats (triangles, maps, reduction error,
   timings, VRAM), warnings, manifest, per-file and zip downloads, stage logs, *Re-optimise* (new budget without
   regenerating), delete.
5. **Settings** – auto-process, default variations/quality/style/budget/texture, theme, API token, service health.

## Manual checklist (used for acceptance)

- [ ] Create page renders in light and dark; examples load; Ctrl+Enter submits.
- [ ] Session page shows placeholders, then images with framing chips; selection + Make 3D dialog works.
- [ ] Hold for review → item appears in Queue as held; Process → runs; asset page shows viewer + stats.
- [ ] Start now (toggle on) → job goes straight to queued.
- [ ] Library lists sessions and assets; search/favourite/archive/restore/purge work.
- [ ] Zip download contains GLBs, previews, textures, manifest (master excluded unless `?include_master=true`).
- [ ] Re-optimise re-runs only the Blender stage.
- [ ] `python -m pytest tests/test_portal_mock.py` passes.

## Dev

```powershell
cd web; npm install; npm run dev     # http://localhost:5173 proxying /v1 to the running service
npm run build                        # dist/ - served by the API process (config.toml [paths] web_dist)
npm run check:i18n                   # dictionary + interpolation checks (no browser needed)
```
- [ ] Style editor: edit a preset's look, generate, then `GET /v1/settings` shows it under `style_edits`; Reset to preset removes it; the Custom card requires a clause.

## Languages

The portal is bilingual (English / 简体中文). Everything lives in `web/src/i18n.ts`:

```tsx
const t = useT();                 // strings; re-renders on language change
const { ago, dur, stage } = useFmt();  // localised "3 min ago", "12 s", and pipeline stage names
t("create.subtitle", { n: 4 });   // {var} interpolation
```

* **`en` is the source of truth.** `zh` is typed `Record<MsgKey, string>`, so a key added to `en` and forgotten in
  `zh` fails `tsc -b` (and therefore `npm run build`). There is no way to ship a half-translated dictionary.
* **Plurals** need no library: pass a numeric `n` and the lookup tries `<key>_plural` when `n !== 1`. Chinese has
  no plural form, so both keys usually hold the same sentence.
* **Where it is stored:** `localStorage["as-lang"]` (per browser, so it needs no round-trip and no restart).
  A first-time visitor gets their browser's language; the switcher is on **Settings → Appearance → Language**.
  Consumed by `useStore().lang` / `setLang`, alongside the theme.
* **Adding a language:** add the code to `Lang` in `api.ts`, add a full dictionary to `i18n.ts`, add it to `LANGS`,
  and set `document.documentElement.lang` in `store.setLang` if it is not `en`/`zh`.

**Not translated (a separate job, all of it server-side):** pipeline warnings and error messages, pydantic
validation errors, stage log contents, and the text inside `presets/*.yaml` (style descriptions, image-model
descriptions, example titles). Those reach the UI as data, so they stay English until the backend or the preset
files carry translations.
