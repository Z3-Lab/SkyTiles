<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import {
  BoundingSphere,
  Cartesian2,
  Cartesian3,
  Color,
  EllipsoidTerrainProvider,
  Occluder,
  HeadingPitchRange,
  ImageryLayer,
  Ion,
  LabelCollection,
  Math as CesiumMath,
  PointPrimitiveCollection,
  Rectangle,
  ScreenSpaceEventHandler,
  ScreenSpaceEventType,
  SceneTransforms,
  TileMapServiceImageryProvider,
  Viewer,
  buildModuleUrl,
  createWorldImageryAsync,
  createWorldTerrainAsync,
} from 'cesium'
import 'cesium/Build/Cesium/Widgets/widgets.css'
import { api, message } from '../api'
import { cameraViewQuery } from '../mapView'
import type { MapPoint, Photo, PhotoReference, SelectionPoint } from '../types'

const props = defineProps<{ filterQuery: string; revision: number; token: string }>()
const emit = defineEmits<{ select: [photo: Photo]; export: [photos: PhotoReference[]] }>()
const container = ref<HTMLElement>()
const error = ref('')
const tileMessage = ref('内置基础地球')
const loading = ref(false)
const pointCount = ref(0)
const clustered = ref(false)
const hover = ref<Photo | null>(null)
const hoverX = ref(0)
const hoverY = ref(0)
const boxMode = ref(false)
const selecting = ref(false)
const selection = ref<SelectionPoint[]>([])
const selectionMessage = ref('')
const selectionError = ref('')
const menu = ref<{ x: number; y: number } | null>(null)
const menuElement = ref<HTMLElement>()
const drag = ref<{ x: number; y: number; endX: number; endY: number; append: boolean } | null>(null)
const boxStyle = computed(() =>
  drag.value
    ? {
        left: `${Math.min(drag.value.x, drag.value.endX)}px`,
        top: `${Math.min(drag.value.y, drag.value.endY)}px`,
        width: `${Math.abs(drag.value.endX - drag.value.x)}px`,
        height: `${Math.abs(drag.value.endY - drag.value.y)}px`,
      }
    : {},
)
let selectedPoints: PointPrimitiveCollection | undefined
let selectionController: AbortController | undefined
let selectionSequence = 0
let cameraInput = true
let viewer: Viewer | undefined
let points: PointPrimitiveCollection | undefined
let labels: LabelCollection | undefined
let handler: ScreenSpaceEventHandler | undefined
let alive = true
let removeMove: (() => void) | undefined
let mapTimer: ReturnType<typeof setTimeout> | undefined
let hoverTimer: ReturnType<typeof setTimeout> | undefined
let mapController: AbortController | undefined
let hoverController: AbortController | undefined
let currentHover: number | null = null
let mapSequence = 0
let layerSequence = 0
const cache = new Map<number, Photo>()

function cancelBox() {
  ++selectionSequence
  selectionController?.abort()
  if (viewer && (boxMode.value || selecting.value))
    viewer.scene.screenSpaceCameraController.enableInputs = cameraInput
  boxMode.value = false
  selecting.value = false
  drag.value = null
  if (selection.value.length)
    selectionMessage.value = `已选 ${selection.value.length.toLocaleString()} 张 · 右键导出 · Shift 可追加框选`
}

function clearSelection() {
  cancelBox()
  selection.value = []
  selectedPoints?.removeAll()
  selectionMessage.value = ''
  selectionError.value = ''
  menu.value = null
  viewer?.scene.requestRender()
}

function startBox() {
  if (!viewer || selecting.value) return
  if (boxMode.value) {
    cancelBox()
    return
  }
  clearHover()
  menu.value = null
  viewer.camera.cancelFlight()
  cameraInput = viewer.scene.screenSpaceCameraController.enableInputs
  viewer.scene.screenSpaceCameraController.enableInputs = false
  boxMode.value = true
  selectionError.value = ''
  selectionMessage.value = ''
}

