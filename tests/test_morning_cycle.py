import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from configure_bloomin8_pull import next_local_slot
from bloomin8_pull_server import PullServer, PullServerError
import test_server


class MorningCycleTests(unittest.TestCase):
    def config(self):
        cfg = test_server.PullServerTests().remote_config()
        cfg.scheduled_local_time = '08:00'
        cfg.commute_local_times = ('06:00', '06:30', '07:00')
        cfg.commute_weekdays = (0, 1, 2, 3, 4)
        cfg.commute_snapshot_path = Path('/private/snapshot.json')
        return cfg

    @patch('bloomin8_pull_server.datetime')
    def test_chain_including_weekend_dst_and_old_0745_wake(self, clock):
        clock.fromisoformat.side_effect = datetime.fromisoformat
        server = PullServer(self.config())
        for moment, lane, following in [
            ('2026-10-23T05:59:00+02:00', 'waiting', '2026-10-23T04:00:00Z'),
            ('2026-10-23T06:00:00+02:00', 'commute', '2026-10-23T04:30:00Z'),
            ('2026-10-23T06:30:00+02:00', 'commute', '2026-10-23T05:00:00Z'),
            ('2026-10-23T07:00:00+02:00', 'commute', '2026-10-23T06:00:00Z'),
            ('2026-10-23T08:00:00+02:00', 'infographic', '2026-10-24T06:00:00Z'),
            ('2026-10-24T06:00:00+02:00', 'waiting', '2026-10-24T06:00:00Z'),
            ('2026-10-24T08:00:00+02:00', 'infographic', '2026-10-25T07:00:00Z'),
            ('2026-10-25T07:45:00+01:00', 'waiting', '2026-10-25T07:00:00Z'),
            ('2026-10-25T08:00:00+01:00', 'infographic', '2026-10-26T05:00:00Z'),
        ]:
            clock.now.side_effect = lambda tz=None, value=moment: datetime.fromisoformat(value).astimezone(tz)
            with self.subTest(moment=moment):
                self.assertEqual(server.display_slot({}), (lane, following))
                d = {'timezone': 'Europe/Ljubljana', 'scheduled_local_time': '08:00',
                     'commute_snapshot_path': '/private/snapshot.json',
                     'commute_local_times': ['06:00', '06:30', '07:00'], 'commute_weekdays': [0,1,2,3,4]}
                self.assertEqual(next_local_slot(d, datetime.fromisoformat(moment)).isoformat().replace('+00:00','Z'), following)

    @patch('bloomin8_pull_server.datetime')
    def test_second_slot_rejects_first_slots_stale_snapshot(self, clock):
        clock.fromisoformat.side_effect = datetime.fromisoformat
        clock.now.side_effect = lambda tz=None: datetime.fromisoformat('2026-10-23T06:30:00+02:00').astimezone(tz)
        with tempfile.TemporaryDirectory() as directory:
            cfg = self.config()
            cfg.commute_snapshot_path = Path(directory) / 'snapshot.json'
            cfg.commute_snapshot_path.write_text(json.dumps({'mock': False, 'generated_at':'2026-10-23T05:55:00+02:00',
                                                            'image_filename':'commute-test_P.jpg'}))
            (Path(directory) / 'commute-test_P.jpg').touch()
            with self.assertRaises(PullServerError):
                PullServer(cfg).load_commute_snapshot()

    def test_archive_rotation_is_stable_and_filters_language_age_and_today(self):
        from archive_rotation import select_entry
        rows = [dict(image_filename='Jane.png',language='sl',age_suitability_keys=['age_6'],date='2026-04-01'),
                dict(image_filename='River.png',language='sl',age_suitability_keys=['age_6'],date='2026-04-02'),
                dict(image_filename='German.png',language='de',age_suitability_keys=['age_6'],date='2026-04-03'),
                dict(image_filename='Adult.png',language='sl',age_suitability_keys=['adult'],date='2026-04-04'),
                dict(image_filename='New.png',language='sl',age_suitability_keys=['age_6'],date='2026-10-20')]
        names = ['German.png','Adult.png','New.png','Jane.png','River.png']
        day = datetime.fromisoformat('2026-10-20').date()
        chosen = select_entry(rows, names, day, day)
        self.assertEqual(chosen['image_filename'],'Jane.png')
        self.assertEqual(select_entry(list(reversed(rows)), names, day, day), chosen)
        tomorrow = datetime.fromisoformat('2026-10-21').date()
        self.assertEqual(select_entry(rows, names, tomorrow, day)['image_filename'], 'River.png')

    def test_notice_dates_and_route(self):
        from rail_notices import notice_validity, route_relevant
        from datetime import date
        self.assertTrue(route_relevant('Nadomestni prevoz Ljubljana – Jesenice – Beljak'))
        self.assertFalse(route_relevant('Nadomestni prevoz Lesce Bled – Jesenice'))
        self.assertFalse(route_relevant('Nadomestni prevoz Ljubljana – Borovnica'))
        title = 'Nadomestni prevoz Ljubljana – Jesenice (26. 9. – 6. 11. 2026)'
        self.assertEqual(notice_validity(title, 2026, date(2026,10,20))[0], 'active')
        self.assertEqual(notice_validity(title, 2026, date(2026,11,7))[0], 'inactive')
        self.assertEqual(notice_validity('Ljubljana – Kranj (4., 11. in 18. oktober)', 2025, date(2026,10,18))[0], 'inactive')
        self.assertEqual(notice_validity('Ljubljana – Kranj (4., 11. in 18. oktober)', 2026, date(2026,10,18))[0], 'active')
        self.assertEqual(notice_validity('Ljubljana – Kranj (4., 11. in 18. oktober)', 2026, date(2026,10,19))[0], 'inactive')
        self.assertEqual(notice_validity('Ljubljana – Kranj do preklica', 2026, date(2026,10,20))[0], 'unknown')


if __name__ == '__main__':
    unittest.main()
