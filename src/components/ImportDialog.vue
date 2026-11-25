<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api, message } from '../api'
import type { ImportJob } from '../types'
import { jobLabels } from '../types'

const props = defineProps<{ legacyRoot: string }>()
const emit = defineEmits<{ close: []; imported: [] }>()
const tab = ref<'photos' | 'legacy' | 'history'>('photos')
const jsonInput = ref<HTMLInputElement>()
const files = ref<string[]>([])
const sourceRoot = ref('')
const jsonFile = ref<File | null>(null)
const root = ref(props.legacyRoot)
const busy = ref(false)
const error = ref('')
const phase = ref('')
const job = ref<ImportJob | null>(null)
const history = ref<ImportJob[]>([])
let alive = true
const progress = computed(() =>
  job.value?.total ? Math.round((job.value.processed / job.value.total) * 100) : 0,
)
const ready = computed(() =>
  tab.value === 'photos'
    ? files.value.length > 0 || !!sourceRoot.value.trim()
    : jsonFile.value !== null,
)

function editedRoot() {
  files.value = []
  job.value = null
}

async function chooseSource(kind: 'files' | 'folder') {
  busy.value = true
  phase.value = 'pick'
  error.value = ''
  try {
    if (kind === 'files') {
      const result = await api.importPhotos(sourceRoot.value)
      if (result.paths.length) {
        files.value = result.paths
        sourceRoot.value = ''
      }
    } else {
      const result = await api.exportFolder(sourceRoot.value)
      if (result.path) {
        sourceRoot.value = result.path
        files.value = []
      }
    }
    job.value = null
  } catch (cause) {
    error.value = message(cause)
  } finally {
    busy.value = false
    phase.value = ''
  }
}
function changeJson(event: Event) {
  jsonFile.value = (event.target as HTMLInputElement).files?.[0] ?? null
  job.value = null
  error.value = ''
  if (jsonFile.value && jsonFile.value.size > 128 * 1024 * 1024) {
    jsonFile.value = null
    error.value = '索引文件不能超过 128 MB'
  }
}

async function loadHistory() {
  try {
    history.value = (await api.jobs()).items
  } catch (cause) {
    error.value = message(cause)
  }
}

async function follow(id: string) {
  phase.value = 'processing'
  while (alive) {
    const current = await api.job(id)
    job.value = current
    if (!['queued', 'running'].includes(current.state)) break
    await new Promise((resolve) => setTimeout(resolve, 600))
  }
  if (alive) {
    emit('imported')
    await loadHistory()
  }
}

async function start() {
  if (busy.value || !ready.value) return
  busy.value = true
  error.value = ''
  job.value = null
  try {
    let id: string
    if (tab.value === 'photos') {
      phase.value = 'processing'
      id = (await api.localImport(sourceRoot.value.trim(), files.value)).id
    } else {
      if (!jsonFile.value) return
      phase.value = 'processing'
      id = (await api.migrate(jsonFile.value, root.value)).id
    }
    await follow(id)
  } catch (cause) {
    error.value = message(cause)
    await loadHistory()
  } finally {
    busy.value = false
  }
}

async function inspect(entry: ImportJob) {
  busy.value = true
  error.value = ''
  try {
    await follow(entry.id)
  } catch (cause) {
    error.value = message(cause)
  } finally {
    busy.value = false
  }
}

async function cancel(entry: ImportJob) {
  try {
    await api.cancelImport(entry.id)
    await loadHistory()
  } catch (cause) {
    error.value = message(cause)
  }
}

function keydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && !busy.value) emit('close')
}

function switchTab(value: 'photos' | 'legacy' | 'history') {
  tab.value = value
  job.value = null
  if (value === 'history') void loadHistory()
}
onMounted(() => {
  void loadHistory()
  window.addEventListener('keydown', keydown)
})
onUnmounted(() => {
  alive = false
  window.removeEventListener('keydown', keydown)
})
</script>

