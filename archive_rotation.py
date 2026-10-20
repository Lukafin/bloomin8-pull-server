"""Frozen, reviewed archive playlist; no image generation and no latest.json fallback."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path


def select_entry(entries: list[dict], filenames: list[str], day: date, anchor: date) -> dict:
    by_name = {row.get('image_filename'): row for row in entries}
    eligible = []
    for name in dict.fromkeys(filenames):
        row = by_name.get(name)
        if not row or row.get('language') != 'sl' or 'age_6' not in row.get('age_suitability_keys', []):
            continue
        if not isinstance(name, str) or Path(name).name != name:
            continue
        # Freeze eligibility at playlist creation, not at today's changing latest.
        if date.fromisoformat(row['date']) >= anchor:
            continue
        eligible.append(row)
    if len(eligible) < 2:
        raise ValueError('Archive playlist needs at least two older Slovenian age-6+ images')
    return dict(eligible[(day - anchor).days % len(eligible)])


def load_selection(path: Path, day: date) -> dict:
    playlist = json.loads(path.read_text(encoding='utf-8'))
    selected = select_entry(playlist['entries'], playlist['filenames'], day,
                            date.fromisoformat(playlist['anchor_date']))
    selected['display_date'] = day.isoformat()
    return selected
