# 多视角输入：多张参考图 → 一个资产

Asset Studio 平时把**一张**参考图做成 master GLB；**多视角**任务则把同一个物体的**2～12 张**图片合成一个。
一张四分之三视角的图永远看不到背面和两侧，多视角是唯一的办法。

ComfyUI 侧用 `Pixal3DMultiViewConditioning` 实现（2026-09-08 进入 ComfyUI，官方模板 `3d_pixal3d_multi_views`）；
本地 torch 侧走 Pixal3D 自己的 `inference_mv`。

*English: [MULTIVIEW.md](MULTIVIEW.md)*

---

## 1. 怎么提交多视角任务

`POST /v1/jobs`，用 `multiview` 对象代替 prompt：

```jsonc
{
  "name": "rusty-car",
  "style": "mobile_factory",
  "quality": "balanced",
  "lod_fractions": [0.5, 0.25],
  "multiview": {
    "camera_angle_x": 0.6912,           // 水平 FOV，单位是弧度（这里是 39.60°）
    "camera_source": "rig",             // "measured" | "rig" | "approximate"
    "mesh_scale": 1.0,
    "images_b64": {                     // 文件名 -> base64 PNG/JPEG（alpha 会被当作 mask）
      "front.png": "iVBORw0KGgo...",
      "left.png":  "iVBORw0KGgo...",
      "back.png":  "iVBORw0KGgo...",
      "right.png": "iVBORw0KGgo..."
    },
    "frames": [
      {"file_path": "front.png", "name": "front", "transform_matrix": [[...],[...],[...],[0,0,0,1]]},
      {"file_path": "left.png",  "name": "left",  "transform_matrix": [[...],[...],[...],[0,0,0,1]]},
      {"file_path": "back.png",  "name": "back",  "transform_matrix": [[...],[...],[...],[0,0,0,1]]},
      {"file_path": "right.png", "name": "right", "transform_matrix": [[...],[...],[...],[0,0,0,1]]}
    ]
  }
}
```

此时 `reference_candidates`、`variations` 和整条出图链路都会被跳过——图片本身就是参考图
（`presets.generates_reference`）。`multiview` 和 `reference_image_b64` 二选一，不要同时给。

每个 `transform_matrix` 是 4×4 的**相机到世界**矩阵，Z 轴朝上，相机沿自身 −Z 看出去；平移列就是相机位置。

## 2. 哪张图落在哪个视角

本地链路把整个目录交给 Pixal3D，由它按矩阵摆相机。ComfyUI 的节点做不到这一点：它只接受四个**有名字**的
视角，然后自己重建一个固定的、水平的、相隔 90° 的环绕机架。所以映射由控制面决定（`studio/multiview.py`），
规则按可信度从高到低，凡是退而求其次都会出告警：

| 依据 | 什么时候用 |
|---|---|
| 1. `name` | 该帧命名为 `front` / `left` / `back` / `right`。名字优先，即使矩阵与之矛盾——但矛盾会报警，因为两者必有一错 |
| 2. 相机方位角 | 没命名，且矩阵里的相机位置落在某个空槽位的 15° 以内 |
| 3. 输入顺序 | 没命名、矩阵不可用或重复；按 `front, left, back, right` 顺序占下一个空位，并告警说明是哪一帧按位置放的 |

方位角是把节点的机架公式 `position = (sin az, −cos az, sin el) · distance` 反解出来的，即对相机位置取
`az = atan2(x, −y)`。这和 `process_asset.render_views` 渲染预览图用的是**同一个约定**，所以 Asset Studio
自己渲出来的图不需要任何转换。

一个槽位只收一帧：第五张会带告警丢弃；重名保留第一张并告警；仰角超过 10° 的帧会报警（机架是水平的，
该姿态会被当成水平处理）。

`front` 同时也是成品资产的朝向——节点会把机架重新基准到第一个接入的视角上。

## 3. 水平 FOV

四个机架相机共用同一个距离（`_VIEW_PAD · 0.5 / tan(fov/2)`，`_VIEW_PAD = 1.1`），所以 `fov` 同时决定透视
和模型重建出来的尺度。这是唯一一个值得较真的数字。

| `camera_source` | 行为 |
|---|---|
| `rig`、`measured` | 调用方给的 `camera_angle_x` 换算成度后**钉死**在节点上，覆盖工作流里的测量连线 |
| `approximate` | 不信任该 FOV，交给工作流用 `MoGeGeometryToFOV` 从前视图测——这正是节点自己的文档里"给照片用"的写法 |

帧级的 `camera_angle_x`（同样是弧度）优先于输入级的值；两者冲突时以前视图为准并告警。换算后超出节点
1–170° 范围的声明值会被拒绝并告警，而不是硬塞进去。

## 4. 两条链路各自需要什么

