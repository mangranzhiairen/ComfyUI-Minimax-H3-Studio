# 后端模块文档（studio/）

> 配套 README：项目概览见根 [README](../README.md)；前端文档见 [frontend-modules.md](frontend-modules.md)。

## 1. 总览

后端运行在 ComfyUI 进程内（依赖其 `PromptServer`、`folder_paths`、`comfy_extras` 节点体系），不独立成服务。

```
__init__.py                 # 节点/路由注册入口，WEB_DIRECTORY = ./web/dist
nodes/studio_console.py   # 创意工作台节点（INPUT_TYPES + execute），执行入口
studio/
├── http_routes.py          # /minimax/studio/* HTTP 路由（素材列表 + 任务库 CRUD）
├── payload.py              # 数据契约（StudioPayload/ClipPayload）反序列化 + 校验
├── executor.py             # StudioExecutor：编排「采样-解码分离」流水线
├── tasks/                  # 任务子类：__init__.py（TASK_REGISTRY + create_task 工厂）
│                           #          base.py 模板方法 + t2v/i2v/fl2v/r2v/v2v/rv2v 六子类
├── sampling.py             # 官方 MiniMax H3 真实采样链路（conditioning → KSampler → 解码）
├── motion_context.py       # 段间引导：上一段 latent 尾部钉入 + 相位网格
├── segment_cache.py        # SQLite 任务库 + latent/preview 文件 + 两级指纹
├── media_loader.py         # 素材（图/视频/音频）加载为 tensor / AUDIO dict
├── tae_preview.py          # TAE（tiny VAE）采样预览：live + final 动画 WebP
└── tasks.db 位置           # ComfyUI/user/ComfyUI-MiniMaxH3-Studio/tasks.db
```

关键原则：

- **前端权威执行 + DB 纯持久化**：执行器读到的 payload 由前端 Queue 时实时构建并随节点 widget（`timeline_data` JSON）传入——**时间线内容/执行参数不从 DB 读回**；DB（SQLite）只做任务/历史的持久化记录。注意：执行中锁定的 latent 校验（`get_sample_canvas`）与缓存命中（`get_sample_meta`）会读 DB 记录，但不会用 DB 里的时间线内容覆盖执行输入。
- **数据契约单一入口**：`payload.load()` 严格校验结构（版本、模式、素材数量上限、指纹格式），不符即抛 `PayloadValidationError`，不做历史形态兼容。
- **采样-解码分离**：逐段采样只产出 AV latent 并写盘（指纹命名、内存零驻留），任务末尾统一从磁盘加载解码、流式合并成片。
- **模型由 ComfyUI 管理**：本插件不加载/不卸载模型，只经 `TaskContext` 持有引用；`unload_models_after` 仅是采样完成后调用 `model_management.free_memory` 提前腾显存。

## 2. 数据契约（payload.py）

前端 `timeline_data` widget 内容（JSON 字符串）：

```
{ "taskId": string|null, "payload": StudioPayload }
```

`StudioPayload`（v1）：

| 字段 | 类型 | 说明 |
|---|---|---|
| version | int | 契约版本（=1，不符拒绝） |
| canvas | { fps, width, height } | 全局画布 |
| clips | ClipPayload[] | 片段序列 |
| totalDurationSec | float | 各段时长之和 |

`ClipPayload`（segment）：`id / mode(t2v|i2v|fl2v|r2v|v2v|rv2v) / prompt / durationSec / enabled / continuity / sampleFp?` 以及素材字段 `firstFrame / lastFrame / refImages[] / refVideos[] / refAudios[] / sourceVideo`（`{path, kind}` 结构）。

- `sampleFp`：抽卡级反悔——锁定某次历史采样，Queue 时该段跳过采样直接用该 latent 出片（16 位 hex 校验）。
- `continuity`：段间续接开关（本段把上一段 latent 尾部钉入）。
- 校验清单：clips **至少 1 段**（空时间线拒绝）、模式白名单（t2v/i2v/fl2v/r2v/v2v/rv2v）、时长范围 **1.0–30.0s**（`MIN/MAX_DURATION_SEC`）、素材数量上限（图 9、视频 3、音频 3）、`sampleFp` 16 位 hex 格式等。

辅助：

