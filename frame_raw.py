"""Deliver monochrome pixels only after the frame's authenticated native wake."""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from PIL import Image

LOG = logging.getLogger(__name__)


def pack_monochrome(path: Path, dimensions: tuple[int, int]) -> bytes:
    with Image.open(path) as image:
        if image.size != dimensions or image.width * image.height % 8:
            raise ValueError('Raw image dimensions must exactly match the physical screen')
        pixels = image.convert('L').point([0] * 160 + [1] * 96).tobytes()
    packed = bytearray()
    for offset in range(0, len(pixels), 8):
        word = 0
        for index in pixels[offset:offset+8]:
            word = (word << 3) | index
        packed.extend(word.to_bytes(3, 'big'))
    # Verify the complete documented MSB-first 3-bit representation before upload.
    for offset in range(0, len(packed), 3):
        word = int.from_bytes(packed[offset:offset+3], 'big')
        original = pixels[(offset // 3) * 8:(offset // 3) * 8 + 8]
        if bytes((word >> shift) & 7 for shift in range(21, -1, -3)) != original:
            raise ValueError('Raw pixel packing round-trip failed')
    return bytes(packed)


def deliver_raw(image: Path, base_url: str, expected_serial: str, upstream_url: str,
                token: str, next_cron_time: str, *, session=None,
                sleep=time.sleep, timeout: float = 40) -> dict:
    """One upload, observed writing/displaying→ready, and exact wake read-back."""
    own_session = session is None
    session = session or requests.Session()
    base_url = base_url.rstrip('/')

    def get(path):
        response = session.get(base_url + path, timeout=(3, 5))
        response.raise_for_status()
        return response.json()

    try:
        info = get('/deviceInfo')
        if not expected_serial or info.get('sn') != expected_serial:
            raise ValueError('Unexpected device identity; refusing device writes')
        # Native wake supplies a short LAN window; keep it awake during delivery.
        get('/whistle')
        dimensions = (int(info['width']), int(info['height']))
        packed = pack_monochrome(image, dimensions)
        filename = image.stem + '.bin'
        response = session.post(base_url + '/image/dataUpload', params={'filename': filename},
                                files={'dithered_image': (filename, packed, 'application/octet-stream')},
                                timeout=(3, 30))
        response.raise_for_status()
        uploaded = response.json()
        if uploaded.get('path') != '/imageData/' + filename:
            raise ValueError('Device raw upload read-back path mismatch')
        deadline, active = time.monotonic() + timeout, False
        while time.monotonic() < deadline:
            sleep(2)
            state = get('/state')
            if state.get('status') == 3:
                active = True
            if state.get('status') == 100 and active:
                break
            if state.get('status') not in {0, 3, 100}:
                raise RuntimeError('Device reported a raw display error')
            get('/whistle')
        else:
            raise TimeoutError('No observed raw writing/displaying-to-ready transition')
        expected_wake = int(datetime.fromisoformat(next_cron_time.replace('Z', '+00:00')).timestamp())
        settings = get('/upstream/pull_settings')
        if not (settings.get('upstream_on') is True and settings.get('upstream_url') == upstream_url
                and settings.get('next_cron_time') == expected_wake):
            # Also restores the normal upstream after the protected native test route.
            response = session.put(base_url + '/upstream/pull_settings', json={
                'upstream_on': True, 'upstream_url': upstream_url, 'token': token,
                'cron_time': next_cron_time}, timeout=(3, 8))
            response.raise_for_status()
            settings = get('/upstream/pull_settings')
        if not (settings.get('upstream_on') is True and settings.get('upstream_url') == upstream_url
                and settings.get('next_cron_time') == expected_wake):
            raise ValueError('Next native wake read-back failed')
        return {'status': 'ok', 'raw_path': uploaded['path'], 'active_to_ready_verified': True,
                'next_wake_verified': True, 'next_cron_time': next_cron_time,
                'finished_at': datetime.now(timezone.utc).isoformat()}
    finally:
        if own_session:
            session.close()


class RawWakeDelivery:
    """One bounded worker per frame, started only by its authenticated pull."""
    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()
        self.finished = threading.Event()
        self.finished.set()

    @property
    def receipt_path(self):
        return self.config.commute_snapshot_path.parent / 'delivery.json'

    def start(self, snapshot: dict, next_cron_time: str) -> bool:
        if snapshot.get('mock') is not False:
            raise ValueError('Mock snapshot cannot trigger automatic raw delivery')
        if not self.lock.acquire(blocking=False):
            return False
        try:
            try:
                previous = json.loads(self.receipt_path.read_text())
            except (OSError, ValueError):
                previous = {}
            if previous.get('status') == 'ok' and previous.get('snapshot_filename') == snapshot['image_filename']:
                self.lock.release()
                return False
            from commute_board import atomic_json
            atomic_json(self.receipt_path, {'status': 'running', 'snapshot_filename': snapshot['image_filename'],
                                          'started_at': datetime.now(timezone.utc).isoformat()})
            self.finished.clear()
            threading.Thread(target=self._run, args=(snapshot, next_cron_time), daemon=True,
                             name='commute-raw-wake').start()
            return True
        except Exception:
            self.finished.set()
            self.lock.release()
            raise

    def wait(self, timeout=50):
        return self.finished.wait(timeout)

    def _run(self, snapshot, next_cron_time):
        from commute_board import atomic_json
        result = {'status': 'error', 'error': 'Delivery worker interrupted'}
        try:
            image = self.config.commute_snapshot_path.parent / snapshot['image_filename']
            result = deliver_raw(image, self.config.device_base_url, self.config.expected_device_sn,
                                 self.config.public_base_url, self.config.device_token, next_cron_time)
            LOG.info('Commute raw wake display verified; next wake %s', next_cron_time)
        except Exception as exc:
            result = {'status': 'error', 'error': str(exc), 'finished_at': datetime.now(timezone.utc).isoformat()}
            LOG.error('Commute raw wake display failed: %s', type(exc).__name__)
        finally:
            try:
                atomic_json(self.receipt_path, dict(result, snapshot_filename=snapshot['image_filename']))
            finally:
                self.finished.set()
                self.lock.release()


def check_daily_delivery(config: dict, now=None) -> str:
    """Quiet on verified success, actionable message when daily display is missing."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(config['timezone'])
    now = now or datetime.now(tz)
    path = Path(config['commute_snapshot_path'])
    try:
        snapshot = json.loads(path.read_text())
        receipt = json.loads((path.parent / 'delivery.json').read_text())
        if snapshot.get('mock') is not False or datetime.fromisoformat(snapshot['generated_at']).astimezone(tz).date() != now.astimezone(tz).date():
            raise ValueError('Missing current live snapshot')
        if receipt.get('status') != 'ok' or receipt.get('snapshot_filename') != snapshot['image_filename']:
            raise ValueError('No verified delivery receipt for the current snapshot')
        if datetime.fromisoformat(receipt['finished_at']).astimezone(tz).date() != now.astimezone(tz).date():
            raise ValueError('Delivery receipt is stale')
        if not receipt.get('active_to_ready_verified') or not receipt.get('next_wake_verified'):
            raise ValueError('Display or next wake is not verified')
    except (OSError, ValueError, KeyError, TypeError):
        return 'Prometni okvir: današnji prikaz ob 06.00 ni potrjen. Preveri prebujanje okvirja in delivery.json; tišina ne pomeni uspešnega prikaza.'
    return ''


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Check the daily native-wake delivery receipt')
    parser.add_argument('--check-config', required=True, type=Path)
    args = parser.parse_args()
    message = check_daily_delivery(json.loads(args.check_config.read_text()))
    if message:
        print(message)
