import type { GlobalThemeOverrides } from "naive-ui";

/**
 * Naive UI 主题覆盖 —— 「现代轻量 + 青蓝」基调
 * 背景用 slate 蓝灰色系，强调色用 sky 青蓝。
 */
export const themeOverrides: GlobalThemeOverrides = {
  common: {
    primaryColor: "#38BDF8",
    primaryColorHover: "#7DD3FC",
    primaryColorPressed: "#0EA5E9",
    primaryColorSuppl: "#38BDF8",
    infoColor: "#38BDF8",
    successColor: "#34D399",
    warningColor: "#FBBF24",
    errorColor: "#F87171",
    borderRadius: "8px",
    bodyColor: "#0F172A",
    cardColor: "#1E293B",
    modalColor: "#1E293B",
    popoverColor: "#1E293B",
    tableColor: "#1E293B",
    inputColor: "#0F172A",
    inputColorDisabled: "#1E293B",
    borderColor: "#334155",
    dividerColor: "#334155",
    textColorBase: "#E2E8F0",
    textColor1: "#F8FAFC",
    textColor2: "#E2E8F0",
    textColor3: "#94A3B8",
    textColorDisabled: "#475569",
    hoverColor: "rgba(56, 189, 248, 0.08)",
    fontFamily:
      '"Inter", "PingFang SC", "Microsoft YaHei", system-ui, -apple-system, sans-serif',
  },
};

/** 时间线 UI 自定义色板（组件内直接使用，与 Naive 主题一致） */
export const palette = {
  bg: "#0F172A",
  panel: "#1E293B",
  panelHover: "#243349",
  border: "#334155",
  borderStrong: "#475569",
  accent: "#38BDF8",
  accentHover: "#7DD3FC",
  accentDim: "rgba(56, 189, 248, 0.12)",
  text: "#E2E8F0",
  textDim: "#94A3B8",
  textFaint: "#64748B",
  danger: "#F87171",
} as const;
