#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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


def next_local_slot(cfg: dict, now: datetime | None = None) -> datetime:
    tz = ZoneInfo(cfg['timezone'])
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    times = [(cfg['scheduled_local_time'], range(7))]
    if cfg.get('commute_snapshot_path'):
        times.append((cfg.get('commute_local_time', '06:00'), cfg.get('commute_weekdays', range(7))))
    slots = []
    for value, weekdays in times:
        hour, minute = map(int, value.split(':'))
        for offset in range(8):
            slot = (now + timedelta(days=offset)).replace(hour=hour, minute=minute, second=0, microsecond=0)
            if slot > now and slot.weekday() in weekdays:
                slots.append(slot)
                break
    return min(slots).astimezone(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--arm-next', action='store_true', help='Arm next configured local slot instead of a 3-minute test')
    parser.add_argument('--ip', default=DEVICE_IP)
    parser.add_argument('--expected-sn', help='Verify exact frame identity before changing its schedule')
    args = parser.parse_args()
    cfg = load_config()
    base_url = f'http://{args.ip}'
    when = next_local_slot(cfg) if args.arm_next else datetime.now(timezone.utc) + timedelta(minutes=3)
    cron_time = when.replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    payload = {
        'upstream_on': True,
        'upstream_url': cfg['public_base_url'],
        'token': cfg['device_token'],
        'cron_time': cron_time,
    }

    try:
        info = request(f'{base_url}/deviceInfo')
        expected = args.expected_sn or cfg.get('expected_device_sn')
        if not expected or info.get('sn') != expected:
            raise ValueError('Set --expected-sn or config expected_device_sn; device identity must match before a write')
        request(f'{base_url}/upstream/pull_settings', method='PUT', payload=payload)
        get_result = request(f'{base_url}/upstream/pull_settings', method='GET')
        if not (get_result.get('upstream_on') is True
                and get_result.get('upstream_url') == cfg['public_base_url']
                and get_result.get('next_cron_time') == int(when.timestamp())):
            raise ValueError('Device schedule read-back mismatch')
    except (urllib.error.URLError, ValueError) as exc:
        print(json.dumps({'status': 'error', 'error': str(exc)}, ensure_ascii=False))
        return 1

    print(json.dumps({
        'status': 'ok',
        'cron_time': cron_time,
        'local_time': when.astimezone(ZoneInfo(cfg['timezone'])).isoformat(),
        'identity_verified': True,
        'schedule_readback_verified': True,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
