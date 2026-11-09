"""Deliver monochrome pixels only after the frame's authenticated native wake."""
from __future__ import annotations

import json
import logging
import threading
import time
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import requests
from PIL import Image, ImageOps

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


def pack_color(path: Path, dimensions: tuple[int, int]) -> bytes:
    """Vendor six-color indices, MSB-first 3 bits/pixel, full round-trip proof."""
    colors = [(0, 0, 0), (255, 255, 255), (255, 0, 0),
              (0, 255, 0), (0, 0, 255), (255, 255, 0)]
    palette = Image.new('P', (1, 1))
    palette.putpalette([channel for color in colors for channel in color] + [0] * (768 - 18))
    with Image.open(path) as image:
        if image.size != dimensions or image.width * image.height % 8:
            raise ValueError('Raw image dimensions must exactly match the physical screen')
        pixels = image.convert('RGB').quantize(palette=palette, dither=Image.Dither.FLOYDSTEINBERG).tobytes()
    # Pillow may select a duplicate black palette entry above index 5.
    pixels = bytes(index if index < 6 else 0 for index in pixels)
    packed = bytearray()
    for offset in range(0, len(pixels), 8):
        word = 0
        for index in pixels[offset:offset + 8]:
            word = (word << 3) | index
        packed.extend(word.to_bytes(3, 'big'))
    for offset in range(0, len(packed), 3):
        word = int.from_bytes(packed[offset:offset + 3], 'big')
        if bytes((word >> shift) & 7 for shift in range(21, -1, -3)) != pixels[(offset // 3) * 8:(offset // 3) * 8 + 8]:
            raise ValueError('Color pixel packing round-trip failed')
    return bytes(packed)


def deliver_raw(image: Path, base_url: str, expected_serial: str, upstream_url: str,
                token: str, next_cron_time: str, *, session=None,
                sleep=time.sleep, timeout: float = 40, packer=pack_monochrome) -> dict:
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
        packed = packer(image, dimensions)
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

    @property
    def infographic_receipt_path(self):
        return self.config.commute_snapshot_path.parent / 'infographic-delivery.json'

    def infographic_verified(self, metadata, app):
        try:
            receipt = json.loads(self.infographic_receipt_path.read_text())
        except (OSError, ValueError):
            return False
        return (receipt.get('status') == 'ok'
                and receipt.get('snapshot_filename') == app.image_public_url(metadata).rsplit('/', 1)[-1]
                and receipt.get('source_filename') == metadata.get('image_filename')
                and receipt.get('display_date') == str(metadata.get('display_date') or metadata.get('date'))
                and receipt.get('active_to_ready_verified') is True
                and receipt.get('next_wake_verified') is True)

    def start_infographic(self, metadata, next_cron_time, app):
        """Share the frame lock, but never overwrite the commuter receipt."""
        if not self.lock.acquire(blocking=False):
            return False
        try:
            if self.infographic_verified(metadata, app):
                self.lock.release()
                return False
            from commute_board import atomic_json
            atomic_json(self.infographic_receipt_path, {
                'status': 'running', 'snapshot_filename': app.image_public_url(metadata).rsplit('/', 1)[-1],
                'started_at': datetime.now(timezone.utc).isoformat()})
            self.finished.clear()
            threading.Thread(target=self._run_infographic, args=(dict(metadata), next_cron_time, app),
                             daemon=True, name='infographic-raw-wake').start()
            return True
        except Exception:
            self.finished.set()
            self.lock.release()
            raise

    def _run_infographic(self, metadata, next_cron_time, app):
        from commute_board import atomic_json
        result = {'status': 'error', 'error': 'Delivery worker interrupted'}
        filename = app.image_public_url(metadata).rsplit('/', 1)[-1]
        try:
            with tempfile.TemporaryDirectory(prefix='infographic-raw-', dir=self.infographic_receipt_path.parent) as directory:
                directory = Path(directory)
                source = app.latest_image_path(metadata, directory)
                image_path = directory / (Path(filename).stem + '.png')
                with Image.open(source) as original:
                    fitted = ImageOps.pad(original.convert('RGB'), app.output_dimensions(),
                                          method=Image.Resampling.LANCZOS, color='white')
                    if self.config.display_orientation == 'landscape':
                        fitted = fitted.transpose(Image.Transpose.ROTATE_270)
                    fitted.save(image_path)
                result = deliver_raw(image_path, self.config.device_base_url, self.config.expected_device_sn,
                                     self.config.public_base_url, self.config.device_token, next_cron_time,
                                     packer=pack_color)
            LOG.info('Infographic raw wake display verified; next wake %s', next_cron_time)
        except Exception as exc:
            result = {'status': 'error', 'error': str(exc), 'finished_at': datetime.now(timezone.utc).isoformat()}
            LOG.error('Infographic raw wake display failed: %s', type(exc).__name__)
        finally:
            try:
                atomic_json(self.infographic_receipt_path, dict(result, snapshot_filename=filename,
                    source_filename=metadata.get('image_filename'),
                    display_date=str(metadata.get('display_date') or metadata.get('date'))))
            finally:
                self.finished.set()
                self.lock.release()

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
    if now.astimezone(tz).weekday() not in config.get('commute_weekdays', range(7)):
        return ''
    path = Path(config['commute_snapshot_path'])
    try:
        snapshot = json.loads(path.read_text())
        receipt = json.loads((path.parent / 'delivery.json').read_text())
        if snapshot.get('mock') is not False or datetime.fromisoformat(snapshot['generated_at']).astimezone(tz).date() != now.astimezone(tz).date():
            raise ValueError('Missing current live snapshot')
        if len(config.get('commute_local_times', [])) > 1:
            generated = datetime.fromisoformat(snapshot['generated_at'])
            if not -300 <= (now - generated).total_seconds() <= 15 * 60:
                raise ValueError('Snapshot is stale for the current edition')
        if receipt.get('status') != 'ok' or receipt.get('snapshot_filename') != snapshot['image_filename']:
            raise ValueError('No verified delivery receipt for the current snapshot')
        if datetime.fromisoformat(receipt['finished_at']).astimezone(tz).date() != now.astimezone(tz).date():
            raise ValueError('Delivery receipt is stale')
        if not receipt.get('active_to_ready_verified') or not receipt.get('next_wake_verified'):
            raise ValueError('Display or next wake is not verified')
    except (OSError, ValueError, KeyError, TypeError):
        return 'Prometni okvir: zadnja jutranja izdaja ni potrjena. Preveri prebujanje okvirja in delivery.json; tišina ne pomeni uspešnega prikaza.'
    return ''


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Check the daily native-wake delivery receipt')
    parser.add_argument('--check-config', required=True, type=Path)
    args = parser.parse_args()
    message = check_daily_delivery(json.loads(args.check_config.read_text()))
    if message:
        print(message)
