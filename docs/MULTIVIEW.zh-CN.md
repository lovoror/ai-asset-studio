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
  "lod_fractions": [0.7],
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

## 7. 还没做的部分

* **没有门户界面。** 目前多视角任务只能通过 API/CLI 提交，对话框仍只收一张参考图。下一步是多选图片 + 选模式
  （「每张各做一个」还是「合并成一个多视角资产」）。
* **没有自动补视角。** 缺的视角目前要调用方自己提供（或见第 6 节）。
* **机架图的取景注意事项。** `ImageCropToMask` 会把**每个**视角各自归一化到自己的轮廓包围盒，所以细长物体
  （比如汽车的正视与侧视）在各视角里被放大的倍率并不相同。这是上游模板的既有配方，也正是它让任意照片可用；
  但物理上自洽的机架应该所有视角共用一个尺度。如果重建结果看起来被压扁了，这里是第一个该回头看的地方。
