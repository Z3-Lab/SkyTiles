<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { version } from '../package.json'
import { api, message } from './api'
import type { Config, LibraryInfo, Photo, PhotoReference, Stats } from './types'
import { photoTime } from './types'
import GlobeView from './components/GlobeView.vue'
import ImportDialog from './components/ImportDialog.vue'
import PhotoPreview from './components/PhotoPreview.vue'
import ConfirmRemoval from './components/ConfirmRemoval.vue'
import ExportDialog from './components/ExportDialog.vue'

const config = ref<Config>({ cesiumToken: '', dataDir: '', legacyRoot: '', exportRoot: '' })
const exportSelection = ref<PhotoReference[] | null>(null)
const stats = ref<Stats>({
  total: 0,
  withGps: 0,
  withoutGps: 0,
  missing: 0,
  west: null,
  east: null,
  south: null,
  north: null,
})
const photos = ref<Photo[]>([])
const total = ref(0)
const page = ref(1)
const search = ref('')
const query = ref('')
const status = ref('all')
const loading = ref(true)
const error = ref('')
const preview = ref<Photo | null>(null)
const importOpen = ref(false)
const settingsOpen = ref(false)
const tokenDraft = ref('')
const saving = ref(false)
const libraryInfo = ref<LibraryInfo | null>(null)
const settingsError = ref('')
const checkingLibrary = ref(false)
const removal = ref<{ kind: 'photo'; photo: Photo } | { kind: 'library'; count: number } | null>(
  null,
)
const removing = ref(false)
const removalError = ref('')
const notice = ref('')
const stopped = ref(false)
const revision = ref(0)
const globe = ref<InstanceType<typeof GlobeView>>()
let searchTimer: ReturnType<typeof setTimeout> | undefined
let controller: AbortController | undefined
let refreshSequence = 0

const filterQuery = computed(() =>
  new URLSearchParams({ q: query.value, status: status.value }).toString(),
)
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / 40)))

async function refresh() {
  const sequence = ++refreshSequence
  controller?.abort()
  controller = new AbortController()
  loading.value = true
  error.value = ''
  try {
    const [result, summary] = await Promise.all([
      api.photos(`?${filterQuery.value}&page=${page.value}`, controller.signal),
      api.stats(`?${filterQuery.value}`),
    ])
    if (sequence !== refreshSequence) return
    photos.value = result.items
    total.value = result.total
    stats.value = summary
    const lastPage = Math.max(1, Math.ceil(result.total / 40))
    if (page.value > lastPage) page.value = lastPage
  } catch (cause) {
    if (sequence === refreshSequence) error.value = message(cause)
  } finally {
    if (sequence === refreshSequence) loading.value = false
  }
}

watch(search, (value) => {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    query.value = value
  }, 300)
})
watch([query, status], () => {
  page.value = 1
  void refresh()
})
watch(page, () => {
  void refresh()
})

async function imported() {
  page.value = 1
  revision.value++
  await refresh()
  globe.value?.fitLibrary()
}

function refreshLibrary() {
  revision.value++
  void refresh()
}

function locatePreview() {
  if (preview.value) globe.value?.locate(preview.value)
  preview.value = null
}

function openSettings() {
  tokenDraft.value = config.value.cesiumToken
  settingsOpen.value = true
  void loadLibraryInfo()
}

async function loadLibraryInfo() {
  checkingLibrary.value = true
  settingsError.value = ''
  try {
    libraryInfo.value = await api.libraryInfo()
  } catch (cause) {
    settingsError.value = message(cause)
  } finally {
    checkingLibrary.value = false
  }
}

function askRemove(photo: Photo) {
  removalError.value = ''
  removal.value = { kind: 'photo', photo }
}

async function askClearLibrary() {
  await loadLibraryInfo()
  if (settingsError.value || !libraryInfo.value) return
  if (libraryInfo.value.activeExports) {
    settingsError.value = '正在导出影像，请等待复制完成后再清空。'
    return
  }
  if (libraryInfo.value.activeImports) {
    settingsError.value = '正在建立索引或压缩缩略图，请等待任务完成后重试。'
    return
  }
  removalError.value = ''
  removal.value = { kind: 'library', count: libraryInfo.value.photoCount }
}

