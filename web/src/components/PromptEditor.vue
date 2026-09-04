<script setup lang="ts">
// 提示词编辑器：contenteditable 富文本 + 原子 token chip + 两种快捷输入。
//  - @ 素材引用：候选来自父组件 items，token（<Picture N> 等）渲染为 contenteditable=false 的
//    原子 chip（光标自动跳过、整体删除、不可部分选中），缩略图仅是 UI 视觉层
//  - / 结构化符号补全：MiniMax H3 提示词指南符号表（promptSnippets.ts），插入纯文本，
//    支持 [Shot N] / (S1) / <Subject N> 智能编号递增与 {cursor} 光标停留
// 数据层始终是纯文本（extractText 提取），经 update:modelValue 同步给父组件，后端契约不变。
import { computed, nextTick, onMounted, ref, watch } from "vue";
import type { MediaKind, PromptPickerItem, ReferenceMedia } from "@/types/timeline";
import {
  SNIPPET_GROUPS,
  assembleSnippet,
  dynamicTagIcon,
  filterSnippets,
  resolveSmartNumber,
  scanDynamicTags,
  snippetPreview,
  type SnippetDef,
} from "./promptSnippets";

const props = defineProps<{
  /** 提示词纯文本（v-model） */
  modelValue: string;
  /** @ 素材选择器候选（父组件按片段模式与素材计算） */
  items: PromptPickerItem[];
  /** token → 素材（渲染 chip 缩略图；未引用到素材则无图） */
  resolveMedia: (kind: "Picture" | "Video" | "Audio", n: number) => ReferenceMedia | undefined;
}>();

const emit = defineEmits<{ (e: "update:modelValue", v: string): void }>();

// ---------- 媒体工具（ComfyUI 素材查看 / 缩略图） ----------

/** ComfyUI 素材查看 URL（input 目录内文件）；preview=true 时生成 webp 缩略图（≤512px） */
function mediaViewUrl(name: string, subfolder = "", preview = false): string {
  const params = new URLSearchParams({ filename: name, type: "input" });
  if (preview) params.set("preview", "webp");
  if (subfolder) params.set("subfolder", subfolder);
  return `/view?${params.toString()}`;
}

/** 素材缩略图：优先已缓存的 preview（上传/反序列化生成，已是 webp 小图）；无 preview 时兜底按 path 重建 */
function thumbOf(media?: ReferenceMedia): string | undefined {
  if (!media) return undefined;
  if (media.preview) return media.preview;
  if (media.kind === "image" && media.path) {
    const parts = media.path.split("/");
    const filename = parts.pop() ?? media.path;
    return mediaViewUrl(filename, parts.join("/"), true);
  }
  return undefined;
}

const KIND_ICON: Record<MediaKind, string> = { image: "🖼️", video: "🎞️", audio: "🎵" };

/** 构造类型图标（无缩略图时的兜底视觉）：emoji 经 CSS ::before（data-emoji）渲染，不落文本节点。
 *  编辑器纯文本提取（extractText）会递归收集 chip 内文本，图标若用 textContent
 *  会把 emoji 泄漏进提示词数据层（<Picture N> 之外混入图标字符）。 */
function buildKindIcon(kind: "Picture" | "Video" | "Audio", mediaKind?: MediaKind): HTMLElement {
  const icon = document.createElement("span");
  icon.className = "ref-icon";
  icon.dataset.emoji =
    KIND_ICON[mediaKind ?? (kind === "Picture" ? "image" : kind === "Video" ? "video" : "audio")];
  return icon;
}

// ---------- contenteditable 原子编辑器 ----------
// token（<Picture N> 等）渲染为 contenteditable=false 的原子占位符 chip；实际数据
// 仍是纯文本字符串（extractText 提取），与后端契约一致。
// chip 缩略图是素材区的实时引用而非快照：创建时按当前 resolveMedia 取图，素材区
// 变化（增删/替换/换位）后 refreshChipThumbs 按编号重新解析刷新，不残留旧图。

/** 构造原子 token chip：缩略图 + 类型底色（contenteditable=false，光标不可进入） */
function buildTokenEl(kind: "Picture" | "Video" | "Audio", n: number): HTMLElement {
  const span = document.createElement("span");
  span.className = `ref-token ref-${kind.toLowerCase()}`;
  span.contentEditable = "false";
  span.title = `<${kind} ${n}>`;
  span.dataset.tokKind = kind;
  span.dataset.tokN = String(n);
  const media = props.resolveMedia(kind, n);
  const thumb = thumbOf(media);
  if (thumb) {
    const img = document.createElement("img");
    img.className = "ref-thumb";
    img.src = thumb;
    img.loading = "lazy";
    img.alt = "";
    span.append(img);
  } else {
    span.append(buildKindIcon(kind, media?.kind));
  }
  const txt = document.createElement("span");
  txt.className = "ref-text";
  txt.textContent = `<${kind} ${n}>`;
  span.append(txt);
  return span;
}

/** 刷新全部 chip 的素材视觉：缩略图按 <Picture N> 编号实时解析素材区当前数据。
 *  素材增删/替换/换位后调用（数据层纯文本不变，DOM 不重建）；仅替换缩略图或类型
 *  图标，不动 chip 文本与光标。 */
