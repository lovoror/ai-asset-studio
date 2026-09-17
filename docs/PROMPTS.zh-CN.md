# 提示词与参考图

这条流水线生成的每一张图，用的都是流水线自己组装出来的提示词。这件事很长时间里是看不见的：门户只收集一句话的想法，API 负责组装指令，
想知道模型到底读到了什么，唯一的办法是去读代码。本文记录组装出来的内容、每一部分的来源，以及如何修改——包括不改代码的改法。

图片生成之后去了哪里见 [`RESULTS.md`](RESULTS.md)；多视角（2D 多角度）这条链路及其测量数据见
[`MULTIVIEW.md`](MULTIVIEW.zh-CN.md)。

## 1. 组装出来的参考图提示词

`services/studio/studio/promptbuilder.py::build_reference_prompt` 用请求和风格定义拼出一条指令，顺序如下：

| 部分 | 来源 | 示例 |
|---|---|---|
| 主体 | 请求的 `prompt`，去掉结尾的句号 | `a small rusty green hatchback.` |
| 风格子句 | 风格的 `style_clause`，空白已折叠 | `stylized mobile-game asset, hand-painted ...` |
| 材质 | 请求的 `materials`，否则用风格的 `default_materials` | `Materials: weathered sheet metal and rubber.` |
| 配色 | 请求的 `palette`，否则用风格的 `default_palette` | `Colour palette: rust, olive, grey.` |
| 真实尺寸 | 给了 `height_m` / `width_m` / `depth_m` 时 | `Real-world scale: height about 1.5 m; ...` |
| 背景 | 风格的 `background` | `Background: plain flat light grey studio background, uniform, ...` |
| 构图 | `REFERENCE_RULES` | `Composition: exactly one object, the entire object fully visible ...` |
| 渲染方式 | 固定句子 | `Rendered as a clean 3D asset presentation image, ...` |

`REFERENCE_RULES` 不是审美问题，而是硬要求：一个物体、完整且居中留白、略高于物体的四分之三正面视角、轻微透视、柔和均匀的光、
没有景深、整体清晰、主要部件之间轮廓分明、没有场景和道具、物体上任何地方都不能有文字。Pixal3D 是照着看到的东西重建的，
违反这些规则的图会得到一个旁边多出一个物体、底部被裁掉、或者贴图里烤进文字的模型。

反向提示词是 `BASE_NEGATIVE`——上述规则的镜像（文字/字母/logo、多物体或重复物体、被裁切、杂乱背景、手和人、景深与模糊、
镜头光晕、生硬或戏剧化的光、低质量、形变、jpeg 伪影、过饱和）。之后按这个顺序追加：

1. 风格的 `negative_extra`（如果有）；
2. 请求的 `negative_extra`。

## 2. 不改代码地修改

| 想要的效果 | 做法 |
|---|---|
| 改风格的措辞、背景或额外反向词 | 设置表里的 `style_edits`（门户：Settings → styles）。键是风格 id 或 `custom`。写入时整张表被替换，`{}` 表示清空。 |
| 一次性追加反向词 | 每次请求的 `negative_extra`——是追加到风格的列表，而不是替换 |
| 完全按我给的提示词发 | 每次请求的 `final_prompt` / `final_negative_prompt`：它们替换组装出来的文本，而阶段请求里的 `template` 仍然记录「原本会组装成什么」 |
| 复用已有图片的材质/配色/尺寸 | 每次请求的 `materials` / `palette` / `height_m` |
| 改环绕图的指令或工作流 | 设置 `views_prompt` / `views_workflow`（见第 6 节），否则用下面列出的内置默认值 |

`GET /capabilities` 会公布 UI 需要的各个部件，免得把值写死：

- `styles.<id>.style_clause`、`.background`、`.negative_extra`、`.default_materials`、`.default_palette`——
  应用 `style_edits` 之后的当前生效值；
- `prompt_defaults`——`reference_rules`、`base_negative`、`views_prompt`、`views_workflow`、`views_size`、
  `views_steps`、`views_grounding_px`、`views_fov_degrees`，以及 `style_fields`（哪些风格字段可以编辑）；
- `request_schema` / `image_job_schema`——由 pydantic 模型生成的完整字段表。

## 3. 生成之前先看提示词

```http
POST /v1/prompt/preview
{"prompt": "a rusty green hatchback", "style": "mobile_factory", "materials": "weathered steel"}
```

```json
{"prompt": "a rusty green hatchback. <style clause>. Materials: weathered steel. Background: ...",
 "negative_prompt": "text, letters, ...",
 "template": {"subject": "a rusty green hatchback", "style": "...", "style_clause": "...",
              "materials": "weathered steel", "palette": null, "dimensions": null,
              "background": "...", "view": "three-quarter front view from slightly above",
              "lighting": "soft even neutral studio illumination", "constraints": "exactly one object, ..."},
 "style_label": "..."}
```

不会创建任务，也不会碰 GPU：这就是模板渲染出来的结果。`template` 把文本拆回它由哪些部分拼成，UI 因此可以在卡片旁展示提示词、
并让人只改其中一段——画布把改完的文本作为 `final_prompt` 发回来。风格 id 不在预设里时返回 `422` 和 `unknown style ...`。

## 4. 用图片思考：参考图