- `align_frame_count(n)`：帧数吸附到模型网格 **17k+5**（5, 22, 39, 56…）。
- `clip_to_snapshot(clip)`：片段 → 提示词条目快照（**纯画面语义**：mode/prompt/素材；不含 enabled/continuity/durationSec/sampleFp 等执行态与规格——这些不属于内容身份）。

## 3. 任务层（tasks/）

### 3.1 base.py —— 模板方法

- `SamplingConfig`：全局采样工艺（seed/steps/cfg/sampler/scheduler/shift_video/shift_audio），由节点 widget 注入，**不进 timeline_data**。
- `TaskContext`：执行期运行时上下文——canvas、sampling、模型依赖（model/video_vae/audio_vae/clip，由节点接入后注入）、continuity_frames、进度回调。
- `ConditioningResult`：模式无关的条件描述（节点名、prompt、尺寸、长度、首尾帧、参考素材 dict、ref_image_size）；`with_length()` 派生采样长度（段间引导 = 可见帧 + 上下文帧）。
- `SegmentResult`：单段结果（segment_index、frames、conditioning、av_latent…）。
- `BaseTask.execute()` 模板：`validate() → build_conditioning() → [段间引导：长度加预算] → run_minimax_conditioning → [钉入上一段 latent] → sample（SamplerCustomAdvanced 组合）→ 返回 AV latent（不解码）`。
- `use_continuity = prev_av is not None`：是否真续接取决于执行器是否传入上一段 latent。

### 3.2 模式子类

| 子类 | 模式 | 条件构建（build_conditioning） |
|---|---|---|
| t2v | 文生视频 | 仅 prompt → ImageToVideo 风格（无首尾帧） |
| i2v | 图生视频 | 首帧图（firstFrame） |
| fl2v | 首尾帧 | 首帧 + 尾帧 |
| r2v | 参考主体 | 参考图/参考视频/参考音频（ReferenceToVideo），支持 `<Picture/Video/Audio N>` 标签 |
| v2v | 视频转视频 | 源视频（sourceVideo） |
| rv2v | 参考改视频 | 源视频 + 参考图/参考音频 |

参考图送入官方节点时 `ref_image_size` 固定为 `"match"`（缩到生成画布面积、不裁切）；如需 `"max"`（2048 短边保真）需扩展透传。

## 4. 执行器（executor.py）

`StudioExecutor.run(timeline_data)`：

1. **解析**：JSON envelope → `load_payload` 严格校验。
2. **前置校验** `_validate_locked_latent`：已启用且锁定了 `sampleFp` 的片段，其缓存画布必须等于当前画布（防解码/引导时形状崩溃；内容一致性由前端"启用采样"保证）。
3. **建任务**：无 taskId 时兜底建占位任务；`update_task_node_id` 同步当前节点 id（节点 id 可能变化，保证 latent 路径推导一致）；状态置 running。
4. **采样阶段** `_sample_all`：
   - `enabled=false` 的段**整体跳过**（不采样、不查缓存、不更新 prev 链）。
   - 逐段计算两级指纹 → **缓存决策**（优先级）：锁定 sampleFp 且文件存在 → 同参数 sample_fp 命中 → 真实采样。
   - 缓存命中：读 DB 记录的 (sample_len, frames) 直接入解码队列，并把该文件设为下段引导源。
   - 真实采样：`load_av_latent(prev_file)`（段间引导，内存 0 驻留）→ `task.execute(prev_av)` → latent 写盘 → final preview 生成 → `record_version_sample` 固化历史 → `studio_clip_done` 广播 → 更新 prev 链。
5. **解码阶段** `_decode_and_merge`：按序从磁盘加载 latent → `decode_av_latent` → 裁掉引导前缀（trim）→ 写入**预分配的合并 tensor**（峰值 = 单段解码 + 合并帧，非 N 倍全量）→ 音频流累积合并。
6. 状态 done/failed；返回 `ExecutionResult`（report + images + audio）。

## 5. 官方采样链路（sampling.py）

对齐 ComfyUI 官方 `video_minimax_h3_r2v.json` 工作流：

