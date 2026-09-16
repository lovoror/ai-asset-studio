# 阶段服务

**阶段服务**（stage worker）是一个很小的 HTTP 服务，它在拥有该阶段 Python 环境的那台机器上，把任务的一个阶段当作子进程来运行。
它就是 `services/runner/runner.py`：只用标准库，不含任何 CUDA 代码，不依赖 torch，也没有 Web 框架。控制面为每个角色启动一个 worker 进程，
通过 HTTP 调用它，并且从不与它共享文件系统。

「为什么要有这个东西」的简短回答是：它取代了 Docker。asset-studio 刻意不使用容器（自 0.2 起）：阶段服务让
`reference`、`pixal3d` 和 `blender` 可以跑在 Python 环境和 GPU 各不相同的机器上，而控制面始终掌握任务数据库和全部产物。

本文是这一概念及其边界的参考。跨机器**如何部署** worker 请看 [`DISTRIBUTED.md`](DISTRIBUTED.md)——尤其是它的第 5 节（网络上传了什么）、
第 7 节（迁移 Blender）、第 8 节（用 ComfyUI 代替本机 GPU）和第 9 节（故障排查）；这些内容本文不再重复。

## 1. 阶段服务是什么

`services/runner/runner.py` 是一个 `ThreadingHTTPServer`，只做一件事：对每一次阶段尝试，创建一次运行、接收上传的输入、启动
`python -m <module> --request <run>/request.json`、镜像日志，最后把该阶段的输出目录以 zip 返回。它**一次只跑一个阶段子进程**
（`CURRENT["run_id"]`）；在已有运行进行时再发一次 `start`，会得到 `409 busy`。

角色在启动时就固定下来：命令行上的 `--roles`，或环境变量 `STUDIO_WORKER_ROLES`（`scripts/serve.py` 两者都会传，来源是
`[worker] roles` 和你要启动的那个角色）。不带 `--roles` 启动的 worker 接受所有角色。

```text
python -m runner.runner --name blender-worker --roles blender \
    --host 0.0.0.0 --port 8703 --token <token> --work-dir var/data/worker/blender
```

构造这条命令行的是 `scripts/serve.py <role>`。它使用的解释器，优先取 `[python] <role>`，否则就是运行 `serve.py` 的那个解释器；
端口取 `[worker] <role>_port`（默认 8701/8702/8703）；工作目录是 `[paths] data/worker/<role>`——每个角色一个，因为每个 worker
启动时会清理**自己的**临时目录。

### 端点

鉴权：只要配置了 `[worker] token`，除 `/health` 以外的每个端点都需要 bearer 令牌。

| 端点 | 作用 |
|---|---|
| `POST /runs` | 用 `{"job_id","stage","module"}` 创建一次运行；`module` 不在该 worker 的允许清单里时返回 `403` |
| `PUT /runs/<id>/input/<name>` | 以原始请求体上传一个输入文件（`Content-Length` 或 chunked）；仅在运行处于 `created` 状态时可用 |
| `POST /runs/<id>/start` | 用 `{"request": {...}}` 启动阶段；若有其他运行正在进行则返回 `409` |
| `GET /runs/<id>` | `state`（`created`/`running`/`exited`）、`pid`、`returncode`、`cancelled`、`peak_rss_mb`、`peak_gpu_used_mib` |
| `GET /runs/<id>/log?offset=N` | 从字节偏移 `N` 开始的阶段 stdout+stderr；`X-Log-Size` 给出总长度，调用方据此追加并从该处继续（也支持 `?tail=N`） |
| `GET /runs/<id>/bundle` | 该运行 `out` 目录的 zip（仅存储压缩；内容本来就是已压缩的 GLB/PNG/JPEG） |
| `POST /runs/<id>/cancel` | 终止整棵进程树：先优雅终止，10 秒后强制终止 |
| `DELETE /runs/<id>` | 删除该运行的记录及其工作目录 |
| `POST /cancel_current` | 取消当前正在进行的运行——用于控制面重启后的恢复 |
| `POST /exec` | 短的同步辅助调用（不做 GPU 工作），用于 `--list` 这类探测 |
| `GET /health` | `ok`、`busy`、`run_id`、`name`、`roles`、`version`、`platform`、`python`——唯一不需要鉴权的端点 |
| `GET /gpu` | `nvidia-smi` 摘要：GPU 与计算进程 |

