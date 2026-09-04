import { defineConfig, type PluginOption } from "vite";
import vue from "@vitejs/plugin-vue";
import Components from "unplugin-vue-components/vite";
import { NaiveUiResolver } from "unplugin-vue-components/resolvers";
import { fileURLToPath, URL } from "node:url";
import { readFileSync } from "node:fs";
import { minimaxStudioDevMock } from "./src/dev/mockPlugin.ts";

// 插件版本（读 package.json，注入构建产物；前端运行时与后端 /version 对比做缓存自检）
const pkg = JSON.parse(
  readFileSync(fileURLToPath(new URL("./package.json", import.meta.url)), "utf-8"),
) as { version?: string };
const PLUGIN_VERSION = pkg.version ?? "0.0.0";

// 集成交互模式：dev / preview 均为浏览器独立预览（不依赖 ComfyUI）
// 构建模式：build --mode lib 产出挂载进 ComfyUI 节点的单文件脚本
export default defineConfig(({ mode }) => {
  const commonPlugins: PluginOption[] = [
    vue(),
    // Naive UI 按需自动引入（模板中直接使用组件，无需手动 import）
    Components({
      resolvers: [NaiveUiResolver()],
      dts: "src/auto-imports.d.ts",
    }),
  ];

  if (mode === "lib") {
    // ComfyUI 节点集成产物：ES Module 单文件。
    // ComfyUI 以 <script type="module"> 加载扩展，相对路径 import
    // ../../scripts/app.js 在运行时由 ComfyUI 前端解析（必须 external 保留）。
    return {
      plugins: commonPlugins,
      resolve: {
        alias: {
          "@": fileURLToPath(new URL("./src", import.meta.url)),
        },
      },
      build: {
        outDir: "dist",
        emptyOutDir: true,
        cssCodeSplit: false,
        lib: {
          entry: fileURLToPath(new URL("./src/main.ts", import.meta.url)),
          formats: ["es"],
          fileName: () => "minimax-h3-studio.js",
        },
        rollupOptions: {
          // ComfyUI 前端运行时提供，构建时排除（保留相对路径 import）
          external: [/^\.\.\/\.\.\/scripts\//],
          output: {
            // CSS 与 JS 同名，方便扩展内注入
            assetFileNames: "minimax-h3-studio[extname]",
          },
        },
      },
      // 库模式不会自动替换 process.env.NODE_ENV，
      // Vue 3 运行时依赖它（浏览器无 process 全局，必须替换成字面量）
      define: {
        "process.env.NODE_ENV": JSON.stringify("production"),
        // 构建版本（前端缓存自检：与后端 /minimax/studio/version 对比）
        __STUDIO_VERSION__: JSON.stringify(PLUGIN_VERSION),
      },
    };
  }

  // 浏览器独立预览（开发/预览）
  return {
    plugins: [
      ...commonPlugins,
      // dev-only mock 后端：提供 /view、/minimax/studio/*、/upload 本地实现（见 mockPlugin）
      minimaxStudioDevMock(),
    ],
    resolve: {
      alias: {
        "@": fileURLToPath(new URL("./src", import.meta.url)),
      },
    },
    server: {
      port: 5178,
      open: true,
    },
  };
});