```
MiniMaxH3ImageToVideo / ReferenceToVideo   # conditioning + AV latent（V3 节点，输出 NodeOutput）
→ MiniMaxH3SigmaShift（video/audio shift，ModelSamplingAV）
→ BasicScheduler → BasicGuider/CFGGuider → KSamplerSelect → RandomNoise
→ SamplerCustomAdvanced                     # 官方组合，非 comfy.sample.sample
→ VAEDecode（视频）+ VAEDecodeAudio（音频）直接解码同一 AV latent（不分离）
```

- `run_minimax_conditioning(ctx, cr)`：素材经 `media_loader` 加载后按关键词参数传入官方节点（`ref_images` 等 dict 结构即官方 `ref_image_N` autogrow 语义）；宽高向下对齐 32。
- 进度：包装 `guider.sample` 注入每步回调 → `TaskContext.progress` → executor 广播前端。
- V3 节点输出统一经 `unpack_node_output` 取 args（兼容 tuple/list 旧式）。

## 6. 段间引导（motion_context.py）

Motion Context **latent 直接传递**方案（不 VAE 解码）：

- 模型像素网格：`FRAME_PER_TOKEN = (1,4,4,4,4)`（5 步周期）；帧数 = `17k+5`。像素帧 ↔ latent step 换算见 `pixel_frames_for_latent_t / steps_for_frames / step_offsets`。
- 上下文长度可选窗口：5/22/39/56 帧（`CONTEXT_FRAME_CHOICES`），默认 22；`generation_frame_budget(visible, context)` 计算采样总长（= align(visible+ctx)）与要裁的 ctx。
- `apply_motion_context(positive, latent, prev_av, ctx)`：从上一段 latent **尾部按整步切块**（相位断言：tail 起点必须在 5 周期位置 0，否则拒绝错位 join）→ 构造 `minimax_keyframes`（resolved_frame_index 按步真实偏移）→ 与 conditioning 已有非 0 锚（如 fl2v 尾帧）合并 → 音频同样从上一段 latent 尾部切出作为 `minimax_refs` 音频 → 返回新 positive + trim 帧数。
- `trim_context_prefix(images, audio, trim)`：解码后同步裁掉画面前缀与音频前导（按 fps/sr 换算采样点）。

## 7. 数据层（segment_cache.py）

分层：**SQLite（user 目录）存任务元数据 + 张量文件（output 子目录）存 AV latent**。

### 7.1 SQLite 表

```
tasks            id / node_id / name / timeline(时间线当前完整数据 JSON，覆盖式保存)
                 / sampling_json / status / created_at / updated_at
clip_versions    历史版本（纯 Model）：id / task_id / clip_id / content_fp / canvas / snapshot / created_at
version_samples  采样记录：id / task_id / clip_id / version_id / content_fp / canvas / sample_fp
                 / seed / duration_sec / continuity / frames / sample_len / created_at
                 UNIQUE(task_id, clip_id, sample_fp)
```

- 历史只在采样成功时固化（`record_version_sample`）：同 clip 同 content_fp 复用条目，否则新建；样本 INSERT OR REPLACE。
- `init_db()`：无文件建表；有文件校验关键结构，不符直接删库重建（开发期策略，不做迁移）。

### 7.2 latent 文件与两级指纹

```
output/minimax_h3_studio/{node_id}/latent_{sample_fp}.pt
output/minimax_h3_studio/{node_id}/preview_{sample_fp}.webp
```

- `content_fingerprint(seg)` → content_fp：**纯画面语义**（mode/prompt/素材 path）。执行态/画布/时长不参与——版本身份。
- `sample_fingerprint(clip_id, content_fp, sampling, …)` → sample_fp：clip 归属 + 内容 + **画布 + 时长 + 工艺（seed/cfg/steps/sampler/shift）+ continuity**。文件防覆盖分量；clip_id 使缓存卡片私有。
- 指纹 hash 进文件名 → 文件存在即命中（加载无需比对）；`latent_exists` 标准路径 + 根目录扫描（节点 id 变化兜底）。

### 7.3 任务操作

- 删除：`delete_task / delete_clip_history / delete_version_sample / delete_clip_version`，样本文件删除带**跨任务引用保护**（`_sample_fp_referenced_elsewhere` 仍被引用则只删记录留文件）。
- 导出/导入：`export_task`（时间线剥 sampleFp 锁定 + 版本与采样元数据，不含素材/latent 文件，`type` 标记校验）→ `import_task` 建新任务重建历史（version id 重映射）。
- **复制** `duplicate_task`：DB 深拷贝——新 tasks 行（timeline 剥 sampleFp）+ clip_versions 逐行 INSERT 为新行；**不复制 version_samples 与 latent/preview 文件**；clip_id 沿用。

