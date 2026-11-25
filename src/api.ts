import type {
  Config,
  ImportJob,
  LibraryInfo,
  MapPoint,
  Photo,
  PhotoPage,
  RemovalResult,
  Stats,
  SelectionPoint,
  PhotoReference,
  ExportJob,
} from './types'

function errorText(data: unknown): string {
  if (data && typeof data === 'object' && 'detail' in data) {
    return typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)
  }
  return '请求失败，请检查本地服务是否仍在运行'
}

export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...options,
    headers: { 'X-SkyTiles-Client': '1', ...options.headers },
  })
  const data: unknown = await response.json().catch(() => null)
  if (!response.ok) throw new Error(errorText(data))
  return data as T
}

export const api = {
  config: () => request<Config>('/config'),
  libraryInfo: () => request<LibraryInfo>('/library'),
  removePhoto: (photo: Photo) =>
    request<RemovalResult>(`/photos/${photo.id}?version=${encodeURIComponent(photo.version)}`, {
      method: 'DELETE',
    }),
  clearLibrary: (expectedCount: number) =>
    request<RemovalResult>('/library', {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirmation: 'CLEAR', expectedCount }),
    }),
  stats: (query = '') => request<Stats>(`/stats${query}`),
  photos: (query: string, signal?: AbortSignal) =>
    request<PhotoPage>(`/photos${query}`, { signal }),
  photo: (id: number, signal?: AbortSignal) => request<Photo>(`/photos/${id}`, { signal }),
  points: (query: string, signal?: AbortSignal) =>
    request<{ items: MapPoint[]; total: number; clustered: boolean }>(`/map${query}`, { signal }),
  jobs: () => request<{ items: ImportJob[] }>('/imports'),
  selectionPoints: (query: string, signal?: AbortSignal) =>
    request<{ items: SelectionPoint[]; snapshot: number; next: number | null }>(
      `/map/selection${query}`,
      { signal },
    ),
  newExport: (photos: PhotoReference[], destination: string, folderName: string) =>
    request<{ id: string }>('/exports', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ photos, destination, folderName }),
    }),
  exports: () => request<{ items: ExportJob[] }>('/exports'),
  exportJob: (id: string) => request<ExportJob>(`/exports/${id}`),
  exportFolder: (initialDirectory: string) =>
    request<{ path: string | null }>('/dialogs/export-folder', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ initialDirectory }),
    }),
  job: (id: string) => request<ImportJob>(`/imports/${id}`),
  importPhotos: (initialDirectory: string) =>
    request<{ paths: string[] }>('/dialogs/import-photos', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ initialDirectory }),
    }),
  localImport: (root: string, files: string[]) =>
    request<{ id: string }>('/imports/local', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ root, files }),
    }),
  cancelImport: (id: string) => request(`/imports/${id}`, { method: 'DELETE' }),
  migrate: (file: File, root: string) => {
    const form = new FormData()
    form.append('file', file)
    form.append('root', root)
    return request<{ id: string }>('/imports/legacy', { method: 'POST', body: form })
  },
  saveConfig: (cesiumToken: string) =>
    request('/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cesiumToken }),
    }),
  shutdown: () => request('/shutdown', { method: 'POST' }),
}

export function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}