function refreshChipThumbs(): void {
  const ed = editorEl.value;
  if (!ed) return;
  for (const chip of ed.querySelectorAll<HTMLElement>(".ref-token")) {
    const kind = chip.dataset.tokKind as "Picture" | "Video" | "Audio" | undefined;
    const n = Number(chip.dataset.tokN);
    if (!kind || !Number.isFinite(n)) continue;
    const media = props.resolveMedia(kind, n);
    const thumb = thumbOf(media);
    const mediaEl = chip.querySelector<HTMLElement>(".ref-thumb, .ref-icon");
    const textEl = chip.querySelector<HTMLElement>(".ref-text");
    if (thumb) {
      if (mediaEl instanceof HTMLImageElement) {
        if (mediaEl.src !== thumb) mediaEl.src = thumb; // 同 URL 不重设（浏览器缓存命中）
        continue;
      }
      mediaEl?.remove();
      const img = document.createElement("img");
      img.className = "ref-thumb";
      img.src = thumb;
      img.loading = "lazy";
      img.alt = "";
      if (textEl) chip.insertBefore(img, textEl);
      else chip.append(img);
    } else {
      if (mediaEl && !(mediaEl instanceof HTMLImageElement)) continue; // 无素材且已是类型图标：保持
      mediaEl?.remove();
      const icon = buildKindIcon(kind, media?.kind);
      if (textEl) chip.insertBefore(icon, textEl);
      else chip.append(icon);
    }
  }
}

const TOKEN_RE = /<(Picture|Video|Audio)\s+(\d+)>/g;
/** token 字符串解析（非 g 正则，避免 lastIndex 干扰） */
const TOKEN_PARSE_RE = /<(Picture|Video|Audio)\s+(\d+)>/;

/** 按纯文本重建编辑器 DOM（token 转原子 chip）；可选光标偏移（默认末尾） */
function renderPrompt(text: string, caretOffset?: number): void {
  const ed = editorEl.value;
  if (!ed) return;
  const frag = document.createDocumentFragment();
  let last = 0;
  TOKEN_RE.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = TOKEN_RE.exec(text))) {
    // 只 append 非空段：避免空文本节点（frag.append("") 会创建空 Text 节点，
    // 导致 chip 位于开头时点击光标落入空节点内、紧邻删除判断失效）
    if (m.index > last) frag.append(text.slice(last, m.index));
    frag.append(buildTokenEl(m[1] as "Picture" | "Video" | "Audio", Number(m[2])));
    last = m.index + m[0].length;
  }
  if (last < text.length) frag.append(text.slice(last));
  ed.replaceChildren(frag);
  lastText = text;
  if (caretOffset != null) {
    const r = offsetToRange(ed, caretOffset);
    if (r) {
      const sel = window.getSelection();
      sel?.removeAllRanges();
      sel?.addRange(r);
    }
  }
}

/** 递归提取编辑器纯文本（文本节点直取；token chip 的 <Picture N> 一并收集；<br> 换行） */
function extractText(root: HTMLElement): string {
  let out = "";
  const walk = (el: Node): void => {
    if (el.nodeType === Node.TEXT_NODE) {
      out += el.nodeValue ?? "";
      return;
    }
    if (el instanceof HTMLBRElement) {
      out += "\n";
      return;
    }
    el.childNodes.forEach(walk);
  };
  walk(root);
  return out;
}

/** 文本偏移 → DOM Range（定位光标用）。
 *  chip（contenteditable=false）视为原子单元：不进入其内部定位，
 *  偏移落在 chip 边界时把光标放到 chip 前（可编辑文本一侧）。 */
