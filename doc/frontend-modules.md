# 前端模块文档（web/）

> 配套 README：项目概览见根 [README](../README.md)；后端文档见 [backend-modules.md](backend-modules.md)。

## 1. 总览

前端是嵌在 ComfyUI 节点里的 Vue 3 时间线面板（产品名"创意工作台"）。技术栈：**Vue 3（`<script setup>`）+ TypeScript（严格模式）+ Vite（lib 模式）+ Naive UI + Pinia**。

两个构建入口（同一套组件与 store）：

- **main.ts —— ComfyUI 集成入口**：lib 模式产物 `web/dist/minimax-h3-studio.js`（Vue/Pinia/Naive UI 全打进产物；ComfyUI Frontend 1.33.9+ 不再对外暴露 Vue，须独立打包运行）。Python 端声明自定义 widget 类型 `MINIMAX_H3_STUDIO_UI`，本文件 `getCustomWidgets()` 注册同名类型，节点创建时挂载 Vue 面板。
- **preview.ts —— 独立浏览器预览**：`npm run dev` + 访问 `http://localhost:5178`。**不依赖 ComfyUI**，通过内置 dev mock 后端（`web/src/dev/`）全量跑前端交互（任务库 / 时间线 / 历史 / 素材 / 自动假采样），用于快速调视觉与验证前端功能（详见 §9）。

## 2. 目录结构

```
web/src/
├── main.ts               # ComfyUI 集成入口（widget 注册 + 数据桥 + 自动保存订阅）
├── preview.ts            # 独立预览入口（装 dev mock → 播种演示任务 → 自动跑假采样）
├── dev/                  # 仅 dev 预览用的 mock 后端（不进 lib 构建）：见 §9
├── App.vue               # 根布局：工具栏 + 时间线 + 详情面板（含 naive-ui provider）
├── stores/timeline.ts    # 唯一状态源（Pinia）
├── types/timeline.ts     # 数据契约类型（Clip/ClipPayload/PromptSnapshot/…）
├── components/           # UI 组件（见 §5）
├── composables/          # useDrag / usePreviewPlayer / usePreviewThumb
├── utils/promptDiff.ts   # 提示词版本 diff（历史比对）
├── env.d.ts              # 类型声明（构建时环境）
├── auto-imports.d.ts     # unplugin-vue-components 自动生成
└── styles/               # theme.ts（调色板）+ global.css
```

## 3. 数据流架构

**前端权威 + DB 纯持久化 + 执行只认契约**：

```
编辑（store）── $onAction 订阅（main.ts，防抖 100ms）──→ saveToDb（写 tasks.timeline）
Queue ── serializeValue 实时构建 {taskId, payload} ──→ timeline_data widget ──→ 后端 executor
加载工作流 ── nodeCreated 读 widget → loadFromPayload（无 taskId 时为纯时间线模式）
```

- **唯一状态源**：Pinia `timeline` store 持有时间线全部状态；组件不各自维护数据。
- **自动保存**：main.ts 里 `$onAction` 订阅 store 的外部编辑 action（跳过 INTERNAL_ACTIONS 集合，防止回调内再触发递归），100ms 防抖后 `saveToDb()`；**无 taskId 时不保存**（`saveToDb` 内部 `if (!taskId) return false`），需用户显式新建/加载任务后才开始落库——工具栏「＋ 片段」在未选任务时会先弹新建任务对话框，建成功后再添加。
- **widget 同步**：`$subscribe` 监听 state 变化把 `taskId` 同步到 widget；工作流 json 只存 `{taskId, payload?}`。
- **版本自检**：构建时注入 `__STUDIO_VERSION__`（web/package.json version），运行时与后端 `/minimax/studio/version` 比对，不一致时在控制台 `console.warn` 提示强刷浏览器（旧 JS 会把空时间线写进工作流 json 造成数据丢失）；仅日志提示，不做强制刷新。
- **切换任务前先保存**（Toolbar）：`saveToDb() → loadTask(nextId)`。

## 4. store 与契约（stores/timeline.ts + types/timeline.ts）

### 状态

| 字段 | 说明 |
|---|---|
| clips | 时间线片段数组（UI 数据 = Clip） |
| canvas | 全局画布 { fps, width, height } |
| selectedId / zoom | 选中片段 / 时间线缩放（px 每秒） |
| taskId / taskName / nodeId | 任务库模式（SQLite）上下文 |
| historyByClipId | 按 clip_id 索引的历史（versions + samples，由后端 history 接口拉取） |
| samplingProgress | 当前采样片段的进度 + live 预览（executor 广播） |
| showRestoreModal / showHistoryPanel | 跨组件 UI 弹窗开关 |

### 核心 actions

