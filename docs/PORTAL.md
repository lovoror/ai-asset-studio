# Web portal

Open **http://127.0.0.1:8090** after `scripts\start.ps1`. The portal is a React app bundled into the `api` container
(no CDNs, works offline). Dark/light follows your OS; the sidebar button overrides it.

## Flow

1. **Create** – type a prompt (or pick an example / "Surprise me"), choose a style, optionally open *Advanced*
   (materials, palette, avoid-list, height, seed, title), pick how many variations (default 4, ~5 min each) →
   *Generate variations*. Only images are made at this point.
2. **Session** – variations appear as they finish. Each shows its seed and a *framing* score (technical hints only:
   clipping, margins, background plainness). Click a picture to open it full-screen and browse the batch (arrow keys;
   Esc closes); the box in its corner selects it (⌘/Ctrl+A selects all) → *Make 3D*.
   The dialog sets quality (balanced 1024 / quality 1536), height, triangle budget, texture size, LODs and collision;
   submitting queues the job straight away, and whether it starts immediately or waits for *Process* follows the
   *process automatically* setting (Settings or the Queue page).
   *More variations* re-runs the same prompt with new seeds.
3. **Queue** – running / held / queued / recently failed. Held items wait until *Process* (or *Process all*); queued
   items can be put back on hold; anything can be cancelled; failed jobs can be retried. GPU usage is shown live.
4. **Library** – every session and asset with thumbnails, search, favourites, archive/restore. Asset page: in-browser
   3D inspector (optimized / LODs / collision / master) with eight display modes (shaded, shaded+wire, wireframe,
   normals, clay, toon, UV check, albedo), overlays (ground grid, axes, bounds, shadow), camera presets,
   click-to-select parts with isolate/focus/hide, live triangle/vertex/draw-call/FPS counts, a texture list and scene
   tree, and screenshot export; preview renders (click to enlarge and browse), stats (triangles, maps, reduction
   error, timings, VRAM), warnings, manifest, per-file and zip downloads, stage logs, *Re-optimise* (new budget
   without regenerating), delete.
5. **Settings** – auto-process, default variations/quality/style/budget/texture, theme, API token, service health.

## Canvas

`/canvas` is the *other* way to make images: a **node graph**, not a form and not one-session-per-idea. It is a
graph because a single card that carries the prompt, the style and the references can only ever describe one
request; separate nodes can be reused — one style node feeds every generator, one reference image feeds any
number of generations, and turning a result into the input of the next generation is a *wire* rather than a copy
of the whole request. (The page used to be exactly those bundled cards; a board saved in that shape is migrated
into nodes on load rather than dropped.)

| node | takes | gives |
|---|---|---|
| **Prompt** | — | `prompt` (the idea, plus a collapsed *more* with materials, palette and an avoid-list) |
| **Style** | — | `style` |
| **Image** | — | `image` (one picture: uploaded, or picked from a job) |
| **Generate 2D** | `prompt`, `style`, `image` × n | `image` (the variations of a job) |
| **Make 3D** | `image` | `asset` (a link to the 3D job) |

* **Ports are typed**, so an edge only joins like to like. `Generate 2D → Generate 2D` is legal — that is how you
  iterate on a result — cycles are refused, and an input that takes a single edge *replaces* it when you drop a
  second one. An input taking several (references) keeps them in the order they were connected, and says which is
  which, because order is meaning: in a two-image edit the first is the subject and the second is what it should
  look like.
* **Connecting:** drag from an output port to an input port; click an edge and press Delete (or its ×) to remove
  it. An incompatible or cyclic target is refused with the reason on screen.
* **A Generate node has no prompt or style of its own** — it reads them from its wires and says so when nothing is
  connected. Only the prompt is required; an unconnected style falls back to the saved default (the port says so)
  and references are optional.
* **It shows the prompt it will send.** The composed instruction comes from `POST /v1/prompt/preview` with the
  parts it was built from, and *Composed* / *My text* decides whether that or your own text (`final_prompt`) goes
  out. Switching to *My text* seeds the box with the composed text, so changing the default starts from the
  default.
* **Pan and zoom** by hand: wheel zooms about the pointer, dragging the background (or the middle button, or
  space-drag) pans, nodes are dragged by their header. No pan/zoom library — `web/src/canvas.ts` is the whole
  model (node/port/edge types, connection rules, view math, localStorage).
* Results stay on the node: click to enlarge (the same lightbox as Session, arrow keys browse) or send one to the
  3D pipeline, either from the result or through a *Make 3D* node.
* Everything (nodes, wires, view, and uploads up to the browser's storage quota) is kept in
  `localStorage["as-canvas"]`; if the quota is hit, the uploaded *bytes* are dropped and the node says so rather
  than generating from a picture that is gone.
* A node showing its composed prompt can be taller than the viewport, so after typing you may need to pan or press
  **适应 / Fit** to bring its *Generate* button fully into view. Adding a node already brings it into view.
* The image lane must be able to take a reference: references need a *LoadImage* graph, and if the configured
  workflow is a text-to-image one the pipeline substitutes the shipped `krea2_edit_refs.json` and records that in
  the job's warnings (see [`PROMPTS.md`](PROMPTS.md) §4).

## Manual checklist (used for acceptance)

- [ ] Create page renders in light and dark; examples load; Ctrl+Enter submits.
- [ ] Session page shows placeholders, then images with framing chips; clicking a picture enlarges it and ←/→ browses
      the batch; the corner box selects; the Make 3D dialog works.
- [ ] With *process automatically* on (the default) a submitted 3D job goes straight to queued; with it off the item
      appears in Queue as waiting to start and *Process* runs it. The asset page then shows the inspector + stats.
- [ ] Library lists sessions and assets; search/favourite/archive/restore/purge work.
- [ ] Zip download contains GLBs, previews, textures, manifest (master excluded unless `?include_master=true`).
- [ ] Re-optimise re-runs only the Blender stage.
- [ ] The asset inspector switches between all eight display modes without console errors.
- [ ] Canvas: a fresh board arrives wired (Prompt → Generate 2D); dragging from an output port to an input port
      connects, an incompatible or cyclic drop is refused with a reason, and selecting an edge + Delete removes it;
      typing in the prompt node fills the Generate node's composed prompt from the server; **Fit** frames every
      node; a reference picked from an earlier session generates through the edit graph; *make 3D* works from a
      result and from a Make 3D node; reloading the page puts the board back, and a board saved by the older
      card-based page is migrated rather than dropped.
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