function offsetToRange(root: HTMLElement, offset: number): Range | null {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT, {
    acceptNode(node) {
      if (node instanceof HTMLElement) {
        if (node.classList.contains("ref-token")) return NodeFilter.FILTER_ACCEPT; // chip 整体作为一个单元
        return NodeFilter.FILTER_SKIP;
      }
      // 文本：chip 内部文本不参与定位
      let el: Node | null = node.parentElement;
      while (el && el !== root) {
        if (el instanceof HTMLElement && el.classList.contains("ref-token")) return NodeFilter.FILTER_REJECT;
        el = el.parentElement;
      }
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  let acc = 0;
  let node: Node | null;
  while ((node = walker.nextNode())) {
    if (node instanceof HTMLElement && node.classList.contains("ref-token")) {
      const len = node.textContent?.length ?? 0;
      if (offset <= acc) {
        // 偏移落在 chip 起点：光标停在 chip 前
        const r = document.createRange();
        r.setStartBefore(node);
        r.collapse(true);
        return r;
      }
      if (offset < acc + len) {
        // 偏移落在 chip 内部（理论不出现）：按原子单元停在 chip 前
        const r = document.createRange();
        r.setStartBefore(node);
        r.collapse(true);
        return r;
      }
      // offset === acc + len：光标在 chip 后紧邻，交给后续节点/末尾处理
      acc += len;
      continue;
    }
    const len = node.textContent?.length ?? 0;
    if (len === 0) continue; // 防御性兜底：跳过空文本节点（正常流程已被 normalizePromptDom 收敛，此处防外部异常结构）
    if (acc + len >= offset) {
      const r = document.createRange();
      r.setStart(node, Math.min(offset - acc, len));
      r.collapse(true);
      return r;
    }
    acc += len;
  }
  // offset 到达文本末尾（可能最后一个节点是 chip）：光标停在末尾
  const r = document.createRange();
  r.selectNodeContents(root);
  r.collapse(false);
  return r;
}

/** 收敛编辑器 DOM 为规范形态：合并相邻文本节点 + 移除孤立空文本节点（chip 内部除外）。
 *  作为编辑器不变式：所有可能改变结构的操作（浏览器输入/删除/剪切/粘贴）后调用，
 *  保证光标定位与 chip 紧邻删除判断始终基于干净结构——从源头消除空文本节点等
 *  杂质结构对定位逻辑的干扰，避免按异常场景逐个打补丁。 */
function normalizePromptDom(root: HTMLElement): void {
  root.normalize(); // 标准方法：合并相邻文本节点
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const empties: Text[] = [];
  let n: Node | null;
  while ((n = walker.nextNode())) {
    const t = n as Text;
    if (t.data !== "") continue;
    // chip 内部文本不清理（防御性；buildTokenEl 不会产生空文本）
    let el: Node | null = t.parentElement;
    let inChip = false;
    while (el && el !== root) {
      if (el instanceof HTMLElement && el.classList.contains("ref-token")) {
        inChip = true;
        break;
      }
      el = el.parentElement;
    }
    if (!inChip) empties.push(t);
  }
  empties.forEach((t) => t.remove());
}

/** 按文本偏移恢复光标（DOM 收敛/重建后调用；偏移超出文本长度时钳到末尾） */
function restoreCaret(offset: number): void {
  const ed = editorEl.value;
  if (!ed) return;
  const r = offsetToRange(ed, Math.min(offset, extractText(ed).length));
  if (r) {
    const sel = window.getSelection();
    sel?.removeAllRanges();
    sel?.addRange(r);
  }
}

/** 当前光标处的文本偏移 */
function currentOffset(): number {
  const ed = editorEl.value;
  const sel = window.getSelection();
  if (!ed || !sel?.rangeCount) return lastText.length;
  const range = sel.getRangeAt(0).cloneRange();
  const pre = range.cloneRange();
  pre.selectNodeContents(ed);
  pre.setEnd(range.startContainer, range.startOffset);
  return pre.toString().length;
}

/** 同步父组件：提取纯文本 emit；空编辑器清残留 <br> 以恢复 placeholder */
function syncPrompt(): void {
  const ed = editorEl.value;
  if (!ed) return;
  const text = extractText(ed);
  if (!text && ed.innerHTML !== "") ed.innerHTML = "";
  lastText = text;
  emit("update:modelValue", text);
}

const editorEl = ref<HTMLElement | null>(null);
/** @ 菜单容器（滚动跟随定位用） */
const pickerMenuEl = ref<HTMLElement | null>(null);
/** / 菜单容器 */
const snippetMenuEl = ref<HTMLElement | null>(null);
/** @ 素材选择器打开 */
const pickerOpen = ref(false);
/** / 符号补全菜单打开 */
const snippetOpen = ref(false);
/** 触发符（@ 或 /）所在文本偏移（插入时替换 [triggerOffset, 当前光标) 区间） */
const triggerOffset = ref(0);
/** 选择器定位（相对 prompt-editor） */
const pickerStyle = ref({ top: "0px", left: "0px" });
/** / 后已输入的过滤文本 */
const snippetQuery = ref("");
/** 键盘高亮项（两个菜单共用，打开时从 0 起） */
const activeIndex = ref(0);
/** 编辑器当前文本（currentOffset 兑底） */
let lastText = "";

/** 光标前紧邻的原子 token（Backspace 删除用）；中间隔了内容（含空格）则返回 null。
 *  标准行为：有空格先删空格（光标停在 chip 后），紧邻时再删 chip——不跳过空格。 */
function prevTokenEl(range: Range): HTMLElement | null {
  const ed = editorEl.value;
  if (!ed) return null;
  const tokens = ed.querySelectorAll<HTMLElement>(".ref-token");
  let prev: HTMLElement | null = null;
  for (const t of tokens) {
    const tr = document.createRange();
    tr.selectNode(t);
    // token 终点已在光标之后：后续更不可能
    if (tr.compareBoundaryPoints(Range.END_TO_START, range) > 0) break;
    prev = t;
  }
  if (!prev) return null;
  const tr = document.createRange();
  tr.selectNode(prev);
  return tr.compareBoundaryPoints(Range.END_TO_START, range) === 0 ? prev : null;
}

/** 光标后紧邻的原子 token（Delete 删除用）；中间隔了内容（含空格）则返回 null */
function nextTokenEl(range: Range): HTMLElement | null {
  const ed = editorEl.value;
  if (!ed) return null;
  const tokens = ed.querySelectorAll<HTMLElement>(".ref-token");
  for (const t of tokens) {
    const tr = document.createRange();
    tr.selectNode(t);
    const cmp = tr.compareBoundaryPoints(Range.START_TO_START, range);
    if (cmp >= 0) return cmp === 0 ? t : null;
  }
  return null;
}

/** 过滤后的符号词条（/ 后 query） */
const filteredSnippets = computed(() => filterSnippets(snippetQuery.value));
/** 含匹配项的分组（按 SNIPPET_GROUPS 顺序） */
const visibleGroups = computed(() => {
  const keys = new Set(filteredSnippets.value.map((s) => s.group));
  return SNIPPET_GROUPS.filter((g) => keys.has(g.key));
});

/** 动态标签池（@ 右栏）：扫描当前编辑器文本中的 [Shot N]/(S1)/<Subject N>，零状态、跟随删除 */
const dynamicTags = ref<string[]>([]);

function refreshDynamicTags(): void {
  const ed = editorEl.value;
  dynamicTags.value = ed ? scanDynamicTags(extractText(ed)) : [];
}

function snippetsOfGroup(key: string): SnippetDef[] {
  return filteredSnippets.value.filter((s) => s.group === key);
}

function flatIndexOf(def: SnippetDef): number {
  return filteredSnippets.value.indexOf(def);
}

function onEditorInput(): void {
  const ed = editorEl.value;
  if (!ed) return;
  // 源头治理：输入前收敛 DOM（合并相邻文本、移除空文本节点），保证后续光标定位
  // 与 chip 紧邻删除判断基于规范结构；空节点不贡献字符，光标偏移不变，恢复安全
  const caret = currentOffset();
  normalizePromptDom(ed);
  restoreCaret(caret);
  const prev = lastText;
  syncPrompt();
  const cur = extractText(ed);
  const pos = currentOffset();
  // 新输入的 @（键盘/输入法均触发 input，比 keydown 可靠）：打开素材选择器（互斥关符号菜单）
  if (pos > 0 && cur[pos - 1] === "@" && prev[pos - 1] !== "@") {
    // 符号菜单打开中输入的 @：吞掉紧邻的 /，避免插入后残留触发符
    let offset = pos - 1;
    if (snippetOpen.value && cur[offset - 1] === "/") offset -= 1;
    triggerOffset.value = offset;
    pickerOpen.value = true;
    snippetOpen.value = false;
    activeIndex.value = 0;
    refreshDynamicTags();
    nextTick(() => positionPicker());
    return;
  }
  // 新输入的 /：打开符号补全（互斥关素材选择器）；前一个字符非字母/数字时触发——
  // 允许标签后紧跟（如 <Subject 1>/）、行首/空白后触发，同时避免日期（2026/08）
  // 与 URL 中字母/数字后的 / 误弹
  if (
    pos > 0 &&
    cur[pos - 1] === "/" &&
    prev[pos - 1] !== "/" &&
    (pos === 1 || !/[A-Za-z0-9]/.test(cur[pos - 2]))
  ) {
    // 素材选择器打开中输入的 /：吞掉紧邻的 @，避免插入后残留触发符
    let offset = pos - 1;
    if (pickerOpen.value && cur[offset - 1] === "@") offset -= 1;
    triggerOffset.value = offset;
    snippetOpen.value = true;
    pickerOpen.value = false;
    activeIndex.value = 0;
    snippetQuery.value = "";
    nextTick(() => positionPicker());
    return;
  }
  // 素材选择器打开中：@ 被删则关闭，否则跟随光标并刷新动态标签池
  if (pickerOpen.value) {
    if (cur[triggerOffset.value] !== "@") {
      pickerOpen.value = false;
      return;
    }
    refreshDynamicTags();
    nextTick(() => positionPicker());
    return;
  }
  // 符号补全打开中：/ 被删则关闭，否则跟随光标并更新过滤
  if (snippetOpen.value) {
    if (cur[triggerOffset.value] !== "/") {
      snippetOpen.value = false;
      return;
    }
    snippetQuery.value = cur.slice(triggerOffset.value + 1, pos);
    activeIndex.value = 0;
    nextTick(() => positionPicker());
  }
}

function onEditorKeydown(e: KeyboardEvent): void {
  if (e.key === "Escape") {
    pickerOpen.value = false;
    snippetOpen.value = false;
    return;
  }
  // 任一菜单打开：↑/↓ 导航、Enter 插入高亮项（无候选时不拦截，走浏览器默认）
  if (pickerOpen.value || snippetOpen.value) {
    // @ 菜单候选 = 素材 + 动态标签（扁平连续索引）；/ 菜单候选 = 过滤后的符号词条
    const count = pickerOpen.value ? props.items.length + dynamicTags.value.length : filteredSnippets.value.length;
    if (!count) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      activeIndex.value = (activeIndex.value + 1) % count;
      nextTick(scrollActiveIntoView);
      return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      activeIndex.value = (activeIndex.value - 1 + count) % count;
      nextTick(scrollActiveIntoView);
      return;
    }
    // ←/→：仅 @ 双栏菜单在素材/标签栏间切换（保留栏内相对位置，循环）；/ 菜单单栏无栏概念
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      if (!pickerOpen.value) return;
      const m = props.items.length;
      const t = dynamicTags.value.length;
      if (!m || !t) return; // 单栏时左右无意义
      e.preventDefault();
      const inLeft = activeIndex.value < m;
      const col = inLeft ? activeIndex.value : activeIndex.value - m;
      activeIndex.value = inLeft ? m + Math.min(col, t - 1) : Math.min(col, m - 1);
      nextTick(scrollActiveIntoView);
      return;
    }
    if (e.key === "Enter") {
      e.preventDefault();
      if (pickerOpen.value) {
        const total = props.items.length;
        const idx = activeIndex.value;
        if (idx < total) {
          const item = props.items[idx] ?? props.items[0];
          if (item) insertToken(item.token);
        } else {
          const tag = dynamicTags.value[idx - total];
          if (tag) insertPlainText(tag);
        }
      } else {
        const item = filteredSnippets.value[activeIndex.value] ?? filteredSnippets.value[0];
        if (item) insertSnippet(item);
      }
      return;
    }
    return; // 菜单打开时其余按键交给浏览器默认
  }
  // 菜单关闭时 Enter（含 Shift+Enter）统一换行：插入 \n 文本节点（pre-wrap 显示换行）。
  // 浏览器默认 Enter 会产生 <div> 块级结构，extractText 不处理 div 边界导致换行丢失；
  // \n 文本节点被 extractText 直取，换行可靠保存。Ctrl+Enter（ComfyUI 出队）不拦截。
  if (e.key === "Enter" && !e.ctrlKey && !e.metaKey) {
    e.preventDefault();
    insertPlainTextAtSelection("\n");
    syncPrompt();
    return;
  }
  // 原子 token 整体删除：光标紧邻 chip 时 Backspace/Delete 直接移除（contenteditable=false 元素浏览器默认不删）
  if ((e.key === "Backspace" || e.key === "Delete") && !e.ctrlKey && !e.metaKey) {
    const sel = window.getSelection();
    if (!sel?.rangeCount) return;
    const range = sel.getRangeAt(0);
    if (!range.collapsed) return; // 有选区时交给浏览器默认删除
    const target = e.key === "Backspace" ? prevTokenEl(range) : nextTokenEl(range);
    if (!target) return;
    e.preventDefault();
    const ed = editorEl.value!;
    // 记录 chip 前的文本偏移，删除后光标放回原处（chip 前）
    const pre = document.createRange();
    pre.selectNodeContents(ed);
    const tr = document.createRange();
    tr.selectNode(target);
    pre.setEnd(tr.startContainer, tr.startOffset);
    const off = pre.toString().length;
    target.remove();
    normalizePromptDom(ed); // 删除后收敛：chip 移除可能使两侧文本节点相邻
    syncPrompt();
    ed.focus();
    const r = offsetToRange(ed, off);
    if (r) {
      const s = window.getSelection();
      s?.removeAllRanges();
      s?.addRange(r);
    }
  }
}

