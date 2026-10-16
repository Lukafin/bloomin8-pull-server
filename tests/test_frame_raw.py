import unittest
import tempfile
import json
from pathlib import Path
from unittest.mock import Mock, patch
from datetime import datetime, timezone
from frame_raw import check_daily_delivery
from PIL import Image
from frame_raw import pack_monochrome, deliver_raw


class FakeDevice:
    def __init__(self, serial='test-serial', states=(3, 100)):
        self.serial = serial
        self.states = iter(states)
        self.calls = []
        self.wake = 0
        self.url = 'https://frame.example/pull'

    def get(self, url, **kwargs):
        self.calls.append(('GET', url))
        if url.endswith('/deviceInfo'):
            data = {'sn': self.serial, 'width': 8, 'height': 8}
        elif url.endswith('/state'):
            data = {'status': next(self.states, 0)}
        elif url.endswith('/upstream/pull_settings'):
            data = {'upstream_on': True, 'upstream_url': self.url, 'next_cron_time': self.wake}
        else:
            data = {}
        return Mock(raise_for_status=Mock(), json=lambda: data)

    def post(self, url, **kwargs):
        self.calls.append(('POST', url))
        filename = kwargs['params']['filename']
        raw = kwargs['files']['dithered_image'][1]
        assert len(raw) == 24
        return Mock(raise_for_status=Mock(), json=lambda: {'status': 0, 'path': '/imageData/' + filename})

    def put(self, url, **kwargs):
        self.calls.append(('PUT', url))
        body = kwargs['json']
        from datetime import datetime
        self.wake = int(datetime.fromisoformat(body['cron_time'].replace('Z', '+00:00')).timestamp())
        self.url = body['upstream_url']
        return Mock(raise_for_status=Mock(), json=lambda: {})


class RawDeliveryTests(unittest.TestCase):
    def test_exact_packing_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.jpg'
            Image.new('RGB', (8, 8), 'white').save(path)
            raw = pack_monochrome(path, (8, 8))
            self.assertEqual(raw, bytes.fromhex('249249') * 8)
            with self.assertRaises(ValueError):
                pack_monochrome(path, (16, 16))

    def test_identity_before_write_and_active_to_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.jpg'
            Image.new('RGB', (8, 8), 'white').save(path)
            device = FakeDevice()
            receipt = deliver_raw(path, 'http://192.168.1.2', 'test-serial', 'https://frame.example/pull',
                                  'secret', '2026-10-17T04:00:00Z', session=device,
                                  sleep=lambda _: None, timeout=1)
            self.assertEqual(receipt['status'], 'ok')
            self.assertTrue(receipt['active_to_ready_verified'])
            self.assertTrue(receipt['next_wake_verified'])
            self.assertEqual(sum(c[0] == 'POST' for c in device.calls), 1)
            bad = FakeDevice(serial='other')
            with self.assertRaises(ValueError):
                deliver_raw(path, 'http://192.168.1.2', 'test-serial', 'https://frame.example/pull',
                            'secret', '2026-10-17T04:00:00Z', session=bad, sleep=lambda _: None, timeout=1)
            self.assertFalse(any(c[0] in ['POST', 'PUT'] for c in bad.calls))

    def test_idle_or_old_ready_not_mistaken_for_render(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.jpg'
            Image.new('RGB', (8, 8), 'white').save(path)
            for states in [(0,), (100,)]:
                with self.subTest(states=states), self.assertRaises(TimeoutError):
                    deliver_raw(path, 'http://192.168.1.2', 'test-serial', 'https://frame.example/pull',
                                'secret', '2026-10-17T04:00:00Z', session=FakeDevice(states=states),
                                sleep=lambda _: None, timeout=.01)

    def test_daily_receipt_requires_current_identity_and_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'snapshot.json'
            config = {'timezone': 'Europe/Ljubljana', 'commute_snapshot_path': str(path)}
            now = datetime.now(timezone.utc)
            path.write_text(json.dumps({'mock': False, 'generated_at': now.isoformat(), 'image_filename': 'today_P.jpg'}))
            self.assertTrue(check_daily_delivery(config, now))
            receipt = {'status': 'ok', 'snapshot_filename': 'today_P.jpg', 'finished_at': now.isoformat(),
                       'active_to_ready_verified': True, 'next_wake_verified': True}
            (path.parent / 'delivery.json').write_text(json.dumps(receipt))
            self.assertEqual(check_daily_delivery(config, now), '')
            receipt['snapshot_filename'] = 'other_P.jpg'
            (path.parent / 'delivery.json').write_text(json.dumps(receipt))
            self.assertTrue(check_daily_delivery(config, now))


if __name__ == '__main__':
    unittest.main()
