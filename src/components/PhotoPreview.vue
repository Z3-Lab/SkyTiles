<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import type { Photo } from '../types'
import { photoTime } from '../types'

defineProps<{ photo: Photo }>()
const emit = defineEmits<{ close: []; locate: [] }>()
const failed = ref(false)
function keydown(event: KeyboardEvent) {
  if (event.key === 'Escape') emit('close')
}
onMounted(() => window.addEventListener('keydown', keydown))
onUnmounted(() => window.removeEventListener('keydown', keydown))
</script>

<template>
  <div
    class="preview-backdrop"
    role="dialog"
    aria-modal="true"
    aria-label="照片预览"
    @click.self="emit('close')"
  >
    <button
      class="preview-close icon-button"
      aria-label="关闭预览"
      autofocus
      @click="emit('close')"
    >
      ×
    </button>
    <div class="preview-stage">
      <img
        v-if="photo.imageUrl && !failed"
        :src="photo.imageUrl"
        :alt="photo.filename"
        @error="failed = true"
      />
      <div v-else class="preview-missing">
        <span class="empty-symbol">▧</span>
        <h2>原图暂不可用</h2>
        <p>照片信息已保留。重新导入原图，或选择正确的旧版目录再次迁移即可补齐。</p>
      </div>
    </div>
    <div class="preview-details">
      <div>
        <strong>{{ photo.filename }}</strong>
        <small>{{ photo.relativePath }}</small>
      </div>
      <span>{{ photoTime(photo.capturedAt) }}</span>
      <span v-if="photo.sourcePath" :title="photo.sourcePath" style="overflow-wrap: anywhere">
        原图：{{ photo.sourcePath }}
      </span>
      <span v-if="photo.latitude !== null && photo.longitude !== null">
        经度 {{ photo.longitude.toFixed(6) }}° · 纬度 {{ photo.latitude.toFixed(6) }}°
        <span v-if="photo.altitude !== null">· {{ photo.altitude.toFixed(1) }} m</span>
      </span>
      <button v-if="photo.latitude !== null" class="primary" @click="emit('locate')">
        ⌖ 地图定位
      </button>
    </div>
  </div>
</template>