function onEditorPaste(e: ClipboardEvent): void {
  // stopPropagation：阻断 paste 冒泡到 ComfyUI 全局监听（其会在画布上粘贴节点/图片）
  e.preventDefault();
  e.stopPropagation();
  const ed = editorEl.value;
  if (!ed) return;
  let text = e.clipboardData?.getData("text/plain") ?? "";
  if (!text) {
    // 剪贴板无纯文本（图片/自定义格式）：从 HTML 兜底提取（如从富文本应用复制）
    const html = e.clipboardData?.getData("text/html");
    if (html) text = htmlToPlainText(html);
  }
  if (!text) return; // 无可用文本：不插入
  // 诊断：确认剪贴板内容（F12 控制台查看；复现问题时带回这段信息可快速定位）
  console.debug("[PromptEditor] paste text/plain:", JSON.stringify(text.slice(0, 300)));
  insertPlainTextAtSelection(text);
  // 粘贴内容可能含 <Picture N>：重建为原子 chip，光标保持在粘贴文本后
  renderPrompt(extractText(editorEl.value!), currentOffset());
  syncPrompt();
}

/** 在当前光标处插入纯文本（粘贴用）：Range 精准操作。
 *  弃用 document.execCommand("insertText")——其在 paste 事件（preventDefault 后）中
 *  可能触发浏览器默认粘贴、把剪贴板实际内容（如 ComfyUI 节点）一并插入，
 *  而非仅插入传入文本。Range 插入与剪贴板零耦合，行为完全可控。
 *  注意：不保留浏览器 Undo 栈（Ctrl+Z 无法撤销粘贴，为正确性取舍）。 */
