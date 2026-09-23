import io
import json
import tempfile
import unittest
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

    def test_image_filename_cannot_escape_archive_directory(self):
        server = PullServer(self.remote_config())
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(PullServerError):
                server.latest_image_path(
                    {"image_filename": "../secret.png"}, Path(temp_dir)
                )


if __name__ == "__main__":
    unittest.main()