### 路径占位符

这里没有任何共享：阶段需要的每条路径，要么在启动前上传，要么在运行目录内创建。控制面在发出请求时把绝对路径改写掉
（`runner_client.StagePlan`），worker 在接收时解析这些占位符（`resolve_placeholders`）：

```text
on the control plane                          on the worker
<jobs>/<id>/stages/<stage>/...             -> @out/...                          -> <run>/out/...
<jobs>/<id>/artifacts/master.glb           -> @in/artifacts/master.glb  (uploaded) -> <run>/in/artifacts/master.glb

@out            -> <run>/out                    what the stage produces; this is what the bundle returns
@in/<relpath>   -> <run>/in/<relpath>           a file uploaded before start
@work           -> <run>                        the run directory itself
@presets[/<p>]  -> the worker's own checkout     (STUDIO_PRESETS_DIR on that machine)
@root[/<p>]     -> the worker's own repo root    (STUDIO_ROOT on that machine)
```

`@in/...` 和这些占位符的取值都会被校验：路径不能是绝对路径，不能带盘符，也不能向上穿越（`safe_relpath`、`safe_name`）。
引用到**任务目录之外**文件的请求根本无法传输——该路径会原样保留，阶段会报出它自己的错误。

## 2. 一个任务的五个阶段

`pipeline.STAGES` 是 `["reference", "pixal3d", "blender", "validate", "package"]`。

| # | 阶段 | 做什么 | 角色 | 模块 | 是否离开控制面 |
|---|---|---|---|---|---|
| 1 | `reference` | 根据请求与风格拼出提示词，生成候选参考图，打分并选中一张 | `image` | `image_worker.generate`，或 `comfy_worker.image_generate` | 是——通过 HTTP 发到 `image` 阶段服务 |
| 2 | `pixal3d` | 一张参考图（或多视角目录）→ `master.glb` | `pixal3d` | `pixal3d_worker.generate`，或 `comfy_worker.three_d_generate` | 是——通过 HTTP 发到 `pixal3d` 阶段服务 |
| 3 | `blender` | 基于 `master.glb` 做减面 / 烘焙 / LOD / 碰撞体 / 预览渲染 | `blender` | `blender.process_asset` | 是——通过 HTTP 发到 `blender` 阶段服务 |
| 4 | `validate` | 对母版、优化后资产、每个 LOD 和碰撞体调用 `validate_glb` | — | `studio.validate.validate_glb` | **否**——在控制面进程内运行 |
| 5 | `package` | 写出 `manifest.json`、产物清单和每个文件的 `sha256` | — | `studio.pipeline` | **否**——在控制面进程内运行 |

**只有 `reference`、`pixal3d` 和 `blender` 会被发到阶段服务。** `validate` 和 `package` 只是对已经在任务目录里的文件做纯 Python
处理，所以它们直接调用进程内函数：没有 worker，没有上传，没有下载，也没有 `result.json` 需要回收。这也是就绪闸门从不检查
`validate` 或 `package` worker 的原因——它们根本不存在。

### 用哪个模块，以及为什么

| 该 lane 指向 | `reference` 运行 | `pixal3d` 运行 |
|---|---|---|
| 本地后端 | `image_worker.generate`（diffusers + torch） | `pixal3d_worker.generate`（Pixal3D / TRELLIS.2 + torch） |
| 某个 ComfyUI 服务（`kind = "comfyui"`） | `comfy_worker.image_generate` | `comfy_worker.three_d_generate` |

`blender` 始终运行 `blender.process_asset`。这个选择由 `pipeline.JobRun.remote(lane)` 做出，判断依据就是「该 lane 的 `kind` 是不是
`comfyui`」。它之所以重要，是因为这两个系列是完全不同的依赖：`comfy_worker.*` 只 import 标准库和 `worker_paths`（它的 HTTP 客户端是
`urllib`），所以两条 lane 都指向 ComfyUI 的 worker **完全不需要 torch 和 CUDA**——它就是一个 HTTP 代理。而本地模块会 import torch，
并在 worker 进程内加载模型。