async function confirmRemoval() {
  const target = removal.value
  if (!target || removing.value) return
  removing.value = true
  removalError.value = ''
  try {
    const result =
      target.kind === 'photo'
        ? await api.removePhoto(target.photo)
        : await api.clearLibrary(target.count)
    if (target.kind === 'library') {
      clearTimeout(searchTimer)
      search.value = ''
      query.value = ''
      status.value = 'all'
      page.value = 1
      preview.value = null
    } else if (preview.value?.id === target.photo.id) preview.value = null
    revision.value++
    await refresh()
    await loadLibraryInfo()
    removal.value = null
    notice.value = result.cleanupPending
      ? '影像记录已移除。部分文件正被占用，将在下次启动时继续清理。'
      : target.kind === 'photo'
        ? '已移除该影像。'
        : `已清空影像库，共移除 ${result.removed} 张影像及导入、导出记录。`
  } catch (cause) {
    removalError.value = message(cause)
  } finally {
    removing.value = false
  }
}

async function saveSettings() {
  saving.value = true
  try {
    await api.saveConfig(tokenDraft.value.trim())
    config.value = await api.config()
    settingsOpen.value = false
  } catch (cause) {
    error.value = message(cause)
  } finally {
    saving.value = false
  }
}

async function shutdown() {
  if (!window.confirm('退出 SkyTiles 本地服务？数据会保留，双击 EXE 可重新打开。')) return
  try {
    await api.shutdown()
    stopped.value = true
  } catch (cause) {
    error.value = message(cause)
  }
}

onMounted(async () => {
  try {
    config.value = await api.config()
  } catch (cause) {
    error.value = message(cause)
  }
  await refresh()
})
onUnmounted(() => {
  controller?.abort()
  clearTimeout(searchTimer)
})
</script>