function position(event: PointerEvent) {
  const rect = container.value!.getBoundingClientRect()
  return {
    x: Math.max(0, Math.min(rect.width, event.clientX - rect.left)),
    y: Math.max(0, Math.min(rect.height, event.clientY - rect.top)),
  }
}

function beginDrag(event: PointerEvent) {
  if (event.button !== 0 || selecting.value) return
  const point = position(event)
  drag.value = { ...point, endX: point.x, endY: point.y, append: event.shiftKey }
  ;(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
}

function moveDrag(event: PointerEvent) {
  if (!drag.value) return
  const point = position(event)
  drag.value.endX = point.x
  drag.value.endY = point.y
}

async function endDrag(event: PointerEvent) {
  if (!drag.value || !viewer || event.button !== 0) return
  moveDrag(event)
  const box = { ...drag.value }
  if (Math.abs(box.endX - box.x) < 4 || Math.abs(box.endY - box.y) < 4) {
    drag.value = null
    return
  }
  const currentViewer = viewer
  const sequence = ++selectionSequence
  selectionController = new AbortController()
  selecting.value = true
  const query = viewportQuery()
  const selected = new Map(box.append ? selection.value.map((point) => [point.id, point]) : [])
  const ellipsoid = currentViewer.scene.globe.ellipsoid
  const occluder = new Occluder(
    new BoundingSphere(Cartesian3.ZERO, 1),
    ellipsoid.transformPositionToScaledSpace(currentViewer.camera.positionWC),
  )
  try {
    let next: number | null = 0
    while (next !== null) {
      query.set('after', String(next))
      const result = await api.selectionPoints(`?${query}`, selectionController.signal)
      if (!alive || sequence !== selectionSequence) return
      query.set('snapshot', String(result.snapshot))
      next = result.next
      for (const point of result.items) {
        const world = Cartesian3.fromDegrees(point.longitude, point.latitude, point.altitude ?? 0)
        const surface = Cartesian3.fromDegrees(
          point.longitude,
          point.latitude,
          Math.max(1, point.altitude ?? 0),
        )
        if (
          !occluder.isPointVisible(ellipsoid.transformPositionToScaledSpace(surface)) ||
          Cartesian3.dot(
            Cartesian3.subtract(world, currentViewer.camera.positionWC, new Cartesian3()),
            currentViewer.camera.directionWC,
          ) <= 0
        )
          continue
        const screen = SceneTransforms.worldToWindowCoordinates(currentViewer.scene, world)
        if (
          screen &&
          screen.x >= Math.min(box.x, box.endX) &&
          screen.x <= Math.max(box.x, box.endX) &&
          screen.y >= Math.min(box.y, box.endY) &&
          screen.y <= Math.max(box.y, box.endY)
        )
          selected.set(point.id, point)
        if (selected.size > 100000) throw new Error('单次最多选择 100,000 张，请缩小框选范围')
      }
    }
    selection.value = [...selected.values()]
    selectedPoints?.removeAll()
    for (const point of selection.value)
      selectedPoints?.add({
        position: Cartesian3.fromDegrees(point.longitude, point.latitude, point.altitude ?? 0),
        id: { ...point, count: 1 },
        color: Color.fromCssColorString('#ffd36b'),
        pixelSize: 12,
        outlineColor: Color.fromCssColorString('#ffffff'),
        outlineWidth: 2,
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      })
    selectionMessage.value = selection.value.length
      ? `已选 ${selection.value.length.toLocaleString()} 张 · 右键导出 · Shift 可追加框选`
      : '框内没有影像，请调整视角后重新框选'
    currentViewer.scene.requestRender()
  } catch (cause) {
    if (alive && sequence === selectionSequence) selectionError.value = message(cause)
  } finally {
    if (sequence === selectionSequence) cancelBox()
  }
}

function contextMenu(event: MouseEvent) {
  if (selecting.value) return
  cancelBox()
  clearHover()
  const rect = container.value!.getBoundingClientRect()
  menu.value = {
    x: Math.max(4, Math.min(event.clientX - rect.left, rect.width - 230)),
    y: Math.max(4, Math.min(event.clientY - rect.top, rect.height - 195)),
  }
}

function exportSelected() {
  menu.value = null
  if (selection.value.length)
    emit(
      'export',
      selection.value.map(({ id, version }) => ({ id, version })),
    )
}

function exportHistory() {
  menu.value = null
  emit('export', [])
}

function dismissMenu(event: PointerEvent) {
  if (!menuElement.value?.contains(event.target as Node)) menu.value = null
}

function escape(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    cancelBox()
    menu.value = null
  }
}

