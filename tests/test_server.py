import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from bloomin8_pull_server import Config, PullServer, PullServerError, load_config


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


class PullServerTests(unittest.TestCase):
    def remote_config(self) -> Config:
        return Config(
            timezone_name="Europe/Ljubljana",
            scheduled_local_time="07:30",
            retry_interval_minutes=15,
            public_base_url="https://frame.example/pull",
            device_token="device-secret",
            image_token="image-secret",
            device_width=1200,
            device_height=1600,
            latest_json_path=None,
            latest_json_url="https://archive.example/latest.json",
            image_base_url="https://archive.example/",
            remote_timeout_seconds=20,
        )

    def test_load_config_accepts_remote_archive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "timezone": "Europe/Ljubljana",
                        "scheduled_local_time": "07:30",
                        "retry_interval_minutes": 15,
                        "public_base_url": "https://frame.example/pull",
                        "device_token": "device-secret",
                        "image_token": "image-secret",
                        "device_width": 1200,
                        "device_height": 1600,
                        "latest_json_url": "https://archive.example/latest.json",
                        "image_base_url": "https://archive.example/",
                        "remote_timeout_seconds": 12,
                        "latest_json_path": None,
                    }
                ),
                encoding="utf-8",
            )
            config = load_config(path)

        self.assertEqual(config.scheduled_local_time, "07:30")
        self.assertEqual(config.latest_json_url, "https://archive.example/latest.json")
        self.assertIsNone(config.latest_json_path)
        self.assertEqual(config.remote_timeout_seconds, 12)

    @patch("bloomin8_pull_server.urlopen")
    def test_remote_metadata_bypasses_cache(self, mocked_urlopen):
        metadata = {"date": "2026-09-23", "image_filename": "Morning.png"}
        mocked_urlopen.return_value = FakeResponse(json.dumps(metadata).encode("utf-8"))

        loaded = PullServer(self.remote_config()).load_latest_metadata()

        self.assertEqual(loaded, metadata)
        request = mocked_urlopen.call_args.args[0]
        self.assertRegex(request.full_url, r"latest\.json\?v=\d+$")
        self.assertEqual(request.get_header("Cache-control"), "no-cache")

    @patch("bloomin8_pull_server.urlopen")
    def test_remote_image_is_downloaded_from_public_archive(self, mocked_urlopen):
        mocked_urlopen.return_value = FakeResponse(b"image-bytes")
        server = PullServer(self.remote_config())

        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = server.latest_image_path(
                {"image_filename": "Morning image.png"}, Path(temp_dir)
            )
            self.assertEqual(image_path.read_bytes(), b"image-bytes")

        request = mocked_urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://archive.example/Morning%20image.png",
        )

    @patch("bloomin8_pull_server.datetime")
    def test_completed_image_never_reschedules_same_morning(self, clock):
        server = PullServer(self.remote_config())
        for instant in ["2026-10-16T05:27:40+00:00", "2026-10-16T05:29:18+00:00",
                        "2026-10-16T05:30:00+00:00", "2026-10-16T05:30:25+00:00"]:
            clock.now.side_effect = lambda tz=None: datetime.fromisoformat(instant).astimezone(tz)
            with self.subTest(instant=instant):
                self.assertEqual(server.next_scheduled_utc(completed_date="2026-10-16"),
                                 "2026-10-17T05:30:00Z")

    @patch("bloomin8_pull_server.datetime")
    def test_completed_image_preserves_local_time_across_dst(self, clock):
        server = PullServer(self.remote_config())
        clock.now.side_effect = lambda tz=None: datetime.fromisoformat("2026-10-24T05:27:00+00:00").astimezone(tz)
        self.assertEqual(server.next_scheduled_utc(completed_date="2026-10-24"),
                         "2026-10-25T06:30:00Z")

    @patch("bloomin8_pull_server.datetime")
    def test_uncompleted_schedule_can_still_use_today(self, clock):
        clock.now.side_effect = lambda tz=None: datetime.fromisoformat("2026-10-16T05:27:00+00:00").astimezone(tz)
        self.assertEqual(PullServer(self.remote_config()).next_scheduled_utc(),
                         "2026-10-16T05:30:00Z")

    def test_ready_response_marks_date_completed(self):
        server = PullServer(self.remote_config())
        with patch.object(server, "load_latest_metadata", return_value={"date": "2026-10-16", "person": "Alberto Bayo"}), \
             patch.object(server, "todays_local_date", return_value="2026-10-16"), \
             patch.object(server, "next_scheduled_utc", return_value="2026-10-17T05:30:00Z") as schedule:
            response = server.build_pull_response({})
        schedule.assert_called_once_with(completed_date="2026-10-16")
        self.assertEqual(response["data"]["next_cron_time"], "2026-10-17T05:30:00Z")

    def test_format_version_bypasses_failed_current_image_cache(self):
        url = PullServer(self.remote_config()).image_public_url(
            {"date": "2026-10-16", "person": "Alberto Bayo"})
        self.assertTrue(url.endswith("2026-10-16-alberto-bayo-jpeg420-v1_L.jpg"))

    def test_encoder_produces_baseline_420_jpeg(self):
        server = PullServer(self.remote_config())
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "fixture.ppm"
            source.write_bytes(b"P6\n16 12\n255\n" + bytes([80, 140, 200]) * 16 * 12)
            with patch.object(server, "load_latest_metadata", return_value={
                    "date": "2026-10-16", "person": "Fixture"}), \
                 patch.object(server, "latest_image_path", return_value=source):
                data, filename = server.build_image_bytes()
        self.assertTrue(filename.endswith("-jpeg420-v1_L.jpg"))
        # Parse JPEG markers with stdlib: baseline SOF0, dimensions and sampling.
        offset = 2
        while offset < len(data):
            self.assertEqual(data[offset], 0xFF)
            marker = data[offset + 1]
            length = int.from_bytes(data[offset + 2:offset + 4], "big")
            if marker == 0xC0:
                frame = data[offset + 4:offset + 2 + length]
                self.assertEqual(frame[:6], bytes([8, 6, 64, 4, 176, 3]))
                self.assertEqual([frame[i] for i in [7, 10, 13]], [0x22, 0x11, 0x11])
                break
            self.assertNotIn(marker, [0xC2, 0xDA], "Expected baseline SOF0 before scan")
            offset += 2 + length
        else:
            self.fail("Missing JPEG baseline frame")

    def test_portrait_url_and_dimensions(self):
        config = self.remote_config()
        config.display_orientation = "portrait"
        server = PullServer(config)
        self.assertEqual(server.output_dimensions(), (1200, 1600))
        self.assertTrue(server.image_public_url({"date": "2026-10-16", "person": "Bayo"})
                        .endswith("-jpeg420-v1_P.jpg"))

    def test_portrait_pipeline_does_not_rotate(self):
        config = self.remote_config()
        config.display_orientation = "portrait"
        server = PullServer(config)
        commands = []
        def encode(command):
            commands.append(command)
            Path(command[-1]).write_bytes(b"test-only-encoded-fixture")
        with patch.object(server, "load_latest_metadata", return_value={"person": "Fixture"}), \
             patch.object(server, "latest_image_path", return_value=Path("fixture.png")), \
             patch("bloomin8_pull_server.run_ffmpeg", side_effect=encode):
            _, filename = server.build_image_bytes()
        self.assertIn("scale=1200:1600", commands[0][commands[0].index("-vf") + 1])
        self.assertEqual(commands[1][commands[1].index("-vf") + 1], "null")
        self.assertEqual(commands[1][commands[1].index("-pix_fmt") + 1], "yuvj420p")
        self.assertTrue(filename.endswith("_P.jpg"))

    def test_image_filename_cannot_escape_archive_directory(self):
        server = PullServer(self.remote_config())
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(PullServerError):
                server.latest_image_path(
                    {"image_filename": "../secret.png"}, Path(temp_dir)
                )


if __name__ == "__main__":
    unittest.main()
