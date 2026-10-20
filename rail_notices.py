"""Official SŽ RSS notices: bounded discovery and conservative validity parsing.

Search feeds are not a complete replacement-bus timetable. An empty result is
never evidence that no buses run. Unknown dates remain explicitly unverified.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

from commute_board import clean_text, get, norm

TERMS = ('Kranj', 'Jesenice', 'Vižmarje')
MONTHS = {name: i for i, name in enumerate(
    ('januar', 'februar', 'marec', 'april', 'maj', 'junij', 'julij', 'avgust',
     'september', 'oktober', 'november', 'december'), 1)}


def route_relevant(title):
    text = norm(title)
    return ('nadomestn' in text and 'ljubljana' in text and
            any(word in text for word in ('kranj', 'jesenice', 'vizmarje', 'medvode', 'skofja loka')))


def notice_validity(title, published_year, today):
    """Parse simple explicit title ranges/lists; never silently infer complex ones."""
    groups = [g for g in re.findall(r'\(([^()]*)\)', norm(title)) if re.search(r'\d', g)]
    if len(groups) > 1:
        states = [notice_validity(f'({group})', published_year, today)[0] for group in groups]
        if all(state == 'inactive' for state in states):
            return 'inactive', 'Pretekla veljavnost'
        return 'unknown', 'Več odsekov ali terminov; preveri obvestilo SŽ'
    if not groups:
        return 'unknown', 'Veljavnost ni zanesljivo razbrana'
    text = groups[0].strip()
    named_months = [MONTHS[word] for word in re.findall(r'[a-z]+', text) if word in MONTHS]
    years = [int(value) for value in re.findall(r'\b\d{4}\b', text)]
    # Safe upper bound for past named-month compound lists, not numeric ranges.
    if named_months and not re.search(r'\d\.\s*\d', text):
        upper_year = max(years or [published_year])
        if (upper_year, max(named_months)) < (today.year, today.month):
            return 'inactive', 'Pretekla veljavnost'
    # 26. 9. – 6. 11. 2026; optional year on both bounds.
    m = re.fullmatch(r'(\d{1,2})\.\s*(\d{1,2})\.\s*(?:(\d{4})\s*)?[–—-]\s*(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})?', text)
    if m:
        d1, m1, y1, d2, m2, y2 = m.groups()
        end_year = int(y2 or y1 or published_year)
        start_year = int(y1 or (end_year - (int(m1) > int(m2))))
        start, end = date(start_year, int(m1), int(d1)), date(end_year, int(m2), int(d2))
        label = f'{start.day}. {start.month}.–{end.day}. {end.month}. {end.year}'
        return ('active' if start <= today <= end else 'inactive'), label
    # A single named-month day/list such as 4., 11. in 18. oktober 2026.
    m = re.fullmatch(r'([\d.,\s]+(?:in\s+[\d.\s]+)?)\s+([a-z]+)(?:\s+(\d{4}))?', text)
    if m and m[2] in MONTHS:
        year, month = int(m[3] or published_year), MONTHS[m[2]]
        days = [int(value) for value in re.findall(r'\d+', m[1])]
        dates = [date(year, month, day) for day in days]
        return ('active' if today in dates else 'inactive'), f'{groups[0]} ({year})'
    return 'unknown', 'Veljavnost ni zanesljivo razbrana'


def collect_notices(session, now):
    today = now.date()
    found, failed = {}, []
    for term in TERMS:
        try:
            response = get(session, 'https://potniski.sz.si/feed/?' + urlencode({'s': term}))
            root = ET.fromstring(response.content)
            channel = root.find('channel')
            if channel is None:
                raise ValueError('Unexpected SŽ RSS schema')
            for row in channel.findall('item'):
                title, url = clean_text(row.findtext('title') or ''), row.findtext('link') or ''
                if not route_relevant(title) or not url.startswith('https://potniski.sz.si/'):
                    continue
                published = parsedate_to_datetime(row.findtext('pubDate') or '')
                # Old undated material is not evidence of current restrictions.
                if published.year < today.year - 1:
                    continue
                state, validity = notice_validity(title, published.year, today)
                if state == 'inactive':
                    continue
                if state == 'unknown' and published.year != today.year:
                    continue
                route = re.split(r'\bin obratno\b|\(', title, maxsplit=1)[0]
                route = re.sub(r'^Nadomestni (?:avtobusni )?prevoz (?:na relaciji |med )?', '', route, flags=re.I)
                found[url] = {'title': title, 'route': route.strip(), 'validity': validity,
                              'validity_status': state, 'url': url, 'published_at': published.isoformat(),
                              'detail': 'Za v obvestilu navedene vlake; preveri vozni red SŽ.'}
        except (ValueError, TypeError, ET.ParseError, OSError) as exc:
            failed.append(f'{term}: {type(exc).__name__}')
        except Exception as exc:
            # Network failure must not discard working rail/road/weather panels.
            failed.append(f'{term}: {type(exc).__name__}')
    items = sorted(found.values(), key=lambda x: (x['validity_status'] != 'active', x['url']))
    status = 'unavailable' if len(failed) == len(TERMS) else ('partial' if failed else 'ok')
    return {'status': status, 'items': items, 'source': 'SŽ Potniški promet (uradni RSS)',
            'retrieved_at': now.isoformat(), 'errors': failed,
            'note': 'Preverjena javna obvestila niso celoten vozni red nadomestnih prevozov.'}