function viewportQuery() {
  return viewer
    ? cameraViewQuery(viewer.camera, props.filterQuery)
    : new URLSearchParams(props.filterQuery)
}

async function photo(id: number, signal?: AbortSignal) {
  const cached = cache.get(id)
  if (cached) return cached
  const result = await api.photo(id, signal)
  if (cache.size > 100) cache.clear()
  cache.set(id, result)
  return result
}

function clearHover() {
  clearTimeout(hoverTimer)
  hoverController?.abort()
  currentHover = null
  hover.value = null
}

async function refreshPoints() {
  if (!viewer || !points || !labels) return
  const sequence = ++mapSequence
  mapController?.abort()
  mapController = new AbortController()
  loading.value = true
  const query = viewportQuery()
  try {
    const result = await api.points(`?${query}`, mapController.signal)
    if (!alive || sequence !== mapSequence) return
    points.removeAll()
    labels.removeAll()
    clearHover()
    for (const point of result.items) {
      const position = Cartesian3.fromDegrees(point.longitude, point.latitude, point.altitude ?? 0)
      points.add({
        position,
        id: point,
        color: Color.fromCssColorString(point.id === null ? '#e9b55e' : '#4de0c3'),
        pixelSize: point.id === null ? 18 : 8,
        outlineWidth: 2,
        outlineColor: Color.fromCssColorString('#102c39'),
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      })
      if (point.id === null)
        labels.add({
          position,
          text: point.count.toLocaleString(),
          font: '12px sans-serif',
          pixelOffset: new Cartesian2(0, -22),
          fillColor: Color.WHITE,
          outlineWidth: 3,
          outlineColor: Color.BLACK,
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
        })
    }
    pointCount.value = result.total
    clustered.value = result.clustered
    error.value = ''
    viewer.scene.requestRender()
  } catch (cause) {
    if (alive && sequence === mapSequence) error.value = message(cause)
  } finally {
    if (sequence === mapSequence) loading.value = false
  }
}

function queueRefresh() {
  clearTimeout(mapTimer)
  mapTimer = setTimeout(() => {
    void refreshPoints()
  }, 120)
}

async function fitLibrary() {
  cancelBox()
  try {
    const stats = await api.stats(`?${props.filterQuery}`)
    if (
      !alive ||
      !viewer ||
      boxMode.value ||
      selecting.value ||
      stats.west === null ||
      stats.east === null ||
      stats.south === null ||
      stats.north === null
    )
      return
    const width = stats.east - stats.west
    const height = stats.north - stats.south
    if (width < 0.001 && height < 0.001) {
      viewer.camera.flyTo({
        destination: Cartesian3.fromDegrees(stats.west, stats.south, 1800),
        duration: 1.2,
      })
    } else {
      viewer.camera.flyTo({
        destination: Rectangle.fromDegrees(
          Math.max(-180, stats.west - Math.max(width * 0.15, 0.002)),
          Math.max(-90, stats.south - Math.max(height * 0.15, 0.002)),
          Math.min(180, stats.east + Math.max(width * 0.15, 0.002)),
          Math.min(90, stats.north + Math.max(height * 0.15, 0.002)),
        ),
        duration: 1.2,
      })
    }
  } catch (cause) {
    if (alive) error.value = message(cause)
  }
}

function locate(item: Photo) {
  if (!viewer || item.latitude === null || item.longitude === null) return
  cancelBox()
  viewer.camera.flyToBoundingSphere(
    new BoundingSphere(
      Cartesian3.fromDegrees(item.longitude, item.latitude, item.altitude ?? 0),
      80,
    ),
    { duration: 1, offset: new HeadingPitchRange(0, CesiumMath.toRadians(-65), 1200) },
  )
}