- 时间线编辑：`addClip / duplicateClip / removeClip / moveClip / updateClip / select / updateCanvas`（updateCanvas 变更画布会清全部 latent 勾选并持久化）
- 序列化/恢复：`serialize()`（契约出口）/ `loadFromPayload`（工作流恢复）
- 任务库：`newTask / loadTask / unloadTask / createTask / renameTask / duplicateTask / deleteTask / saveToDb / fetchTaskList / exportTask / importTaskFile`
- 历史与反悔：`fetchHistory / addClipsFromHistory / promptSnapshotOf / loadPromptEntry（卡片级回填）/ applySample（抽卡级锁定）/ releaseSample / deleteClip / deleteSample / deleteVersion`
- 采样进度：`setSamplingProgress / setLivePreview`（合并语义：进度与 preview 独立更新，防止预览闪断）

### 契约类型（types/timeline.ts）

- `Clip`（UI 模型）：id/mode/prompt/durationSec/enabled/continuity + 素材槽（firstFrame/lastFrame/refImages/refVideos/refAudios/sourceVideo，均为 `ReferenceMedia`）+ sampleFp（latent 锁定）
- `ReferenceMedia`：name/path/preview（preview 仅前端展示，不进数据契约）
- `ClipPayload`：发给后端的序列化形态（素材只保留 path + kind）
- `PromptSnapshot`：历史条目画面语义快照（mode/prompt/素材）
- `VersionSample`：采样记录（含 sampleFp/canvas/durationSec/exists/previewUrl 等运行时信息）
- 序列化规约：`toClipPayload`（UI → 契约）/ `fromClipPayload`（契约 → UI，图片按 path 重建 /view?preview=webp 预览 URL）

## 5. 组件职责

| 组件 | 职责 |
|---|---|
| App.vue | 布局（Toolbar + Timeline + ClipDetailPanel）、naive-ui provider |
| Toolbar.vue | 任务库下拉（新建/重命名/**复制**/导出/导入/删除，名称弹窗）、恢复片段入口、画布参数（ResolutionParam）、＋片段、总时长 |
| Timeline.vue | 时间线容器：缩放/平移、fit 显示全部、选中联动 |
| TimelineTrack.vue | 片段轨道行渲染 |
| TimelineRuler.vue | 时间刻度尺 |
| ClipCard.vue | 单个片段卡片：模式色、素材缩略图、采样进度/结果态、缓存命中绿标、hover 大预览弹框、删除/复制、打开历史/恢复 |
| ClipDetailPanel.vue | 选中片段编辑面板：模式/时长/续接/历史入口 + **PromptEditor + 参考素材区** |
| PromptEditor.vue | contenteditable 富文本：`@` 素材引用 + `/` 符号补全 + 原子 chip（见 §6） |
| RefGrid.vue | 参考素材网格（ClipDetailPanel 参考素材区）：紧凑列表 + SortableJS 拖拽排序，编号 = 下标 + 1、无空位，含「＋添加」入口卡 |
| UploadSlot.vue | 素材槽：本地上传（POST /upload）与"选已有"（list_input_media）、预览缩略图、替换/移除 |
| PromptHistoryPanel.vue | 历史弹窗主体：按画布分组的版本列表 + 采样记录（抽卡锁定/删除、预览播放）、版本比对（PromptDiffModal） |
| HistoryRestoreModal.vue | 从历史手动挑选片段恢复到时间线（跨任务库流程） |
| PromptDiffModal.vue | 提示词版本 diff 弹窗（基于 utils/promptDiff.ts） |
| PreviewThumb.vue | 预览缩略图：取 WebP **首帧**静态显示（usePreviewThumb）；动画播放由采样 live 播放器（usePreviewPlayer）另担 |
| ResolutionParam.vue | 画布分辨率/帧率选择：宽高比（官方 ResolutionSelector 同款 8 种）**× 目标百万像素（0.1~2 MP）** 驱动宽高，32 倍数向上取整；按钮显示 WxH@fps |
| promptSnippets.ts | `/` 符号表（纯 TS：镜头/说话者/对话/字段/任务类型/关系标记/相机运动/引用标签 8 组 + 智能编号） |

## 6. 提示词编辑器（PromptEditor.vue）