有两个阶段有时根本不会运行：

* 调用方自带参考图、任务带多视角目录，或者该任务是某个生图任务的变体时，`reference` 会被跳过
  （`stage_reference_provided`、`stage_reference_from_parent`）。此时这个任务不需要 `image` worker。
* **生图任务**（`kind = "image"`）只跑 `reference` 再跑 `package` 就结束：它产出的是方案，在你选中之前不会生成任何 3D。

## 3. 为什么要有阶段服务

Docker 是被刻意从这个项目里去掉的（README：「since 0.2 no Docker either」）。阶段服务取代了它的位置，而它的每一项性质都对应着
容器曾经解决的问题：

* **按角色隔离环境。** `bpy` 每个发行版只发布一个解释器 tag（4.5/5.0 是 cp311，5.1+ 是 cp313），而且完全没有 cp312 版本，
  而控制面可以跑在任何 Python 3.11+ 上。torch、diffusers 和 numpy 的版本在各角色之间同样互相冲突。一个角色一个虚拟环境，
  一个角色一个 worker 进程，绝不共用解释器。
* **在没有共享卷的情况下，把仓库里的 Python 代码跑在另一台机器上。** worker 有自己的 checkout 和自己的模型缓存；跨机器的只有
  HTTP。输入靠上传，输出以结果包返回，而上面的占位符让每条路径都落在本次运行自己的目录里。
* **进程隔离，长期驻留的进程不占显存。** worker 刻意不 import 任何 CUDA/torch 代码，所以常驻进程不持有显存；阶段子进程退出时，
  驱动会释放它的全部 CUDA 分配。没有这一点，2D 和 3D 阶段就只能共处一个进程、一个 CUDA 上下文。
* **崩溃与 OOM 的隔离。** 段错误、显存耗尽或卡死的阶段只会杀掉它自己的子进程。超时（`[stage] stage_timeout_s`，默认 7200 秒）
  或取消时，控制面取消该次运行，worker 杀掉整棵**进程树**——Windows 上用 `taskkill /F /T`，其他地方在 10 秒 `SIGTERM` 宽限后
  用 `killpg(SIGKILL)`——因为阶段会 fork 出辅助程序，只杀 python 进程会把 GPU 一直占着。
* **用模块允许清单代替远程 shell。** worker 只接受它启动时所带角色对应的模块：

  | 角色 | 可以运行的模块 |
  |---|---|
  | `image` | `image_worker.generate`、`comfy_worker.image_generate` |
  | `pixal3d` | `pixal3d_worker.generate`、`comfy_worker.three_d_generate` |
  | `blender` | `blender.process_asset` |

  其他模块一律以 `403` 拒绝（`/exec` 使用同一份清单）。额外的模块只能显式开启，通过 `--allow-module` /
  `STUDIO_WORKER_EXTRA_MODULES`，所以默认始终是关闭的。2D 那台机器不会被要求去跑 Blender，令牌被盗也无法把 worker 变成
  通用的远程 shell。
* **bearer 令牌，以及拒绝被意外暴露。** 这个 API 会运行代码，所以它不是开放的：除 `/health` 外的每个端点都需要 `[worker] token`，
  而 `bind` 不是回环地址的 worker 在**没有令牌时会拒绝启动**。`/health` 保持开放是有意的，这样监控和 `serve.py doctor` 才能工作。

worker 同时是一次性的：启动时它会清空整个工作根目录（阶段的临时目录从来不是权威数据——控制面自己保留每一份产物），
24 小时后丢弃已完成的运行、1 小时后丢弃从未启动的运行，而且一次运行只会写自己目录下的文件。

## 4. 两条正交的配置轴

这是最容易混淆的部分。这里有两个互相独立的设置，回答的是两个不同的问题。