function insertPlainTextAtSelection(text: string): void {
  const ed = editorEl.value;
  const sel = window.getSelection();
  if (!ed || !sel?.rangeCount) return;
  const range = sel.getRangeAt(0).cloneRange();
  range.deleteContents(); // 有选区则替换选区
  const node = document.createTextNode(text);
  range.insertNode(node);
  range.setStartAfter(node);
  range.collapse(true);
  sel.removeAllRanges();
  sel.addRange(range);
}

/** HTML → 纯文本（粘贴兜底；DOMParser 解析后取 body 文本，<br> 转为换行） */
function htmlToPlainText(html: string): string {
  const doc = new DOMParser().parseFromString(html, "text/html");
  doc.body.querySelectorAll("br").forEach((br) => br.replaceWith("\n"));
  return doc.body.textContent ?? "";
}

/** 提取当前选区纯文本并写入剪贴板（copy/cut 共用）。
 *  规避 Chrome 在含 contenteditable=false 原子 chip 的选区上默认复制失败、
 *  导致剪贴板残留旧内容（如之前复制的 ComfyUI 节点）的问题。
 *  返回是否成功（无选区/无文本时不拦截，交给浏览器默认）。 */
function copySelectionToClipboard(e: ClipboardEvent): boolean {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed) return false;
  const container = document.createElement("div");
  container.appendChild(sel.getRangeAt(0).cloneContents());
  const text = extractText(container);
  if (!text) return false;
  // stopPropagation：阻断 copy/cut 冒泡到 ComfyUI 全局监听（避免画布同时执行节点复制/剪切）
  e.preventDefault();
  e.stopPropagation();
  e.clipboardData?.setData("text/plain", text);
  return true;
}

function onEditorCopy(e: ClipboardEvent): void {
  copySelectionToClipboard(e);
}

function onEditorCut(e: ClipboardEvent): void {
  if (!copySelectionToClipboard(e)) return;
  const sel = window.getSelection();
  if (!sel?.rangeCount) return;
  const ed = editorEl.value;
  if (!ed) return;
  const caret = currentOffset();
  sel.getRangeAt(0).deleteContents();
  normalizePromptDom(ed); // 剪切后收敛，光标偏移钳到新文本长度内
  restoreCaret(caret);
  syncPrompt();
}

