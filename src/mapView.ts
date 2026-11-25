import type { Camera } from 'cesium'

export function cameraViewQuery(camera: Camera, filter: string) {
  const position = camera.positionWC
  const volume = camera.frustum.computeCullingVolume(position, camera.directionWC, camera.upWC)
  const query = new URLSearchParams(filter)
  query.set(
    'view',
    JSON.stringify({
      camera: [position.x, position.y, position.z],
      planes: volume.planes.map((plane) => [plane.x, plane.y, plane.z, plane.w]),
    }),
  )
  return query
}
