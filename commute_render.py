"""Small, deterministic Pillow renderer: no browser, network or device access."""
from __future__ import annotations


from datetime import datetime
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 1200, 1600
INK, PAPER = '#101010', '#ffffff'
FONT_ROOT = Path('/usr/share/fonts/truetype/dejavu')


def font(size: int, kind: str = 'sans', bold: bool = False):
    family = {'sans': 'DejaVuSans', 'serif': 'DejaVuSerif', 'mono': 'DejaVuSansMono'}[kind]
    return ImageFont.truetype(str(FONT_ROOT / f'{family}{"-Bold" if bold else ""}.ttf'), size)


def lines_for(draw, text, face, width):
    """Wrap by measured glyph width, including long source tokens."""
    lines, current = [], ''
    for word in str(text).split():
        candidate = f'{current} {word}'.strip()
        if draw.textlength(candidate, font=face) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = ''
        for character in word:
            if current and draw.textlength(current + character, font=face) > width:
                lines.append(current)
                current = ''
            current += character
    if current:
        lines.append(current)
    return lines


def paragraph(draw, text, xy, width, size=32, max_lines=3, bold=False, fill=INK):
    face = font(size, bold=bold)
    lines = lines_for(draw, text, face, width)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        while lines[-1] and draw.textlength(lines[-1] + ' …', font=face) > width:
            lines[-1] = lines[-1][:-1]
        lines[-1] += ' …'
    x, y = xy
    for line in lines:
        draw.text((x, y), line, font=face, fill=fill)
        y += size + 10
    return y