```text
Axis A - config.toml [servers]  (or Settings -> Stage workers)
  WHICH MACHINE runs the stage process, and where this control plane reaches it.

    [servers]
    image   = "local"                       -> http://127.0.0.1:8701  (this machine, over loopback HTTP)
    pixal3d = "local"                       -> http://127.0.0.1:8702
    blender = "local"                       -> http://127.0.0.1:8703
    # or, a worker on another machine (a bare "host:port" works too):
    image   = "http://192.168.1.21:8701"    -> that machine's runner, plain HTTP
    pixal3d = "http://192.168.1.22:8702"
    blender = "http://192.168.1.22:8703"

Axis B - generation backend, stored in SQLite, edited on Settings -> Generation backends
         (or PUT /v1/settings). WHICH ComfyUI SERVER that stage process talks to.

    image_kind   = "comfyui"   image_url   = "http://127.0.0.1:8188"
                               image_workflow = "KREA-2-TURBO.json"
    three_d_kind = "comfyui"   three_d_url = "http://10.130.136.171:8388"
                               three_d_workflow = "3d_pixal3d_trellis2_image_to_model.json"
```

两者是串联关系，而不是二选一：

```text
control plane  --- Axis A --->  stage worker process  --- Axis B --->  ComfyUI server
(this repo)                     (image_worker.* or                     (the GPU)
                                 comfy_worker.*)
```

`local` 并不是进程内调用。它的意思是「在本机启动那个 worker 进程，但仍然通过回环 HTTP 在端口 8701/8702/8703 上访问它」
（`config.normalize_endpoint`）。无论 worker 在本机还是在另一台机器上，整套传输协议完全一样；变化的只有地址。

### 由此得到的结论

因为 `comfy_worker.*` 是不依赖 torch 的 HTTP 代理，**「生图和生 3D 跑在不同的服务器上」这件事是由轴 B——即 ComfyUI 地址——
满足的，而不是轴 A。** 一个完全正常工作的单机部署，完全可以三个 `[servers]` 都是 `local`：

```text
control machine (one checkout of this repo)              GPU boxes (no asset-studio installed)
+------------------------------------------------+
| studio.api        portal + HTTP API            |
| studio.worker     orchestrator                 |
| runner:image      loopback HTTP :8701 ---------+---> http://127.0.0.1:8188       RTX 4060 8 GB
| runner:pixal3d    loopback HTTP :8702 ---------+---> http://10.130.136.171:8388  RTX 4090 24 GB (--novram)
| runner:blender    loopback HTTP :8703 (CPU)    |
| var/jobs  var/data (SQLite)                    |
+------------------------------------------------+

[servers] image = "local"   pixal3d = "local"   blender = "local"
Settings  image -> http://127.0.0.1:8188        three_d -> http://10.130.136.171:8388
```

三个 worker 进程都在这台机器上，全部通过回环访问；两台 ComfyUI 服务，其中一台是远程的。轴 A 完全不需要改动。
那两台 ComfyUI 机器上根本没有 asset-studio。

有一个不对称值得知道：**阶段服务**由控制面调用，所以对**它**的健康探测来自控制面；而 **ComfyUI 服务**由 **worker** 调用，
所以那个探测是通过 worker 自己的 `/exec` 发出的，报告的是 worker 看到的结果。因此，一个 ComfyUI 地址完全可能从你的笔记本
访问不到，但对那个阶段来说一切正常。

## 5. 什么时候真的需要改轴 A

当**阶段进程**必须放到别处时，才改 `[servers]`（或设置页的「阶段服务」）：

* **把 Blender 放到 CPU 更强的机器上。** Blender 只用 CPU；如果 3D 那台机器有空闲核心，而且母版 GLB 已经在那里，
  迁移这个阶段就能免去来回搬运母版（见 DISTRIBUTED.md 第 7 节）。
* **在 4090 那台机器上跑本地（非 ComfyUI）Pixal3D 模型。** 如果你想用 `pixal3d_worker.generate` 而不是
  `comfy_worker.three_d_generate`，那台机器上必须有 torch + TRELLIS.2 环境和权重，于是 `pixal3d` 阶段进程要搬过去。