## 8. HTTP 路由（http_routes.py）

前缀 `/minimax/studio`，全部显式注册 `/api` 前缀双路径（ComfyUI 的 add_routes 自动加前缀早于节点加载，需自行双注册）。

| 方法与路径 | 用途 |
|---|---|
| GET /list_input_media | 素材列表（kind=image/video/audio/reference_audio） |
| GET /version | 插件版本（前端缓存自检） |
| POST /tasks · GET /tasks | 建任务（空时间线）/ 任务列表 |
| GET /tasks/{id} · PUT /tasks/{id}/timeline | 读任务 / 保存时间线草稿 |
| PUT /tasks/{id}/name | 重命名 |
| POST /tasks/{id}/duplicate | **复制任务**（DB 深拷贝，不带 latent 缓存） |
| DELETE /tasks/{id} | 删除任务（连带 latent/preview 文件） |
| GET /tasks/{id}/history | 片段历史（版本 + 采样，含文件存在性/previewUrl） |
| DELETE /tasks/{id}/clips/{clip_id} | 删除片段全部历史 |
| DELETE /tasks/{id}/clips/{clip_id}/samples/{sample_fp} | 删单个采样样本 |
| DELETE /tasks/{id}/clips/{clip_id}/versions/{version_id} | 删历史版本及其采样 |
| GET /tasks/{id}/export · POST /tasks/import | 任务导入导出 |

## 9. 采样预览（tae_preview.py）

- 产品规格：动画 WebP，8fps、最大边 832px、预览时长 = 视频实际时长（抽帧降帧率，不做快进）；final 帧数 = 段时长 × 8fps；live 是过程示意（24 帧上限循环）。
- 技术：ComfyUI 官方 TAESD 与 H3 不兼容（64 宽 3 上采样 vs taeh3 96 宽 4 + patch_size=2），需按 checkpoint keys 动态重建 flat 解码器；temporal 格式用前缀解码 + linspace 抽帧。解码器从 `models/vae_approx/taeh3.safetensors` 懒加载（单例），缺失静默降级。
- 线程模型：解码在采样回调内同步执行（x0 为 GPU tensor，生命周期仅在回调），PIL → WebP 编码放异步线程（有界队列满则丢），不阻塞采样。

## 10. 节点与事件

### 节点（nodes/studio_console.py）

`MiniMaxH3StudioConsole`（CATEGORY=MiniMaxH3，注册键见 `__init__.py` 的 `NODE_CLASS_MAPPINGS`，当前仅此一个注册项）：

- widgets：`timeline_data`（隐藏 STRING，前端自动填 `{taskId, payload}`）、seed/steps/cfg/sampler/scheduler/shift_video/shift_audio/unload_models_after、`studio_console_ui`（内嵌面板）。
- optional inputs：model / video_vae / audio_vae（r2v/v2v/rv2v 需要）/ clip（type=minimax）。缺 model/video_vae/clip 返回占位输出并回显错误。
- 输出：report(STRING) / images(IMAGE) / audio(AUDIO) / fps(FLOAT) / frame_count(INT)。

### WebSocket 事件（executor → 前端）

| 事件 | 时机/内容 |
|---|---|
| studio_progress | 段级进度（phase=sampling/decoding + value + 步数） |
| studio_preview | live 预览动画 WebP（base64，节流约每段 2-3 次） |
| studio_clip_done | 单段采样完成（versionId + final previewUrl → 前端刷新历史/卡片） |

## 11. 开发与测试

```bash
# 后端纯函数单测（无需 ComfyUI 环境；torch 依赖）
python tests/test_studio.py

# 语法检查
python -m py_compile studio/segment_cache.py studio/http_routes.py
```

注意：Windows 下 `python` 可能是 Microsoft Store 占位符，请使用 ComfyUI venv：`& "D:\Python\ComfyUI\.venv\Scripts\python.exe"`。Python 端改动需重启 ComfyUI 生效。
