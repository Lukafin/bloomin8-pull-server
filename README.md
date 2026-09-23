# bloomin8-pull-server

Small Python server for BLOOMIN8 scheduled pull mode.

It exposes:
- `GET /health`
- `GET /eink_pull`
- `GET /eink_signal`
- `GET /image/<token>/<name>_L.jpg`

The server reads the latest daily image from the public Famous People Infographic Network archive (or an optional local archive), prepares a BLOOMIN8-compatible landscape JPEG, and returns a public image URL to the frame.

## Why this exists

BLOOMIN8 supports a native pull workflow via `/upstream/pull_settings`:
- the frame sleeps until a configured UTC `cron_time`
- then wakes up and calls `{upstream_url}/eink_pull`
- downloads the returned `image_url`
- displays it
- sleeps again until `next_cron_time`

This avoids the push-while-sleeping problem common with battery-powered e-ink frames.

## Features

- token-protected `/eink_pull` via `X-Access-Token`
- tokenized image URL path segment
- automatic latest-image lookup from a remote or local `latest.json`
- dynamic JPEG preparation using `ffmpeg`
- landscape `_L.jpg` output for BLOOMIN8 pull mode
- optional `/eink_signal` feedback endpoint
- simple systemd user-service deployment
- works well behind Tailscale Funnel

## Repository layout

- `bloomin8_pull_server.py` — main HTTP server
- `configure_bloomin8_pull.py` — helper to configure the device via `/upstream/pull_settings`
- `bloomin8_pull_config.example.json` — example config (no secrets)
- `systemd/bloomin8-pull.service` — user systemd unit
- `.gitignore`

## Requirements

Runtime dependencies:
- Python 3.11+
- `ffmpeg`

No third-party Python packages are required.

## Config

Copy the example config and fill in your values:

```bash
cp bloomin8_pull_config.example.json bloomin8_pull_config.json
```

Important fields:
- `timezone`
- `scheduled_local_time`
- `retry_interval_minutes`
- `public_base_url`
- `device_token`
- `image_token`
- `device_width`
- `device_height`
- `latest_json_url` and `image_base_url` for the public archive
- `latest_json_path` as an alternative local source

For the live Infographic Network, use:

```text
latest_json_url=https://lukafin.github.io/artists-infographic-archive/latest.json
image_base_url=https://lukafin.github.io/artists-infographic-archive/
```

Remote metadata requests bypass intermediary caches so a newly published morning infographic is seen promptly.

## Run locally

```bash
python3 bloomin8_pull_server.py --host 127.0.0.1 --port 8092 --config ./bloomin8_pull_config.json
```

Health check:

```bash
curl http://127.0.0.1:8092/health
```

Manual pull test:

```bash
curl -H 'X-Access-Token: <device-token>' \
  'http://127.0.0.1:8092/eink_pull?device_id=test&pull_id=abc&cron_time=2026-04-13T09:00:00Z&battery=50'
```

## Deploy with systemd

Copy the service file and update the paths if needed:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/bloomin8-pull.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now bloomin8-pull.service
systemctl --user status bloomin8-pull.service
```

## Expose publicly with Tailscale Funnel

Example path-based route:

```bash
sudo tailscale funnel --bg --set-path /bloomin8-pull 8092
sudo tailscale funnel status
```

Example public base URL:

```text
https://your-host.ts.net/bloomin8-pull
```

## Configure the frame

Use the helper script:

```bash
python3 configure_bloomin8_pull.py
```

It sends:
- `upstream_on=true`
- `upstream_url=<public_base_url>`
- `token=<device_token>`
- `cron_time=<now + 3 minutes>`

Or call the device directly:

```bash
curl -X PUT http://<device-ip>/upstream/pull_settings \
  -H 'content-type: application/json' \
  -d '{
    "upstream_on": true,
    "upstream_url": "https://your-host.ts.net/bloomin8-pull",
    "token": "your-secret-token",
    "cron_time": "2026-04-13T09:44:17Z"
  }'
```

## BLOOMIN8 pull image notes

For native pull mode, the vendor docs require:
- JPEG images
- filenames ending in `_P.jpg` or `_L.jpg`
- for landscape mode, serve `_L.jpg`
- the file itself should be stored rotated for the device’s landscape handling

This server prepares that dynamically from the source archive image.

## Security notes

- `device_token` is required for `/eink_pull`
- image URLs are protected by an unguessable path token
- do not commit real secrets
- use HTTPS for public exposure

## License

MIT
