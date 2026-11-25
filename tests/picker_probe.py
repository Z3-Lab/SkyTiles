"""Manual desktop smoke test: persist the real native folder-dialog response."""
import argparse
import json
from pathlib import Path

import httpx

parser = argparse.ArgumentParser()
parser.add_argument('port', type=int)
parser.add_argument('initial_directory')
parser.add_argument('output')
args = parser.parse_args()
response = httpx.post(f'http://127.0.0.1:{args.port}/api/dialogs/export-folder',
                      headers={'X-SkyTiles-Client': '1'},
                      json={'initialDirectory': args.initial_directory}, timeout=300)
Path(args.output).write_text(json.dumps({'status': response.status_code, 'response': response.json()},
                                       ensure_ascii=False), encoding='utf-8')
