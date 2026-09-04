<script setup lang="ts">
import { onMounted, ref } from "vue";
import { usePreviewThumb } from "@/composables/usePreviewThumb";

const props = defineProps<{ url: string }>();

const canvas = ref<HTMLCanvasElement | null>(null);
const { showFromUrl } = usePreviewThumb(canvas);

onMounted(() => void showFromUrl(props.url));
</script>

<template>
  <canvas ref="canvas" class="sample-thumb"></canvas>
</template>
