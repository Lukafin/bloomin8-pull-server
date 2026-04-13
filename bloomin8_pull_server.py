#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import formatdate
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

LOG = logging.getLogger("bloomin8_pull_server")


@dataclass
class Config:
    timezone_name: str
    scheduled_local_time: str
    retry_interval_minutes: int
    public_base_url: str
    device_token: str
    image_token: str
    device_width: int
    device_height: int
    latest_json_path: Path

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)


class PullServerError(RuntimeError):
    pass


class PullServer:
    def __init__(self, config: Config) -> None:
        self.config = config

    def load_latest_metadata(self) -> dict[str, Any]:
        try:
            return json.loads(self.config.latest_json_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise PullServerError(f"Missing latest.json at {self.config.latest_json_path}") from exc
        except json.JSONDecodeError as exc:
            raise PullServerError(f"Invalid latest.json: {exc}") from exc

    def latest_image_path(self, metadata: dict[str, Any]) -> Path:
        image_filename = metadata.get("image_filename")
        if not image_filename:
            raise PullServerError("latest.json missing image_filename")
        image_path = self.config.latest_json_path.parent / image_filename
        if not image_path.exists():
            raise PullServerError(f"Latest image does not exist: {image_path}")
        return image_path

    def todays_local_date(self) -> str:
        return datetime.now(self.config.timezone).date().isoformat()

    def scheduled_time_local(self, base: datetime | None = None) -> datetime:
        now_local = base.astimezone(self.config.timezone) if base else datetime.now(self.config.timezone)
        hour, minute = [int(part) for part in self.config.scheduled_local_time.split(":", 1)]
        candidate = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now_local:
            candidate += timedelta(days=1)
        return candidate

    def retry_time_utc(self) -> str:
        next_time = datetime.now(timezone.utc) + timedelta(minutes=self.config.retry_interval_minutes)
        return next_time.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def next_scheduled_utc(self) -> str:
        next_local = self.scheduled_time_local()
        return next_local.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def landscape_storage_dimensions(self) -> tuple[int, int]:
        return max(self.config.device_width, self.config.device_height), min(self.config.device_width, self.config.device_height)

    def image_public_url(self, metadata: dict[str, Any]) -> str:
        date_part = str(metadata.get("date") or "unknown")
        person_slug = slugify(str(metadata.get("person") or "unknown"))
        filename = f"{date_part}-{person_slug}_L.jpg"
        return f"{self.config.public_base_url}/image/{self.config.image_token}/{filename}"

    def build_pull_response(self, request_query: dict[str, list[str]]) -> dict[str, Any]:
        metadata = self.load_latest_metadata()
        latest_date = str(metadata.get("date") or "")
        today_local = self.todays_local_date()
        requested_cron_time = first_or_empty(request_query.get("cron_time"))
        LOG.info("eink_pull requested: latest_date=%s today_local=%s cron_time=%s", latest_date, today_local, requested_cron_time)

        if latest_date == today_local:
            return {
                "status": 200,
                "type": "SHOW",
                "message": "Latest daily image available",
                "data": {
                    "next_cron_time": self.next_scheduled_utc(),
                    "image_url": self.image_public_url(metadata),
                },
            }

        return {
            "status": 204,
            "message": f"Today's image is not published yet (latest: {latest_date or 'unknown'})",
            "data": {
                "next_cron_time": self.retry_time_utc(),
            },
        }

    def build_image_bytes(self) -> tuple[bytes, str]:
        metadata = self.load_latest_metadata()
        source_image = self.latest_image_path(metadata)
        target_width, target_height = self.landscape_storage_dimensions()

        with tempfile.TemporaryDirectory(prefix="bloomin8-pull-") as temp_dir:
            landscape_path = Path(temp_dir) / "landscape.jpg"
            final_path = Path(temp_dir) / "latest_L.jpg"

            scale_and_pad = [
                "ffmpeg",
                "-y",
                "-i",
                str(source_image),
                "-vf",
                (
                    f"scale={target_width}:{target_height}:force_original_aspect_ratio=decrease,"
                    f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2:white"
                ),
                "-q:v",
                "2",
                str(landscape_path),
            ]
            rotate_for_landscape_mode = [
                "ffmpeg",
                "-y",
                "-i",
                str(landscape_path),
                "-vf",
                "transpose=1",
                "-q:v",
                "2",
                str(final_path),
            ]

            run_ffmpeg(scale_and_pad)
            run_ffmpeg(rotate_for_landscape_mode)
            data = final_path.read_bytes()

        filename = f"{metadata.get('date', 'unknown')}-{slugify(str(metadata.get('person') or 'unknown'))}_L.jpg"
        return data, filename


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "Bloomin8PullServer/1.0"

    @property
    def app(self) -> PullServer:
        return self.server.app  # type: ignore[attr-defined]

    def do_HEAD(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.respond_json(HTTPStatus.OK, {"status": "ok"}, include_body=False)
            return
        if parsed.path.startswith("/image/"):
            self.handle_image(parsed, include_body=False)
            return
        self.respond_json(HTTPStatus.NOT_FOUND, {"error": "not found", "path": parsed.path}, include_body=False)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.respond_json(HTTPStatus.OK, {"status": "ok"})
            return
        if parsed.path == "/eink_pull":
            self.handle_eink_pull(parsed)
            return
        if parsed.path == "/eink_signal":
            self.handle_eink_signal(parsed)
            return
        if parsed.path.startswith("/image/"):
            self.handle_image(parsed)
            return
        self.respond_json(HTTPStatus.NOT_FOUND, {"error": "not found", "path": parsed.path})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        LOG.info("%s - %s", self.address_string(), format % args)

    def handle_eink_pull(self, parsed) -> None:
        token = self.headers.get("X-Access-Token", "")
        if token != self.app.config.device_token:
            self.respond_json(HTTPStatus.FORBIDDEN, {"error": "invalid token"})
            return
        query = parse_qs(parsed.query)
        payload = self.app.build_pull_response(query)
        self.respond_json(HTTPStatus.OK, payload)

    def handle_eink_signal(self, parsed) -> None:
        query = parse_qs(parsed.query)
        LOG.info("eink_signal: %s", json.dumps({k: v for k, v in query.items()}, ensure_ascii=False))
        self.respond_json(HTTPStatus.OK, {"status": 200, "message": "feedback recorded"})

    def handle_image(self, parsed, include_body: bool = True) -> None:
        prefix = f"/image/{self.app.config.image_token}/"
        if not parsed.path.startswith(prefix):
            self.respond_json(HTTPStatus.FORBIDDEN, {"error": "invalid image token"}, include_body=include_body)
            return
        try:
            image_bytes, filename = self.app.build_image_bytes()
        except PullServerError as exc:
            self.respond_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)}, include_body=include_body)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(image_bytes)))
        self.send_header("Content-Disposition", f'inline; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("Last-Modified", formatdate(usegmt=True))
        self.end_headers()
        if include_body:
            self.wfile.write(image_bytes)

    def respond_json(self, status: HTTPStatus, payload: dict[str, Any], include_body: bool = True) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if include_body:
            self.wfile.write(body)


def first_or_empty(values: list[str] | None) -> str:
    return values[0] if values else ""


def slugify(value: str) -> str:
    import re

    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "image"


def run_ffmpeg(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise PullServerError(f"ffmpeg failed: {result.stderr.strip()}")


def load_config(path: Path) -> Config:
    data = json.loads(path.read_text(encoding="utf-8"))
    return Config(
        timezone_name=data["timezone"],
        scheduled_local_time=data["scheduled_local_time"],
        retry_interval_minutes=int(data["retry_interval_minutes"]),
        public_base_url=data["public_base_url"].rstrip("/"),
        device_token=data["device_token"],
        image_token=data["image_token"],
        device_width=int(data["device_width"]),
        device_height=int(data["device_height"]),
        latest_json_path=Path(data["latest_json_path"]),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BLOOMIN8 scheduled pull server")
    default_config = Path(__file__).resolve().parent / "bloomin8_pull_config.json"
    parser.add_argument("--config", default=str(default_config))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8092)
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(message)s")
    config = load_config(Path(args.config))
    app = PullServer(config)
    server = ThreadingHTTPServer((args.host, args.port), RequestHandler)
    server.app = app  # type: ignore[attr-defined]
    LOG.info("Starting BLOOMIN8 pull server on http://%s:%s", args.host, args.port)
    LOG.info("Public base URL: %s", config.public_base_url)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