<template>
  <div v-if="stopped" class="stopped">
    <div class="brand-mark">S</div>
    <h1>SkyTiles 已退出</h1>
    <p>数据已保留。下次打开司图即可继续浏览。</p>
  </div>
  <div v-else class="workspace">
    <header class="topbar">
      <div class="brand">
        <span class="brand-mark">S</span>
        <div>
          <strong>
            SkyTiles
            <span>司图</span>
          </strong>
          <small>让每一张影像，都有迹可循</small>
        </div>
      </div>
      <div class="top-actions">
        <span class="local-badge">
          <i />
          本地影像库
        </span>
        <button class="quiet" @click="openSettings">设置</button>
        <button class="quiet" @click="shutdown">退出</button>
        <button class="secondary" @click="exportSelection = []">导出记录</button>
        <button class="primary" @click="importOpen = true">＋ 导入影像</button>
      </div>
    </header>
    <div v-if="error" class="error-banner" role="alert">
      {{ error }}
      <button @click="refreshLibrary">重试</button>
    </div>
    <div v-if="notice" class="notice-banner" role="status">
      {{ notice }}
      <button class="quiet" aria-label="关闭提示" @click="notice = ''">×</button>
    </div>
    <main class="main-layout">
      <aside class="library">
        <div class="library-heading">
          <div>
            <span class="eyebrow">PHOTO LIBRARY</span>
            <h1>
              影像库
              <span>{{ stats.total.toLocaleString() }}</span>
            </h1>
          </div>
          <button class="icon-button" title="刷新影像库" @click="refreshLibrary">↻</button>
        </div>
        <div class="stat-row">
          <div>
            <strong>{{ stats.withGps.toLocaleString() }}</strong>
            <span>已定位</span>
          </div>
          <div>
            <strong>{{ stats.withoutGps.toLocaleString() }}</strong>
            <span>无 GPS</span>
          </div>
          <div>
            <strong>{{ stats.missing.toLocaleString() }}</strong>
            <span>原图缺失</span>
          </div>
        </div>
        <div class="library-filters">
          <input
            v-model="search"
            type="search"
            placeholder="搜索文件名或相对路径"
            aria-label="搜索影像"
          />
          <select v-model="status" aria-label="筛选照片">
            <option value="all">全部影像</option>
            <option value="gps">有 GPS</option>
            <option value="no_gps">无 GPS</option>
            <option value="missing">原图缺失</option>
          </select>
        </div>
        <div class="photo-list" :aria-busy="loading">
          <div v-if="loading && !photos.length" class="list-empty">
            <span class="spinner" />
            正在读取影像库…
          </div>
          <div v-else-if="!photos.length" class="list-empty">
            <span class="empty-symbol">▧</span>
            <strong>{{ query || status !== 'all' ? '没有匹配的影像' : '从第一组航拍开始' }}</strong>
            <p>导入照片，或从旧版 JSON 索引迁移影像库。</p>
            <button class="primary" @click="importOpen = true">导入影像</button>
          </div>
          <article v-for="photo in photos" :key="photo.id" class="photo-card">
            <button
              class="photo-open"
              :aria-label="`查看 ${photo.filename}`"
              @click="preview = photo"
            >
              <img
                v-if="photo.thumbUrl"
                :src="photo.thumbUrl"
                loading="lazy"
                alt=""
                @error="($event.target as HTMLImageElement).style.visibility = 'hidden'"
              />
              <span v-else>▧</span>
            </button>
            <div class="photo-info">
              <button class="photo-title" :title="photo.relativePath" @click="preview = photo">
                {{ photo.filename }}
              </button>
              <small>{{ photoTime(photo.capturedAt) }}</small>
              <div class="photo-tags">
                <span :class="['tag', photo.latitude === null ? 'muted' : 'located']">
                  {{ photo.latitude === null ? '无 GPS' : '已定位' }}
                </span>
                <span v-if="photo.missing" class="tag warning">原图缺失</span>
              </div>
            </div>
            <div class="photo-actions">
              <button
                class="locate-button"
                :disabled="photo.latitude === null"
                title="定位到地图"
                @click="globe?.locate(photo)"
              >
                ⌖
              </button>
              <button
                class="remove-button"
                type="button"
                :disabled="removing"
                :aria-label="`移除 ${photo.filename}`"
                title="移除此影像"
                @click="askRemove(photo)"
              >
                <svg
                  viewBox="0 0 24 24"
                  width="17"
                  height="17"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="1.6"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                  aria-hidden="true"
                >
                  <path d="M3 6h18M9 6V4h6v2M5 6l1 14h12l1-14M10 10v6M14 10v6" />
                </svg>
              </button>
            </div>
          </article>
        </div>
        <div class="pagination">
          <span>{{ total.toLocaleString() }} 张 · {{ page }} / {{ pageCount }} 页</span>
          <div>
            <button :disabled="page <= 1 || loading" aria-label="上一页" @click="page--">‹</button>
            <button :disabled="page >= pageCount || loading" aria-label="下一页" @click="page++">
              ›
            </button>
          </div>
        </div>
      </aside>
      <section class="map-pane">
        <GlobeView
          ref="globe"
          :filter-query="filterQuery"
          :revision="revision"
          :token="config.cesiumToken"
          @select="preview = $event"
          @export="exportSelection = $event"
        />
        <div class="map-caption">
          <span class="live-dot" />
          三维空间浏览
          <span>悬停预览 · 点击查看 · 滚轮缩放</span>
        </div>
      </section>
    </main>
    <footer class="statusbar">
      <span>SQLite · 本地存储</span>
      <span :title="config.dataDir">{{ config.dataDir || '正在连接本地服务…' }}</span>
      <span>SkyTiles {{ version }}</span>
    </footer>
    <ImportDialog
      v-if="importOpen"
      :legacy-root="config.legacyRoot"
      @close="importOpen = false"
      @imported="imported"
    />
    <PhotoPreview v-if="preview" :photo="preview" @close="preview = null" @locate="locatePreview" />
    <ExportDialog
      v-if="exportSelection"
      :photos="exportSelection"
      :export-root="config.exportRoot"
      @close="exportSelection = null"
    />
    <div v-if="settingsOpen" class="dialog-backdrop" @click.self="settingsOpen = false">
      <section
        class="dialog settings-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="settings-title"
      >
        <header class="dialog-header">
          <div>
            <span class="eyebrow">PREFERENCES</span>
            <h2 id="settings-title">地图与存储</h2>
          </div>
          <button class="icon-button" aria-label="关闭设置" @click="settingsOpen = false">×</button>
        </header>
        <div class="dialog-body">
          <div class="field-label">
            <div class="token-field-heading">
              <label for="cesium-token">Cesium ion Access Token</label>
              <a
                class="secondary token-link"
                href="https://ion.cesium.com/tokens"
                target="_blank"
                rel="noopener noreferrer"
              >
                获取 Cesium Token ↗
              </a>
            </div>
            <input
              id="cesium-token"
              v-model="tokenDraft"
              type="password"
              autocomplete="off"
              placeholder="可选，用于在线卫星底图和地形"
            />
          </div>
          <p class="help">
            未配置时使用内置基础地球。在线卫星影像与地形需要网络及有效的 Cesium Token。 点击“获取
            Cesium Token”后，在官方页面登录或注册账号，创建 Token 并复制到上方保存。
          </p>
          <label class="field-label">
            影像库位置
            <code class="storage-path">{{ config.dataDir }}</code>
          </label>
          <p class="help">升级时保留整个 data 目录。备份请先退出程序，再复制该目录。</p>
          <p class="help">
            新导入只保存原图路径与最长边 128 像素的小缩略图。备份索引时也请另行备份原图目录。
          </p>
          <p v-if="libraryInfo?.managedPhotos" class="help">
            当前还有 {{ libraryInfo.managedPhotos.toLocaleString() }} 张旧版原图副本。
            在“导入影像”中重新选择原图目录，核对内容一致后会自动关联原位置并释放副本。
            已有缩略图会在后台自动压缩，可在导入记录中查看进度。
          </p>
          <div class="library-danger-zone">
            <h3>清空影像库</h3>
            <p>
              删除全部影像记录、图库中的原图副本、缩略图和导入、导出记录。保留已导出的文件、Cesium
              Token 等设置，以及导入来源目录中的文件。
            </p>
            <small v-if="libraryInfo">
              整个影像库共 {{ libraryInfo.photoCount.toLocaleString() }} 张影像、{{
                libraryInfo.batchCount
              }}
              条导入记录，与左侧筛选无关。
            </small>
            <button
              class="danger"
              :disabled="checkingLibrary || removing || saving"
              @click="askClearLibrary"
            >
              {{ checkingLibrary ? '正在读取影像库…' : '清空影像库全部数据' }}
            </button>
            <p v-if="settingsError" class="inline-error" role="alert">{{ settingsError }}</p>
          </div>
        </div>
        <footer class="dialog-footer">
          <button class="quiet" @click="settingsOpen = false">取消</button>
          <button class="primary" :disabled="saving" @click="saveSettings">
            {{ saving ? '保存中…' : '保存设置' }}
          </button>
        </footer>
      </section>
    </div>
    <ConfirmRemoval
      v-if="removal"
      :title="removal.kind === 'photo' ? '移除这张影像？' : '清空整个影像库？'"
      :description="
        removal.kind === 'photo'
          ? `将永久移除“${removal.photo.relativePath}”的影像记录、图库中的原图副本和缩略图。导入来源文件保留，此操作不可撤销。`
          : `将永久删除整个影像库的 ${removal.count} 张影像、原图副本、缩略图和全部导入、导出记录，不受搜索或筛选影响。已导出的文件、Cesium Token 等设置及导入来源文件保留，此操作不可撤销。`
      "
      :confirm-label="removal.kind === 'photo' ? '确认移除' : '确认清空全部数据'"
      :confirmation-text="removal.kind === 'library' ? '清空' : undefined"
      :busy="removing"
      :error="removalError"
      @cancel="removal = null"
      @confirm="confirmRemoval"
    />
  </div>
</template>
