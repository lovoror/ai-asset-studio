# asset-studio

**中文** · [English](#english) · [完整英文文档 / full English documentation →](README.en.md)

> **来源声明 / Provenance** — 本仓库是 [`zorrobyte/asset-studio`](https://github.com/zorrobyte/asset-studio) 的**衍生版本**，
> 在原作者的项目之上做了去容器化、多机可配置、中英双语工作台等改动；原项目与本仓库的代码同为 **0BSD** 许可，上游的提交历史与
> 作者信息完整保留在本仓库的 git log 中。详见文末「来源与致谢」。
>
> This repository is a **derivative work** based on [`zorrobyte/asset-studio`](https://github.com/zorrobyte/asset-studio)
> (0BSD). The upstream commit history and authorship are preserved in this repository's git log. See
> "Provenance & credits" at the end.

---

## 中文

### 它是什么

**输入一句话，得到能直接进游戏引擎的 3D 资产。**

全部跑在自己的机器上：没有账号、没有上传、没有云。0.2 起也不需要 Docker —— 控制面（FastAPI）通过 HTTP 调用一组
**可独立寻址的阶段服务**，所以生图和生 3D 可以分别落在两台不同显卡的机器上，各自用各自的环境。

你描述一个东西（例如 *"stylized industrial water pump station, painted teal metal, copper pipes"*），
它先画出参考图，把参考图变成高精度 3D 主模型，再把主模型优化到你指定的三角面预算、把细节烘焙进贴图、
生成 LOD 和碰撞体、渲染预览，最后交给你一个可以直接丢进 Godot / Unity / Blender 的文件夹。

### 流水线（五个阶段）

| 阶段 | 做什么 | 在哪跑 |
|---|---|---|
| `reference` | 生成参考图（多个候选，并做技术指标打分） | `image` 阶段服务 |
| `pixal3d` | 参考图 → 高精度 3D 主模型（GLB） | `pixal3d` 阶段服务 |
| `blender` | 优化到目标面数、烘焙贴图、生成 LOD 与碰撞体、渲染预览 | `blender` 阶段服务（bpy 4.5） |
| `validate` | GLB 校验（面数 / 贴图 / 尺度；0 错误才算通过） | 控制面进程内 |
| `package` | 打包 zip + 写 `manifest.json` | 控制面进程内 |

### 本仓库相对上游的改动

1. **去掉 Docker**：每个角色一个原生 Python 环境（`requirements/<role>.txt`），不再需要 compose，也不需要共享卷 ——
   文件通过 HTTP 推拉（上传输入、下载产物 zip）。
2. **多机可配置**：`[servers]` 里每个阶段可填 `local` 或一个 URL；`[python]` 可为每个角色单独指定解释器
   （Blender 角色必须是 Python 3.11，因为 bpy 只发 cp311 轮子）。
3. **中英双语工作台**：约 300 条文案中英一一对应，默认跟随系统语言，可在设置里切换。
4. **设置页可直接配地址并测连通性**：生成后端（ComfyUI 地址 + workflow）和三个阶段服务地址都在这里配，每行带 Test 按钮，
   测的是输入框里的值（未保存也能测）。
5. **就绪门禁**：任何必需的服务不通，**新任务直接被拒绝**（HTTP 422 `not_ready` + 结构化原因），
   但工作台本身照常可用 —— 方便你就地修地址，而不是排队几分钟后才失败。
6. **3D 检视器重做**（three.js）：8 种显示模式（着色 / 着色+网格 / 线框 / **法线** / 素模 / **卡通（色阶分级 + 描边）** / **UV 检查** / 反照率）、
   叠加层（网格地面 / 坐标轴 / 包围盒 / 阴影）、视角预设、**点选部件并可隔离 / 聚焦 / 隐藏**、
   实时面数·顶点·网格·材质·贴图·绘制批次·帧率、贴图清单与场景树、一键截图。
7. **界面设计系统重做**：统一 token、明暗双主题、骨架屏与空态、响应式、键盘可达。
8. **新增文档**：[`docs/STAGES.md`](docs/STAGES.md)（英文）与 [`docs/STAGES.zh-CN.md`](docs/STAGES.zh-CN.md)（中文），
   专门讲清"阶段服务是什么、为什么存在、两条配置轴怎么区分、坏了怎么查"。
9. **生成 3D 默认直接开始**，不再先进"待审核"。

### 快速开始（Windows 优先）

前置：**Python 3.11**（Blender 角色必须是 3.11）、**Node 18+**（只用来构建工作台）。可选 [uv](https://docs.astral.sh/uv/)。

```powershell
# 1) 配置：config.toml 不进版本管理，每台机器一份
copy config.example.toml config.toml

# 2) 装依赖：控制面 + 本机要跑的角色
python scripts\bootstrap.py --role control
python scripts\bootstrap.py --role blender    # 本机做后处理时才需要（要求 Python 3.11）
python scripts\bootstrap.py --role comfy      # 只把生成转发给 ComfyUI 时用它：完全不需要 torch

# 3) 构建工作台：web/dist 不入库，克隆后必须构建一次
cd web; npm install; npm run build; cd ..

# 4) 启动
scripts\start.ps1                             # Windows：后台运行，日志写到 var\logs\control.log
```

然后打开 **http://127.0.0.1:8090**（端口在 `config.toml` 的 `[control] port`）。

macOS / Linux 用 `scripts/bootstrap.sh` 与 `scripts/serve.sh`，或直接 `python scripts/serve.py all` 前台跑全套。
权重按机器下载：`python scripts/prefetch.py --role <角色>`（`--mv` 会额外拉多视角权重，约 +17 GB）。

### 两条配置轴（最容易搞混的地方）

| 轴 | 存在哪 | 回答什么问题 |
|---|---|---|
| `[servers]` | `config.toml`，也可在设置页改（存 SQLite） | **哪个进程**跑这个阶段的 Python。`local` = 本机启一个 worker，仍然走 127.0.0.1 的 HTTP，**不是进程内调用** |
| 生成后端 | SQLite，在设置页改 | 那个阶段进程**去连哪一台 ComfyUI** |

因为 `comfy_worker.*` 是**不含 torch 的 HTTP 代理**，"2D 和 3D 在两台服务器"这件事是靠**第二轴**满足的。
所以三个 `[servers]` 全填 `local` 是完全正常、也是推荐的单机用法。展开说明见
[`docs/STAGES.md`](docs/STAGES.md) / [`docs/STAGES.zh-CN.md`](docs/STAGES.zh-CN.md)。

### 常用命令

```powershell
python scripts\serve.py doctor                   # 环境体检：各角色的解释器、缺哪些依赖、能不能跑
python cli\assetctl.py generate --prompt "..."   # 一条命令走完生图 → 3D
python cli\assetctl.py images  --prompt "..."    # 只生参考图（便宜，先挑再做成 3D）
python cli\assetctl.py make3d  --image-job <id> --candidate cand_00.png --start
python cli\assetctl.py view    <job-id>          # 在本地 Blender 里带标注对比打开产物
python cli\assetctl.py queue                     # 队列与显存状态
python -m pytest -q tests                        # 测试（不需要 GPU 和模型）
```

### 一次实测（本机，仅供参考；换机器差异很大）

| 环节 | 结果 |
|---|---|
| 生 3D 主模型 | 394.9 s → 998,592 面 |
| Blender 优化 | 36.4 s → 5,962 面（目标的 0.60%）、1024² 贴图、2 级 LOD、200 面碰撞体、校验 0 错误 |
| 产物体积 | 成品 GLB 3.4 MB；主模型 GLB 64.7 MB |

更多实测见 [`docs/RESULTS.md`](docs/RESULTS.md) 与 [`BENCHMARKS.md`](BENCHMARKS.md)。

### 目录

| 路径 | 是什么 |
|---|---|
| `config.example.toml` | 所有配置项都在这里带注释；复制成 `config.toml`（不进版本管理） |
| `scripts/serve.py` | 跨平台启动器 + `doctor`：在本机跑某个角色 |
| `scripts/bootstrap.py` | 按角色安装依赖（`requirements/<role>.txt`） |
| `scripts/prefetch.py` | 在本机下载该角色需要的权重 |
| `scripts/start.ps1` / `stop.ps1` / `logs.ps1` | Windows 上后台运行控制面 |
| `services/studio/studio/` | FastAPI 应用（`api.py`）、编排器（`worker.py`）、流水线、任务库、预设、校验 |
| `services/runner/runner.py` | 阶段服务本体：只用标准库的 HTTP 服务，一次跑一个阶段子进程 |
| `services/studio/studio/runner_client.py` | 传输协议的控制面一侧（上传输入、取回产物包） |
| `services/image_worker/` · `services/pixal3d_worker/` · `services/comfy_worker/` · `services/blender/` | 各阶段的实现 |
| `web/` | React 工作台（创建 → 队列 → 素材库 → 3D 检视），由 API 托管 |
| `cli/` · `mcp/` | 无依赖 CLI、以及同样动作的 MCP 封装 |
| `presets/` | `styles.yaml`、`quality.yaml`（含 OOM 降级阶梯）、`workflows/` |
| `docs/` | 部署、工作台、阶段服务、实测记录 |

### 文档

| 文档 | 内容 |
|---|---|
| [`README.en.md`](README.en.md) | 完整英文文档（本仓库 README 的英文原文） |
| [`docs/STAGES.md`](docs/STAGES.md) · [`docs/STAGES.zh-CN.md`](docs/STAGES.zh-CN.md) | 阶段服务是什么、为什么存在、两条配置轴、故障排查（英 / 中） |
| [`docs/DISTRIBUTED.md`](docs/DISTRIBUTED.md) | 跨机器部署：worker 机器怎么装、怎么连、token 与安全 |
| [`docs/PORTAL.md`](docs/PORTAL.md) | 工作台功能与语言设置 |
| [`docs/RESULTS.md`](docs/RESULTS.md) · [`BENCHMARKS.md`](BENCHMARKS.md) | 实测记录与性能 |

---

## English

### What it is

**Type one sentence, get a game-ready 3D asset.** Everything runs on your own machines: no accounts, no uploads, no
cloud. Since 0.2 there is no Docker either — a FastAPI control plane calls **independently addressable stage
services** over HTTP, so the image generator and the 3D generator can live on two different boxes, each with the
environment and GPU it needs.

You describe an object; asset-studio paints a reference image, turns it into a high-detail 3D master, optimizes that
master down to the triangle budget you asked for, bakes the detail into textures, builds LODs and a collision hull,
renders previews, and hands you a folder ready for Godot, Unity or Blender.

### The pipeline (five stages)

| Stage | What it does | Where it runs |
|---|---|---|
| `reference` | Reference image candidates + a technical score | `image` stage service |
| `pixal3d` | Reference image → high-detail 3D master (GLB) | `pixal3d` stage service |
| `blender` | Decimate to budget, bake maps, LODs, collision hull, previews | `blender` stage service (bpy 4.5) |
| `validate` | GLB validation (triangles / textures / scale) | in the control-plane process |
| `package` | Zip + `manifest.json` | in the control-plane process |

### What this repository changes

1. **No Docker.** One native Python environment per role (`requirements/<role>.txt`); files travel over HTTP
   (inputs uploaded, result bundle downloaded), so no shared volume is needed.
2. **Configurable machines.** Each stage in `[servers]` is `local` or a URL; `[python]` sets a per-role interpreter
   (the Blender role must be Python 3.11 — bpy ships cp311 wheels only).
3. **Bilingual portal** (~300 message keys, zh + en), following the system language, switchable in Settings.
4. **Addresses configured and tested in the UI**, for both the generation backends (ComfyUI URL + workflow) and the
   three stage services, with a Test button that probes the value in the box.
5. **Readiness gate.** If a required server does not answer, new jobs are refused up front (HTTP 422 `not_ready`
   with structured reasons) while the portal stays usable, so you can fix the address in place.
6. **A real 3D inspector** (three.js): 8 display modes (shaded / shaded+wire / wireframe / **normals** / clay /
   **toon (banded ramp + outline)** / **UV check** / albedo), overlays (ground grid, axes, bounds, shadow), camera
   presets, **pick a part to isolate, focus or hide it**, live triangle/vertex/mesh/material/texture/draw-call/FPS
   counts, a texture list and scene tree, and screenshot export.
7. **A rebuilt design system**: one token set, light and dark themes, skeletons and empty states, responsive,
   keyboard reachable.
8. **New docs**: [`docs/STAGES.md`](docs/STAGES.md) and [`docs/STAGES.zh-CN.md`](docs/STAGES.zh-CN.md).
9. **3D generation starts immediately** by default.

### Quick start (Windows first)

Requires **Python 3.11**, **Node 18+** (only to build the portal) and optionally [uv](https://docs.astral.sh/uv/).

```powershell
copy config.example.toml config.toml          # config.toml is git-ignored: one per machine
python scripts\bootstrap.py --role control    # the control plane
python scripts\bootstrap.py --role blender    # only if this machine does the bpy post-processing
python scripts\bootstrap.py --role comfy      # proxying to ComfyUI needs no torch at all
cd web; npm install; npm run build; cd ..     # web/dist is not committed: build the portal once
scripts\start.ps1                             # or: python scripts\serve.py all
```

Then open **http://127.0.0.1:8090**. On macOS/Linux use `scripts/bootstrap.sh` / `scripts/serve.sh`.

### The two configuration axes

| Axis | Lives in | Answers |
|---|---|---|
| `[servers]` | `config.toml` (or Settings, stored in SQLite) | **which process** runs that stage; `local` still means an HTTP worker on 127.0.0.1, not an in-process call |
| generation backend | SQLite, edited in Settings | **which ComfyUI** that stage process talks to |

Because `comfy_worker.*` are torch-free HTTP proxies, "2D and 3D on different servers" is satisfied by the *second*
axis — a single-machine setup with all three `[servers]` set to `local` is normal and recommended.

### Documentation

[`README.en.md`](README.en.md) (full English documentation) · [`docs/STAGES.md`](docs/STAGES.md) ·
[`docs/DISTRIBUTED.md`](docs/DISTRIBUTED.md) · [`docs/PORTAL.md`](docs/PORTAL.md) ·
[`docs/RESULTS.md`](docs/RESULTS.md) · [`BENCHMARKS.md`](BENCHMARKS.md)

---

## 来源与致谢 / Provenance & credits

### 出处 / Origin

| | |
|---|---|
| **上游项目 / Upstream** | [`zorrobyte/asset-studio`](https://github.com/zorrobyte/asset-studio) —— 本仓库由它衍生而来。上游的提交历史、提交信息与作者信息都完整保留在本仓库的 git log 中；本仓库在其之上新增了前述改动。 |
| **本仓库 / This fork** | `lovoror/ai-asset-studio`（当前仓库） |
| **代码许可 / Code licence** | [BSD Zero Clause License (0BSD)](LICENSE) —— 两个仓库同为 0BSD：随便用，商用也行，不要求署名。`LICENSE` 文件沿用上游，署名行为 "Copyright (c) 2026 asset-studio contributors"。 |
| **模型许可 / Model licences** | **模型有自己的许可，而且约束的是你用它们生成出来的东西。** 代码是 0BSD 不代表产物也是，发布资产前请逐一核对。 |

本项目**下载并使用**的模型各有条款，以下为上游文档中的说明，请以各模型页面为准：

* **Apache-2.0**：Qwen-Image-2512、Z-Image Turbo、FLUX.2 Klein 4B
* **FLUX 非商用许可**：FLUX.2 Klein 9B
* **另有条款（见 Hugging Face 模型页）**：Pixal3D / TRELLIS.2、RMBG-2.0
  （本项目默认使用开源的 BiRefNet 替代 gated 的 RMBG-2.0）

### 致谢 / Credits

asset-studio 本质是**围绕别人的模型和工具做编排**。真正的功劳属于这些项目：

| 项目 / Project | 用途 / Used for |
|---|---|
| [Pixal3D](https://huggingface.co/TencentARC/Pixal3D) | 单图 / 多视图 → 3D |
| [TRELLIS.2](https://github.com/microsoft/TRELLIS) | 图 → 3D 的基础环境（o-voxel、FlexGEMM、CuMesh、nvdiffrast） |
| [Qwen-Image-2512](https://huggingface.co/Qwen/Qwen-Image-2512) | 参考图生成 |
| [meshoptimizer](https://github.com/zeux/meshoptimizer) | 网格简化 |
| [MoGe](https://github.com/microsoft/MoGe) | 单目几何 / 相机估计 |
| [NAF](https://github.com/valeoai/NAF) · [BiRefNet](https://huggingface.co/ZhengPeng7/BiRefNet) | 条件控制 · 抠图 |
| [Blender](https://www.blender.org/)（bpy） | 后处理、烘焙与预览渲染 |
| [three.js](https://threejs.org/) | 工作台的 3D 检视器 |
| [React](https://react.dev/) · [Vite](https://vite.dev/) · [lucide](https://lucide.dev/) | 工作台前端 |
| [FastAPI](https://fastapi.tiangolo.com/) · [uvicorn](https://www.uvicorn.org/) | 控制面 HTTP 服务 |
| [ComfyUI](https://github.com/comfyanonymous/ComfyUI) | 可选的生成后端 |

依赖的精确版本、模型 revision 与上游 commit 都记录在
[`manifests/dependency-manifest.json`](manifests/dependency-manifest.json) 中。
