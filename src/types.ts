export interface Photo {
  id: number
  version: string
  filename: string
  relativePath: string
  imageUrl: string | null
  thumbUrl: string | null
  latitude: number | null
  longitude: number | null
  altitude: number | null
  capturedAt: string | null
  missing: boolean
  batchId: string
  sourcePath: string | null
}

export interface LibraryInfo {
  photoCount: number
  batchCount: number
  activeImports: number
  activeExports: number
  linkedPhotos: number
  managedPhotos: number
  cleanupPending: number
}

export interface RemovalResult {
  removed: number
  cleanupPending: number
}

export interface Stats {
  total: number
  withGps: number
  withoutGps: number
  missing: number
  west: number | null
  east: number | null
  south: number | null
  north: number | null
}

export interface Config {
  cesiumToken: string
  dataDir: string
  legacyRoot: string
  exportRoot: string
}

export interface PhotoPage {
  items: Photo[]
  total: number
  page: number
  pageSize: number
}

export interface MapPoint {
  id: number | null
  latitude: number
  longitude: number
  altitude: number | null
  count: number
}

export interface PhotoReference {
  id: number
  version: string
}

export interface SelectionPoint extends PhotoReference {
  latitude: number
  longitude: number
  altitude: number | null
}

export interface ExportJob {
  id: string
  state: 'queued' | 'running' | 'completed' | 'failed' | 'interrupted'
  target_dir: string
  total: number
  processed: number
  copied: number
  missing: number
  errors: number
  message: string | null
  created_at: string
  issues?: { filename: string; reason: string }[]
}

export const exportLabels: Record<ExportJob['state'], string> = {
  queued: '排队中',
  running: '复制中',
  completed: '已完成',
  failed: '导出失败',
  interrupted: '已中断',
}

export interface ImportJob {
  id: string
  kind: 'photos' | 'legacy' | 'maintenance'
  name: string
  state: 'uploading' | 'queued' | 'running' | 'completed' | 'failed' | 'interrupted' | 'cancelled'
  total: number
  processed: number
  imported: number
  duplicates: number
  without_gps: number
  missing: number
  errors: number
  message: string | null
  created_at: string
  issues?: { filename: string; reason: string }[]
  issueCount?: number
}

export const jobLabels: Record<ImportJob['state'], string> = {
  uploading: '等待上传完成',
  queued: '排队中',
  running: '解析中',
  completed: '已完成',
  failed: '任务失败',
  interrupted: '已中断，可重新导入',
  cancelled: '已取消',
}

export function photoTime(value: string | null): string {
  if (!value) return '拍摄时间未知'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}