// ---------- 点击聚焦 ----------
// 点击输入框时显式聚焦：保证光标进入编辑器、@ / ↑↓ 等快捷键可正常监听；
// 焦点进入后 ComfyUI 画布的快捷键（绑定在 canvas 上）自然不响应。

/** 点击编辑器任意位置（含空白/占位符区）：显式聚焦（不 preventDefault，光标仍按点击位置定位） */
function onEditorMousedown(): void {
  editorEl.value?.focus();
}

/** 失焦：关闭选择器 */
function onEditorBlur(): void {
  pickerOpen.value = false;
  snippetOpen.value = false;
}

/** 选择器定位到光标下方；下方空间不足时翻到上方，并 clamp 在编辑器可视区内 */
function positionPicker(): void {
  const ed = editorEl.value;
  const sel = window.getSelection();
  if (!ed || !sel?.rangeCount) return;
  const rect = sel.getRangeAt(0).getBoundingClientRect();
  const edRect = ed.getBoundingClientRect();
  const isSnippet = snippetOpen.value;
  const PICKER_W = isSnippet ? 300 : dynamicTags.value.length ? 460 : 264;
  const itemCount = isSnippet
    ? filteredSnippets.value.length
    : props.items.length + dynamicTags.value.length;
  const groupCount = isSnippet ? visibleGroups.value.length : 0;
  const PICKER_H = Math.min(48 + itemCount * 30 + groupCount * 20, 300);
  let left = rect.left - edRect.left;
  let top = rect.bottom - edRect.top + 4;
  left = Math.max(2, Math.min(left, ed.clientWidth - PICKER_W - 4));
  if (top + PICKER_H > ed.clientHeight) top = Math.max(2, rect.top - edRect.top - PICKER_H - 4);
  pickerStyle.value = { top: `${top}px`, left: `${left}px` };
}

/** 键盘导航后滚动菜单，使高亮项可见（仅键盘触发；鼠标 hover 不滚动，避免跳动） */
function scrollActiveIntoView(): void {
  const menu = pickerOpen.value ? pickerMenuEl.value : snippetOpen.value ? snippetMenuEl.value : null;
  if (!menu) return;
  const active = menu.querySelector<HTMLElement>(".rp-active");
  if (!active) return;
  const menuRect = menu.getBoundingClientRect();
  const actRect = active.getBoundingClientRect();
  if (actRect.top < menuRect.top) {
    menu.scrollTop += actRect.top - menuRect.top;
  } else if (actRect.bottom > menuRect.bottom) {
    menu.scrollTop += actRect.bottom - menuRect.bottom;
  }
}

/** 插入原子 token：Range 精准插入（不重建 DOM，保留编辑器原生状态），替换 [@, 光标) 区间 */
function insertToken(token: string): void {
  const ed = editorEl.value;
  const sel = window.getSelection();
  const parsed = TOKEN_PARSE_RE.exec(token);
  if (!ed || !sel?.rangeCount || !parsed) return;
  const start = offsetToRange(ed, triggerOffset.value);
  const endOff = Math.max(triggerOffset.value + 1, currentOffset());
  const end = offsetToRange(ed, endOff);
  if (!start || !end) return;
  // 删除 [@, 光标) 内容（含 @ 及后续输入）
  const range = sel.getRangeAt(0).cloneRange();
  range.setStart(start.startContainer, start.startOffset);
  range.setEnd(end.startContainer, end.startOffset);
  range.deleteContents();
  // 依次插入 chip → 尾随空格，光标停在空格后
  const chip = buildTokenEl(parsed[1] as "Picture" | "Video" | "Audio", Number(parsed[2]));
  range.insertNode(chip);
  range.setStartAfter(chip);
  range.collapse(true);
  const space = document.createTextNode(" ");
  range.insertNode(space);
  range.setStartAfter(space);
  range.collapse(true);
  sel.removeAllRanges();
  sel.addRange(range);
  pickerOpen.value = false;
  syncPrompt();
}

/** 插入动态标签纯文本（[Shot N]/(S1)/<Subject N>）：替换 [@, 光标) 区间，光标落在文本末尾。
 *  与 insertToken 不同：标签无素材，不渲染原子 chip，作为普通文本可继续编辑。 */
function insertPlainText(token: string): void {
  const ed = editorEl.value;
  const sel = window.getSelection();
  if (!ed || !sel?.rangeCount) return;
  const start = offsetToRange(ed, triggerOffset.value);
  const endOff = Math.max(triggerOffset.value + 1, currentOffset());
  const end = offsetToRange(ed, endOff);
  if (!start || !end) return;
  const range = sel.getRangeAt(0).cloneRange();
  range.setStart(start.startContainer, start.startOffset);
  range.setEnd(end.startContainer, end.startOffset);
  range.deleteContents();
  const node = document.createTextNode(token);
  range.insertNode(node);
  range.setStartAfter(node);
  range.collapse(true);
  sel.removeAllRanges();
  sel.addRange(range);
  pickerOpen.value = false;
  syncPrompt();
}

