import json
import threading
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import Mock, patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from bloomin8_pull_server import PullServer, RequestHandler
from commute_board import publish
import test_server


class RawWakeHttpTests(unittest.TestCase):
    def test_infographic_callback_gates_writes_and_retries_unverified_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = test_server.PullServerTests().remote_config()
            cfg.commute_snapshot_path = Path(directory) / 'snapshot.json'
            cfg.expected_device_sn = 'test-frame'
            cfg.device_base_url = 'http://frame.invalid'
            cfg.infographic_delivery_mode = 'raw-on-wake'
            app = PullServer(cfg)
            app.raw_delivery = Mock()
            metadata = {'date': app.todays_local_date(), 'person': 'Fixture', 'image_filename': 'fixture.png'}
            server = ThreadingHTTPServer(('127.0.0.1', 0), RequestHandler)
            server.app = app
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                with patch.object(app, 'display_slot', return_value=('infographic', '2099-01-01T06:00:00Z')), patch.object(app, 'load_latest_metadata', return_value=metadata):
                    for identity, verified in [('probe', True), ('test-frame', True), ('test-frame', False)]:
                        app.raw_delivery.reset_mock()
                        app.raw_delivery.infographic_verified.return_value = verified
                        with urlopen(Request(base + '/eink_pull?device_id=' + identity, headers={'X-Access-Token': 'device-secret'}), timeout=3) as response:
                            body = json.load(response)
                        self.assertEqual(body['status'], 204)
                        self.assertNotIn('image_url', body['data'])
                        self.assertEqual(app.raw_delivery.start_infographic.call_count, int(identity == 'test-frame'))
                        self.assertEqual(app.raw_delivery.wait.call_count, int(identity == 'test-frame'))
                        self.assertEqual(body['data']['next_cron_time'] == '2099-01-01T06:00:00Z', verified)
                    app.raw_delivery.reset_mock()
                    with self.assertRaises(HTTPError) as denied:
                        urlopen(base + '/eink_pull?device_id=test-frame', timeout=3)
                    self.assertEqual(denied.exception.code, 403)
                    app.raw_delivery.start_infographic.assert_not_called()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_real_http_native_callback_is_gated_and_no_jpeg(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = test_server.PullServerTests().remote_config()
            cfg.scheduled_local_time = '07:45'
            cfg.commute_snapshot_path = Path(directory) / 'snapshot.json'
            cfg.expected_device_sn = 'test-frame'
            cfg.commute_delivery_mode = 'raw-on-wake'
            cfg.device_base_url = 'http://frame.invalid'
            app = PullServer(cfg)
            app.raw_delivery = Mock()
            publish({'mock': False, 'generated_at': datetime.now(cfg.timezone).isoformat(),
                     'trains': {'status': 'ok', 'items': []}, 'roads': {'status': 'ok', 'items': []}}, cfg.commute_snapshot_path)
            server = ThreadingHTTPServer(('127.0.0.1', 0), RequestHandler)
            server.app = app  # type: ignore[attr-defined]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                with patch.object(app, 'display_slot', return_value=('commute', '2026-10-17T05:45:00Z')):
                    for path in ['/eink_pull?device_id=probe', '/eink_pull?device_id=test-frame', '/commute-preview/eink_pull?device_id=test-frame']:
                        app.raw_delivery.reset_mock()
                        triggered = threading.Event()
                        app.raw_delivery.start.side_effect = lambda *args: triggered.set()
                        with urlopen(Request(base+path, headers={'X-Access-Token': 'device-secret'}), timeout=3) as response:
                            self.assertEqual(response.status, 200)
                            body = json.load(response)
                        self.assertEqual(body['status'], 204)
                        self.assertNotIn('image_url', body['data'])
                        if 'probe' not in path:
                            self.assertTrue(triggered.wait(timeout=1))
                        self.assertEqual(app.raw_delivery.start.call_count, 0 if 'probe' in path else 1)
                        self.assertEqual(app.raw_delivery.wait.call_count, 0 if 'probe' in path else 1)
                    for path, headers in [('/eink_pull?device_id=test-frame', {}),
                                          ('/commute-preview/eink_pull?device_id=other', {'X-Access-Token': 'device-secret'})]:
                        with self.assertRaises(HTTPError) as denied:
                            urlopen(Request(base+path, headers=headers), timeout=3)
                        self.assertEqual(denied.exception.code, 403)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
