<script setup lang="ts">
import { darkTheme } from "naive-ui";
import Toolbar from "@/components/Toolbar.vue";
import Timeline from "@/components/Timeline.vue";
import ClipDetailPanel from "@/components/ClipDetailPanel.vue";
import HistoryRestoreModal from "@/components/HistoryRestoreModal.vue";
import { themeOverrides } from "@/styles/theme";

// 数据同步（时间线 → timeline_data widget）由 main.ts 的 nodeCreated 统一处理：
// 时间线变化 → store 订阅 → timeline_data.value（工作流保存/Queue 的载体）。
// 本组件只负责 UI 渲染。
</script>

<template>
  <n-config-provider :theme="darkTheme" :theme-overrides="themeOverrides">
    <n-message-provider>
      <n-dialog-provider>
        <div class="console">
          <Toolbar />
          <Timeline />
          <ClipDetailPanel />
        </div>
        <!-- 从历史恢复片段面板（工具栏/空态区共用入口） -->
        <HistoryRestoreModal />
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>

<style scoped>
.console {
  display: flex;
  flex-direction: column;
  gap: 10px;
  height: 100%;
  min-height: 0;
  overflow-y: auto;
}
</style>
