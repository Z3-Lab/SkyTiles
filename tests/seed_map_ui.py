"""Create an isolated, repeatable map-selection fixture for browser validation."""
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient
from tests.test_app import HEADERS, image_bytes
from backend.app import create_app
from backend.config import Settings

ROOT = Path(__file__).resolve().parent.parent
root = ROOT / '.test-output/map-export-ui'
source = root / 'source/imgs/航线A'
source.mkdir(parents=True, exist_ok=True)
items = []
for index, (color, latitude, longitude) in enumerate([
    ('red', 31.20, 121.50), ('blue', 31.23, 121.54), ('green', 31.25, 121.51),
]):
    name = f'航线A/航拍_{index + 1}.jpg'
    (source.parent / name).write_bytes(image_bytes(color, gps=True))
    items.append(dict(relativePath=name, latitude=latitude, longitude=longitude, altitude=100))
items.append(dict(relativePath='缺失原图.jpg', latitude=31.22, longitude=121.52, altitude=80))
settings = Settings(ROOT, root / 'data', ROOT / 'dist', False)
with TestClient(create_app(settings), headers=HEADERS) as client:
    response = client.post('/api/imports/legacy', files={'file': (
        'index.json', json.dumps({'items': items}).encode(), 'application/json')},
        data={'root': str(source.parent.parent)})
    job_id = response.json()['id']
    for _ in range(100):
        job = client.get('/api/imports/' + job_id).json()
        if job['state'] not in ['queued', 'running']:
            break
        time.sleep(.05)
    print(job['state'], client.get('/api/stats').json())
