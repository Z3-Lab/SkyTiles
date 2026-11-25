<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api, message } from '../api'
import type { ExportJob, PhotoReference } from '../types'
import { exportLabels, photoTime } from '../types'

const props = defineProps<{ photos: PhotoReference[]; exportRoot: string }>()
const emit = defineEmits<{ close: [] }>()
const tab = ref<'export' | 'history'>(props.photos.length ? 'export' : 'history')
const destination = ref(props.exportRoot)
const folderName = ref(`SkyTiles_${new Date().toLocaleString('sv-SE').replace(/[^0-9]/g, '_')}`)
const history = ref<ExportJob[]>([])
const job = ref<ExportJob | null>(null)
const error = ref('')
const submitting = ref(false)
const picking = ref(false)
const busy = computed(
  () =>
    submitting.value ||
    picking.value ||
    (!!job.value && ['queued', 'running'].includes(job.value.state)),
)
const progress = computed(() =>
  job.value?.total ? Math.round((job.value.processed / job.value.total) * 100) : 0,
)
let alive = true
let timer: ReturnType<typeof setTimeout> | undefined
let followSequence = 0

async function loadHistory() {
  try {
    history.value = (await api.exports()).items
  } catch (cause) {
    if (alive) error.value = message(cause)
  }
}

function showHistory() {
  job.value = null
  tab.value = 'history'
  void loadHistory()
}

async function follow(id: string) {
  clearTimeout(timer)
  const sequence = ++followSequence
  try {
    const current = await api.exportJob(id)
    if (!alive || sequence !== followSequence) return
    job.value = current
    error.value = ''
    if (['queued', 'running'].includes(current.state))
      timer = setTimeout(() => {
        void follow(id)
      }, 600)
    else void loadHistory()
  } catch (cause) {
    if (!alive || sequence !== followSequence) return
    error.value = `${message(cause)}；稍后将自动重试，也可刷新页面后在导出记录中查看。`
    timer = setTimeout(() => {
      void follow(id)
    }, 2000)
  }
}

async function start() {
  if (busy.value || !props.photos.length || !destination.value.trim()) return
  submitting.value = true
  error.value = ''
  try {
    const result = await api.newExport(props.photos, destination.value, folderName.value)
    await follow(result.id)
  } catch (cause) {
    error.value = message(cause)
  } finally {
    submitting.value = false
  }
}

async function chooseFolder() {
  picking.value = true
  error.value = ''
  try {
    const result = await api.exportFolder(destination.value)
    if (result.path) destination.value = result.path
  } catch (cause) {
    error.value = message(cause)
  } finally {
    picking.value = false
  }
}

function close() {
  if (!busy.value) emit('close')
}
function escape(event: KeyboardEvent) {
  if (event.key === 'Escape') close()
}
onMounted(() => {
  void loadHistory()
  document.addEventListener('keydown', escape)
})
onUnmounted(() => {
  alive = false
  ++followSequence
  clearTimeout(timer)
  document.removeEventListener('keydown', escape)
})
</script>

<template>
  <div class="dialog-backdrop" @click.self="close">
    <section
      class="dialog export-dialog"
      role="dialog"
      aria-modal="true"
      aria-labelledby="export-title"
    >
      <header class="dialog-header">
        <div>
          <span class="eyebrow">EXPORT ORIGINALS</span>
          <h2 id="export-title">批量导出影像</h2>
        </div>
        <button class="icon-button" aria-label="关闭导出" :disabled="busy" @click="close">×</button>
      </header>
      <div class="tabs">
        <button :class="{ active: tab === 'export' }" :disabled="busy" @click="tab = 'export'">
          导出所选 {{ photos.length ? `(${photos.length.toLocaleString()})` : '' }}
        </button>
        <button :class="{ active: tab === 'history' }" :disabled="busy" @click="showHistory">
          导出记录
        </button>
      </div>
      <div class="dialog-body">
        <template v-if="tab === 'export' && !job">
          <p class="export-summary">
            {{
              photos.length
                ? `已选 ${photos.length.toLocaleString()} 张影像`
                : '先在地图上点击“框选影像”，拖动框出照片点位，再右键批量导出。'
            }}
          </p>
          <label class="field-label" for="export-destination">导出到</label>
          <div class="export-destination">
            <input
              id="export-destination"
              v-model="destination"
              :disabled="busy"
              placeholder="目标文件夹的完整路径，例如 D:\影像导出"
            />
            <button class="secondary" :disabled="busy" @click="chooseFolder">
              {{ picking ? '请查看文件夹选择窗口…' : '选择文件夹' }}
            </button>
          </div>
          <label class="field-label" for="export-folder">新文件夹名称</label>
          <input
            id="export-folder"
            v-model="folderName"
            maxlength="100"
            :disabled="busy"
            placeholder="留空则按导出时间命名"
          />
          <p class="help">
            将在目标位置新建文件夹，保留照片的子目录和原始文件内容。同名文件自动编号；原图缺失的影像会跳过，并写入导出报告。
          </p>
        </template>
        <template v-if="tab === 'history' && !job">
          <div v-if="!history.length" class="list-empty">暂无导出记录</div>
          <button
            v-for="entry in history"
            :key="entry.id"
            class="export-history-item"
            @click="follow(entry.id)"
          >
            <span>
              <strong>
                {{ exportLabels[entry.state] }} · {{ entry.copied }} / {{ entry.total }} 张
              </strong>
              <small>{{ photoTime(entry.created_at) }}</small>
            </span>
            <small>{{ entry.target_dir }}</small>
          </button>
        </template>
        <div v-if="job" class="export-result" aria-live="polite">
          <div class="export-progress-heading">
            <strong>{{ exportLabels[job.state] }}</strong>
            <span>{{ job.processed.toLocaleString() }} / {{ job.total.toLocaleString() }}</span>
          </div>
          <progress :value="progress" max="100" :aria-label="`导出进度 ${progress}%`" />
          <div class="export-counts">
            <span>
              已复制
              <strong>{{ job.copied }}</strong>
            </span>
            <span>
              原图缺失
              <strong>{{ job.missing }}</strong>
            </span>
            <span>
              失败
              <strong>{{ job.errors }}</strong>
            </span>
          </div>
          <label class="field-label">保存位置</label>
          <p class="export-path">{{ job.target_dir }}</p>
          <p class="help">{{ job.message || '正在复制原图，请保持服务运行…' }}</p>
          <div v-if="job.issues?.length" class="export-issues">
            <p v-for="(issue, index) in job.issues" :key="index">
              <strong>{{ issue.filename }}</strong>
              <span>{{ issue.reason }}</span>
            </p>
            <small v-if="job.missing + job.errors > 100">
              此处显示前 100 条，完整结果见目标文件夹中的“导出报告.json”。
            </small>
          </div>
        </div>
        <p v-if="picking" class="help" role="status">
          正在打开系统文件夹窗口；选择后会自动填入路径，取消则保留当前路径。
        </p>
        <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
      </div>
      <footer class="dialog-footer">
        <button v-if="job && !busy" class="secondary" @click="showHistory">返回导出记录</button>
        <button class="secondary" :disabled="busy" @click="close">
          {{ busy ? '请等待导出完成' : '关闭' }}
        </button>
        <button
          v-if="tab === 'export' && !job"
          class="primary"
          :disabled="busy || !photos.length || !destination.trim()"
          @click="start"
        >
          {{ submitting ? '正在开始…' : '复制到新文件夹' }}
        </button>
      </footer>
    </section>
  </div>
</template>