一次生成可以不只依赖提示词，而是以 0、1 或多张图为条件。`ImageJobRequest.references` 是一个有序列表（最多 6 项），每项是：

- `{"image_b64": "<base64 PNG/JPEG>", "label": "front"}`——用户桌面上拖进来的图，内联传输；
- `{"job_id": "20260917-124330-ade52616", "file": "artifacts/reference_candidates/cand_00.png", "label": "style"}`
  ——磁盘上已有任务里的文件；画布上的一张卡片把内容喂给另一张卡片，就是这样做的，不必让几 MB 数据穿过浏览器。

`file` 是该任务目录内的纯相对路径：不能有 `..`、不能以斜杠开头，控制面在入队前会再校验一次（不存在则 `404`）。
`GET /v1/jobs/<id>` 会把整个列表回显，但把 `image_b64` **换成** `"inline": true|false`——请求在每次轮询时都会返回，
而内联参考图是几 MB 的数据。

顺序是有含义的。双图编辑里第一张是主体、第二张是要借用的风格或细节，所以对调它们会改变结果，而不是报错。

### 参考图怎么进到图里

阶段服务会逐张上传参考图，并写进所选工作流的 `LoadImage` 节点：

- **哪个节点是哪张**，看节点的**标题**：按标题顺序的 `REFERENCE1`、`REFERENCE2`……没有标题时按节点 id 数字顺序。
  请给你的加载节点起标题（见 `presets/workflows/README.md`）。
- **没有参考图的槽位会被断开**——删掉它的输入，让这条分支从输出节点不可达，ComfyUI 就不会去校验工作流自带的占位文件名。
  「1 张参考图进双图工作流」和「3 个视角进 4 视角工作流」能成立，靠的就是这一点。
- **上传时会改名**为 `<槽位>_<内容摘要><扩展名>`，因为上传是按名字覆盖的：两张恰好同名的参考图，否则会让所有槽位都读到同一张图。
- 每张参考图会先复制进**本任务**目录（`ref0.<ext>`、`ref1.<ext>`），因为只有任务目录下的文件才会被发往另一台机器上的 worker。

`references` 需要编辑类工作流和 ComfyUI 图像通道。本地 torch 通道只按提示词生成，别的什么都不做，所以在这种通道上带参考图的请求会在提交时被拒绝，
报 `generating from reference images needs the ComfyUI image backend`——而不是生成一张不相干的图。`workflow` 指定图文件
（`presets/workflows` 里任意 `*.json`）；内置的多参考图工作流是 `krea2_edit_refs.json`，而文生图工作流没有地方放参考图。

## 5. 单次请求的生成参数

模型预设会固定尺寸和步数，因为这些数值是按显卡选好的（krea2-turbo 用 1024² 正是这个原因）。请求可以覆盖它们，覆盖发生在预设**之后**。

| 字段 | 含义 |
|---|---|
| `width` / `height` | 输出尺寸，16 的倍数，64..4096 |
| `steps` | 1..100 |
| `cfg` | 该系列的引导值，0..20——qwen 用 `true_cfg_scale`，klein 和 zimage 用 `guidance_scale`，ComfyUI 模型用 `cfg` |

`cfg` 之所以按系列分名，是因为本地 worker 只读自己系列的那个键；API 负责映射，调用方不必知道最终落到哪个模型上。
ComfyUI 通道还接受 `workflow`，采样器和调度器来自模型预设。

## 6. 环绕图的指令

对于需要自己生成多视角输入的任务，流水线用**第二遍**编辑来画一张四格图，使用 `VIEWS_PROMPT` 和
`presets/workflows/krea2_turnaround.json`：

> Convert the object in the image to a Character Sheet of exactly four views arranged in one horizontal row of
> four equal square panels. Panel one: the front view. Panel two: the full side profile, with the object facing
> the left edge of the panel. Panel three: the rear view. Panel four: the full side profile, with the object
> facing the right edge of the panel. Every panel shows the whole object uncropped and centred, at the same scale
> and the same camera distance, on a plain flat light grey background. No extra views, no close-ups, no text.

两个侧面是按**物体在画面里朝向哪边**来命名的，这与 `Pixal3DMultiViewConditioning` 的机位所采用的约定一致。
关于这条指令有两个测量结论，细节见 [`MULTIVIEW.md`](MULTIVIEW.zh-CN.md) 第 7 节：

- 模型在两个侧面格子里画出**同一个**侧面很常见，且与参考图有关。有一张参考图在 4 个种子里有 2 个画对了成对的两个侧面；
  另一张参考图则在每个种子、每种措辞下都画出同一个侧面（7 次绘制）——包括明确点名两侧的措辞，以及明令禁止重复的措辞。
  没有任何措辞被证明能解决它，所以这条指令保持不变，改由流水线重画一次这样的图，最后才用镜像兜底。
- 这条指令是**重新写入**而不是依赖工作流自带的：工作流文件里带着一份副本，控制面每次运行都会用 `views_prompt` 覆盖它。

相关设置：`views_prompt`、`views_workflow`、`auto_multiview`（新任务是否默认生成视角），以及 `views_grounding_px`——
交给 Qwen3-VL 视觉编码器的最长边，节点包文档给的范围是 384–768。把它留成 0（"native"）会让整张参考图走 CPU 视觉编码器：
一张 2000×2000 的图这样就花了两小时，设成 768 只要九分钟。