async function configureLayers() {
  if (!viewer) return
  const sequence = ++layerSequence
  const token = props.token
  viewer.terrainProvider = new EllipsoidTerrainProvider()
  tileMessage.value = token ? '正在连接在线地图…' : '内置基础地球'
  try {
    const provider = await TileMapServiceImageryProvider.fromUrl(
      buildModuleUrl('Assets/Textures/NaturalEarthII'),
    )
    if (!alive || sequence !== layerSequence || !viewer) return
    viewer.imageryLayers.removeAll()
    viewer.imageryLayers.addImageryProvider(provider)
    if (token) {
      Ion.defaultAccessToken = token
      const [imagery, terrain] = await Promise.all([
        createWorldImageryAsync(),
        createWorldTerrainAsync(),
      ])
      if (!alive || sequence !== layerSequence || !viewer) return
      viewer.imageryLayers.removeAll()
      viewer.imageryLayers.add(new ImageryLayer(imagery))
      viewer.terrainProvider = terrain
      tileMessage.value = '在线卫星影像与地形'
    }
    viewer.scene.requestRender()
  } catch {
    if (alive && sequence === layerSequence)
      tileMessage.value = '在线地图不可用，使用基础地球；请检查 Token 和网络'
  }
}

watch(
  () => props.token,
  () => {
    void configureLayers()
  },
)
watch(
  () => [props.filterQuery, props.revision],
  () => {
    cache.clear()
    clearSelection()
    clearHover()
    queueRefresh()
  },
)

onMounted(() => {
  if (!container.value) return
  try {
    viewer = new Viewer(container.value, {
      baseLayer: false,
      baseLayerPicker: false,
      geocoder: false,
      animation: false,
      timeline: false,
      sceneModePicker: false,
      navigationHelpButton: false,
      fullscreenButton: false,
      homeButton: false,
      infoBox: false,
      selectionIndicator: false,
      requestRenderMode: true,
      maximumRenderTimeChange: Number.POSITIVE_INFINITY,
    })
    points = viewer.scene.primitives.add(new PointPrimitiveCollection())
    labels = viewer.scene.primitives.add(new LabelCollection())
    selectedPoints = viewer.scene.primitives.add(new PointPrimitiveCollection())
    document.addEventListener('pointerdown', dismissMenu)
    document.addEventListener('keydown', escape)
    removeMove = viewer.camera.moveEnd.addEventListener(queueRefresh)
    handler = new ScreenSpaceEventHandler(viewer.scene.canvas)
    handler.setInputAction((movement: { position: Cartesian2 }) => {
      const picked: { id?: MapPoint } | undefined = viewer?.scene.pick(movement.position)
      const item = picked?.id
      if (!item || !viewer) return
      clearHover()
      if (item.id === null) {
        const height = Math.max(80, viewer.camera.positionCartographic.height * 0.3)
        viewer.camera.flyTo({
          destination: Cartesian3.fromDegrees(item.longitude, item.latitude, height),
          duration: 0.7,
        })
      } else {
        const selectedRevision = props.revision
        void photo(item.id)
          .then((result) => {
            if (alive && props.revision === selectedRevision) emit('select', result)
          })
          .catch((cause) => {
            error.value = message(cause)
          })
      }
    }, ScreenSpaceEventType.LEFT_CLICK)
    handler.setInputAction((movement: { endPosition: Cartesian2 }) => {
      const picked: { id?: MapPoint } | undefined = viewer?.scene.pick(movement.endPosition)
      const id = picked?.id?.id ?? null
      hoverX.value = Math.min(
        movement.endPosition.x + 16,
        Math.max(8, (container.value?.clientWidth ?? 300) - 252),
      )
      hoverY.value = Math.min(
        movement.endPosition.y + 16,
        Math.max(8, (container.value?.clientHeight ?? 300) - 215),
      )
      if (id === currentHover) return
      clearHover()
      if (id === null) return
      currentHover = id
      hoverTimer = setTimeout(() => {
        hoverController = new AbortController()
        void photo(id, hoverController.signal)
          .then((result) => {
            if (alive && currentHover === id) hover.value = result
          })
          .catch(() => {})
      }, 100)
    }, ScreenSpaceEventType.MOUSE_MOVE)
    viewer.camera.setView({ destination: Cartesian3.fromDegrees(105, 32, 18000000) })
    void configureLayers()
    void fitLibrary()
    queueRefresh()
  } catch (cause) {
    error.value = `地图初始化失败：${message(cause)}。请使用支持 WebGL 的浏览器。`
  }
})

