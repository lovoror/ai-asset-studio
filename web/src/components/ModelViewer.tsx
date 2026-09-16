import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { RoomEnvironment } from "three/examples/jsm/environments/RoomEnvironment.js";
import { Camera, Crosshair, Download, Eye, EyeOff, Image as ImageIcon, Info, Maximize2, RotateCw, Ruler } from "lucide-react";
import { fmtInt } from "../api";
import { useT } from "../i18n";

/* ===============================================================================================================
   A model inspector, not a poster viewer.

   Built on three.js rather than <model-viewer>, because the things that make a viewer useful for judging a
   generated asset - normal shading, a wireframe over the shaded surface, a UV checker, per-part selection and
   isolation, live triangle and draw-call counts - are exactly what the poster viewer's public API cannot do.

   React owns the settings; the scene lives in refs and is never rebuilt by a re-render.
   =============================================================================================================== */

export type ViewMode = "shaded" | "shadedWire" | "wire" | "normals" | "matte" | "uv" | "unlit";
type ViewKey = "persp" | "front" | "back" | "left" | "right" | "top" | "bottom";
type BgKey = "light" | "dark" | "studio";
type OverlayKey = "grid" | "axes" | "bounds" | "shadow";

export interface ViewerVariant { label: string; file: string; tris?: number | null }
export interface ViewerMap { name: string; url: string; note?: string }
interface PartInfo { id: string; name: string; tris: number; verts: number; material: string; visible: boolean }
interface LiveStats {
  tris: number; verts: number; meshes: number; materials: number; textures: number;
  draws: number; fps: number; size: [number, number, number] | null;
}

const MODES: ViewMode[] = ["shaded", "shadedWire", "wire", "normals", "matte", "uv", "unlit"];
const VIEWS: ViewKey[] = ["persp", "front", "back", "left", "right", "top", "bottom"];
const BGS: BgKey[] = ["studio", "light", "dark"];
const OVERLAYS: OverlayKey[] = ["grid", "axes", "bounds", "shadow"];

const MODE_LABEL = {
  shaded: "viewer.shaded", shadedWire: "viewer.shadedWire", wire: "viewer.wireframe", normals: "viewer.normals",
  matte: "viewer.matte", uv: "viewer.uv", unlit: "viewer.unlit",
} as const;
const VIEW_LABEL = {
  persp: "viewer.persp", front: "viewer.front", back: "viewer.back", left: "viewer.left",
  right: "viewer.right", top: "viewer.top", bottom: "viewer.bottom",
} as const;
const BG_LABEL = { studio: "viewer.bgStudio", light: "viewer.bgLight", dark: "viewer.bgDark" } as const;
const OVERLAY_LABEL = { grid: "viewer.grid", axes: "viewer.axes", bounds: "viewer.bounds", shadow: "viewer.shadows" } as const;

const BG_COLOR: Record<BgKey, number> = { light: 0xe9ebef, dark: 0x14171d, studio: 0xd8dbe1 };
const ENV_INTENSITY: Record<BgKey, number> = { light: 1.25, dark: 0.7, studio: 0.9 };
const EMPTY_STATS: LiveStats = { tris: 0, verts: 0, meshes: 0, materials: 0, textures: 0, draws: 0, fps: 0, size: null };

const TEXTURE_SLOTS = ["map", "normalMap", "roughnessMap", "metalnessMap", "aoMap", "emissiveMap", "alphaMap"] as const;

/** A procedural UV checker: the classic uv_grid look without shipping an image in the bundle.
 *
 * Fine cells on purpose: a baked asset maps every island into a small part of the atlas, so a coarse checker would
 * sample as flat white and hide exactly the distortion this view exists to reveal.
 */
function uvChecker(size = 2048, cells = 64): THREE.Texture {
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const g = canvas.getContext("2d")!;
  const s = size / cells;
  for (let y = 0; y < cells; y++) {
    for (let x = 0; x < cells; x++) {
      g.fillStyle = (x + y) % 2 ? "#c6ccd6" : "#f4f6f9";
      g.fillRect(x * s, y * s, s, s);
    }
  }
  g.strokeStyle = "#1d4ed8";
  g.lineWidth = size / 160;
  g.strokeRect(g.lineWidth, g.lineWidth, size - 2 * g.lineWidth, size - 2 * g.lineWidth);
  g.strokeStyle = "#dc2626";
  g.beginPath();
  g.moveTo(size / 2, 0); g.lineTo(size / 2, size);
  g.moveTo(0, size / 2); g.lineTo(size, size / 2);
  g.stroke();
  g.fillStyle = "#0f766e";
  g.fillRect(0, 0, s, s);
  g.fillStyle = "#b45309";
  g.fillRect(size - s, size - s, s, s);
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
  tex.anisotropy = 4;
  return tex;
}

