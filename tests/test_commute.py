import json
import tempfile
import unittest
import threading
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer
from PIL import Image
from bloomin8_pull_server import PullServer, PullServerError, RequestHandler
from configure_bloomin8_pull import next_local_slot
from commute_render import render_board, mock_data
import test_server


class CommuteTests(unittest.TestCase):
    def server(self):
        config = test_server.PullServerTests().remote_config()
        config.scheduled_local_time = '07:45'
        config.commute_local_time = '06:00'
        config.commute_snapshot_path = Path('/does/not/exist/snapshot.json')
        return PullServer(config)

    @patch('bloomin8_pull_server.datetime')
    def test_slots_and_dst(self, clock):
        clock.fromisoformat.side_effect = datetime.fromisoformat
        server = self.server()
        for instant, slot, next_time in [
            ('2026-10-16T03:59:00+00:00', 'waiting', '2026-10-16T04:00:00Z'),
            ('2026-10-16T04:00:00+00:00', 'commute', '2026-10-16T05:45:00Z'),
            ('2026-10-16T05:44:00+00:00', 'commute', '2026-10-16T05:45:00Z'),
            ('2026-10-16T05:45:00+00:00', 'infographic', '2026-10-17T04:00:00Z'),
            ('2026-10-24T05:45:00+00:00', 'infographic', '2026-10-25T05:00:00Z'),
            ('2026-10-25T05:00:00+00:00', 'commute', '2026-10-25T06:45:00Z')]:
            clock.now.side_effect = lambda tz=None: datetime.fromisoformat(instant).astimezone(tz)
            with self.subTest(instant=instant):
                self.assertEqual(server.display_slot({}), (slot, next_time))

    @patch('bloomin8_pull_server.datetime')
    def test_early_frame_uses_valid_requested_wake(self, clock):
        clock.fromisoformat.side_effect = datetime.fromisoformat
        clock.now.side_effect = lambda tz=None: datetime.fromisoformat('2026-10-16T03:57:40+00:00').astimezone(tz)
        self.assertEqual(self.server().display_slot({'cron_time': ['2026-10-16T04:00:00Z']}),
                         ('commute', '2026-10-16T05:45:00Z'))
        self.assertEqual(self.server().display_slot({'cron_time': ['2099-01-01T00:00:00Z']})[0], 'waiting')

    @patch('bloomin8_pull_server.datetime')
    def test_weekday_only_commute_preserves_weekend_infographic_and_dst(self, clock):
        clock.fromisoformat.side_effect = datetime.fromisoformat
        server = self.server()
        server.config.commute_weekdays = (0, 1, 2, 3, 4)
        for instant, slot, next_time in [
            ('2026-10-16T04:00:00+00:00', 'commute', '2026-10-16T05:45:00Z'),
            ('2026-10-16T12:00:00+00:00', 'infographic', '2026-10-17T05:45:00Z'),
            ('2026-10-17T04:00:00+00:00', 'waiting', '2026-10-17T05:45:00Z'),
            ('2026-10-17T05:45:00+00:00', 'infographic', '2026-10-18T05:45:00Z'),
            ('2026-10-18T05:45:00+00:00', 'infographic', '2026-10-19T04:00:00Z'),
            ('2026-10-19T04:00:00+00:00', 'commute', '2026-10-19T05:45:00Z'),
            ('2026-10-24T12:00:00+00:00', 'infographic', '2026-10-25T06:45:00Z'),
            ('2026-10-25T05:00:00+00:00', 'waiting', '2026-10-25T06:45:00Z'),
            ('2026-10-25T06:45:00+00:00', 'infographic', '2026-10-26T05:00:00Z')]:
            clock.now.side_effect = lambda tz=None: datetime.fromisoformat(instant).astimezone(tz)
            with self.subTest(instant=instant):
                self.assertEqual(server.display_slot({}), (slot, next_time))
                cfg = {'timezone': 'Europe/Ljubljana', 'scheduled_local_time': '07:45',
                       'commute_local_time': '06:00', 'commute_snapshot_path': '/private/snapshot.json',
                       'commute_weekdays': [0, 1, 2, 3, 4]}
                self.assertEqual(next_local_slot(cfg, datetime.fromisoformat(instant)).timestamp(),
                                 datetime.fromisoformat(next_time.replace('Z', '+00:00')).timestamp())

    @patch('bloomin8_pull_server.datetime')
    def test_weekend_early_wake_does_not_load_or_deliver_commute(self, clock):
        clock.fromisoformat.side_effect = datetime.fromisoformat
        clock.now.side_effect = lambda tz=None: datetime.fromisoformat('2026-10-17T04:00:00+00:00').astimezone(tz)
        server = self.server()
        server.config.commute_weekdays = (0, 1, 2, 3, 4)
        with patch.object(server, 'load_commute_snapshot', side_effect=AssertionError('weekend commute forbidden')):
            response = server.build_pull_response({})
        self.assertEqual(response['status'], 204)
        self.assertEqual(response['data']['next_cron_time'], '2026-10-17T05:45:00Z')

    def test_weekend_checker_is_quiet_without_snapshot(self):
        from frame_raw import check_daily_delivery
        cfg = {'timezone': 'Europe/Ljubljana', 'commute_snapshot_path': '/does/not/exist',
               'commute_weekdays': [0, 1, 2, 3, 4]}
        self.assertEqual(check_daily_delivery(cfg, datetime.fromisoformat('2026-10-17T06:05:00+02:00')), '')
        self.assertEqual(check_daily_delivery(cfg, datetime.fromisoformat('2026-10-18T06:05:00+02:00')), '')
        self.assertNotEqual(check_daily_delivery(cfg, datetime.fromisoformat('2026-10-19T06:05:00+02:00')), '')

    @patch('bloomin8_pull_server.datetime')
    def test_missing_commute_returns_retry_not_infographic(self, clock):
        clock.fromisoformat.side_effect = datetime.fromisoformat
        clock.now.side_effect = lambda tz=None: datetime.fromisoformat('2026-10-16T04:00:00+00:00').astimezone(tz)
        server = self.server()
        with patch.object(server, 'load_latest_metadata', side_effect=AssertionError('wrong lane')):
            response = server.build_pull_response({})
        self.assertEqual(response['status'], 204)
        self.assertEqual(response['data']['next_cron_time'], '2026-10-16T04:01:00Z')

    def test_mock_and_stale_snapshots_never_served(self):
        with tempfile.TemporaryDirectory() as directory:
            server = self.server()
            server.config.commute_snapshot_path = Path(directory) / 'snapshot.json'
            for data in [{'mock': True, 'generated_at': datetime.now(server.config.timezone).isoformat(), 'image_filename': 'mock_P.jpg'},
                         {'mock': False, 'generated_at': '2001-01-01T06:00:00+01:00', 'image_filename': 'old_P.jpg'},
                         {'mock': False, 'generated_at': datetime.now(server.config.timezone).isoformat(), 'image_filename': '../bad.jpg'}]:
                server.config.commute_snapshot_path.write_text(json.dumps(data))
                with self.assertRaises(PullServerError):
                    server.load_commute_snapshot()

    def test_render_is_portrait_baseline_420(self):
        with tempfile.TemporaryDirectory() as directory:
            output = render_board(mock_data(datetime.fromisoformat('2026-10-16T06:00:00+02:00')), Path(directory) / 'demo_P.jpg')
            with Image.open(output) as image:
                self.assertEqual(image.size, (1200, 1600))
                self.assertFalse(image.info.get('progressive', False))
                self.assertEqual(image.layer, [(1, 2, 2, 0), (2, 1, 1, 1), (3, 1, 1, 1)])

    def test_device_arm_preserves_slovenian_time_across_dst(self):
        cfg = {'timezone': 'Europe/Ljubljana', 'commute_snapshot_path': '/private/snapshot.json',
               'commute_local_time': '06:00', 'scheduled_local_time': '07:45'}
        self.assertEqual(next_local_slot(cfg, datetime.fromisoformat('2026-10-24T12:00:00+00:00')).isoformat(),
                         '2026-10-25T05:00:00+00:00')
        self.assertEqual(next_local_slot(cfg, datetime.fromisoformat('2026-10-25T05:01:00+00:00')).isoformat(),
                         '2026-10-25T06:45:00+00:00')

    def test_real_http_roundtrip_and_tokens(self):
        from commute_board import publish
        from io import BytesIO
        with tempfile.TemporaryDirectory() as directory:
            app = self.server()
            app.config.commute_snapshot_path = Path(directory) / 'snapshot.json'
            data = {'generated_at': datetime.now(app.config.timezone).isoformat(), 'mock': False,
                    'trains': {'status': 'unavailable', 'items': []}, 'roads': {'status': 'ok', 'items': []}}
            manifest = publish(data, app.config.commute_snapshot_path)
            http = ThreadingHTTPServer(('127.0.0.1', 0), RequestHandler)
            http.app = app  # type: ignore[attr-defined]
            thread = threading.Thread(target=http.serve_forever, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{http.server_port}'
            try:
                with self.assertRaises(HTTPError) as denied:
                    urlopen(base + '/eink_pull', timeout=3)
                self.assertEqual(denied.exception.code, 403)
                with patch.object(app, 'display_slot', return_value=('commute', '2026-10-17T05:45:00Z')):
                    with urlopen(Request(base + '/eink_pull', headers={'X-Access-Token': 'device-secret'}), timeout=3) as response:
                        body = json.load(response)
                    self.assertEqual(body['status'], 200)
                    self.assertTrue(body['data']['image_url'].endswith(manifest['image_filename']))
                path = '/image/image-secret/' + manifest['image_filename']
                with urlopen(base + path, timeout=3) as response:
                    self.assertEqual(response.headers['Content-Type'], 'image/jpeg')
                    encoded = response.read()
                self.assertEqual(encoded, (Path(directory) / manifest['image_filename']).read_bytes())
                with Image.open(BytesIO(encoded)) as image:
                    self.assertEqual(image.size, (1200, 1600))
                for bad_path, expected in [('/image/wrong/' + manifest['image_filename'], 403),
                                           ('/image/image-secret/commute-other_P.jpg', 404)]:
                    with self.assertRaises(HTTPError) as denied:
                        urlopen(base + bad_path, timeout=3)
                    self.assertEqual(denied.exception.code, expected)
            finally:
                http.shutdown()
                http.server_close()
                thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