| | 本地（`kind = "local"`） | ComfyUI（`kind = "comfyui"`） |
|---|---|---|
| 模块 | `pixal3d_worker.generate`（`mode = "multiview"`） | `comfy_worker.three_d_generate` |
| 输入 | `views_dir` + `transforms.json`（`reference` 阶段写入） | `views` = `{槽位: 路径}` + `view_fov_degrees`（控制面算好） |
| 工作流 | — | `presets/workflows/3d_pixal3d_multi_views.json` |
| 权重 | Pixal3D 多视角权重 | `pixal3d_multiview_int8_convrot.safetensors` |

### ComfyUI 的前置条件

* 3D 服务器上的 **ComfyUI 必须 ≥ 0.35.0** —— 更早的版本里没有 `Pixal3DMultiViewConditioning`，任务会以
  `… has no Pixal3DMultiViewConditioning node` 失败。更新方式和任何 ComfyUI 检出一样：`git pull`、装依赖、
  重启。
* `models/diffusion_models/` 里要有那个多视角权重，另外还需要共用的 `dino_v3_L_naf_fp32.safetensors`、
  `trellis_2_shape_vae_bf16.safetensors`、`trellis_2_texture_vae_bf16.safetensors`。
* **显存峰值在贴图 VAE 解码，不在采样。** 一个 19,415 面资产、1536 上采样、四视角的任务跑完了 49 个节点，然后在
  8 GB 卡上的 `VaeDecodeTextureTrellis` 崩了（已分配 3.40 GiB，又申请 2.04 GiB）。同一个任务把 `resolution` 调到
  **1024** 后 **3.0 分钟**跑完，产出一个 56 MB 的带贴图 GLB（698,803 面，baseColor + metallicRoughness + normal，
  含 TEXCOORD_0），而且它的前视图就是输入的前视图——槽位映射把模型摆正了。24 GB 卡在 1536 下余量充足；小卡上把
  `resolution`（`Trellis2UpsampleStage.target_resolution`）调低即可——这正是 `balanced` 画质档 OOM 降级阶梯里的同一步。

工作流由 `three_d_multiview_workflow`（设置项，默认 `3d_pixal3d_multi_views.json`）指定，所以无论单图工作流
配的是哪个，多视角任务都走自己那张图。

## 5. 多视角工作流本身

`presets/workflows/3d_pixal3d_multi_views.json` 是**生成**出来的，不是手工改的，生成脚本是
`var/build_multiview_workflow.py`，源文件是单图工作流。差异只有四处：

1. `UNETLoader` → `pixal3d_multiview_int8_convrot.safetensors`
2. `Pixal3DConditioning` → `Pixal3DMultiViewConditioning`（两者的 positive/negative 都在 0/1 号输出上，
   所以下游一行都不用改）
3. 每个视角的预处理链——`LoadImage` → `RemoveBackground` → mask 开关 → `MaskPreview` → `ImageCropToMask`
   → `PreviewImage`——按视角数量克隆。这一步不能省：节点要求每个视角都"物体最宽处约占画面 1/1.1，且各视角
   尺度一致"，而 `ImageCropToMask`（`pad_factor = 1.1`、居中方形、黑底）产出的正是这个
4. 去掉 TRELLIS.2 / Pixal3D 分支开关：这张图只走 Pixal3D，`trellis2` 不可能把多视角任务悄悄带回单图老路

worker 不需要被告知哪个节点对应哪个视角：它从每个槽位的输入沿图**回溯**到喂它的那个 `LoadImage`
（`three_d_generate.view_loaders`），因此节点编号始终只是工作流自己的事。

两个脚本保证它不出错：

```powershell
python var/build_multiview_workflow.py                       # 重新生成工作流
python var/check_workflow.py presets/workflows/3d_pixal3d_multi_views.json
```

`check_workflow.py` 做的是 ComfyUI `validate_prompt` 里便宜的那一半，对着在跑的 `/object_info` 检查：必填
输入是否齐全、每条连线是否指向真实存在的节点和输出槽、类型是否匹配。对单图工作流也跑一遍——两个都应该是
`0 problem(s)`。

## 6. 从已有资产渲染一套机架图

`var/render_rig_views.py` 用 `process_asset.render_views` 从任意 GLB 渲染四个视角，而它的相机摆位本来就是
节点的机架（`position = (sin az, −cos az, sin el) · distance`），所以方位角 0/90/180/270 直接对应
front/left/back/right，无需换算：

```powershell
.venv-blender\Scripts\python.exe var\render_rig_views.py <asset.glb> <out_dir> [size]
```

它会打印相机的水平 FOV，这个值就是该填进 `camera_angle_x` 的数（50mm 镜头是 39.60°）。这既是验证链路用的
测试素材，也是"从已有资产生成多视角输入"的基础。

## 7. 不用自己传图：让流水线把视角画出来

