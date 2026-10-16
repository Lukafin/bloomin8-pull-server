import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch
from commute_board import (fresh, road_on_route, route_to_ljubljana, train_candidates,
                           collect, collect_trains, publish)
from pathlib import Path


class SourceTests(unittest.TestCase):
    def test_road_direction_and_corridor(self):
        title = 'A2-E61, Karavanke - Ljubljana: Zastoj'
        for summary, wanted in [
            ('A2, Karavanke - Ljubljana, priključek Vodice - Šmartno v smeri Ljubljane, zastoj 3 km.', True),
            ('A2, Karavanke - Ljubljana, Ljubljana Šmartno v smeri Jesenic, dela.', False),
            ('A2, Karavanke - Ljubljana, Vodice, zaprto v obeh smereh.', True),
            ('A2, Karavanke - Ljubljana, predor Karavanke - Jesenice, dela.', False),
            ('A2, Karavanke - Ljubljana, Lesce - priključek Kranj zahod, zastoj.', False),
            ('A2, Karavanke - Ljubljana, Brnik - Vodice, nesreča.', True)]:
            with self.subTest(summary=summary):
                self.assertEqual(road_on_route(title, summary), wanted)
        self.assertFalse(road_on_route('A2-E70, Obrežje - Ljubljana', 'A2, Obrežje - Ljubljana, Ljubljana, zastoj'))

    def test_route_order_not_just_destination(self):
        k = {'stopId': '1122811', 'stopName': 'Kranj'}
        l = {'stopId': '1122804', 'stopName': 'Ljubljana'}
        self.assertEqual(route_to_ljubljana({'rows': [k, l]}), l)
        self.assertIsNone(route_to_ljubljana({'rows': [l, k]}))
        self.assertIsNone(route_to_ljubljana({'rows': [k]}))

    def test_window_excludes_passed_and_northbound(self):
        now = datetime.fromisoformat('2026-10-16T06:00:00+02:00')
        row = {'trainLabel': 'TEST 1', 'originStopName': 'Jesenice', 'destinationStopName': 'Ljubljana',
               'scheduledDeparture': '2026-10-16T06:10:00+02:00'}
        board = {'stopName': 'Kranj', 'serviceDate': '2026-10-16', 'rows': [
            row, dict(row, scheduledDeparture='2026-10-16T05:50:00+02:00'),
            dict(row, originStopName='Ljubljana'), dict(row, scheduledDeparture='2026-10-16T09:00:00+02:00')]}
        self.assertEqual(train_candidates(board, now), [row])
        with self.assertRaises(ValueError):
            train_candidates(dict(board, serviceDate='2001-01-01'), now)

    def test_stale_and_future_sources_rejected(self):
        now = datetime.fromisoformat('2026-10-16T06:00:00+02:00')
        for other in [now - timedelta(minutes=11), now + timedelta(minutes=6)]:
            with self.assertRaises(ValueError):
                fresh(other, now)
        fresh(now - timedelta(minutes=5), now)

    @patch('commute_board.get')
    def test_uses_kranj_departure_not_train_max_or_arrival(self, get):
        now = datetime.fromisoformat('2026-10-16T06:00:00+02:00')
        source = {'serviceDate': '2026-10-16', 'lastUpdated': now.timestamp() * 1000}
        board = {'stopName': 'Kranj', 'serviceDate': '2026-10-16', 'rows': [
            {'trainLabel': 'TEST 1', 'originStopName': 'Jesenice', 'destinationStopName': 'Ljubljana',
             'scheduledDeparture': '2026-10-16T06:10:00+02:00', 'delayDepartureSec': 900}]}
        timeline = {'serviceDate': '2026-10-16', 'rows': [
            {'stopName': 'Kranj', 'departure': {'delaySec': 120}, 'arrival': {'delaySec': 600}},
            {'stopName': 'Ljubljana', 'arrival': {'delaySec': 700}}]}
        get.side_effect = [Mock(json=lambda: source), Mock(json=lambda: board), Mock(json=lambda: timeline)]
        result = collect_trains(Mock(), now)
        self.assertEqual(result['items'][0]['delay_minutes'], 2)

    @patch('commute_board.collect_trains', side_effect=ValueError('stale'))
    @patch('commute_board.collect_roads', side_effect=ValueError('bad XML'))
    def test_unavailable_never_means_clear(self, *mocks):
        data = collect()
        self.assertFalse(data['mock'])
        self.assertEqual(data['trains']['status'], 'unavailable')
        self.assertEqual(data['roads']['status'], 'unavailable')

    def test_demo_cannot_be_published(self):
        with self.assertRaises(ValueError):
            publish({'mock': True}, Path('/must/not/write/snapshot.json'))


if __name__ == '__main__':
    unittest.main()