def render_board(data: dict, destination: Path) -> Path:
    image = Image.new('RGB', (WIDTH, HEIGHT), PAPER)
    d = ImageDraw.Draw(image)
    left, right = 65, 1135
    is_mock = bool(data.get('mock'))
    d.text((left, 28), 'JUTRANJA IZDAJA  /  GORENJSKA', font=font(25, bold=True), fill=INK)
    d.text((left, 64), 'Na poti.', font=font(112, 'serif', True), fill=INK)
    d.line((left, 209, right, 209), fill=INK, width=6)
    d.text((left, 230), 'KRANJ → LJUBLJANA', font=font(56, bold=True), fill=INK)
    moment = datetime.fromisoformat(data['generated_at'])
    stamp = f'{moment.day}. {moment.month}. {moment.year}  ·  POSNETEK OB {moment:%H:%M}'
    d.text((left, 312), stamp, font=font(27, 'mono'), fill=INK)
    if is_mock:
        d.rectangle((left, 363, right, 418), fill=INK)
        d.text((left + 18, 372), 'DEMO — IZMIŠLJENE ZAMUDE IN ZASTOJI', font=font(31, bold=True), fill=PAPER)
    else:
        d.text((left, 373), 'Jutranji pregled · neprekinjeno spremljanje ni vključeno', font=font(26), fill=INK)
    trains = data['trains']
    d.line((left, 442, right, 442), fill=INK, width=3)
    d.text((left, 462), '01', font=font(29, 'mono'), fill=INK)
    d.text((142, 449), 'Z vlakom', font=font(53, bold=True), fill=INK)
    items = trains.get('items', [])
    notices = data.get('rail_notices')
    limit = 2 if notices is not None else 3
    if trains.get('status') != 'ok':
        d.text((left, 537), 'PODATKI NISO NA VOLJO', font=font(39, bold=True), fill=INK)
        paragraph(d, trains.get('note') or 'Zamud trenutno ni mogoče preveriti.', (left, 602), 1065, 32, 3)
    elif not items:
        d.text((left, 541), 'Ni objavljenih zamud', font=font(44, bold=True), fill=INK)
        paragraph(d, trains.get('note') or 'Za smer Kranj–Ljubljana v preverjenem jutranjem oknu.', (left, 614), 1065, 31, 3)
    else:
        d.text((left, 531), 'VLAK / ODHOD IZ KRANJA', font=font(25, bold=True), fill=INK)
        d.text((850, 531), 'ZAMUDA', font=font(25, bold=True), fill=INK)
        y = 581
        for item in items[:limit]:
            label = str(item.get('train') or 'Vlak')
            departure = str(item.get('departure') or 'čas ni potrjen')
            paragraph(d, label, (left, y), 690, 36, 1, bold=True)
            d.text((left, y + 49), f'Odhod {departure}', font=font(29, 'mono'), fill=INK)
            delay = item.get('delay_minutes')
            delay_text = f'+{delay} min' if delay is not None else item.get('status_label', 'motnja')
            face = font(46, 'mono', True)
            d.text((right - d.textlength(delay_text, font=face), y + 7), delay_text, font=face, fill=INK)
            d.line((left, y + 99, right, y + 99), fill=INK, width=1)
            y += 112
        note_y = 810 if notices is not None else 930
        if len(items) > limit:
            d.text((left, note_y), f'Še {len(items)-limit} objavljenih zamud · glej zamudil.si', font=font(26), fill=INK)
        elif trains.get('note'):
            paragraph(d, trains['note'], (left, note_y), 1065, 25, 1)
    if notices is not None:
        d.line((left, 850, right, 850), fill=INK, width=2)
        bus_items = notices.get('items', [])
        if bus_items:
            item = bus_items[0]
            label = 'NADOMESTNI AVTOBUSI · SŽ' if item.get('validity_status') == 'active' else 'AVTOBUSI · VELJAVNOST NI POTRJENA'
            d.text((left, 861), label, font=font(26, bold=True), fill=INK)
            paragraph(d, item.get('route', ''), (left, 899), 1065, 26, 1)
            extra = f" · še {len(bus_items)-1} obvestil" if len(bus_items) > 1 else ''
            paragraph(d, item.get('validity', '') + extra + ' · za navedene vlake, glej SŽ', (left, 939), 1065, 22, 1)
        else:
            d.text((left, 861), 'NADOMESTNI AVTOBUSI · PREVERI SŽ', font=font(26, bold=True), fill=INK)
            message = 'Vir SŽ ni dosegljiv; avtobusov ni mogoče preveriti.' if notices.get('status') == 'unavailable' else 'V preverjenih obvestilih ni potrditve za danes.'
            paragraph(d, message, (left, 899), 1065, 25, 1)
            d.text((left, 939), 'To ne pomeni, da nadomestnih prevozov ni.', font=font(22), fill=INK)
    d.line((left, 979, right, 979), fill=INK, width=5)
    d.text((left, 1007), '02', font=font(29, 'mono'), fill=INK)
    d.text((142, 993), 'Po avtocesti A2', font=font(49, bold=True), fill=INK)
    roads = data['roads']
    road_items = roads.get('items', [])
    if roads.get('status') != 'ok':
        d.text((left, 1079), 'PODATKI NISO NA VOLJO', font=font(39, bold=True), fill=INK)
        paragraph(d, roads.get('note') or 'Zastojev trenutno ni mogoče preveriti.', (left, 1143), 1065, 32, 4)
    elif not road_items:
        d.text((left, 1083), 'Ni objavljenih opozoril', font=font(42, bold=True), fill=INK)
        paragraph(d, roads.get('note') or 'Na A2 od Kranja proti Ljubljani.', (left, 1154), 1065, 31, 3)
    else:
        y = 1080
        for item in road_items[:2]:
            y = paragraph(d, item.get('title') or 'Prometno opozorilo', (left, y), 1065, 34, 1, bold=True)
            y = paragraph(d, item.get('detail') or '', (left, y+4), 1065, 30, 2)
            y += 18
        if len(road_items) > 2:
            d.text((left, 1414), f'Še {len(road_items)-2} obvestil · glej promet.si', font=font(25), fill=INK)
    d.line((left, 1460, right, 1460), fill=INK, width=2)
    sources = 'DEMO / brez živih virov' if is_mock else 'zamudil.si (neuradno) · DARS' + (' · SŽ' if notices is not None else '')
    d.text((left, 1481), 'VIRA  ' + sources, font=font(25), fill=INK)
    d.text((left, 1523), 'Jutranji posnetek. Pred odhodom preveri aktualno stanje.', font=font(24), fill=INK)
    weather = data.get('weather', {})
    if weather:
        caption = 'Vreme: DEMO · izmišljena napoved' if is_mock else 'Vreme: Open-Meteo · dnevni vrh verjetnosti padavin'
        if weather.get('status') == 'partial':
            caption += ' · delni podatki'
        d.text((left, 1563), caption, font=font(19), fill=INK)
        if weather.get('rain_possible') is True:
            # Plain vector umbrella: stable on e-ink; no emoji font dependency.
            d.pieslice((1020, 1483, 1130, 1553), 180, 360, fill=INK)
            d.line((1075, 1518, 1075, 1550), fill=INK, width=6)
            d.arc((1053, 1539, 1075, 1561), 0, 180, fill=INK, width=6)
            d.line((1053, 1547, 1053, 1550), fill=INK, width=6)
            probability = weather.get('max_probability')
            label = f'DEŽ {probability:.0f}%' if isinstance(probability, (int, float)) else 'MOŽEN DEŽ'
            d.text((1015, 1569), label, font=font(19, bold=True), fill=INK)
        elif weather.get('rain_possible') is None:
            d.text((1000, 1519), 'Vreme: ?', font=font(24, bold=True), fill=INK)
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format='JPEG', quality=94, subsampling=2, progressive=False)
    return destination


def mock_data(now: datetime) -> dict:
    return {
        'generated_at': now.isoformat(), 'mock': True,
        'weather': {'status': 'ok', 'rain_possible': True, 'max_probability': 70},
        'trains': {'status': 'ok', 'items': [
            {'train': 'LP 0001 · TESTNI VLAK', 'departure': '06:12', 'delay_minutes': 8},
            {'train': 'RE 0002 · TESTNI VLAK', 'departure': '06:38', 'delay_minutes': 15},
            {'train': 'LP 0003 · TESTNI VLAK', 'departure': '07:02', 'delay_minutes': 5}]},
        'roads': {'status': 'ok', 'items': [
            {'title': 'Vodice → Šmartno · TEST', 'detail': 'Izmišljen zastoj: 3 km počasnega prometa v smeri Ljubljane.'},
            {'title': 'Šmartno → Ljubljana · TEST', 'detail': 'Izmišljen dogodek za preverjanje berljivosti zaslona.'}]}}