任务里打开 `generate_views`（或在设置里开 `auto_multiview` 作为新任务默认值），流水线就会自己画那几个环绕视角：
一次指令编辑，把选中的参考图画成一排视角，再切成上面那四张图。**这是"从一句提示词就能走多视角重建"的关键一步。**

```
提示词 -> 参考图 -> 多角度条（一次编辑，2048×512）
      -> front/left/back/right（studio/sheet.py）-> Pixal3D 多视角 -> master.glb
```

它跑在 **reference 阶段内部**，而不是单独一个阶段——因为视角是从参考图派生的：参考图作废时它们必须一起作废，
而重试逻辑本来就是整块丢弃 reference 阶段的结果。图条落在 `stages/reference/turnaround/`，四个视角落在
`stages/reference/views/`——和"调用方自己传多图"产出的形状完全一致，所以下游全是已有且已测过的代码。

| 设置 | 默认 | 为什么 |
|---|---|---|
| `views_workflow` | `krea2_turnaround.json` | 编辑图；需要 ComfyUI 图像链路 |
| `views_prompt` | 内置的那句 turnaround 指令 | 决定这一排长什么样的旋钮 |
| 画布（`VIEWS_SIZE`） | 2560×512，即 **4:1 分五格正方形** | 见下——宽高比决定"排不排成一排"，宽度决定格子形状 |
| `VIEWS_STEPS` | 8 步、CFG 1 | 节点作者文档里的 turbo 最快路径 |
| `VIEWS_GROUNDING_PX` | 768 | 节点文档里的 Qwen3-VL grounding 分辨率（384–768） |
| `VIEWS_FOV_DEGREES` | 20 | 节点自身默认值所描述的"多视角生成器输出" |

有四件事值得知道，都是实测的：

* **画布宽高比决定"排不排得成一排"。** 1:1 时无论指令怎么写、加不加 4-View LoRA，模型都只会把参考图重画成一张；
  4:1 时它就排成一排。
* **宽度必须给"模型自己画的那一格"留位置。** 因为那一格总会出现，2048 宽会被分成**五格 410px**——而 410×512 是竖长条，
  扁宽物体的侧视（汽车约 2:1）塞不进去。模型于是把它裁掉，只剩窄的正视和后视能用：重建出来的模型两侧要么缺、要么畸形。
  2560 宽才有 512×512 的正方形格子，实测侧视是完整的（宽高比 1.59 和 1.69）。
* **`grounding_px` 不是小细节。** 它限制交给 Qwen3-VL 的最长边，而 `0` 表示"原生"：整张参考图会走 **CPU** 视觉编码器。
  2000×2000 画布那样跑要**两小时**；流水线里 768 + 8 步只要 **2.9 分钟**。
* **模型画哪一侧并不可靠。** 多张实测：有的给出正确的镜像对（翻转后相关 0.95），有的把**同一侧画了两次**（不翻转相关 0.65）。
  把同一侧当成 "left" 和 "right" 交给我架，等于就物体的一半告诉它两件互相矛盾的事——"车不是车"就是这么来的。
  所以 `studio/sheet.py` 会读这一对（不翻转 vs 翻转），发现是重复就把右视图镜像过来：对左右对称的物体是精确的，
  对不对称物体是近似，但都强于自相矛盾。

`studio/sheet.py` 另外两件必须做对的事：按**位置**剔除模型自绘的源图（源图是按"居中偏移"重采样到画布的，那一格离中线
**2px**，而最近的真实视角离中线 360px；按"像不像参考图"判断只有 1.26 倍优势，因为它终究是重绘不是复制）；以及四个视角共用
**同一个取景窗**（尺寸取最宽那格），否则窄的正视图会被放大到跟宽的侧视图一样大。每格只从自己那块面板取像素，
邻居视角不会漏一条边进来。

需要控制面机器上装了 **Pillow**（本来就有，缩略图在用），并且图像链路必须是 ComfyUI：本地 torch 链路只能从提示词生图，
没法把一张图编辑成别的视角。图像链路是本地时，任务会给出告警并退回"只用单张参考图重建"。

## 8. 还没做的部分

* **没有门户界面。** 目前多视角任务只能通过 API/CLI 提交（`generate_views: true`），对话框仍只收一张参考图。
  下一步是支持多选图片，以及一个"把其他角度也画出来"的开关。
* **四个视角里"右侧"最弱。** 模型把"right side view"画成右前方四分之三视角的概率比左侧高，这也是最需要靠
  `views_prompt` 调的一个方向。
* **机架图的取景注意事项。** `ImageCropToMask` 会把**每个**视角各自归一化到自己的轮廓包围盒，所以细长物体
  （比如汽车的正视与侧视）在各视角里被放大的倍率并不相同。这是上游模板的既有配方，也正是它让任意照片可用；
  但物理上自洽的机架应该所有视角共用一个尺度。如果重建结果看起来被压扁了，这里是第一个该回头看的地方。
