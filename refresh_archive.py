"""Stage published frame candidates; admit only after explicit visual review.

The daily agent runs candidates, inspects the real image, then approve/reject.
No device writes, image generation, public publication or service restart.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from PIL import Image
from archive_rotation import load_selection

BASE = 'https://lukafin.github.io/artists-infographic-archive/'
MAX_BYTES = 25 * 1024 * 1024


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def atomic_json(path, data):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def fetch(url):
    req = Request(url, headers={'User-Agent': 'Bloomin8ReviewedArchive/1.0', 'Cache-Control': 'no-cache'})
    with urlopen(req, timeout=30) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('Archive response exceeds size limit')
    return data


def public_entries():
    data = json.loads(fetch(BASE + 'entries.json'))
    return data if isinstance(data, list) else data['entries']


def eligible(row, since, today):
    name = row.get('image_filename')
    if not isinstance(name, str) or not name or Path(name).name != name or '\\' in name:
        return False
    try:
        source_date = date.fromisoformat(row['date'])
    except (KeyError, TypeError, ValueError):
        return False
    return (since < source_date <= today and row.get('language') == 'sl'
            and 'age_6' in row.get('age_suitability_keys', []))


def validate_image(data):
    with Image.open(io.BytesIO(data)) as image:
        image.verify()
    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
        if width < 768 or height < 1024 or not 1.15 <= height / width <= 1.8:
            raise ValueError('Image is not a legible-size portrait candidate')
        return [width, height]


def candidates(playlist_path, today, limit=3):
    playlist = read_json(playlist_path)
    refresh = playlist.get('refresh', {})
    if refresh.get('approved_on') == today.isoformat():
        return {'status': 'already_refreshed_today', 'candidates': []}
    since = date.fromisoformat(refresh.get('since', playlist['anchor_date']))
    seen = set(playlist['filenames']) | set(refresh.get('rejected', {}))
    rows = sorted(public_entries(), key=lambda row: (row.get('date', ''), row.get('image_filename') or ''), reverse=True)
    stage = playlist_path.parent / 'review-candidates'
    stage.mkdir(mode=0o700, exist_ok=True)
    pending, failures = {}, []
    for row in rows:
        if not eligible(row, since, today) or row['image_filename'] in seen:
            continue
        name = row['image_filename']
        seen.add(name)
        try:
            data = fetch(BASE + quote(name, safe=''))
            dimensions = validate_image(data)
            path = stage / name
            path.write_bytes(data)
            pending[name] = {'entry': row, 'sha256': hashlib.sha256(data).hexdigest(),
                             'dimensions': dimensions, 'path': str(path)}
        except Exception as exc:
            failures.append({'filename': name, 'error': str(exc)})
            continue
        if len(pending) >= limit:
            break
    atomic_json(stage / 'pending.json', {'staged_on': today.isoformat(), 'candidates': pending})
    return {'status': 'visual_review_required', 'candidates': list(pending.values()), 'failures': failures}


def review(playlist_path, today, name, note, approve):
    if len(note.strip()) < 20:
        raise ValueError('An explicit visual-review note is required')
    playlist = read_json(playlist_path)
    refresh = playlist.setdefault('refresh', {'since': playlist['anchor_date']})
    if name in playlist['filenames']:
        return {'status': 'already_approved', 'filename': name}
    if refresh.get('approved_on') == today.isoformat():
        raise ValueError('Only one new selection per local day is permitted')
    staged = read_json(playlist_path.parent / 'review-candidates' / 'pending.json')
    if staged['staged_on'] != today.isoformat():
        raise ValueError('Candidate review is stale; stage again')
    candidate = staged['candidates'][name]
    if not approve:
        refresh.setdefault('rejected', {})[name] = {'date': today.isoformat(), 'note': note}
        atomic_json(playlist_path, playlist)
        return {'status': 'rejected', 'filename': name}
    row = candidate['entry']
    if not eligible(row, date.fromisoformat(refresh['since']), today):
        raise ValueError('Candidate is no longer eligible')
    # Confirm that this exact image identity is still publicly published.
    fields = ('date', 'image_filename', 'language', 'age_suitability_keys', 'person')
    if not any(all(r.get(k) == row.get(k) for k in fields) for r in public_entries()):
        raise ValueError('Candidate is no longer in the published archive')
    data = (playlist_path.parent / 'review-candidates' / name).read_bytes()
    if hashlib.sha256(data).hexdigest() != candidate['sha256']:
        raise ValueError('Candidate bytes changed after staging')
    validate_image(data)
    public_data = fetch(BASE + quote(name, safe=''))
    if hashlib.sha256(public_data).hexdigest() != candidate['sha256']:
        raise ValueError('Published bytes changed after staging')
    current = load_selection(playlist_path, today)['image_filename']
    names = playlist['filenames']
    index = names.index(current)
    # New first tomorrow; keep today's exact selection at index -1. Rotate the
    # remaining reviewed list rather than arbitrarily jumping to its beginning.
    playlist['filenames'] = [name] + names[index + 1:] + names[:index + 1]
    playlist['anchor_date'] = (today + timedelta(days=1)).isoformat()
    playlist['entries'].append(row)
    refresh['approved_on'] = today.isoformat()
    refresh.setdefault('reviews', {})[name] = {'date': today.isoformat(), 'note': note,
                                              'sha256': candidate['sha256']}
    images = playlist_path.parent / 'images'
    images.mkdir(mode=0o700, exist_ok=True)
    target = images / name
    target.write_bytes(data)  # publish immutable bytes before metadata points at them
    if hashlib.sha256(target.read_bytes()).hexdigest() != candidate['sha256']:
        raise ValueError('Cached image hash mismatch')
    backup = playlist_path.with_name('playlist.before-refresh-' + today.isoformat() + '.json')
    if not backup.exists():
        shutil.copy2(playlist_path, backup)
    atomic_json(playlist_path, playlist)
    return {'status': 'approved', 'filename': name, 'display_date': playlist['anchor_date'],
            'today_unchanged': load_selection(playlist_path, today)['image_filename'] == current,
            'tomorrow': load_selection(playlist_path, today + timedelta(days=1))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--playlist', type=Path, required=True)
    sub = parser.add_subparsers(dest='action', required=True)
    listing = sub.add_parser('candidates')
    listing.add_argument('--limit', type=int, choices=range(1, 6), default=3)
    for action in ('approve', 'reject'):
        command = sub.add_parser(action)
        command.add_argument('filename')
        command.add_argument('--review-note', required=True)
    args = parser.parse_args()
    today = datetime.now(ZoneInfo('Europe/Ljubljana')).date()
    # Serialize concurrent daily/manual reviewers; the serving path only reads
    # the atomically replaced playlist and never needs this lock.
    with args.playlist.with_suffix('.refresh.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.action == 'candidates':
            result = candidates(args.playlist, today, args.limit)
        else:
            result = review(args.playlist, today, args.filename, args.review_note, args.action == 'approve')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
