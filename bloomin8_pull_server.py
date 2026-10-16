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
from urllib.error import URLError
from urllib.parse import parse_qs, quote, urljoin, urlparse
from urllib.request import Request, urlopen
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
    latest_json_path: Path | None
    latest_json_url: str
    image_base_url: str
    remote_timeout_seconds: int
    display_orientation: str = "landscape"
    commute_local_time: str = "06:00"
    commute_snapshot_path: Path | None = None
    commute_delivery_mode: str = "native-jpeg"
    device_base_url: str = ""
    expected_device_sn: str = ""
    commute_weekdays: tuple[int, ...] = tuple(range(7))

    def __post_init__(self) -> None:
        if any(type(day) is not int or not 0 <= day <= 6 for day in self.commute_weekdays):
            raise ValueError("commute_weekdays must contain integers 0=Monday through 6=Sunday")
        if self.display_orientation not in {"portrait", "landscape"}:
            raise ValueError("display_orientation must be portrait or landscape")
        if self.commute_snapshot_path and self.commute_local_time >= self.scheduled_local_time:
            raise ValueError("commute time must precede infographic time")
        if self.commute_delivery_mode not in {"native-jpeg", "raw-on-wake"}:
            raise ValueError("Unknown commute delivery mode")
        if self.commute_delivery_mode == "raw-on-wake" and not (self.commute_snapshot_path and self.device_base_url and self.expected_device_sn):
            raise ValueError("Raw-on-wake needs snapshot path, LAN device URL and exact serial")

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)


class PullServerError(RuntimeError):
    pass


