"""Visibility of elevated photo locations in a Cesium/WGS84 camera frustum."""
import math
from functools import lru_cache

from fastapi import HTTPException
from pydantic import BaseModel, Field, ValidationError, ConfigDict

A = 6378137.0
B = 6356752.314245179
E2 = 1 - (B / A) ** 2


class ViewVolume(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    camera: tuple[float, float, float]
    planes: list[tuple[float, float, float, float]] = Field(min_length=6, max_length=6)


@lru_cache(maxsize=65536)
def cartesian(latitude, longitude, altitude):
    lat, lon = math.radians(latitude), math.radians(longitude)
    sin_lat, cos_lat = math.sin(lat), math.cos(lat)
    radius = A / math.sqrt(1 - E2 * sin_lat * sin_lat)
    return ((radius + altitude) * cos_lat * math.cos(lon),
            (radius + altitude) * cos_lat * math.sin(lon),
            (radius * (1 - E2) + altitude) * sin_lat)


def view_filter(db, value):
    """Register a connection-local predicate; never clip airborne points to a ground footprint."""
    try:
        volume = ViewVolume.model_validate_json(value)
        if any(abs(math.hypot(*plane[:3]) - 1) > 0.001 for plane in volume.planes):
            raise ValueError('视锥平面法向量无效')
    except (ValidationError, ValueError) as error:
        raise HTTPException(422, '三维视野参数无效') from error
    cx, cy, cz = (volume.camera[0] / A, volume.camera[1] / A, volume.camera[2] / B)
    horizon = cx * cx + cy * cy + cz * cz - 1

    def visible(latitude, longitude, altitude):
        if latitude is None or longitude is None:
            return 0
        height = altitude or 0
        x, y, z = cartesian(latitude, longitude, height)
        # Plane normals face inward. A small tolerance absorbs floating-point roundoff.
        if any(nx * x + ny * y + nz * z + distance < -0.01
               for nx, ny, nz, distance in volume.planes):
            return 0
        if horizon > 0:
            # Match the globe horizon test, without hiding below-sea-level GPS records.
            sx, sy, sz = (x, y, z) if height >= 1 else cartesian(latitude, longitude, 1)
            dx, dy, dz = sx / A - cx, sy / A - cy, sz / B - cz
            towards_center = -(dx * cx + dy * cy + dz * cz)
            if (towards_center > horizon and
                    towards_center * towards_center > horizon * (dx * dx + dy * dy + dz * dz)):
                return 0
        return 1

    db.create_function('photo_in_view', 3, visible, deterministic=True)
    return 'photo_in_view(latitude,longitude,altitude)=1'
