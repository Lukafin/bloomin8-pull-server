import hashlib
import io
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from PIL import Image
import refresh_archive as refresh
from archive_rotation import load_selection


class ArchiveRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'playlist.json'
        self.day = date(2026, 11, 9)
        self.old = [dict(date='2026-08-01', image_filename=name, language='sl',
                         age_suitability_keys=['age_6']) for name in ['a.png', 'b.png']]
        refresh.atomic_json(self.path, {'anchor_date': '2026-10-21', 'filenames': ['a.png', 'b.png'], 'entries': self.old})
        self.new = dict(date='2026-11-09', image_filename='new.png', person='Hobotnica',
                        language='sl', age_suitability_keys=['age_6'])
        buffer = io.BytesIO()
        Image.new('RGB', (1024, 1536), 'white').save(buffer, format='PNG')
        self.data = buffer.getvalue()
        self.note = 'Actual image inspected: Slovenian, clear portrait, age appropriate.'
        self.entries = patch.object(refresh, 'public_entries', return_value=[self.new])
        self.fetch = patch.object(refresh, 'fetch', return_value=self.data)
        self.entries.start()
        self.fetch.start()
        self.addCleanup(self.entries.stop)
        self.addCleanup(self.fetch.stop)

    def stage(self):
        return refresh.candidates(self.path, self.day)

    def approve(self):
        return refresh.review(self.path, self.day, 'new.png', self.note, True)

    def test_approval_preserves_today_prioritizes_tomorrow_and_retains_rotation(self):
        previous = load_selection(self.path, self.day)
        result = self.stage()
        self.assertEqual(len(result['candidates']), 1)
        approved = self.approve()
        self.assertTrue(approved['today_unchanged'])
        self.assertEqual(load_selection(self.path, self.day), previous)
        self.assertEqual(load_selection(self.path, self.day + timedelta(days=1))['image_filename'], 'new.png')
        cycle = [load_selection(self.path, self.day + timedelta(days=n))['image_filename'] for n in range(1, 4)]
        self.assertEqual(set(cycle), {'a.png', 'b.png', 'new.png'})
        self.assertEqual((self.path.parent / 'images' / 'new.png').read_bytes(), self.data)
        self.assertEqual(refresh.candidates(self.path, self.day)['status'], 'already_refreshed_today')
        self.assertEqual(self.approve()['status'], 'already_approved')
        self.assertTrue(self.path.with_name('playlist.before-refresh-2026-11-09.json').exists())

    def test_running_server_consumes_refreshed_playlist_without_restart(self):
        from datetime import datetime
        from bloomin8_pull_server import PullServer
        import test_server
        config = test_server.PullServerTests().remote_config()
        config.archive_rotation_path = self.path
        server = PullServer(config)
        with patch('bloomin8_pull_server.datetime') as clock:
            clock.now.return_value = datetime(2026, 11, 10)
            old = server.load_latest_metadata()['image_filename']
            self.stage()
            self.approve()
            self.assertNotEqual(old, 'new.png')
            self.assertEqual(server.load_latest_metadata()['image_filename'], 'new.png')
            metadata = server.load_latest_metadata()
            self.assertEqual(server.latest_image_path(metadata, self.path.parent).read_bytes(), self.data)

    def test_second_distinct_admission_same_day_is_refused(self):
        self.stage()
        self.approve()
        with self.assertRaisesRegex(ValueError, 'Only one'):
            refresh.review(self.path, self.day, 'another.png', self.note, True)

    def test_reject_persists_and_never_changes_selection(self):
        previous = load_selection(self.path, self.day)
        self.stage()
        refresh.review(self.path, self.day, 'new.png', 'Actual visible text is too dense for the children.', False)
        self.assertEqual(load_selection(self.path, self.day), previous)
        self.assertEqual(self.stage()['candidates'], [])

    def test_eligibility_filters_wrong_language_age_future_old_and_traversal(self):
        for changes in [dict(language='en'), dict(age_suitability_keys=['adult']),
                        dict(date='2026-11-10'), dict(date='2026-10-21'),
                        dict(image_filename='../new.png'), dict(image_filename='a\\new.png')]:
            with self.subTest(changes=changes):
                self.assertFalse(refresh.eligible(self.new | changes, date(2026, 10, 21), self.day))
        self.assertTrue(refresh.eligible(self.new, date(2026, 10, 21), self.day))

    def test_landscape_rejected_before_review(self):
        buffer = io.BytesIO()
        Image.new('RGB', (1536, 1024)).save(buffer, format='PNG')
        with patch.object(refresh, 'fetch', return_value=buffer.getvalue()):
            result = self.stage()
        self.assertEqual(result['candidates'], [])
        self.assertEqual(len(result['failures']), 1)

    def test_changed_staged_bytes_do_not_modify_playlist(self):
        self.stage()
        before = self.path.read_bytes()
        (self.path.parent / 'review-candidates' / 'new.png').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'bytes changed'):
            self.approve()
        self.assertEqual(self.path.read_bytes(), before)

    def test_unpublished_image_and_changed_public_bytes_fail_closed(self):
        self.stage()
        before = self.path.read_bytes()
        with patch.object(refresh, 'public_entries', return_value=[]):
            with self.assertRaisesRegex(ValueError, 'published archive'):
                self.approve()
        with patch.object(refresh, 'fetch', return_value=b'changed'):
            with self.assertRaisesRegex(ValueError, 'Published bytes changed'):
                self.approve()
        self.assertEqual(self.path.read_bytes(), before)

    def test_stale_review_refused_and_since_survives_anchor_changes(self):
        self.stage()
        with self.assertRaisesRegex(ValueError, 'stale'):
            refresh.review(self.path, self.day + timedelta(days=1), 'new.png', self.note, True)
        self.approve()
        self.assertEqual(refresh.read_json(self.path)['refresh']['since'], '2026-10-21')


if __name__ == '__main__':
    unittest.main()
