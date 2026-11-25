// Compare the request geometry with Cesium's own frustum and horizon tests.
import assert from 'node:assert/strict'
import {
  BoundingSphere, Camera, Cartesian3, Cartographic, Ellipsoid, GeographicProjection,
  Intersect, Math as CMath, Occluder, Rectangle, SceneMode,
} from 'cesium'
import { cameraViewQuery } from '../src/mapView.ts'

const scene = {
  drawingBufferWidth: 1000, drawingBufferHeight: 700,
  canvas: { clientWidth: 1000, clientHeight: 700 },
  mapProjection: new GeographicProjection(), mode: SceneMode.SCENE3D,
}
const camera = new Camera(scene)
const photos = [
  [120, 35.006, 1700], [120, 35.015, 1700], [120, 35.04, 1700],
  [120, 35.075, 0], [120.02, 35.04, 1700], [120, 35.04, 2200],
  [120, 45, 1700], [120, 39, 100000], [-60, -35, 1700],
].map(([longitude, latitude, altitude], index) => ({ id: index + 1, longitude, latitude, altitude }))
const cases = []
for (const pitch of [-65, -40, -20, -10, -5]) {
  camera.setView({ destination: Cartesian3.fromDegrees(120, 35, 2000),
    orientation: { heading: 0, pitch: CMath.toRadians(pitch), roll: 0 } })
  const volume = camera.frustum.computeCullingVolume(camera.positionWC, camera.directionWC, camera.upWC)
  const occluder = new Occluder(new BoundingSphere(Cartesian3.ZERO, 1),
    Ellipsoid.WGS84.transformPositionToScaledSpace(camera.positionWC))
  const expected = photos.filter(point => {
    const position = Cartesian3.fromDegrees(point.longitude, point.latitude, point.altitude)
    const surface = Cartesian3.fromDegrees(point.longitude, point.latitude, Math.max(1, point.altitude))
    return volume.computeVisibility(new BoundingSphere(position, 0)) !== Intersect.OUTSIDE
      && occluder.isPointVisible(Ellipsoid.WGS84.transformPositionToScaledSpace(surface))
  }).map(point => point.id)
  const ground = camera.computeViewRectangle()
  const missed = photos.filter(point => expected.includes(point.id) &&
    !Rectangle.contains(ground, Cartographic.fromDegrees(point.longitude, point.latitude))).map(point => point.id)
  const query = cameraViewQuery(camera, 'q=flight&status=gps')
  assert.equal(query.get('q'), 'flight')
  assert.equal(query.get('status'), 'gps')
  assert.equal(query.has('south'), false)
  cases.push({ pitch, view: JSON.parse(query.get('view')), expected, missed })
}
assert.ok(cases.find(test => test.pitch === -20).missed.includes(1), 'Must reproduce the near aerial-point regression')
process.stdout.write(JSON.stringify({ photos, cases }))