<template>
  <div class="dialog-backdrop" @click.self="!busy && emit('close')">
    <section
      class="dialog import-dialog"
      role="dialog"
      aria-modal="true"
      aria-labelledby="import-title"
    >
      <header class="dialog-header">
        <div>
          <span class="eyebrow">BUILD YOUR LIBRARY</span>
          <h2 id="import-title">导入影像</h2>
        </div>
        <button
          class="icon-button"
          :disabled="busy"
          aria-label="关闭导入面板"
          @click="emit('close')"
        >
          ×
        </button>
      </header>
      <div class="tabs">
        <button :class="{ active: tab === 'photos' }" :disabled="busy" @click="switchTab('photos')">
          照片导入
        </button>
        <button :class="{ active: tab === 'legacy' }" :disabled="busy" @click="switchTab('legacy')">
          旧版 JSON 迁移
        </button>
        <button
          :class="{ active: tab === 'history' }"
          :disabled="busy"
          @click="switchTab('history')"
        >
          导入记录
        </button>
      </div>
      <div class="dialog-body">
        <template v-if="tab === 'photos'">
          <div class="dropzone">
            <span class="upload-symbol">↥</span>
            <strong>关联本机原图，只建立索引</strong>
            <p>支持 JPG、JPEG、PNG、WebP，自动提取坐标与拍摄时间</p>
            <div>
              <button class="secondary" :disabled="busy" @click="chooseSource('files')">
                选择照片
              </button>
              <button class="secondary" :disabled="busy" @click="chooseSource('folder')">
                选择文件夹
              </button>
            </div>
          </div>
          <label class="field-label" style="margin-top: 20px">
            原图文件夹（也可直接填写完整路径，将包含子文件夹）
            <input
              v-model="sourceRoot"
              :disabled="busy"
              placeholder="例如 D:\航测照片"
              @input="editedRoot"
            />
          </label>
          <div v-if="files.length" class="selection-summary">
            <strong>{{ files.length.toLocaleString() }} 张照片</strong>
            <span>仅保存原图路径和压缩缩略图</span>
            <small>{{ files[0] }}{{ files.length > 1 ? ` 等 ${files.length} 个文件` : '' }}</small>
          </div>
          <p class="help">
            不复制原图。预览和导出直接读取原位置，请保留原文件及路径。相同内容重新导入可关联新路径；核对一致后会释放旧版原图副本。无
            GPS 的照片也可建立索引。
          </p>
        </template>
        <template v-else-if="tab === 'legacy'">
          <div class="migration-note">
            <strong>继续使用你已有的影像成果</strong>
            <p>
              直接读取旧版索引中的坐标、高程与时间，无需重新解析
              EXIF。只记录原图的原始位置，不复制图片。
            </p>
          </div>
          <input
            ref="jsonInput"
            class="visually-hidden"
            type="file"
            accept=".json,application/json"
            @change="changeJson"
          />
          <label class="field-label">
            1. 选择旧版索引文件（最大 128 MB）
            <button class="file-picker secondary" :disabled="busy" @click="jsonInput?.click()">
              {{ jsonFile?.name || '选择 public/img-index/index.json' }}
            </button>
          </label>
          <label class="field-label">
            2. 旧项目或 public 目录（本机路径）
            <input v-model="root" :disabled="busy" placeholder="例如 D:\航测项目\public" />
          </label>
          <p class="help">
            该目录下应有 imgs 文件夹，可同时包含
            thumbs。目录为空或照片缺失时仍可导入元数据，之后再次迁移可补齐。JSON 本身不包含原图。
          </p>
        </template>
        <template v-else>
          <div v-if="!history.length" class="list-empty">还没有导入记录</div>
          <div v-for="entry in history" :key="entry.id" class="history-entry">
            <div>
              <strong>{{ entry.name }}</strong>
              <small>
                {{ new Date(entry.created_at).toLocaleString('zh-CN') }} · {{ entry.total }} 张
              </small>
              <span>
                {{ jobLabels[entry.state] }} · 新增 {{ entry.imported }} · 重复
                {{ entry.duplicates }} · 失败 {{ entry.errors }}
              </span>
            </div>
            <div class="history-actions">
              <button class="quiet" :disabled="busy" @click="inspect(entry)">查看</button>
              <button
                v-if="entry.state === 'uploading'"
                class="quiet"
                :disabled="busy"
                @click="cancel(entry)"
              >
                取消上传
              </button>
            </div>
          </div>
        </template>
        <div v-if="busy || job" class="import-progress" aria-live="polite">
          <div class="progress-heading">
            <strong>
              {{
                phase === 'pick'
                  ? '请在系统窗口中选择原图'
                  : job
                    ? jobLabels[job.state]
                    : '正在建立索引…'
              }}
            </strong>
            <span>{{ progress }}%</span>
          </div>
          <div class="progress-track"><div :style="{ width: `${progress}%` }" /></div>
          <small v-if="job">已处理 {{ job.processed }} / {{ job.total }} 张</small>
          <div v-if="job" class="result-counts">
            <span>
              新增
              <b>{{ job.imported }}</b>
            </span>
            <span>
              重复 / 补齐
              <b>{{ job.duplicates }}</b>
            </span>
            <span>
              无 GPS
              <b>{{ job.without_gps }}</b>
            </span>
            <span>
              原图缺失
              <b>{{ job.missing }}</b>
            </span>
            <span>
              失败
              <b>{{ job.errors }}</b>
            </span>
          </div>
          <p v-if="job?.message" class="help">{{ job.message }}</p>
        </div>
        <div v-if="job?.issues?.length" class="issue-list">
          <strong>处理提示（{{ job.issueCount }} 条，最多显示前 100 条）</strong>
          <p v-for="(issue, index) in job.issues" :key="index">
            <b>{{ issue.filename }}</b>
            <span>{{ issue.reason }}</span>
          </p>
        </div>
        <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
      </div>
      <footer class="dialog-footer">
        <span class="help">
          {{ busy ? '处理期间请保持程序运行' : '仅保存索引和小缩略图，原图留在原位置' }}
        </span>
        <div>
          <button class="quiet" :disabled="busy" @click="emit('close')">
            {{ job ? '完成' : '关闭' }}
          </button>
          <button
            v-if="tab !== 'history'"
            class="primary"
            :disabled="!ready || busy"
            @click="start"
          >
            {{ busy ? '正在导入…' : tab === 'legacy' ? '迁移到 SQLite' : '开始导入' }}
          </button>
        </div>
      </footer>
    </section>
  </div>
</template>
