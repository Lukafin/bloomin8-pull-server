#!/usr/bin/env python3
"""Once-daily public-source commuter snapshot; no LLM/browser/device writes."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from commute_render import render_board, mock_data

TZ = ZoneInfo('Europe/Ljubljana')
RAIL_BASE = 'https://api.zamudil.si/api'
ROAD_URL = 'https://www.promet.si/dc/b2b.dogodki.rss?roadtype=highway&eventtype=all&language=sl_SI'
NS = {'a': 'http://www.w3.org/2005/Atom'}


def norm(value):
    return ''.join(c for c in unicodedata.normalize('NFKD', str(value)).lower() if not unicodedata.combining(c))


def instant(value):
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Source timestamp is missing timezone')
    return result


def fresh(value, now, maximum_minutes=10):
    age = (now - value).total_seconds()
    if age < -300 or age > maximum_minutes * 60:
        raise ValueError('Source is stale or clock is invalid')


def get(session, url):
    response = session.get(url, timeout=(5, 15))
    response.raise_for_status()
    if len(response.content) > 5 * 1024 * 1024:
        raise ValueError('Source response too large')
    return response


def train_candidates(board, now):
    if board.get('serviceDate') != now.astimezone(TZ).date().isoformat() or board.get('stopName') != 'Kranj':
        raise ValueError('Wrong station or service date')
    rows = board.get('rows')
    if not isinstance(rows, list):
        raise ValueError('Unexpected railway board schema')
    chosen = []
    for row in rows:
        scheduled = row.get('scheduledDeparture')
        if not scheduled:
            continue
        departure = instant(scheduled)
        predicted = instant(row['predictedDeparture']) if row.get('predictedDeparture') else departure
        # Never resurrect already departed services, but retain a late train whose
        # scheduled departure has passed and expected departure is still ahead.
        if predicted < now or departure > now + timedelta(hours=2):
            continue
        if norm(row.get('originStopName', '')).startswith('ljubljana') or norm(row.get('destinationStopName', '')) == 'kranj':
            continue
        chosen.append(row)
    return sorted(chosen, key=lambda r: r['scheduledDeparture'])


def route_to_ljubljana(timeline):
    rows = timeline.get('rows')
    if not isinstance(rows, list):
        raise ValueError('Unexpected railway timeline schema')
    kranj = next((i for i, row in enumerate(rows) if row.get('stopId') == '1122811' or row.get('stopName') == 'Kranj'), None)
    if kranj is None:
        return None
    return next((row for row in rows[kranj+1:] if norm(row.get('stopName', '')).startswith('ljubljana')), None)


def collect_trains(session, now):
    global_info = get(session, RAIL_BASE + '/global').json()
    source_time = datetime.fromtimestamp(global_info['lastUpdated'] / 1000, timezone.utc)
    fresh(source_time, now)
    if global_info.get('serviceDate') != now.astimezone(TZ).date().isoformat():
        raise ValueError('Railway source service date is not today')
    board = get(session, RAIL_BASE + '/stations/Kranj/board').json()
    candidates = train_candidates(board, now)
    if len(candidates) > 16:
        raise ValueError('Unexpectedly large two-hour railway candidate set')
    items, checked, unknown = [], 0, 0
    for row in candidates:
        label = row.get('trainLabel')
        if not isinstance(label, str) or not label:
            raise ValueError('Missing train identity')
        timeline = get(session, f'{RAIL_BASE}/trains/{quote(label, safe="")}/timeline?day={now.astimezone(TZ).date().isoformat()}').json()
        if timeline.get('serviceDate') != board['serviceDate']:
            raise ValueError('Mismatched train service date')
        destination = route_to_ljubljana(timeline)
        if not destination:
            continue
        checked += 1
        kranj = next(x for x in timeline['rows'] if x.get('stopId') == '1122811' or x.get('stopName') == 'Kranj')
        departure = kranj.get('departure') or {}
        delay_sec = departure.get('delaySec')
        if kranj.get('cancelled') or destination.get('cancelled'):
            items.append({'train': label, 'departure': instant(row['scheduledDeparture']).astimezone(TZ).strftime('%H:%M'),
                          'delay_minutes': None, 'status_label': 'ODPOVED'})
        elif delay_sec is None:
            unknown += 1
        elif not isinstance(delay_sec, (int, float)) or isinstance(delay_sec, bool):
            raise ValueError('Invalid delay value')
        elif delay_sec > 0:
            suffix = ' · Šiška' if 'siska' in norm(destination.get('stopName', '')) else ''
            items.append({'train': label + suffix, 'departure': instant(row['scheduledDeparture']).astimezone(TZ).strftime('%H:%M'),
                          'delay_minutes': math.ceil(delay_sec / 60)})
    window = f'{now.astimezone(TZ):%H:%M}–{(now + timedelta(hours=2)).astimezone(TZ):%H:%M}'
    note = f'Okno {window} · odhodov: {checked}'
    if unknown:
        note += f' · {unknown} brez žive napovedi'
    return {'status': 'ok', 'items': items, 'note': note, 'source': 'zamudil.si (neuradno)',
            'source_at': source_time.isoformat(), 'checked_departures': checked, 'unknown_departures': unknown}


def road_on_route(title, summary):
    t, s = norm(title), norm(summary)
    if not re.search(r'a2\s*-?\s*e61', t):
        return False
    body = s.split(',', 2)[-1]
    if not re.search(r'\b(kranj|brnik|vodice|smartno|brod|sentvid|koseze|ljubljana)\b', body):
        return False
    # The generic corridor label Karavanke–Ljubljana is not an event location.
    if re.search(r'\b(jesenice|podtabor|naklo|lesce|karavanke)\b', body) and not re.search(r'\b(brnik|vodice|smartno|brod|sentvid|koseze)\b', body):
        return False
    both = any(x in body for x in ['obe smeri', 'obeh smereh', 'vsako smer'])
    match = re.search(r'v smeri ([^,.;]+)', body)
    if match and not both:
        direction = match.group(1)
        if any(x in direction for x in ['jesenic', 'karavank', 'avstrij', 'kranj']):
            return False
        if not any(x in direction for x in ['ljubljan', 'sentvid', 'kosez']):
            return False
    return True


def clean_text(value):
    return ' '.join(html.unescape(re.sub('<[^>]+>', ' ', str(value))).split())


def collect_roads(session, now):
    response = get(session, ROAD_URL)
    root = ET.fromstring(response.content)
    if root.tag != '{http://www.w3.org/2005/Atom}feed':
        raise ValueError('Unexpected road feed schema')
    source_time = instant(root.findtext('a:updated', namespaces=NS))
    fresh(source_time, now)
    items = []
    for entry in root.findall('a:entry', NS):
        title = clean_text(entry.findtext('a:title', default='', namespaces=NS))
        summary = clean_text(entry.findtext('a:summary', default='', namespaces=NS))
        if not road_on_route(title, summary):
            continue
        # Preserve source wording/units. Never invent a delay from queue length.
        detail = ','.join(summary.split(',')[2:]).strip() or summary
        if 'v smeri' not in norm(detail) and not any(x in norm(detail) for x in ['obeh smereh', 'vsako smer', 'obe smeri']):
            detail += ' Smer v obvestilu ni določena.'
        location = detail.split(',')[0].strip()
        items.append({'title': location or title, 'detail': detail, 'source_title': title,
                      'id': entry.findtext('a:id', default='', namespaces=NS),
                      'priority': 0 if any(x in norm(summary) for x in ['zastoj', 'nesrec', 'zaprt', 'zapora']) else 1})
    items.sort(key=lambda x: x['priority'])
    return {'status': 'ok', 'items': items, 'source': 'Promet.si / DARS', 'source_at': source_time.isoformat(),
            'note': 'Ni ustreznih objavljenih obvestil za A2 Kranj–Ljubljana. To ni meritev pretočnosti.'}


def collect(now=None):
    now = now or datetime.now(TZ)
    result = {'generated_at': now.isoformat(), 'mock': False}
    with requests.Session() as session:
        session.headers.update({'User-Agent': 'PrivateMorningCommuteBoard/1.0', 'Cache-Control': 'no-cache'})
        for channel, collector in [('trains', collect_trains), ('roads', collect_roads)]:
            try:
                result[channel] = collector(session, now)
            except (requests.RequestException, ValueError, KeyError, TypeError, ET.ParseError, OverflowError) as exc:
                result[channel] = {'status': 'unavailable', 'items': [], 'error': f'{type(exc).__name__}: {exc}',
                                   'source': 'zamudil.si (neuradno)' if channel == 'trains' else 'Promet.si / DARS',
                                   'note': 'Vir ni dosegljiv ali podatki niso dovolj sveži. To ne pomeni, da zamud oziroma zastojev ni.'}
    return result


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as output:
        try:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
            os.replace(output.name, path)
        finally:
            if os.path.exists(output.name):
                os.unlink(output.name)


def publish(data, snapshot_path):
    if data.get('mock') is not False:
        raise ValueError('Never publish demo data to the native daily feed')
    digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:12]
    day = instant(data['generated_at']).astimezone(TZ).date().isoformat()
    filename = f'commute-{day}-{digest}_P.jpg'
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    render_board(data, snapshot_path.parent / filename)
    manifest = dict(data, image_filename=filename)
    atomic_json(snapshot_path, manifest)
    # A small private rolling cache; unrelated files are never touched.
    for old in snapshot_path.parent.glob('commute-????-??-??-????????????_P.jpg'):
        if old.name != filename and old.stat().st_mtime < datetime.now().timestamp() - 7 * 86400:
            old.unlink()
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--mock-preview', type=Path, help='Render demo only; never publish or change device')
    args = parser.parse_args()
    if args.mock_preview:
        render_board(mock_data(datetime.now(TZ)), args.mock_preview)
        print(args.mock_preview)
        return 0
    if not args.config:
        parser.error('--config required for live publication')
    config = json.loads(args.config.read_text())
    target = Path(config['commute_snapshot_path'])
    data = collect()
    publish(data, target)
    problems = [key for key in ['trains', 'roads'] if data[key]['status'] != 'ok']
    if problems:
        print('Prometni okvir: slika je pripravljena, vendar vir ni na voljo: ' + ', '.join(problems) + '. Na sliki je opozorilo, ne trditev o tekočem prometu.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
