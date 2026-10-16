import unittest
from datetime import datetime
from unittest.mock import patch, Mock
from tempfile import TemporaryDirectory
from pathlib import Path
from PIL import Image
from commute_board import parse_weather, collect_weather
from commute_render import mock_data, render_board


class WeatherTests(unittest.TestCase):
    now = datetime.fromisoformat('2026-10-16T06:00:00+02:00')

    def payload(self, kranj, ljubljana):
        return [{'latitude': lat, 'longitude': lon, 'timezone': 'Europe/Ljubljana',
                 'daily_units': {'precipitation_probability_max': '%'},
                 'daily': {'time': ['2026-10-16'], 'precipitation_probability_max': [p]}}
                for lat, lon, p in [(46.24, 14.36, kranj), (46.05, 14.51, ljubljana)]]

    def test_either_city_at_threshold_shows_umbrella(self):
        for k, l in [(30, 0), (0, 30), (80, 70)]:
            with self.subTest(kranj=k, ljubljana=l):
                data = parse_weather(self.payload(k, l), self.now)
                self.assertEqual(data['status'], 'ok')
                self.assertTrue(data['rain_possible'])
                self.assertEqual(data['max_probability'], max(k, l))

    def test_below_threshold_stays_clear(self):
        data = parse_weather(self.payload(29, 0), self.now)
        self.assertFalse(data['rain_possible'])

    def test_missing_city_not_mistaken_for_dry(self):
        data = parse_weather(self.payload(0, None), self.now)
        self.assertEqual(data['status'], 'partial')
        self.assertIsNone(data['rain_possible'])
        self.assertTrue(parse_weather(self.payload(80, None), self.now)['rain_possible'])

    def test_wrong_day_units_timezone_location_rejected(self):
        for field, value in [('date', '2000-01-01'), ('units', 'mm'), ('timezone', 'UTC'), ('latitude', 0)]:
            payload = self.payload(0, 0)
            for row in payload:
                if field == 'date': row['daily']['time'] = [value]
                elif field == 'units': row['daily_units']['precipitation_probability_max'] = value
                else: row[field] = value
            data = parse_weather(payload, self.now)
            self.assertEqual(data['status'], 'unavailable')
            self.assertIsNone(data['rain_possible'])

    def test_invalid_probability_not_used(self):
        for probability in [-1, 101, True, '60', float('nan')]:
            data = parse_weather(self.payload(probability, probability), self.now)
            self.assertEqual(data['status'], 'unavailable')
            self.assertIsNone(data['rain_possible'])

    @patch('commute_board.get')
    def test_both_cities_requested_in_one_fetch(self, get):
        get.return_value = Mock(json=lambda: self.payload(30, 0))
        self.assertTrue(collect_weather(Mock(), self.now)['rain_possible'])
        self.assertEqual(get.call_count, 1)
        url = get.call_args.args[1]
        self.assertIn('timezone=Europe%2FLjubljana', url)
        self.assertIn('forecast_days=1', url)

    def test_umbrella_drawn_only_when_rain_possible(self):
        with TemporaryDirectory() as directory:
            pixels = []
            for value in [True, False]:
                data = mock_data(self.now)
                data['weather']['rain_possible'] = value
                path = render_board(data, Path(directory) / f'{value}.jpg')
                with Image.open(path) as image:
                    pixels.append(image.getpixel((1075, 1498)))
            self.assertLess(max(pixels[0]), 50)
            self.assertGreater(min(pixels[1]), 240)


if __name__ == '__main__':
    unittest.main()