* **隔离生图 lane。** 把 `image` 放到单独一台机器，可以让一次很长的生图不占用控制机，也让你能单独重启这条 lane。

以 Blender 为例的操作步骤。在 worker 机器上：

```toml
# config.toml on the worker machine
[worker]
bind  = "0.0.0.0"          # accept the control machine's connections
token = "<shared secret>"  # a non-loopback bind refuses to start without it
roles = "blender"          # optional: this box only ever runs the blender stage

[python]
blender = ".venv-blender/Scripts/python.exe"   # bpy is a cp311 wheel; see requirements/blender.txt
```

```powershell
scripts\bootstrap.ps1 blender          # or: python scripts\bootstrap.py --role blender
python scripts\serve.py blender        # one worker process, foreground; Ctrl-C to stop
```

`scripts/serve.py` 把角色作为**位置参数**（`control|image|pixal3d|blender|all|doctor`）；`--role` 属于
`scripts/bootstrap.py`，不属于 `serve.py`。

在控制机上：

```toml
# config.toml on the control machine
[servers]
blender = "http://192.168.1.22:8703"

[worker]
token = "<shared secret>"   # what this control plane sends on every worker call
```

```powershell
python scripts\serve.py doctor     # prints the topology and probes every stage worker
```

防火墙：把 worker 的端口**从控制机方向入站**开放——TCP 8701（`image`）、8702（`pixal3d`）、8703（`blender`），
或者 `[worker] <role>_port` 里配置的值。控制面只会主动向外调用；worker 从不回调。

worker 机器上必须满足的条件：

| 要求 | 原因 |
|---|---|
| 本仓库的一份 checkout | worker 从**那棵**代码树运行 `runner.runner` 和阶段模块；`@root/...` 也解析到那里 |
| 该角色的虚拟环境 | `scripts/bootstrap.py --role <role>`；当它不是运行 `serve.py` 的解释器时，要在 `[python] <role>` 里指定 |
| `presets/` | 该角色读取的预设。ComfyUI lane 还会**在 worker 上**解析它的工作流 JSON——只存在于你笔记本上的工作流会「找不到」 |
| 它自己的 `config.toml` | `[worker] bind`/`token`、`[paths] data`、`[paths] models`（那台机器的权重） |
| 它自己的临时目录 | `[paths] data/worker/<role>`；由 worker 自己创建和清理 |

## 6. 运行行为

### 就绪闸门

`[stage] require_ready` 默认为**开启**（`config.REQUIRE_READY`）。开启时，开始新工作——创建、放行和重试——都会先问
`backends.ready_for_job()`：这个任务真正会用到的每台服务器是否有响应：

* 任务需要的阶段服务（`required_stages`：生图任务需要 `image`；资产任务需要 `pixal3d` 和 `blender`，只有在必须**生成**参考图时才
  额外需要 `image`；从 `blender` 重试只需要 `blender`）；
* 每个 `kind = comfyui` 的 lane：它的地址**以及**工作流文件，且是通过 worker 探测的。

只要有缺失，请求会立刻被拒绝，返回 HTTP `422` 和结构化的响应体：

```json
{
  "error": "not_ready",
  "problems": [{"code": "worker_unreachable", "role": "pixal3d", "url": "http://127.0.0.1:8702",
                "detail": "the pixal3d stage worker at http://127.0.0.1:8702 is unreachable: ..."}],
  "message": "cannot start: ..."
}
```

这些 code（`worker_unreachable`、`backend_not_configured`、`backend_unreachable`）正是工作台横幅本地化时使用的。
**只有新工作会被拦住**：服务本身继续运行，所以你仍然能打开工作台去修正地址，素材库也照常可用——这正是生成服务挂掉时你需要的。
把 `[stage] require_ready = false` 设上，就能恢复「先排队」的行为，适合宁愿排队重试的部署。

`GET /v1/readiness` 是给界面的同一份信息（也决定按钮是否禁用）。`POST /v1/backends/test` 用表单里的值而不是已保存的值测试单个地址，
所以候选地址可以在保存前先测。