/** 插入结构化符号：替换 [/, 光标) 区间；智能编号替换 {n}，光标落 {cursor} 处或文本末尾 */
function insertSnippet(def: SnippetDef): void {
  const ed = editorEl.value;
  const sel = window.getSelection();
  if (!ed || !sel?.rangeCount) return;
  const n = def.smart ? resolveSmartNumber(def.smart, extractText(ed)) : null;
  const { text, cursor } = assembleSnippet(def, n);
  const start = offsetToRange(ed, triggerOffset.value);
  const endOff = Math.max(triggerOffset.value + 1, currentOffset());
  const end = offsetToRange(ed, endOff);
  if (!start || !end) return;
  const range = sel.getRangeAt(0).cloneRange();
  range.setStart(start.startContainer, start.startOffset);
  range.setEnd(end.startContainer, end.startOffset);
  range.deleteContents();
  const node = document.createTextNode(text);
  range.insertNode(node);
  // 光标定位：有 {cursor} 落其处（在插入节点内），否则落到节点后
  if (cursor != null && cursor > 0) range.setStart(node, cursor);
  else range.setStartAfter(node);
  range.collapse(true);
  sel.removeAllRanges();
  sel.addRange(range);
  snippetOpen.value = false;
  syncPrompt();
}

// 初始渲染 + 外部 prompt 变化（父组件恢复参数/清空等）重建；自身输入时文本已一致，天然跳过
onMounted(() => {
  renderPrompt(props.modelValue);
});
watch(
  () => props.modelValue,
  (v) => {
    const ed = editorEl.value;
    if (ed && v !== extractText(ed)) renderPrompt(v ?? "");
  },
);
// 素材区变化（切换片段/模式、素材增删/替换/换位，pickerItems 重新计算）：
// - 关闭可能过期的选择器菜单
// - 刷新 chip 缩略图——chip 视觉实时引用素材区数据（resolveMedia 按编号取当前素材），
//   删除/替换素材后不再残留 chip 创建时的旧图；同文本跨片段切换也靠此刷新
watch(
  () => props.items,
  () => {
    pickerOpen.value = false;
    snippetOpen.value = false;
    refreshChipThumbs();
  },
);
</script>

<template>
  <div class="prompt-pane">
    <div class="pane-title">
      提示词 <span class="pane-hint">@ 引用素材 · / 结构化符号</span>
    </div>
    <div class="prompt-editor">
      <div
        ref="editorEl"
        class="prompt-input"
        contenteditable="true"
        data-placeholder="描述这个镜头的画面、运动、镜头语言…"
        spellcheck="false"
        @input="onEditorInput"
        @keydown="onEditorKeydown"
        @paste="onEditorPaste"
        @copy="onEditorCopy"
        @cut="onEditorCut"
        @mousedown="onEditorMousedown"
        @blur="onEditorBlur"
      ></div>

      <!-- @ 素材/标签选择器（跟随光标；有动态标签时双栏：左素材 + 右动态标签） -->
      <div
        v-if="pickerOpen"
        ref="pickerMenuEl"
        class="ref-picker"
        :class="{ 'picker-dual': dynamicTags.length }"
        :style="pickerStyle"
        @mousedown.prevent
      >
        <div class="rp-col">
          <div class="rp-col-title">素材</div>
          <div v-if="!items.length" class="rp-empty">暂无参考素材，请先在右侧素材区添加</div>
          <div v-else class="rp-grid">
            <button
              v-for="(item, i) in items"
              :key="item.token"
              class="rp-item"
              :class="{ 'rp-active': activeIndex === i }"
              @click="insertToken(item.token)"
              @mousemove="activeIndex = i"
            >
              <img v-if="item.thumb" :src="item.thumb" class="rp-thumb" alt="" loading="lazy" />
              <span v-else class="rp-icon">{{ KIND_ICON[item.kind] }}</span>
              <span class="rp-label">{{ item.label }}</span>
              <span class="rp-token">{{ item.token }}</span>
            </button>
          </div>
        </div>
        <div v-if="dynamicTags.length" class="rp-divider"></div>
        <div v-if="dynamicTags.length" class="rp-col">
          <div class="rp-col-title">标签 <span class="rp-hint">用 / 创建</span></div>
          <div class="rp-grid">
            <button
              v-for="(tag, j) in dynamicTags"
              :key="tag"
              class="rp-item"
              :class="{ 'rp-active': activeIndex === items.length + j }"
              @click="insertPlainText(tag)"
              @mousemove="activeIndex = items.length + j"
            >
              <span class="rp-icon">{{ dynamicTagIcon(tag) }}</span>
              <span class="rp-label">{{ tag }}</span>
            </button>
          </div>
        </div>
      </div>

      <!-- / 结构化符号补全（分组 + 过滤 + 键盘导航） -->
      <div v-if="snippetOpen" ref="snippetMenuEl" class="ref-picker sp-picker" :style="pickerStyle" @mousedown.prevent>
        <div v-if="!filteredSnippets.length" class="rp-empty">无匹配的结构化符号</div>
        <div v-else class="sp-grid">
          <template v-for="g in visibleGroups" :key="g.key">
            <div class="sp-group">{{ g.icon }} {{ g.title }}</div>
            <button
              v-for="item in snippetsOfGroup(g.key)"
              :key="`${item.group}:${item.token}:${item.label}`"
              class="rp-item sp-item"
              :class="{ 'rp-active': activeIndex === flatIndexOf(item) }"
              :title="item.hint"
              @click="insertSnippet(item)"
              @mousemove="activeIndex = flatIndexOf(item)"
            >
              <span class="rp-label">{{ item.label }}</span>
              <span class="sp-token">{{ snippetPreview(item) }}</span>
            </button>
          </template>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.prompt-pane {
  flex: 3;
  min-width: 0;
  min-height: 0; /* 参考面板固定行高下：内容超高在输入框内滚动，不撑破等高布局 */
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px;
  border: 1px solid var(--dc-border);
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.02);
}

