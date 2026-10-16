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
    def test_real_http_native_callback_is_gated_and_no_jpeg(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = test_server.PullServerTests().remote_config()
            cfg.scheduled_local_time = '07:45'
            cfg.commute_snapshot_path = Path(directory) / 'snapshot.json'
            cfg.expected_device_sn = 'test-frame'
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