- contenteditable 富文本；结构化 token（`<Picture N>` / `<Video N>` / `<Audio N>`）渲染为 `contenteditable=false` 的**原子 chip**：光标自动跳过、整体删除、不可部分选中。
- **数据层始终是纯文本**（`extractText` 递归提取）→ `update:modelValue` 同步父组件 → 契约不变；缩略图是纯 UI 视觉层。
- **缩略图实时联动**：chip 存 dataset（kind/n），素材区变化（pickerItems 重算）后 `refreshChipThumbs()` 按编号重新解析当前素材更新缩略图/类型图标——不固化插入时的旧快照，删除/替换/换位后与右侧素材区槽位同步。
- `@` 检测用 input 事件对比前后文本（兼容输入法）；`/` 触发有前导字符限制（非字母数字，避免日期/URL 误弹）；选择器跟随光标、双栏（素材 + 动态标签）键盘导航（↑↓/Enter/←→/Esc）。
- 剪贴板全套自定义：copy/cut 提取纯文本；paste 用 Range API（弃 execCommand）；三者均 stopPropagation 与 ComfyUI 全局监听隔离。
- 图标 emoji 用 `data-emoji` + CSS `::before` 渲染（不落文本节点），防止 extractText 把装饰字符混入提示词数据层。

## 7. composables

- **useDrag.ts**：时间线片段拖拽排序/移动（pointer 事件，含 stopPropagation 约定）。
- **usePreviewPlayer.ts**：动画 WebP 播放器——WebCodecs `ImageDecoder` 解码为 VideoFrame[] 后由 JS 定时器按帧率逐帧绘制到 canvas；帧率语义对齐 KJNodes Model Preview Override（预览时长 = 视频实际时长：fps = 帧数 ÷ 时长）。用于采样 live 预览与大预览弹框。
- **usePreviewThumb.ts**：把给定 WebP 用 `ImageDecoder` 解出**第一帧**画到 canvas（contain 完整显示）；仅静态首帧，不含动画播放/懒加载/裁切/hover 播放。

## 8. 与后端/ComfyUI 的桥

- HTTP：经 `window.app.api.fetchApi`（自动加 `/api` 前缀）调后端 `/minimax/studio/*` 路由（素材列表、任务 CRUD、复制、历史、导入导出）。
- WebSocket 事件（后端 `server.send_sync` → 前端 app 监听）：

| 事件 | 前端处理 |
|---|---|
| studio_progress | 卡片进度条（phase=sampling/decoding + 步数/总步数） |
| studio_preview | live 预览动画 WebP（base64，采样中作卡片 live 帧） |
| studio_clip_done | 单段采样完成 → 刷新该片段历史 |
| executed | 整节点执行完成 → 清采样进度 + 刷新历史 |

- widget 生命周期：`nodeCreated` 挂载面板与订阅（只绑一次）；`onRemove` 清理；工作流加载 `loadedGraphNode` 时序防御（_stRestoring 期间禁止覆盖 widget.value）。

## 9. 构建与开发

```bash
npm install
npm run build            # lib 模式 → dist/minimax-h3-studio.js + .css（Vue 全打包）
npx vue-tsc --noEmit     # TS 类型检查
npm run dev              # 独立预览（http://localhost:5178）
```

### dev mock 后端（`web/src/dev/`，仅预览用，不进 lib 构建）

`npm run dev` 走的是浏览器独立预览（`index.html` → `preview.ts`），**不依赖 ComfyUI**。为了让前端功能（任务库 / 历史 / 素材 / 采样）能在 dev 全量跑通，`web/src/dev/` 提供了一个**自包含的 mock 后端**，只被 `preview.ts` 与 vite dev 分支引用：

- **`mockPlugin.ts`**：vite dev 中间件插件（见 `vite.config.ts` dev 分支），本地实现 `/view`、`/minimax/studio/*`（任务 CRUD/历史/复制/导入导出）、`/upload` 的内存后端，返回与真实后端一致的字段，图片用内存色块/上传字节可显示；状态存 dev server 进程内存（刷新不丢、重启 dev server 重置）。
- **`mockClient.ts`**：前端侧接线——`installMockApi()` 把 `window.app.api` 装上（fetchApi 直连 dev mock），`ensureDemoTask()` 播种一个演示任务，`runMockSampling()` 对启用片段自动跑一段假采样（进度 sampling→decoding、live 帧、写历史）。

`preview.ts` 的 bootstrap：装 mock api → 播种演示任务 → 挂载 App → `store.loadTask(演示任务)`（走与 ComfyUI 相同的真实 loadTask 链路）→ 自动跑一遍假采样，让历史区/卡片徽标出现可验证数据。

> 说明：`web/src/dev/` **不参与 lib 构建**（`vite.config.ts` 的 lib 分支不含该插件、preview.ts 不在 lib entry）——生产产物不含 dev mock。main.ts 是 ComfyUI 专用入口，dev 预览不加载它。

产物约定：`web/dist/` 由 .gitignore 忽略、随插件发布；前端改动只需浏览器硬刷新（Ctrl+Shift+R），无需重启 ComfyUI。样式注意：DOM API 创建的元素不带 scoped data 属性，需 `:deep()` 穿透。