/* 功能区域名：底色框标签 */
.pane-title {
  font-size: 11px;
  font-weight: 600;
  color: #93c5fd;
  align-self: flex-start;
  padding: 2px 8px;
  background: rgba(96, 165, 250, 0.14);
  border-radius: 6px;
  white-space: nowrap;
}
.pane-hint {
  font-weight: 400;
  font-size: 10px;
  color: var(--dc-text-faint);
  margin-left: 2px;
}

/* ---------- 提示词编辑器（contenteditable 原子 token） ---------- */
.prompt-editor {
  position: relative;
  flex: 1;
  min-height: 0;
}
.prompt-input {
  width: 100%;
  height: 100%;
  min-height: 0;
  overflow-y: auto;
  padding: 8px 10px;
  border: 1px solid var(--dc-border);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.02);
  font-family: inherit;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  overflow-wrap: break-word;
  outline: none;
  color: var(--dc-text);
}
/* 聚焦反馈：边框高亮 + 光晕，直观确认已进入编辑态 */
.prompt-input:focus {
  border-color: rgba(96, 165, 250, 0.7);
  box-shadow: 0 0 0 2px rgba(96, 165, 250, 0.15);
}
.prompt-input:empty::before {
  content: attr(data-placeholder);
  color: var(--dc-text-faint);
  pointer-events: none;
}
.prompt-input::-webkit-scrollbar {
  width: 8px;
}
.prompt-input::-webkit-scrollbar-thumb {
  background: var(--dc-border-strong);
  border-radius: 4px;
}

/* 原子 token chip（contenteditable=false；DOM API 创建，需 :deep） */
.prompt-input :deep(.ref-token) {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  vertical-align: middle;
  margin: 0 2px;
  padding: 0 5px;
  border-radius: 5px;
  font-size: 11px;
  font-weight: 500;
  white-space: nowrap;
  cursor: default;
  user-select: none;
  background: rgba(96, 165, 250, 0.16);
  border: 1px solid rgba(96, 165, 250, 0.45);
  color: #93c5fd;
}
.prompt-input :deep(.ref-token.ref-video) {
  background: rgba(74, 222, 128, 0.14);
  border-color: rgba(74, 222, 128, 0.4);
  color: #86efac;
}
.prompt-input :deep(.ref-token.ref-audio) {
  background: rgba(251, 191, 36, 0.14);
  border-color: rgba(251, 191, 36, 0.4);
  color: #fcd34d;
}
.prompt-input :deep(.ref-thumb) {
  width: 14px;
  height: 14px;
  object-fit: cover;
  border-radius: 3px;
}
.prompt-input :deep(.ref-icon) {
  font-size: 12px;
  line-height: 1;
}
/* 图标 emoji 经伪元素渲染（data-emoji）：不占文本节点，避免被 extractText 混入提示词数据层 */
.prompt-input :deep(.ref-icon)::before {
  content: attr(data-emoji);
}
.prompt-input :deep(.ref-text) {
  font-variant-numeric: tabular-nums;
}

/* @ / / 选择器（跟随光标，单列列表） */
.ref-picker {
  position: absolute;
  z-index: 40;
  width: 264px;
  max-width: calc(100% - 8px);
  max-height: 300px;
  overflow-y: auto;
  padding: 6px;
  background: var(--dc-panel);
  border: 1px solid var(--dc-border-strong);
  border-radius: 8px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
}
.sp-picker {
  width: 300px;
}
/* @ 双栏：左素材 + 右动态标签 */
.picker-dual {
  display: flex;
  gap: 6px;
  width: 460px;
}
.picker-dual .rp-col {
  flex: 1;
  min-width: 0;
}
.rp-divider {
  width: 1px;
  flex-shrink: 0;
  background: var(--dc-border);
}
.rp-col-title {
  font-size: 10px;
  font-weight: 600;
  color: var(--dc-text-faint);
  padding: 2px 8px 4px;
  user-select: none;
}
.rp-hint {
  font-weight: 400;
  color: var(--dc-text-faint);
}
.rp-empty {
  font-size: 12px;
  color: var(--dc-text-faint);
  text-align: center;
  padding: 8px 0;
}
.rp-grid,
.sp-grid {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.rp-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 8px;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--dc-text);
  font-size: 11px;
  cursor: pointer;
  transition: background 0.12s ease;
  min-width: 0;
}
.rp-item:hover {
  background: rgba(96, 165, 250, 0.12);
}
.rp-active {
  background: rgba(96, 165, 250, 0.18);
  outline: 1px solid rgba(96, 165, 250, 0.5);
}
.rp-thumb {
  width: 26px;
  height: 26px;
  object-fit: cover;
  border-radius: 4px;
  flex-shrink: 0;
}
.rp-icon {
  font-size: 16px;
  line-height: 1;
  flex-shrink: 0;
}
.rp-label {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rp-token {
  color: var(--dc-text-faint);
  font-variant-numeric: tabular-nums;
  flex-shrink: 0;
}
.sp-item .rp-label {
  font-size: 11px;
}
.sp-token {
  color: var(--dc-text-faint);
  font-size: 10px;
  font-variant-numeric: tabular-nums;
  max-width: 120px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex-shrink: 0;
}

/* 符号补全分组标题 */
.sp-group {
  font-size: 10px;
  font-weight: 600;
  color: var(--dc-text-faint);
  padding: 4px 8px 2px;
  border-bottom: 1px dashed var(--dc-border);
  user-select: none;
}
</style>