function countOf(geometry: THREE.BufferGeometry): { verts: number; tris: number } {
  const verts = geometry.attributes.position?.count ?? 0;
  const tris = geometry.index ? geometry.index.count / 3 : verts / 3;
  return { verts, tris: Math.round(tris) };
}

function materialsOf(material: THREE.Material | THREE.Material[] | undefined): THREE.Material[] {
  return Array.isArray(material) ? material : material ? [material] : [];
}

function disposeTree(root: THREE.Object3D) {
  root.traverse((o) => {
    const mesh = o as THREE.Mesh;
    if (!mesh.isMesh) return;
    mesh.geometry?.dispose();
    for (const mat of materialsOf(mesh.material)) {
      for (const slot of TEXTURE_SLOTS) {
        const tex = (mat as unknown as Record<string, THREE.Texture | undefined>)[slot];
        if (tex?.isTexture && tex.userData.shared !== true) tex.dispose();
      }
      mat.dispose();
    }
  });
}

export default function ModelViewer({ variants, file, onSelectVariant, urlFor, maps = [], onOpenImage }: {
  variants: ViewerVariant[];
  file: string;
  onSelectVariant: (file: string) => void;
  urlFor: (file: string) => string;
  maps?: ViewerMap[];
  onOpenImage?: (url: string, name: string) => void;
}) {
  const t = useT();
  const boxRef = useRef<HTMLDivElement>(null);

  const [mode, setMode] = useState<ViewMode>("shaded");
  const [view, setView] = useState<ViewKey>("persp");
  // The 3D backdrop starts by matching the UI theme - a bright viewport inside a dark page looks like a mistake.
  const [bg, setBg] = useState<BgKey>(() => {
    const explicit = document.documentElement.dataset.theme;
    const dark = explicit ? explicit === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
    return dark ? "dark" : "studio";
  });
  const [overlays, setOverlays] = useState<Record<OverlayKey, boolean>>({ grid: true, axes: false, bounds: false, shadow: true });
  const [autoRotate, setAutoRotate] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [isolateId, setIsolateId] = useState<string | null>(null);
  const [parts, setParts] = useState<PartInfo[]>([]);
  const [stats, setStats] = useState<LiveStats>(EMPTY_STATS);
  const [progress, setProgress] = useState<{ active: boolean; pct: number; failed: boolean }>({ active: true, pct: 0, failed: false });
  const [panel, setPanel] = useState(true);

  /** Everything the scene needs to read from React, always current, so the scene never has to be rebuilt. */
  const prefs = useRef({ mode, view, bg, overlays, autoRotate, selectedId, isolateId });
  prefs.current = { mode, view, bg, overlays, autoRotate, selectedId, isolateId };

  const core = useRef<{
    load: (url: string) => void;
    reset: () => void;
    screenshot: () => void;
    setVisible: (id: string, on: boolean) => void;
    focus: (id: string) => void;
    applyVisuals: () => void;
    setView: () => void;
    setIsolate: () => void;
  } | null>(null);
  const urlForRef = useRef(urlFor);
  urlForRef.current = urlFor;
  const emit = useRef({
    pick: (_id: string | null) => {},
    parts: (_p: PartInfo[]) => {},
    stats: (_s: LiveStats) => {},
    progress: (_p: { active: boolean; pct: number; failed: boolean }) => {},
  });
  emit.current = {
    pick: setSelectedId,
    parts: setParts,
    stats: setStats,
    progress: setProgress,
  };

  /* ------------------------------------------------------------------ scene: created once */
  useEffect(() => {
    const box = boxRef.current;
    if (!box) return;

    const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true, powerPreference: "high-performance" });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFShadowMap;
    renderer.toneMapping = THREE.NeutralToneMapping;
    renderer.toneMappingExposure = 1.05;
    renderer.domElement.className = "viewer-canvas";
    box.appendChild(renderer.domElement);

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(BG_COLOR[prefs.current.bg]);
    const camera = new THREE.PerspectiveCamera(38, 1, 0.005, 500);
    camera.position.set(1.6, 1.2, 1.8);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.075;
    controls.rotateSpeed = 0.85;
    controls.autoRotateSpeed = 1.7;
    controls.minDistance = 0.02;
    controls.maxDistance = 200;

    const pmrem = new THREE.PMREMGenerator(renderer);
    const envMap = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    scene.environment = envMap;
    scene.environmentIntensity = ENV_INTENSITY[prefs.current.bg];

    const key = new THREE.DirectionalLight(0xffffff, 2.4);
    key.castShadow = true;
    key.shadow.mapSize.set(2048, 2048);
    key.shadow.bias = -0.0007;
    key.shadow.normalBias = 0.008;
    scene.add(key, key.target);
    const fill = new THREE.DirectionalLight(0xffffff, 0.5);
    fill.position.set(-3.4, 1.4, -2.6);
    scene.add(fill);
    scene.add(new THREE.HemisphereLight(0xffffff, 0x8d939e, 0.4));

    // GridHelper(2, 20) has 0.1 m cells; frameObject scales it so one cell stays a readable round number.
    const grid = new THREE.GridHelper(2, 20, 0x7d8797, 0xc4cad4);
    const ground = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), new THREE.ShadowMaterial({ opacity: 0.26 }));
    ground.rotation.x = -Math.PI / 2;
    ground.receiveShadow = true;
    const axes = new THREE.AxesHelper(0.4);
    axes.visible = false;
    const bounds = new THREE.Box3Helper(new THREE.Box3(), new THREE.Color(0xf0b429));
    bounds.visible = false;
    const outline = new THREE.BoxHelper(new THREE.Object3D(), new THREE.Color(0x14b8a6));
    outline.visible = false;
    scene.add(grid, ground, axes, bounds, outline);

    // One material per display mode, shared by every mesh.
    const wireOverlayMat = new THREE.MeshBasicMaterial({
      color: 0x0f172a, wireframe: true, transparent: true, opacity: 0.32, depthWrite: false,
      // the overlay shares the surface geometry, so it needs a nudge forward to avoid z-fighting
      polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -1,
    });
    const wireOnlyMat = new THREE.MeshBasicMaterial({ color: 0x7c8698, wireframe: true });
    const normalMat = new THREE.MeshNormalMaterial();
    const clayMat = new THREE.MeshStandardMaterial({ color: 0xb6bcc6, roughness: 0.62, metalness: 0.02 });
    const checkerTex = uvChecker();
    const checkerMat = new THREE.MeshBasicMaterial({ map: checkerTex });
    const flatMat = new THREE.MeshBasicMaterial({ color: 0xd2d7de });
    const unlitCache = new Map<string, THREE.MeshBasicMaterial>();

    let root: THREE.Object3D | null = null;
    let originals = new Map<THREE.Mesh, THREE.Material | THREE.Material[]>();
    let partMeshes: { id: string; mesh: THREE.Mesh }[] = [];
    const hidden = new Map<string, boolean>();
    let wireOverlays: THREE.Mesh[] = [];
    let raf = 0;
    let disposed = false;
    let fps = 0;

    const raycaster = new THREE.Raycaster();
    const pointer = new THREE.Vector2();

    const resize = () => {
      const w = box.clientWidth || 1;
      const h = box.clientHeight || 1;
      renderer.setSize(w, h, false);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
    };
    resize();
    const observer = new ResizeObserver(resize);
    observer.observe(box);

    /* ------------------------------------------------------------- measurement */
    /** Counts come from the *original* materials: the model has three textures whether or not the clay view is on. */
    function measureStats(): Omit<LiveStats, "draws" | "fps"> {
      const materials = new Set<string>();
      const textures = new Set<string>();
      const box3 = new THREE.Box3();
      let tris = 0, verts = 0, meshes = 0;
      root?.traverse((o) => {
        const mesh = o as THREE.Mesh;
        if (!mesh.isMesh || !mesh.geometry) return;
        const c = countOf(mesh.geometry);
        tris += c.tris;
        verts += c.verts;
        meshes++;
        for (const mat of materialsOf(originals.get(mesh) ?? mesh.material)) {
          materials.add(mat.uuid);
          for (const slot of TEXTURE_SLOTS) {
            const tex = (mat as unknown as Record<string, THREE.Texture | undefined>)[slot];
            if (tex?.isTexture) textures.add(tex.uuid);
          }
        }
        box3.expandByObject(mesh);
      });
      const size = box3.isEmpty() ? null : box3.getSize(new THREE.Vector3());
      return {
        tris, verts, meshes, materials: materials.size, textures: textures.size,
        size: size ? [size.x, size.y, size.z] : null,
      };
    }

    function measureParts(): PartInfo[] {
      return partMeshes.map(({ id, mesh }, i) => {
        const c = countOf(mesh.geometry);
        return {
          id,
          name: mesh.name || mesh.parent?.name || `mesh_${i}`,
          tris: c.tris,
          verts: c.verts,
          material: materialsOf(mesh.material).map((m) => m.name || "material").join(", ") || "-",
          visible: hidden.get(id) !== false,
        };
      });
    }

    function pushStats() {
      emit.current.stats({ ...measureStats(), draws: renderer.info.render.calls, fps });
    }

    /* ------------------------------------------------------------- apply preferences */
    function applyVisibility() {
      const iso = prefs.current.isolateId;
      for (const { id, mesh } of partMeshes) mesh.visible = hidden.get(id) !== false && (!iso || iso === id);
      for (const w of wireOverlays) w.visible = w.userData.wanted === true && (w.userData.mesh as THREE.Mesh | undefined)?.visible === true;
    }

    function applyMode() {
      const m = prefs.current.mode;
      for (const { mesh } of partMeshes) {
        if (m === "shaded" || m === "shadedWire") {
          mesh.material = originals.get(mesh) ?? mesh.material;
        } else if (m === "wire") {
          mesh.material = wireOnlyMat;
        } else if (m === "normals") {
          mesh.material = normalMat;
        } else if (m === "matte") {
          mesh.material = clayMat;
        } else if (m === "uv") {
          mesh.material = checkerMat;
        } else {
          // The base colour comes from the *original* material: reading it from the current one would inherit
          // whatever the previous mode swapped in (the UV checker, most obviously).
          const orig = materialsOf(originals.get(mesh))[0] as THREE.MeshStandardMaterial | undefined;
          const map = orig?.map ?? null;
          if (map) {
            let mat = unlitCache.get(map.uuid);
            if (!mat) { mat = new THREE.MeshBasicMaterial({ map }); unlitCache.set(map.uuid, mat); }
            mesh.material = mat;
          } else {
            mesh.material = flatMat;
          }
        }
      }
      for (const w of wireOverlays) w.userData.wanted = m === "shadedWire";
      applyVisibility();
      pushStats();
    }

    function applyOverlays() {
      const p = prefs.current.overlays;
      grid.visible = p.grid;
      axes.visible = p.axes;
      bounds.visible = p.bounds;
      ground.visible = p.shadow;
      key.castShadow = p.shadow;
    }

    function applyBg() {
      const b = prefs.current.bg;
      scene.background = new THREE.Color(BG_COLOR[b]);
      scene.environmentIntensity = ENV_INTENSITY[b];
    }

    function applySelection() {
      const part = partMeshes.find((p) => p.id === prefs.current.selectedId);
      if (!part) { outline.visible = false; return; }
      outline.setFromObject(part.mesh);
      outline.visible = true;
    }

    /* ------------------------------------------------------------- camera framing */
    function frame(object?: THREE.Object3D, direction?: THREE.Vector3) {
      const box3 = new THREE.Box3();
      if (object) box3.expandByObject(object);
      else if (root) box3.setFromObject(root);
      if (box3.isEmpty()) return;
      const center = box3.getCenter(new THREE.Vector3());
      const size = box3.getSize(new THREE.Vector3());
      const radius = Math.max(size.length() / 2, 1e-4);
      const dist = (radius / Math.sin((camera.fov * Math.PI) / 360)) * 1.32;
      const dir = direction ?? new THREE.Vector3(0.78, 0.5, 0.9).normalize();
      camera.position.copy(center).addScaledVector(dir, dist);
      camera.near = Math.max(radius / 800, 0.001);
      camera.far = radius * 500;
      camera.updateProjectionMatrix();
      controls.target.copy(center);
      controls.update();

      // ground, grid and axes follow the model: one grid cell stays a round number of metres
      const span = Math.max(size.x, size.z, 1e-3);
      const cell = Math.max(Math.pow(10, Math.round(Math.log10(span / 6))), 1e-4);
      grid.scale.setScalar(cell * 10);
      grid.position.set(center.x, box3.min.y - cell * 0.004, center.z);
      ground.scale.set(grid.scale.x * 20 * 1.5, grid.scale.x * 20 * 1.5, 1);
      ground.position.set(center.x, box3.min.y - cell * 0.002, center.z);
      axes.scale.setScalar(Math.max(span * 0.6, 0.02));
      axes.position.set(box3.min.x, box3.min.y, box3.min.z);
      key.target.position.copy(center);
      key.position.set(center.x + span * 1.3, box3.max.y + span * 1.6, center.z + span * 1.5);
      key.shadow.camera.left = -span * 1.5;
      key.shadow.camera.right = span * 1.5;
      key.shadow.camera.top = span * 1.5;
      key.shadow.camera.bottom = -span * 1.5;
      key.shadow.camera.updateProjectionMatrix();
      (bounds as THREE.Box3Helper).box.copy(box3);
      bounds.updateMatrixWorld(true);
    }

    const DIRS: Record<ViewKey, THREE.Vector3> = {
      persp: new THREE.Vector3(0.78, 0.5, 0.9).normalize(),
      front: new THREE.Vector3(0, 0.06, 1).normalize(),
      back: new THREE.Vector3(0, 0.06, -1).normalize(),
      left: new THREE.Vector3(-1, 0.06, 0).normalize(),
      right: new THREE.Vector3(1, 0.06, 0).normalize(),
      // A hair off vertical on purpose: looking straight down leaves the azimuth undefined and the orbit controls
      // would inherit the previous one, showing the model rotated. The tilt also reads better than a flat plan.
      top: new THREE.Vector3(0, 1, 0.24).normalize(),
      bottom: new THREE.Vector3(0, -1, 0.24).normalize(),
    };

    /** Re-fit the camera to the model from the current preset direction. */
    function applyView() {
      if (!root) return;
      frame(undefined, DIRS[prefs.current.view]);
    }

    /* ------------------------------------------------------------- load */
    function adopt(model: THREE.Object3D) {
      if (root) {
        outline.visible = false;
        disposeTree(root);
        root.removeFromParent();
      }
      for (const w of wireOverlays) w.removeFromParent();
      wireOverlays = [];
      originals = new Map();
      hidden.clear();
      root = model;

      let n = 0;
      partMeshes = [];
      model.traverse((o) => {
        const mesh = o as THREE.Mesh;
        if (!mesh.isMesh || !mesh.geometry) return;
        mesh.userData.partId = `p${n++}`;
        mesh.castShadow = true;
        mesh.receiveShadow = true;
        originals.set(mesh, mesh.material);
        partMeshes.push({ id: mesh.userData.partId as string, mesh });
      });

      scene.add(model);
      // a second pass: the meshes are in the scene graph, so matrixWorld is meaningful for the wireframe copies
      for (const { id, mesh } of partMeshes) {
        mesh.updateWorldMatrix(true, false);
        const w = new THREE.Mesh(mesh.geometry, wireOverlayMat);
        w.matrixAutoUpdate = false;
        w.matrix.copy(mesh.matrixWorld);
        w.renderOrder = 2;
        w.userData.mesh = mesh;
        w.userData.partId = id;
        wireOverlays.push(w);
        scene.add(w);
      }

      frame(undefined, DIRS[prefs.current.view]);
      applyBg();
      applyOverlays();
      applyMode();
      applySelection();
      emit.current.progress({ active: false, pct: 100, failed: false });
      emit.current.parts(measureParts());
      pushStats();
    }

    /* ------------------------------------------------------------- picking */
    let downAt: { x: number; y: number } | null = null;
    const onDown = (e: PointerEvent) => { downAt = { x: e.clientX, y: e.clientY }; };
    const onUp = (e: PointerEvent) => {
      const start = downAt;
      downAt = null;
      if (!start || !root) return;
      if (Math.hypot(e.clientX - start.x, e.clientY - start.y) > 5) return;   // that was an orbit drag
      const rect = renderer.domElement.getBoundingClientRect();
      pointer.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
      pointer.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
      raycaster.setFromCamera(pointer, camera);
      const hit = raycaster.intersectObjects(partMeshes.map((p) => p.mesh), false).find((h) => h.object.visible);
      emit.current.pick(hit ? (hit.object.userData.partId as string) : null);
    };
    renderer.domElement.addEventListener("pointerdown", onDown);
    renderer.domElement.addEventListener("pointerup", onUp);
    renderer.domElement.style.cursor = "grab";

    /* ------------------------------------------------------------- the handle React talks to */
    core.current = {
      load: (url: string) => {
        emit.current.progress({ active: true, pct: 0, failed: false });
        emit.current.pick(null);
        new GLTFLoader().load(
          url,
          (gltf) => { if (!disposed) adopt(gltf.scene); },
          (ev) => {
            const loaded = (ev as ProgressEvent).loaded || 0;
            const total = (ev as ProgressEvent).total || 0;
            emit.current.progress({ active: true, pct: total ? Math.round((loaded / total) * 100) : 0, failed: false });
          },
          () => emit.current.progress({ active: false, pct: 0, failed: true }),
        );
      },
      reset: () => frame(undefined, undefined),
      screenshot: () => {
        renderer.render(scene, camera);
        const a = document.createElement("a");
        a.href = renderer.domElement.toDataURL("image/png");
        a.download = `${file.replace(/\.[^.]+$/, "")}_${prefs.current.mode}.png`;
        a.click();
      },
      setVisible: (id, on) => { hidden.set(id, on); applyVisibility(); emit.current.parts(measureParts()); },
      focus: (id) => {
        const part = partMeshes.find((p) => p.id === id);
        if (part) frame(part.mesh);
      },
      applyVisuals: () => {
        applyBg();
        applyOverlays();
        applyMode();
        applySelection();
        controls.autoRotate = prefs.current.autoRotate;
      },
      setView: () => applyView(),
      setIsolate: () => applyVisibility(),
    };

    /* ------------------------------------------------------------- loop */
    controls.autoRotate = prefs.current.autoRotate;
    let frames = 0;
    let lastFps = performance.now();
    let lastStats = 0;
    const tick = () => {
      if (disposed) return;
      raf = requestAnimationFrame(tick);
      controls.update();
      renderer.render(scene, camera);
      const now = performance.now();
      frames++;
      if (now - lastFps > 500) { fps = Math.round((frames * 1000) / (now - lastFps)); frames = 0; lastFps = now; }
      if (now - lastStats > 700 && root) { lastStats = now; pushStats(); }
    };
    tick();

    return () => {
      disposed = true;
      cancelAnimationFrame(raf);
      observer.disconnect();
      renderer.domElement.removeEventListener("pointerdown", onDown);
      renderer.domElement.removeEventListener("pointerup", onUp);
      controls.dispose();
      if (root) disposeTree(root);
      for (const w of wireOverlays) w.removeFromParent();
      for (const mat of [wireOverlayMat, wireOnlyMat, normalMat, clayMat, checkerMat, flatMat]) mat.dispose();
      for (const mat of unlitCache.values()) mat.dispose();
      checkerTex.dispose();
      envMap.dispose();
      pmrem.dispose();
      renderer.dispose();
      renderer.domElement.remove();
      core.current = null;
    };
    // The scene is built exactly once; every setting reaches it through `prefs` and the effects below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /* ------------------------------------------------------------------ React settings -> scene */
  // The scene reads the current settings from `prefs`; these effects only tell it that something changed, which
  // keeps the scene off the re-render path entirely.
  useEffect(() => {
    core.current?.applyVisuals();
  }, [mode, bg, overlays, autoRotate, selectedId]);
  useEffect(() => {
    core.current?.setIsolate();
  }, [isolateId]);
  useEffect(() => {
    core.current?.setView();
  }, [view]);

  // A variant change re-frames the camera, so the new file gets the same preset view. Guarded because the parent
  // only knows the file name after the job has loaded - loading "" would request the artifacts directory.
  useEffect(() => {
    if (file) core.current?.load(urlForRef.current(file));
  }, [file]);

  const selected = parts.find((p) => p.id === selectedId) || null;

  return (
    <div className="viewer-block">
      <div className="viewer" ref={boxRef}>
        <div className="viewer-hud">
          <div className="hud-row"><span className="hud-k">{t("viewer.triangles")}</span><span className="hud-v">{fmtInt(stats.tris)}</span></div>
          <div className="hud-row"><span className="hud-k">{t("viewer.vertices")}</span><span className="hud-v">{fmtInt(stats.verts)}</span></div>
          <div className="hud-row"><span className="hud-k">{t("viewer.meshes")}</span><span className="hud-v">{stats.meshes}</span></div>
          <div className="hud-row"><span className="hud-k">{t("viewer.materials")}</span><span className="hud-v">{stats.materials}</span></div>
          <div className="hud-row"><span className="hud-k">{t("viewer.textures")}</span><span className="hud-v">{stats.textures}</span></div>
          <div className="hud-row"><span className="hud-k">{t("viewer.draws")}</span><span className="hud-v">{stats.draws}</span></div>
          <div className="hud-row"><span className="hud-k">{t("viewer.fps")}</span><span className="hud-v">{stats.fps}</span></div>
        </div>

        {stats.size && (
          <div className="viewer-scale">
            <Ruler size={12} /> {stats.size.map((v) => v.toFixed(3)).join(" × ")} m
          </div>
        )}

        {file && (
          <a className="viewer-download btn sm" href={urlFor(file)} download><Download size={13} /> {t("asset.thisFile")}</a>
        )}

        {progress.active && (
          <div className="viewer-veil">
            <div className="viewer-veil-box">
              <div className="small truncate">{t("viewer.loading")} · {file}</div>
              <div className={"progress" + (progress.pct ? "" : " indeterminate")}>
                <span style={{ width: `${Math.max(progress.pct, 4)}%` }} />
              </div>
            </div>
          </div>
        )}
        {progress.failed && (
          <div className="viewer-veil">
            <div className="viewer-veil-box callout danger">
              <div className="callout-title">{t("viewer.loadFailed")}</div>
              <div className="small mono truncate">{file}</div>
            </div>
          </div>
        )}
      </div>

      <div className="viewer-variants">
        <span className="vgroup-label">{t("asset.variants")}</span>
        {variants.map((v) => (
          <button key={v.file} className={"chip toggle" + (v.file === file ? " accent" : "")} onClick={() => onSelectVariant(v.file)}>
            {v.label}{v.tris != null && <span className="faint num"> · {fmtInt(v.tris)}</span>}
          </button>
        ))}
      </div>

      <div className="viewer-controls">
        <div className="vgroup">
          <span className="vgroup-label">{t("viewer.mode")}</span>
          <div className="segmented">
            {MODES.map((m) => (
              <button key={m} className={mode === m ? "active" : ""} onClick={() => setMode(m)}>{t(MODE_LABEL[m])}</button>
            ))}
          </div>
        </div>
        <div className="vgroup">
          <span className="vgroup-label">{t("viewer.overlays")}</span>
          <div className="row tight">
            {OVERLAYS.map((k) => (
              <button key={k} className={"chip toggle" + (overlays[k] ? " accent" : "")} aria-pressed={overlays[k]}
                      onClick={() => setOverlays((o) => ({ ...o, [k]: !o[k] }))}>{t(OVERLAY_LABEL[k])}</button>
            ))}
          </div>
        </div>
        <div className="vgroup">
          <span className="vgroup-label">{t("viewer.view")}</span>
          <div className="segmented">
            {VIEWS.map((v) => (
              <button key={v} className={view === v ? "active" : ""} onClick={() => setView(v)}>{t(VIEW_LABEL[v])}</button>
            ))}
          </div>
        </div>
        <div className="vgroup">
          <span className="vgroup-label">{t("viewer.background")}</span>
          <div className="segmented">
            {BGS.map((b) => (
              <button key={b} className={bg === b ? "active" : ""} onClick={() => setBg(b)}>{t(BG_LABEL[b])}</button>
            ))}
          </div>
        </div>
        <div className="vgroup">
          <span className="vgroup-label">&nbsp;</span>
          <div className="row tight">
            <button className={"btn sm" + (autoRotate ? " primary" : "")} aria-pressed={autoRotate} onClick={() => setAutoRotate(!autoRotate)}>
              <RotateCw size={13} /> {t("viewer.rotate")}
            </button>
            <button className="btn sm" onClick={() => core.current?.reset()}><Maximize2 size={13} /> {t("viewer.reset")}</button>
            <button className="btn sm" onClick={() => core.current?.screenshot()}><Camera size={13} /> {t("viewer.screenshot")}</button>
            <button className={"btn sm" + (panel ? " primary" : "")} aria-pressed={panel} onClick={() => setPanel(!panel)}>
              <Info size={13} /> {t("viewer.info")}
            </button>
          </div>
        </div>
      </div>

      <div className="viewer-selection">
        <Crosshair size={13} />
        {selected ? (
          <>
            <span className="bold truncate">{selected.name}</span>
            <span className="chip sm num">{fmtInt(selected.tris)} {t("viewer.triangles")}</span>
            <span className="chip sm num">{fmtInt(selected.verts)} {t("viewer.vertices")}</span>
            <span className="chip sm accent truncate" title={selected.material}>{selected.material}</span>
            <div className="row tight" style={{ marginLeft: "auto" }}>
              <button className="btn sm" onClick={() => core.current?.focus(selected.id)}>{t("viewer.focus")}</button>
              <button className={"btn sm" + (isolateId === selected.id ? " primary" : "")} aria-pressed={isolateId === selected.id}
                      onClick={() => setIsolateId(isolateId === selected.id ? null : selected.id)}>{t("viewer.isolate")}</button>
              <button className="btn sm ghost" onClick={() => setSelectedId(null)}>{t("common.clear")}</button>
            </div>
          </>
        ) : (
          <>
            <span className="faint">{t("viewer.nothingSelected")}</span>
            <span className="faint small" style={{ marginLeft: "auto" }}>{t("viewer.clickHint")}</span>
          </>
        )}
      </div>

      {panel && (
        <div className="viewer-panel">
          <div className="vpanel-col">
            <div className="vpanel-head">
              <span>{t("viewer.scene")}</span>
              {isolateId && <button className="btn sm ghost" onClick={() => setIsolateId(null)}>{t("viewer.showAll")}</button>}
            </div>
            <ul className="vpart-list">
              {parts.map((p) => (
                <li key={p.id} className={p.id === selectedId ? "active" : ""}>
                  <button className="icon-toggle" title={p.visible ? t("viewer.hide") : t("viewer.show")}
                          aria-label={p.visible ? t("viewer.hide") : t("viewer.show")}
                          onClick={() => core.current?.setVisible(p.id, !p.visible)}>
                    {p.visible ? <Eye size={13} /> : <EyeOff size={13} />}
                  </button>
                  <button className="vpart-main" onClick={() => setSelectedId(p.id)}><span className="truncate">{p.name}</span></button>
                  <span className="vpart-meta num">{fmtInt(p.tris)}</span>
                  <span className="vpart-mat faint truncate" title={p.material}>{p.material}</span>
                  <button className="icon-toggle" title={t("viewer.focus")} aria-label={t("viewer.focus")}
                          onClick={() => core.current?.focus(p.id)}><Crosshair size={13} /></button>
                </li>
              ))}
              {parts.length === 0 && <li className="faint small">{t("viewer.loading")}…</li>}
            </ul>
          </div>
          <div className="vpanel-col">
            <div className="vpanel-head"><span>{t("viewer.textureMaps")}</span></div>
            <ul className="vmap-list">
              {maps.map((m) => (
                <li key={m.url}>
                  <button onClick={() => onOpenImage?.(m.url, m.name)} title={t("viewer.open")}>
                    <ImageIcon size={13} />
                    <span className="truncate">{m.name}</span>
                    {m.note && <span className="faint">{m.note}</span>}
                    <span className="link">{t("viewer.open")}</span>
                  </button>
                </li>
              ))}
              {maps.length === 0 && <li className="faint small">–</li>}
            </ul>
            <div className="small faint">{t("viewer.modeHint")}</div>
          </div>
        </div>
      )}
    </div>
  );
}
