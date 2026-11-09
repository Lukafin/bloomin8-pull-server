import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from bloomin8_pull_server import PullServer
from frame_raw import RawWakeDelivery, pack_color
import test_server


class InfographicRawTests(unittest.TestCase):
    def config(self, directory):
        cfg = test_server.PullServerTests().remote_config()
        cfg.display_orientation = 'portrait'
        cfg.commute_snapshot_path = Path(directory) / 'snapshot.json'
        cfg.device_base_url = 'http://frame.invalid'
        cfg.expected_device_sn = 'test-frame'
        cfg.commute_delivery_mode = 'raw-on-wake'
        cfg.infographic_delivery_mode = 'raw-on-wake'
        return cfg

    def test_ready_infographic_uses_raw_not_native_jpeg(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PullServer(self.config(directory))
            metadata = {'date': app.todays_local_date(), 'person': 'Test', 'image_filename': 'test.png'}
            with patch.object(app, 'display_slot', return_value=('infographic', '2026-10-24T06:00:00Z')), patch.object(app, 'load_latest_metadata', return_value=metadata):
                result = app.build_pull_response({})
            self.assertEqual(result['status'], 204)
            self.assertEqual(result['data']['delivery'], 'infographic-raw-on-wake')
            self.assertNotIn('image_url', result['data'])

    def test_color_palette_indices_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'palette.png'
            image = Image.new('RGB', (8, 1))
            image.putdata([(0,0,0), (255,255,255), (255,0,0), (0,255,0), (0,0,255), (255,255,0), (0,0,0), (255,255,255)])
            image.save(path)
            raw = pack_color(path, (8, 1))
            word = int.from_bytes(raw, 'big')
            self.assertEqual([(word >> shift) & 7 for shift in range(21, -1, -3)], [0,1,2,3,4,5,0,1])
            with self.assertRaises(ValueError):
                pack_color(path, (16, 1))

    def test_worker_records_exact_edition_separately_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PullServer(self.config(directory))
            source = Path(directory) / 'original.png'
            Image.new('RGB', (30, 40), 'red').save(source)
            metadata = {'date': '2026-08-20', 'display_date': app.todays_local_date(), 'person': 'Vulkan', 'image_filename': source.name}
            worker = app.raw_delivery
            assert worker is not None
            receipt_path = source.parent / 'infographic-delivery.json'
            def verified(image, *args, **kwargs):
                with Image.open(image) as fitted:
                    self.assertEqual(fitted.size, (1200, 1600))
                self.assertIs(kwargs['packer'], pack_color)
                return {'status': 'ok', 'active_to_ready_verified': True, 'next_wake_verified': True,
                        'finished_at': datetime.now(app.config.timezone).isoformat()}
            with patch.object(app, 'latest_image_path', return_value=source), patch('frame_raw.deliver_raw', side_effect=verified) as send:
                self.assertTrue(worker.start_infographic(metadata, '2026-10-24T06:00:00Z', app))
                self.assertTrue(worker.wait(5))
                receipt = json.loads(receipt_path.read_text())
                self.assertEqual(receipt['status'], 'ok')
                self.assertEqual(receipt['display_date'], app.todays_local_date())
                self.assertEqual(receipt['source_filename'], source.name)
                self.assertFalse((source.parent / 'delivery.json').exists())
                self.assertFalse(worker.start_infographic(metadata, '2026-10-24T06:00:00Z', app))
                self.assertEqual(send.call_count, 1)
                metadata['display_date'] = '2026-10-24'
                self.assertTrue(worker.start_infographic(metadata, '2026-10-25T06:00:00Z', app))
                self.assertTrue(worker.wait(5))
                self.assertEqual(send.call_count, 2)

    def test_worker_failure_is_not_success_and_can_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PullServer(self.config(directory))
            assert app.raw_delivery is not None
            metadata = {'date': app.todays_local_date(), 'person': 'Test', 'image_filename': 'missing.png'}
            with patch.object(app, 'latest_image_path', side_effect=OSError('missing source')):
                self.assertTrue(app.raw_delivery.start_infographic(metadata, '2026-10-24T06:00:00Z', app))
                self.assertTrue(app.raw_delivery.wait(5))
                receipt = json.loads((Path(directory) / 'infographic-delivery.json').read_text())
                self.assertEqual(receipt['status'], 'error')
                self.assertTrue(app.raw_delivery.start_infographic(metadata, '2026-10-24T06:00:00Z', app))
                self.assertTrue(app.raw_delivery.wait(5))


if __name__ == '__main__':
    unittest.main()