class PullServer:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.raw_delivery = None
        if config.commute_delivery_mode == "raw-on-wake":
            from frame_raw import RawWakeDelivery
            self.raw_delivery = RawWakeDelivery(config)

    def load_latest_metadata(self) -> dict[str, Any]:
        if self.config.latest_json_url:
            separator = "&" if "?" in self.config.latest_json_url else "?"
            url = f"{self.config.latest_json_url}{separator}v={int(datetime.now(timezone.utc).timestamp())}"
            request = Request(
                url,
                headers={
                    "User-Agent": "Bloomin8PullServer/1.1",
                    "Cache-Control": "no-cache",
                },
            )
            try:
                with urlopen(request, timeout=self.config.remote_timeout_seconds) as response:
                    return json.loads(response.read().decode("utf-8"))
            except (URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PullServerError(f"Could not load remote latest.json: {exc}") from exc

        if self.config.latest_json_path is None:
            raise PullServerError("No latest.json source configured")
        try:
            return json.loads(self.config.latest_json_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise PullServerError(f"Missing latest.json at {self.config.latest_json_path}") from exc
        except json.JSONDecodeError as exc:
            raise PullServerError(f"Invalid latest.json: {exc}") from exc

    def latest_image_path(self, metadata: dict[str, Any], temp_dir: Path) -> Path:
        image_filename = metadata.get("image_filename")
        if not image_filename:
            raise PullServerError("latest.json missing image_filename")

        image_filename = str(image_filename)
        if Path(image_filename).name != image_filename:
            raise PullServerError("latest.json image_filename must be a basename")

        if self.config.latest_json_url:
            image_base_url = self.config.image_base_url or urljoin(self.config.latest_json_url, ".")
            image_url = urljoin(f"{image_base_url.rstrip('/')}/", quote(image_filename))
            request = Request(image_url, headers={"User-Agent": "Bloomin8PullServer/1.1"})
            try:
                with urlopen(request, timeout=self.config.remote_timeout_seconds) as response:
                    image_bytes = response.read(25 * 1024 * 1024 + 1)
            except (URLError, TimeoutError) as exc:
                raise PullServerError(f"Could not download latest image: {exc}") from exc
            if len(image_bytes) > 25 * 1024 * 1024:
                raise PullServerError("Latest image exceeds 25 MiB limit")
            image_path = temp_dir / image_filename
            image_path.write_bytes(image_bytes)
            return image_path

        if self.config.latest_json_path is None:
            raise PullServerError("No local latest.json path configured")
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

    def next_scheduled_utc(self, completed_date: str | None = None) -> str:
        next_local = self.scheduled_time_local()
        # A ready image consumes its local calendar day, even when the frame's
        # clock wakes it before our scheduled instant. Never re-arm that day.
        if completed_date is not None and next_local.date().isoformat() == completed_date:
            next_local += timedelta(days=1)
        return next_local.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def landscape_storage_dimensions(self) -> tuple[int, int]:
        return max(self.config.device_width, self.config.device_height), min(self.config.device_width, self.config.device_height)

    def output_dimensions(self) -> tuple[int, int]:
        width, height = self.landscape_storage_dimensions()
        return (height, width) if self.config.display_orientation == "portrait" else (width, height)

    def image_public_url(self, metadata: dict[str, Any]) -> str:
        date_part = str(metadata.get("date") or "unknown")
        person_slug = slugify(str(metadata.get("person") or "unknown"))
        orientation = "P" if self.config.display_orientation == "portrait" else "L"
        filename = f"{date_part}-{person_slug}-jpeg420-v1_{orientation}.jpg"
        return f"{self.config.public_base_url}/image/{self.config.image_token}/{filename}"

    def build_pull_response(self, request_query: dict[str, list[str]]) -> dict[str, Any]:
        if self.config.commute_snapshot_path:
            slot, next_time = self.display_slot(request_query)
            if slot == "waiting":
                return {"status": 204, "message": "Waiting for next scheduled edition",
                        "data": {"next_cron_time": next_time}}
            if slot == "commute":
                return self.build_commute_response(next_time)
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
                    "next_cron_time": self.display_slot(request_query)[1] if self.config.commute_snapshot_path else self.next_scheduled_utc(completed_date=latest_date),
                    "image_url": self.image_public_url(metadata),
                },
            }

        return {
            "status": 204,
            "message": f"Today's image is not published yet (latest: {latest_date or 'unknown'})",
            "data": {
                "next_cron_time": min(self.retry_time_utc(), self.display_slot(request_query)[1]) if self.config.commute_snapshot_path else self.retry_time_utc(),
            },
        }

    def build_commute_response(self, next_time: str) -> dict[str, Any]:
        try:
            snapshot = self.load_commute_snapshot()
        except PullServerError as exc:
            retry = datetime.now(timezone.utc) + timedelta(minutes=1)
            retry = min(retry, datetime.fromisoformat(next_time.replace("Z", "+00:00")))
            return {"status": 204, "message": str(exc), "data": {"next_cron_time": utc_string(retry)}}
        if self.raw_delivery is not None:
            return {"status": 204, "message": "Commute pixel delivery uses the authenticated native wake",
                    "data": {"next_cron_time": next_time, "delivery": "raw-on-wake"}}
        return {"status": 200, "type": "SHOW", "message": "Morning commute snapshot",
                "data": {"next_cron_time": next_time,
                         "image_url": f"{self.config.public_base_url}/image/{self.config.image_token}/{snapshot['image_filename']}"}}

    def display_slot(self, request_query: dict[str, list[str]]) -> tuple[str, str]:
        """Two local-clock slots, allowing a bounded early device wake (clock drift)."""
        now = datetime.now(self.config.timezone)
        requested = first_or_empty(request_query.get("cron_time"))
        try:
            wake = datetime.fromisoformat(requested.replace("Z", "+00:00"))
            if wake.tzinfo and abs((wake - now).total_seconds()) <= 300:
                now = max(now, wake.astimezone(self.config.timezone))
        except (ValueError, TypeError):
            pass
        ch, cm = map(int, self.config.commute_local_time.split(":"))
        ih, im = map(int, self.config.scheduled_local_time.split(":"))
        commute = now.replace(hour=ch, minute=cm, second=0, microsecond=0)
        infographic = now.replace(hour=ih, minute=im, second=0, microsecond=0)
        if now.weekday() not in self.config.commute_weekdays:
            if now < infographic:
                return "waiting", utc_string(infographic)
        elif now < commute:
            return "waiting", utc_string(commute)
        elif now < infographic:
            return "commute", utc_string(infographic)
        from configure_bloomin8_pull import next_local_slot
        next_slot = next_local_slot({'timezone': self.config.timezone_name,
                                    'scheduled_local_time': self.config.scheduled_local_time,
                                    'commute_snapshot_path': str(self.config.commute_snapshot_path),
                                    'commute_local_time': self.config.commute_local_time,
                                    'commute_weekdays': self.config.commute_weekdays}, now)
        return "infographic", utc_string(next_slot)

    def load_commute_snapshot(self) -> dict[str, Any]:
        try:
            if self.config.commute_snapshot_path is None:
                raise ValueError("Commute display is not enabled")
            snapshot = json.loads(self.config.commute_snapshot_path.read_text(encoding="utf-8"))
            if snapshot.get("mock") is not False:
                raise ValueError("Mock snapshots are never served by the daily feed")
            generated = datetime.fromisoformat(snapshot["generated_at"])
            if generated.tzinfo is None:
                raise ValueError("Commute timestamp must include a timezone")
            now = datetime.now(self.config.timezone)
            if generated.astimezone(self.config.timezone).date() != now.date() or generated > now + timedelta(minutes=5):
                raise ValueError("Today's commute snapshot is not ready")
            name = snapshot["image_filename"]
            if not isinstance(name, str) or Path(name).name != name or not name.startswith("commute-") or not name.endswith("_P.jpg"):
                raise ValueError("Invalid commute image filename")
            image = self.config.commute_snapshot_path.parent / name
            if not image.is_file():
                raise ValueError("Commute image is missing")
            return snapshot
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise PullServerError(str(exc)) from exc

    def build_image_bytes(self) -> tuple[bytes, str]:
        metadata = self.load_latest_metadata()
        target_width, target_height = self.output_dimensions()

        with tempfile.TemporaryDirectory(prefix="bloomin8-pull-") as temp_dir:
            temp_path = Path(temp_dir)
            source_image = self.latest_image_path(metadata, temp_path)
            fitted_path = temp_path / "landscape.jpg"
            final_path = temp_path / "latest_L.jpg"

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
                str(fitted_path),
            ]
            rotate_for_landscape_mode = [
                "ffmpeg",
                "-y",
                "-i",
                str(fitted_path),
                "-vf",
                "null" if self.config.display_orientation == "portrait" else "transpose=1",
                "-pix_fmt",
                "yuvj420p",
                "-q:v",
                "2",
                str(final_path),
            ]

            run_ffmpeg(scale_and_pad)
            run_ffmpeg(rotate_for_landscape_mode)
            data = final_path.read_bytes()

        filename = self.image_public_url(metadata).rsplit("/", 1)[-1]
        return data, filename


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "Bloomin8PullServer/1.1"

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
        if parsed.path == "/commute-preview/eink_pull":
            self.handle_eink_pull(parsed, preview=True)
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

    def handle_eink_pull(self, parsed, preview: bool = False) -> None:
        token = self.headers.get("X-Access-Token", "")
        if token != self.app.config.device_token:
            self.respond_json(HTTPStatus.FORBIDDEN, {"error": "invalid token"})
            return
        query = parse_qs(parsed.query)
        if preview and (self.app.raw_delivery is None or first_or_empty(query.get("device_id")) != self.app.config.expected_device_sn):
            self.respond_json(HTTPStatus.FORBIDDEN, {"error": "Native commute preview is disabled for this device"})
            return
        try:
            payload = self.app.build_commute_response(self.app.display_slot(query)[1]) if preview else self.app.build_pull_response(query)
        except PullServerError as exc:
            next_time = self.app.retry_time_utc()
            if self.app.config.commute_snapshot_path:
                next_time = min(next_time, self.app.display_slot(query)[1])
            payload = {"status": 204, "message": str(exc), "data": {"next_cron_time": next_time}}
        if (self.app.raw_delivery is not None and payload.get("data", {}).get("delivery") == "raw-on-wake"
                and first_or_empty(query.get("device_id")) == self.app.config.expected_device_sn):
            self.app.raw_delivery.start(self.app.load_commute_snapshot(), payload["data"]["next_cron_time"])
            # A 204 may put the frame back to sleep before the LAN transfer.
            # Keep its pull open until raw rendering finishes, below the
            # documented ~60s native HTTP deadline.
            self.app.raw_delivery.wait(timeout=50)
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
        requested_name = parsed.path[len(prefix):]
        if requested_name.startswith("commute-"):
            try:
                snapshot = self.app.load_commute_snapshot()
                if requested_name != snapshot["image_filename"]:
                    raise PullServerError("Commute image is not the current snapshot")
                snapshot_path = self.app.config.commute_snapshot_path
                assert snapshot_path is not None
                image_bytes = (snapshot_path.parent / requested_name).read_bytes()
                filename = requested_name
            except (PullServerError, OSError) as exc:
                self.respond_json(HTTPStatus.NOT_FOUND, {"error": str(exc)}, include_body=include_body)
                return
        else:
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


