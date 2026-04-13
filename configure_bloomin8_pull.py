#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get('BLOOMIN8_PULL_CONFIG', SCRIPT_DIR / 'bloomin8_pull_config.json'))
DEVICE_IP = os.environ.get('BLOOMIN8_IP', '192.168.64.122')
BASE_URL = f'http://{DEVICE_IP}'


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding='utf-8'))


def request(url: str, method: str = 'GET', payload: dict | None = None) -> dict:
    data = None
    headers = {'accept': 'application/json'}
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
        headers['content-type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=12) as r:
        raw = r.read().decode('utf-8', 'replace')
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {'raw': raw}


def main() -> int:
    cfg = load_config()
    cron_time = (datetime.now(timezone.utc) + timedelta(minutes=3)).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    payload = {
        'upstream_on': True,
        'upstream_url': cfg['public_base_url'],
        'token': cfg['device_token'],
        'cron_time': cron_time,
    }

    try:
        put_result = request(f'{BASE_URL}/upstream/pull_settings', method='PUT', payload=payload)
        get_result = request(f'{BASE_URL}/upstream/pull_settings', method='GET')
    except urllib.error.URLError as exc:
        print(json.dumps({'status': 'error', 'error': str(exc), 'device_ip': DEVICE_IP}, ensure_ascii=False))
        return 1

    print(json.dumps({
        'status': 'ok',
        'device_ip': DEVICE_IP,
        'payload': payload,
        'put_result': put_result,
        'get_result': get_result,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