### 重启语义

* **地址改动对下一个任务生效。** `Runner` 在构造时读取 `config.effective_runners()`，而 runner 是按任务/请求构建的，所以新的
  `[servers]` 值或设置页的改动无需重启就会被采用（`PUT /v1/settings` 还会清掉缓存的探测结果）。
* **启停本机的 worker 进程需要重启控制面。** `scripts/serve.py` 在启动时就固定了它的计划：`control` 会启动 `api` + `worker`，
  再为每个 `[servers]` 值为 local 的阶段启动一个 runner。把地址改成或改离 `local`，会改变「应该存在哪些进程」，而这个决定不会被重新读取。

### 探测

探测结果会短暂缓存，并且并行执行。阶段服务与 ComfyUI 的结果缓存 15 秒（`backends.CACHE_S`）——但你按**测试**按钮时始终是实时的——
三个阶段地址并发探测（最多 8 个线程），所以一次就绪检查的代价是最慢的那一次探测，而不是它们的总和。在某些 Windows 环境下，
即使是被拒绝的回环连接也要约 2 秒，这就是「设置页响应很快」和「三个 worker 都挂了时卡十秒」的区别。

## 7. 故障排查

分布式部署的症状/原因/修复对照表在 DISTRIBUTED.md 第 9 节；本表只覆盖与阶段服务这一边界相关的问题。

| 症状 | 原因 | 检查 |
|---|---|---|
| 闸门报告某个角色 `worker_unreachable` | 那个阶段进程没在跑、端口被挡，或地址写错了 | 在两台机器上分别跑 `python scripts\serve.py doctor`；看 `GET /v1/readiness`；worker 上的 `[worker] bind` 必须是 `0.0.0.0` |
| 阶段日志：`missing or invalid worker token`（HTTP 401） | 控制面的 `[worker] token` 与 worker 的不一致 | 对比两台机器上的 `[worker] token`——控制面发送的是**它自己的**值 |
| worker 启动即退出：`refusing to listen on 0.0.0.0 without a token` | 绑定了非回环地址但令牌为空 | 在那台机器上设置 `[worker] token` |
| `module '...' is not served by role(s) [...]` | worker 启动时的 `--roles` / `[worker] roles` 排除了这个阶段 | 用该角色重启它，或放宽 `[worker] roles` |
| 阶段日志里 `no module named ...` | 该角色的依赖在 **worker 机器上**缺失 | 在那台机器上跑 `python scripts\bootstrap.py --role <role>` |
| 后端测试提示 `workflow NOT found on the worker` | 工作流 JSON 在你的 checkout 里存在，但 worker 上没有 | 把它复制到 worker 的 `presets/workflows/` |
| Blender 阶段在 `import bpy` 处挂掉 | 该角色跑在没有匹配 bpy wheel 的解释器上（根本没有 cp312 版本） | 配 `[python] blender`；`python scripts\bootstrap.py --role blender`；`serve.py doctor` 会打印每个组件使用的解释器 |
| `busy`，或 `worker ... stayed busy with another run for 600s` | 上一个控制面留下了未结束的运行 | 在那台 worker 上 `POST /cancel_current`，或 `DELETE /runs/<id>` |
| 把地址改成或改离 `local` 后没有任何变化 | `scripts/serve.py` 在启动时固定了计划 | 重启控制面（先 `scripts\stop.ps1`，再 `scripts\start.ps1`） |
| 结果包收到了，但阶段输出缺失 | 阶段本身失败了；结果包里只有它实际写出的东西 | 读阶段日志，它在阶段运行期间会镜像到控制机 |

## 参见

* [`DISTRIBUTED.md`](DISTRIBUTED.md)——部署这些 worker：拓扑、令牌、传输、迁移 Blender、ComfyUI 服务、故障排查。
* `config.example.toml`——`[servers]`、`[worker]`、`[python]`、`[stage]`。
* `services/runner/runner.py`、`services/studio/studio/runner_client.py`——这套协议的两端。
* `services/studio/studio/backends.py`——探测与就绪闸门。