onUnmounted(() => {
  alive = false
  cancelBox()
  document.removeEventListener('pointerdown', dismissMenu)
  document.removeEventListener('keydown', escape)
  clearTimeout(mapTimer)
  clearHover()
  mapController?.abort()
  removeMove?.()
  handler?.destroy()
  viewer?.destroy()
  viewer = undefined
})
defineExpose({ locate, fitLibrary })
</script>

<template>
  <div class="globe-view" @mouseleave="clearHover" @contextmenu.prevent="contextMenu">
    <div ref="container" class="cesium-container" />
    <div
      v-if="boxMode"
      class="map-selection-overlay"
      @pointerdown.prevent="beginDrag"
      @pointermove="moveDrag"
      @pointerup="endDrag"
      @pointercancel="cancelBox"
    >
      <div v-if="drag" class="map-selection-box" :style="boxStyle" />
    </div>
    <div class="map-tools">
      <button class="secondary" :disabled="selecting || boxMode" @click="fitLibrary">
        ⌖ 全部影像
      </button>
      <button
        class="secondary"
        :class="{ 'selection-active': boxMode }"
        :disabled="selecting"
        :aria-pressed="boxMode"
        @click="startBox"
      >
        {{ boxMode ? '取消框选' : '框选影像' }}
      </button>
      <button
        v-if="selection.length"
        class="secondary"
        :disabled="selecting || boxMode"
        @click="exportSelected"
      >
        导出所选 ({{ selection.length.toLocaleString() }})
      </button>
      <span>
        {{
          loading
            ? '更新点位…'
            : `${pointCount.toLocaleString()} 个拍摄位置${clustered ? ' · 已聚合，点击展开' : ''}`
        }}
      </span>
    </div>
    <div
      v-if="boxMode || selectionMessage || selectionError"
      class="selection-status"
      :class="{ 'selection-failed': selectionError }"
      role="status"
    >
      {{
        selectionError ||
        (selecting
          ? '正在读取框内影像…'
          : boxMode
            ? '按住左键拖动框选 · Shift 追加 · Esc 取消'
            : selectionMessage)
      }}
      <button v-if="selection.length && !boxMode" class="quiet" @click="clearSelection">
        清除选择
      </button>
    </div>
    <div
      v-if="menu"
      ref="menuElement"
      class="map-context-menu"
      role="menu"
      aria-label="地图操作"
      :style="{ left: `${menu.x}px`, top: `${menu.y}px` }"
    >
      <button role="menuitem" @click="startBox">框选影像</button>
      <button role="menuitem" :disabled="!selection.length" @click="exportSelected">
        批量导出所选 ({{ selection.length.toLocaleString() }})
      </button>
      <button role="menuitem" :disabled="!selection.length" @click="clearSelection">
        清除选择
      </button>
      <button role="menuitem" @click="exportHistory">导出记录</button>
    </div>
    <div class="map-source">{{ tileMessage }}</div>
    <div v-if="error" class="map-error" role="alert">
      {{ error }}
      <button class="quiet" @click="refreshPoints">重试</button>
    </div>
    <div v-if="hover" class="hover-card" :style="{ left: `${hoverX}px`, top: `${hoverY}px` }">
      <img v-if="hover.thumbUrl" :src="hover.thumbUrl" alt="" />
      <div v-else class="hover-missing">原图缺失</div>
      <strong>{{ hover.filename }}</strong>
      <small>{{ hover.relativePath }}</small>
    </div>
  </div>
</template>
