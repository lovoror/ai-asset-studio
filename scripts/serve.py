#!/usr/bin/env python3
"""Start (or inspect) an asset-studio role on this machine. One entry point, Windows and macOS/Linux alike.

  python scripts/serve.py control    control plane: HTTP API + orchestrator, plus any worker that runs here
  python scripts/serve.py image      this machine is the 2D server: serves the reference-image stage
  python scripts/serve.py pixal3d    this machine is the 3D server: serves the Pixal3D / TRELLIS.2 stage
  python scripts/serve.py blender    this machine only does Blender post-processing (CPU)
  python scripts/serve.py all        everything on one machine (single-box install, dev)
  python scripts/serve.py doctor     report what this machine can run and whether the servers answer

Every setting comes from config.toml at the repository root (see config.example.toml); STUDIO_* environment
variables override it. Which stages run here is decided by [servers]: a stage set to "local" is started by the
`control` role, and its worker is reached on 127.0.0.1.

To spread the work over machines, on the 2D and 3D servers:

    [worker]
    bind  = "0.0.0.0"     # accept connections from the control machine
    token = "<shared secret>"

    python scripts/serve.py image       # on the 2D server
    python scripts/serve.py pixal3d     # on the 3D server

and on the control machine point at them:

    [servers]
    image   = "http://<2d-host>:8701"
    pixal3d = "http://<3d-host>:8702"

Only the standard library is needed to start a role, so this works before any dependency is installed.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ROOT / "services"
sys.path.insert(0, str(SERVICES / "studio"))

from studio import config  # noqa: E402  (needs the sys.path line above)

RUNNER_ROLES = ("image", "pixal3d", "blender")

# Which module each component runs.
COMPONENT_MODULE = {"api": "studio.api", "worker": "studio.worker"}
for _r in RUNNER_ROLES:
    COMPONENT_MODULE[f"runner:{_r}"] = "runner.runner"


def child_env() -> dict:
    """The environment every child process gets: paths and credentials resolved from config.toml, so a stage
    subprocess started by a worker inherits the same model cache and presets.

    Both `services/studio` (the `studio` package: api, worker) and `services` (the worker modules: runner,
    image_worker, pixal3d_worker, blender, worker_paths) have to be importable.
    """
    env = dict(os.environ)
    parts = [str(SERVICES / "studio"), str(SERVICES)]
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env["PYTHONUNBUFFERED"] = "1"
    env["STUDIO_ROOT"] = str(ROOT)
    env["STUDIO_JOBS_DIR"] = str(config.JOBS_DIR)
    env["STUDIO_DATA_DIR"] = str(config.DATA_DIR)
    env["STUDIO_PRESETS_DIR"] = str(config.PRESETS_DIR)
    env["STUDIO_MANIFESTS_DIR"] = str(config.MANIFESTS_DIR)
    env["STUDIO_WEB_DIST"] = str(config.WEB_DIST or (ROOT / "web" / "dist"))
    env["STUDIO_WORKER_TOKEN"] = config.WORKER_TOKEN
    env["STUDIO_WORKER_BIND"] = config.WORKER_BIND
    if config.HF_HOME:
        env["HF_HOME"] = config.HF_HOME
    if config.TORCH_HOME:
        env["TORCH_HOME"] = config.TORCH_HOME
    if config.EXTRA_MODELS_DIR:
        env["STUDIO_EXTRA_MODELS_DIR"] = str(config.EXTRA_MODELS_DIR)
    if config.GPU_UUID:
        env["STUDIO_GPU_UUID"] = config.GPU_UUID
    if config.VISION_EVALUATOR_URL:
        env["STUDIO_VISION_EVALUATOR_URL"] = config.VISION_EVALUATOR_URL
    env["STUDIO_REMBG_MODEL"] = config.REMBG_MODEL
    return env


def worker_roles(role: str) -> list[str]:
    """The roles a worker process is allowed to run. `[worker] roles` in config.toml narrows this further; a role
    it excludes is a configuration mistake, so fail loudly instead of starting a worker that can serve nothing."""
    if not config.WORKER_ROLES:
        return [role]
    if role not in config.WORKER_ROLES:
        raise SystemExit(f"[worker] roles = {config.WORKER_ROLES} on this machine does not include {role!r}; "
                         f"fix config.toml (or remove the key) before running `serve.py {role}`")
    return list(config.WORKER_ROLES)


def interpreter_for(component: str) -> str:
    """The python that runs this component: `[python] <role>` from config.toml, else the one running serve.py.

    A role may need its own because of a hard wheel constraint (bpy is cp311 for 4.5/5.0 and cp313 for 5.1+).
    """
    key = component.split(":", 1)[1] if component.startswith("runner:") else "control"
    path = config.PYTHON_INTERPRETERS.get(key) or ""
    if path and not Path(path).exists():
        raise SystemExit(f"[python] {key} = {path!r} does not exist; fix config.toml "
                         f"(see requirements/blender.txt for how to create it)")
    return path or sys.executable


def component_cmd(name: str, env: dict) -> list[str]:
    if name.startswith("runner:"):
        role = name.split(":", 1)[1]
        return [interpreter_for(name), "-m", "runner.runner",
                "--name", f"{role}-worker", "--roles", ",".join(worker_roles(role)),
                "--host", env["STUDIO_WORKER_BIND"], "--port", str(config.WORKER_PORTS[role]),
                "--token", config.WORKER_TOKEN,
                # One work directory per role: each worker prunes its own scratch on start, so they must not share.
                "--work-dir", str((config.DATA_DIR / "worker" / role))]
    return [interpreter_for(name), "-m", COMPONENT_MODULE[name]]


def local_roles() -> list[str]:
    """Stages whose worker belongs on this machine: config.toml [servers] or the Settings page saying "local"."""
    return config.local_stage_roles()


def runner_urls() -> dict[str, str]:
    return config.effective_runners(force=True)


def plan(role: str) -> list[str]:
    if role == "all":
        return ["api", "worker"] + [f"runner:{r}" for r in RUNNER_ROLES]
    if role == "control":
        # A worker configured as "local" belongs to this machine, so the control role starts it too. This is the
        # one decision that still needs a restart after the addresses change: the plan is fixed at boot.
        return ["api", "worker"] + [f"runner:{r}" for r in local_roles()]
    return [f"runner:{role}"]


def describe() -> str:
    urls, local = runner_urls(), local_roles()
    return "\n".join(f"  {r:<8} {'local (' + urls[r] + ')' if r in local else urls[r]}" for r in RUNNER_ROLES)


# ---------------------------------------------------------------------------------------------------- run

class Supervisor:
    def __init__(self, names: list[str], env: dict):
        self.names = names
        self.env = env
        self.procs: dict[str, subprocess.Popen] = {}
        self.restarts: dict[str, list[float]] = {n: [] for n in names}
        self.stopping = False

    def start(self, name: str) -> subprocess.Popen:
        cmd = component_cmd(name, self.env)
        print(f"[serve] starting {name}: {' '.join(cmd[1:])}", flush=True)
        kwargs = {}
        if os.name == "nt":
            # Children keep the console; serve.py terminates them explicitly on the way out.
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        p = subprocess.Popen(cmd, cwd=str(ROOT), env=self.env, **kwargs)
        self.procs[name] = p
        return p

    def stop_all(self):
        self.stopping = True
        for name, p in self.procs.items():
            if p.poll() is None:
                print(f"[serve] stopping {name}", flush=True)
                p.terminate()
        deadline = time.time() + 15
        for name, p in self.procs.items():
            while p.poll() is None and time.time() < deadline:
                time.sleep(0.2)
            if p.poll() is None:
                print(f"[serve] force-killing {name}", flush=True)
                p.kill()

    def run(self) -> int:
        for name in self.names:
            self.start(name)
        try:
            while True:
                time.sleep(1.0)
                for name in list(self.procs):
                    p = self.procs[name]
                    rc = p.poll()
                    if rc is None:
                        continue
                    if self.stopping:
                        continue
                    now = time.time()
                    recent = [t for t in self.restarts[name] if now - t < 60]
                    if len(recent) >= 3:
                        print(f"[serve] {name} keeps exiting (last code {rc}); giving up", flush=True)
                        self.stop_all()
                        return 1
                    self.restarts[name] = recent + [now]
                    print(f"[serve] {name} exited with code {rc}; restarting in 3 s", flush=True)
                    time.sleep(3)
                    self.start(name)
        except KeyboardInterrupt:
            print("\n[serve] interrupted", flush=True)
            self.stop_all()
            return 0


# ---------------------------------------------------------------------------------------------------- doctor

def doctor() -> int:
    ok = True
    print(f"asset-studio doctor\n  root     {ROOT}\n  config   {config.CONFIG_PATH}"
          f"{'' if config.CONFIG_PATH.is_file() else '  (missing: using defaults)'}")
    print(f"  python   {sys.version.split()[0]} ({sys.executable})")
    if sys.version_info < (3, 11):
        print("  ! Python 3.11 or newer is required (tomllib)")
        ok = False

    print("\nstages run on:")
    print(describe())

    problems = []
    for d, what in ((config.PRESETS_DIR, "presets"), (config.MANIFESTS_DIR, "manifests")):
        if not d or not d.is_dir():
            problems.append(f"{what} directory missing: {d}")
    if not (config.JOBS_DIR):
        problems.append("jobs directory is not configured")
    for w in ("styles.yaml", "quality.yaml", "image_models.yaml"):
        if config.PRESETS_DIR and not (config.PRESETS_DIR / w).is_file():
            problems.append(f"preset missing: {config.PRESETS_DIR / w}")
    total, used = _disk(config.JOBS_DIR)
    if total:
        state = "" if config.JOBS_DIR and config.JOBS_DIR.is_dir() else "  (not created yet)"
        print(f"\n  jobs dir {config.JOBS_DIR}  ({used:.0f} GB free on that drive){state}")
    if config.WEB_DIST and (config.WEB_DIST / "index.html").is_file():
        print(f"  portal   {config.WEB_DIST}")
    else:
        print(f"  portal   not built ({config.WEB_DIST}) - run: cd web && npm install && npm run build")

    print("\ncomponents on this machine:")
    for name in plan("control"):
        module = COMPONENT_MODULE[name]
        python = interpreter_for(name)
        missing, optional = _missing(name, python)
        if missing:
            state = "missing " + ", ".join(missing)
            ok = False
        elif optional:
            # Not a problem: this stage can still run against a ComfyUI server. Only the local backend needs these.
            state = f"ready (ComfyUI backend; local generation needs {', '.join(optional)})"
        else:
            state = "ready"
        short = python if python == sys.executable else f"{python}  <- [python] {name.split(':')[-1]}"
        print(f"  {name:<16} {module:<22} {state}\n{'':<19}{short}")

    print("\nservers:")
    try:
        from studio.runner_client import Runner
    except ImportError:
        print("  ! httpx is not installed; run: python scripts/bootstrap.py --role control")
        return 1
    urls, local = runner_urls(), local_roles()
    for r in RUNNER_ROLES:
        h = Runner(r).health()
        if h.get("ok"):
            print(f"  {r:<8} ok    {urls[r]}  roles={h.get('roles')} python={h.get('python')}"
                  f"{'  BUSY' if h.get('busy') else ''}")
        elif r in local:
            # Local and silent just means the service is not running yet - `serve.py control` starts it.
            print(f"  {r:<8} not running  {urls[r]}  (started by: python scripts/serve.py control)")
            ok = False
        else:
            print(f"  {r:<8} UNREACHABLE  {urls[r]}  {h.get('error', '')}")
            ok = False

    for p in problems:
        print(f"  ! {p}")
        ok = False
    print("\n" + ("all good" if ok else "problems reported above"))
    return 0 if ok else 1


def _disk(path) -> tuple[float, float]:
    try:
        import shutil
        t, u, f = shutil.disk_usage(str(path if path and path.exists() else ROOT))
        return t, f / 1e9
    except Exception:  # noqa: BLE001
        return 0.0, 0.0


# What each component needs, split into "always" and "only for local (non-ComfyUI) generation". A worker whose
# stage runs against a ComfyUI server is a light HTTP proxy and does not need torch at all, so demanding it here
# would report a healthy ComfyUI-only deployment as broken.
_NEEDED: dict[str, tuple[list[tuple[str, str]], list[tuple[str, str]]]] = {
    "api": ([("fastapi", "fastapi"), ("uvicorn", "uvicorn"), ("httpx", "httpx"), ("yaml", "pyyaml"),
             ("PIL", "pillow")], []),
    "worker": ([("httpx", "httpx")], []),
    "runner:image": ([("httpx", "httpx"), ("PIL", "pillow"), ("numpy", "numpy"), ("scipy", "scipy"),
                      ("yaml", "pyyaml")], [("torch", "torch"), ("diffusers", "diffusers")]),
    "runner:pixal3d": ([("httpx", "httpx"), ("PIL", "pillow"), ("numpy", "numpy")],
                       [("torch", "torch")]),
    "runner:blender": ([("bpy", "bpy")], []),
}


_PROBE = ("import importlib.util,sys\n"
          "ok=[]\n"
          "for m in sys.argv[1:]:\n"
          "    try:\n"
          "        if importlib.util.find_spec(m): ok.append(m)\n"
          "    except Exception:\n"
          "        pass\n"
          "print(' '.join(ok))\n")


def _missing(component: str, python: str | None = None) -> tuple[list[str], list[str]]:
    """(required, optional) packages this component's python cannot import.

    The probe runs *in that interpreter* when it is not the one running serve.py: a role with its own python (the
    Blender one, on 3.11) keeps its packages in that environment, so checking from here would always say "missing".
    """
    required, optional = _NEEDED.get(component, ([], []))
    mods = [m for m, _ in required + optional]
    if not mods:
        return [], []
    python = python or sys.executable
    if python == sys.executable:
        import importlib.util
        have = set()
        for m in mods:
            try:
                if importlib.util.find_spec(m):
                    have.add(m)
            except (ImportError, ValueError):
                pass
    else:
        try:
            r = subprocess.run([python, "-c", _PROBE, *mods], capture_output=True, text=True, timeout=120)
            have = set(r.stdout.split()) if r.returncode == 0 else set()
        except (OSError, subprocess.SubprocessError):
            have = set()
    miss = lambda pairs: [pkg for mod, pkg in pairs if mod not in have]  # noqa: E731
    return miss(required), miss(optional)


def main() -> int:
    ap = argparse.ArgumentParser(description="start (or inspect) an asset-studio role")
    ap.add_argument("role", nargs="?", default="control",
                    choices=["control", "image", "pixal3d", "blender", "all", "doctor"])
    a = ap.parse_args()
    if a.role == "doctor":
        return doctor()

    env = child_env()
    for d in (config.JOBS_DIR, config.DATA_DIR, config.DATA_DIR / "worker"):
        if d:
            Path(d).mkdir(parents=True, exist_ok=True)

    names = plan(a.role)
    if not names:
        print("[serve] nothing to start for this role", flush=True)
        return 0
    print(f"[serve] role={a.role} components={', '.join(names)}\n[serve] stages run on:\n{describe()}", flush=True)
    sup = Supervisor(names, env)

    def _on_signal(signum, frame):   # Ctrl-C and (on POSIX) SIGTERM both take the children down with us
        raise KeyboardInterrupt
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError, AttributeError):
            pass
    return sup.run()


if __name__ == "__main__":
    sys.exit(main())