def utc_string(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
    latest_json_path = data.get("latest_json_path")
    return Config(
        timezone_name=data["timezone"],
        scheduled_local_time=data["scheduled_local_time"],
        retry_interval_minutes=int(data["retry_interval_minutes"]),
        public_base_url=data["public_base_url"].rstrip("/"),
        device_token=data["device_token"],
        image_token=data["image_token"],
        device_width=int(data["device_width"]),
        device_height=int(data["device_height"]),
        latest_json_path=Path(latest_json_path) if latest_json_path else None,
        latest_json_url=str(data.get("latest_json_url") or "").strip(),
        image_base_url=str(data.get("image_base_url") or "").strip(),
        remote_timeout_seconds=int(data.get("remote_timeout_seconds", 20)),
        display_orientation=str(data.get("display_orientation", "landscape")),
        commute_local_time=str(data.get("commute_local_time", "06:00")),
        commute_weekdays=tuple(data.get("commute_weekdays", range(7))),
        commute_snapshot_path=Path(data["commute_snapshot_path"]) if data.get("commute_snapshot_path") else None,
        commute_delivery_mode=str(data.get("commute_delivery_mode", "native-jpeg")),
        device_base_url=str(data.get("device_base_url", "")),
        expected_device_sn=str(data.get("expected_device_sn", "")),
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
