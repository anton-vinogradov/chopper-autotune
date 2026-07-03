"""Analysis phase: aggregate a dataset, rank configs, report, optionally apply the winner.

Works offline on a collected dataset; the printer is only needed for --apply.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from pathlib import Path

from . import tmc
from .dataset import Dataset, RESULTS_HOME
from .moonraker import Moonraker


def latest_dataset(bases=(RESULTS_HOME / 'datasets', Path('datasets'))) -> str:
    for base in bases:
        if not base.is_dir():
            continue
        for path in sorted(base.iterdir(), reverse=True):
            if (path / 'manifest.json').is_file() \
                    and Dataset(path).manifest().get('mode') != 'find-speed':
                return str(path)
    raise SystemExit('no chopper datasets found, pass the dataset directory explicitly')


def aggregate(ds: Dataset, recompute: bool, trim_fraction: float) -> 'list[dict]':
    groups = defaultdict(list)
    for record in ds.records():
        if record.get('kind') != 'move' or record.get('status') != 'ok':
            continue
        if recompute:
            if 'raw' not in record:
                raise SystemExit('--recompute needs raw csv, dataset was collected with --no-raw')
            from .metrics import parse_accel_csv, vibration_score, window
            with ds.open_raw(record) as f:
                data = parse_accel_csv(f)
            if 'steady' in record:
                score = vibration_score(window(data, *record['steady']), 0.0)
            else:
                score = vibration_score(data, trim_fraction)
        else:
            score = record['score']
        key = (record['tbl'], record['toff'], record['hstrt'], record['hend'], record.get('tpfd'))
        groups[key].append(score['median_magnitude'])
    return [{
        'chopper': tmc.Chopper(*key),
        'magnitude': statistics.median(values),
        'spread': max(values) - min(values),
        'n': len(values),
    } for key, values in groups.items()]


def rank(aggregates: 'list[dict]', driver: tmc.Driver, audible_weight: float) -> 'list[dict]':
    for a in aggregates:
        a['chopper_freq_hz'] = tmc.chopper_freq_hz(a['chopper'], driver)
        a['audible'] = tmc.is_audible(a['chopper'], driver)
        a['score'] = a['magnitude'] * (1 + audible_weight if a['audible'] else 1)
    aggregates.sort(key=lambda a: a['score'])
    return aggregates


def print_table(ranked: 'list[dict]', top: int):
    print('%4s %4s %5s %6s %5s %5s %10s %8s %3s %7s %s'
          % ('rank', 'tbl', 'toff', 'hstrt', 'hend', 'tpfd', 'magnitude', 'spread', 'n', 'f_chop', ''))
    for position, a in enumerate(ranked[:top], 1):
        c = a['chopper']
        print('%4d %4d %5d %6d %5d %5s %10.1f %8.1f %3d %5.1fkHz %s'
              % (position, c.tbl, c.toff, c.hstrt, c.hend,
                 c.tpfd if c.tpfd is not None else '-',
                 a['magnitude'], a['spread'], a['n'],
                 a['chopper_freq_hz'] / 1000, 'audible!' if a['audible'] else ''))


def tbl_toff_matrix(ranked: 'list[dict]', driver: tmc.Driver):
    """Median magnitude per (tbl, toff) cell, with the analytic chopper frequency."""
    tbls = sorted({a['chopper'].tbl for a in ranked})
    toffs = sorted({a['chopper'].toff for a in ranked})
    groups = defaultdict(list)
    for a in ranked:
        groups[(a['chopper'].tbl, a['chopper'].toff)].append(a['magnitude'])
    z, text = [], []
    for tbl in tbls:
        z_row, text_row = [], []
        for toff in toffs:
            values = groups.get((tbl, toff))
            freq = tmc.chopper_freq_hz(tmc.Chopper(tbl, toff, 0, 0), driver) if values else None
            z_row.append(statistics.median(values) if values else None)
            text_row.append('%.0f%s' % (z_row[-1], '!' if freq < tmc.AUDIBLE_LIMIT_HZ else '')
                            if values else '')
        z.append(z_row)
        text.append(text_row)
    return tbls, toffs, z, text


def hyst_matrix(ranked: 'list[dict]', tbl: int, toff: int):
    """Magnitude per (hstrt, hend) cell for a fixed tbl/toff."""
    cells = defaultdict(list)
    for a in ranked:
        if a['chopper'].tbl == tbl and a['chopper'].toff == toff:
            cells[(a['chopper'].hstrt, a['chopper'].hend)].append(a['magnitude'])
    hstrts = sorted({k[0] for k in cells})
    hends = sorted({k[1] for k in cells})
    z = [[statistics.median(cells[(hs, he)]) if (hs, he) in cells else None for he in hends]
         for hs in hstrts]
    return hstrts, hends, z


def write_report(ranked: 'list[dict]', driver: tmc.Driver, title: str, path: str, top: int = 30):
    import plotly.graph_objects as go
    heat = {'colorscale': 'RdYlGn', 'reversescale': True, 'colorbar': {'title': 'magnitude'}}
    figures = []

    tbls, toffs, z, text = tbl_toff_matrix(ranked, driver)
    fig = go.Figure(go.Heatmap(x=['toff %d' % o for o in toffs], y=['tbl %d' % t for t in tbls],
                               z=z, text=text, texttemplate='%{text}', **heat))
    fig.update_layout(title='chopper frequency landscape: median magnitude per tbl/toff '
                            '(! = audible, lower is better)', height=320)
    figures.append(fig)

    best = ranked[0]['chopper']
    hstrts, hends, z = hyst_matrix(ranked, best.tbl, best.toff)
    if len(hstrts) > 1 or len(hends) > 1:
        fig = go.Figure(go.Heatmap(x=['hend %d' % h for h in hends],
                                   y=['hstrt %d' % h for h in hstrts], z=z, **heat))
        fig.update_layout(title='hysteresis landscape at the best tbl=%d toff=%d'
                                % (best.tbl, best.toff), height=380)
        figures.append(fig)

    leaders = ranked[:top][::-1]
    fig = go.Figure(go.Bar(
        x=[a['magnitude'] for a in leaders],
        y=[a['chopper'].label() for a in leaders],
        orientation='h',
        marker_color=['#d62728' if a['audible'] else '#1D9E75' for a in leaders],
        hovertext=['magnitude %.1f, spread %.1f, n=%d, f_chop %.1f kHz%s'
                   % (a['magnitude'], a['spread'], a['n'], a['chopper_freq_hz'] / 1000,
                      ', audible' if a['audible'] else '') for a in leaders],
    ))
    fig.update_layout(title='top %d configurations (red = audible chopper)' % len(leaders),
                      xaxis_title='median magnitude', height=max(400, 60 + 18 * len(leaders)))
    figures.append(fig)

    fig = go.Figure(go.Scatter(
        x=[a['chopper_freq_hz'] / 1000 for a in ranked],
        y=[a['magnitude'] for a in ranked],
        mode='markers',
        marker={'color': ['#d62728' if a['audible'] else '#1D9E75' for a in ranked],
                'size': 5, 'opacity': 0.5},
        hovertext=[a['chopper'].label() for a in ranked],
    ))
    fig.add_vline(x=tmc.AUDIBLE_LIMIT_HZ / 1000, line_dash='dash', line_color='#d62728')
    fig.update_layout(title='vibration vs chopper frequency (left of the line is audible)',
                      xaxis_title='chopper frequency, kHz', yaxis_title='median magnitude',
                      height=420)
    figures.append(fig)

    parts = ['<html><head><meta charset="utf-8"><title>%s</title></head>'
             '<body style="font-family: sans-serif; max-width: 1100px; margin: auto">'
             '<h2>%s</h2>' % (title, title)]
    parts += [f.to_html(full_html=False, include_plotlyjs='cdn' if i == 0 else False)
              for i, f in enumerate(figures)]
    parts.append('</body></html>')
    with open(path, 'w') as f:
        f.write('\n'.join(parts))


def spearman(xs: 'list[float]', ys: 'list[float]') -> float:
    def ranks(values):
        order = sorted(range(len(values)), key=values.__getitem__)
        result = [0.0] * len(values)
        for position, index in enumerate(order):
            result[index] = float(position)
        return result

    rx, ry = ranks(xs), ranks(ys)
    n = len(xs)
    d2 = sum((a - b) ** 2 for a, b in zip(rx, ry))
    return 1 - 6 * d2 / (n * (n * n - 1))


def run_compare(args) -> int:
    """Agreement between two datasets: winners, rank correlation, top overlap."""
    sides = []
    for path in (args.dataset_a, args.dataset_b):
        ds = Dataset.open(path)
        driver = tmc.DRIVERS[ds.manifest()['driver']]
        aggregates = aggregate(ds, False, 0.25)
        if not aggregates:
            raise SystemExit('no successful measurements in %s' % path)
        winner = rank(aggregates, driver, args.audible_weight)[0]
        sides.append({'path': path, 'winner': winner,
                      'magnitudes': {a['chopper']: a['magnitude'] for a in aggregates}})

    for tag, side in zip('AB', sides):
        print('%s: %s (%d combos)  winner %s -> %.1f'
              % (tag, side['path'], len(side['magnitudes']),
                 side['winner']['chopper'].label(), side['winner']['magnitude']))

    a, b = sides[0]['magnitudes'], sides[1]['magnitudes']
    common = sorted(set(a) & set(b), key=a.get)
    print('Common combos: %d' % len(common))
    if len(common) < 3:
        print('Too few common combos for correlation')
        return 0

    top = min(args.top, len(common))
    top_a = set(sorted(common, key=a.get)[:top])
    top_b = set(sorted(common, key=b.get)[:top])
    print('Spearman rank correlation: %.3f'
          % spearman([a[c] for c in common], [b[c] for c in common]))
    print('Top-%d overlap: %d/%d' % (top, len(top_a & top_b), top))
    print('Median magnitude scale B/A: %.2f'
          % statistics.median(b[c] / a[c] for c in common))
    return 0


def newest_dataset(bases=(RESULTS_HOME / 'datasets', Path('datasets'))) -> Path:
    """Most recently written dataset of any kind, for progress reporting."""
    candidates = [path for base in bases if base.is_dir() for path in base.iterdir()
                  if (path / 'measurements.jsonl').is_file()]
    if not candidates:
        raise SystemExit('no datasets found')
    return max(candidates, key=lambda p: (p / 'measurements.jsonl').stat().st_mtime)


def run_status(args) -> int:
    from datetime import datetime, timezone
    path = Path(args.dataset) if args.dataset else newest_dataset()
    ds = Dataset.open(path)
    manifest = ds.manifest()
    records = ds.records()
    moves = [r for r in records if r.get('kind') in ('move', 'speed')]
    ok = sum(1 for r in moves if r.get('status') == 'ok')

    print('%s: %s %s, capture %s' % (path, manifest.get('search', manifest.get('mode', 'grid')),
                                     manifest.get('stepper', ''), manifest.get('capture', '?')))
    print('Measurements: %d ok, %d failed' % (ok, len(moves) - ok))
    if len(moves) < 2:
        return 0

    first = datetime.fromisoformat(moves[0]['ts'])
    last = datetime.fromisoformat(moves[-1]['ts'])
    elapsed = (last - first).total_seconds()
    rate = (len(moves) - 1) / elapsed if elapsed > 0 else 0
    print('Pace: %.1f s/move over %dm' % (1 / rate if rate else 0, elapsed // 60))

    age = (datetime.now(timezone.utc) - last).total_seconds()
    if age > 120:
        print('Last measurement %dm ago — the run looks finished or stalled' % (age // 60))

    total = args.total
    if not total and manifest.get('search') == 'grid' and 'ranges' in manifest:
        from .collect import Range, build_plan
        ranges = manifest['ranges']
        plan = build_plan(tmc.DRIVERS[manifest['driver']],
                          Range(*ranges['tbl']), Range(*ranges['toff']),
                          Range(*ranges['hstrt']), Range(*ranges['hend']),
                          Range(*ranges['tpfd']) if ranges.get('tpfd') else None,
                          manifest.get('speeds', [0]),
                          manifest.get('skip_audible', False))
        total = len(plan) * manifest.get('iterations', 1) * 2
    if total and rate:
        remaining = max(0, total - len(moves))
        print('Progress: %d/%d (%.0f%%), ETA %dh %02dm'
              % (len(moves), total, 100 * len(moves) / total,
                 remaining / rate // 3600, remaining / rate % 3600 // 60))
    return 0


def run_analyze(args) -> int:
    dataset = args.dataset or latest_dataset()
    ds = Dataset.open(dataset)
    manifest = ds.manifest()
    driver = tmc.DRIVERS[manifest['driver']]
    aggregates = aggregate(ds, args.recompute, args.trim)
    if not aggregates:
        raise SystemExit('no successful measurements in %s' % dataset)
    ranked = rank(aggregates, driver, args.audible_weight)

    print('%s: %d configurations, driver tmc%s on %s\n'
          % (dataset, len(ranked), driver.name, manifest['stepper']))
    print_table(ranked, args.top)

    if not args.no_html:
        path = args.html or str(Path(dataset) / 'report.html')
        write_report(ranked, driver, 'tmc%s %s' % (driver.name, manifest['stepper']), path)
        print('\nReport: %s' % path)

    best = ranked[0]
    print('\nRecommended for printer.cfg:\n')
    print(tmc.cfg_snippet(driver, manifest['stepper'], best['chopper']))
    if args.apply:
        Moonraker(args.url).set_tmc_fields(manifest['stepper'], best['chopper'].fields())
        print('\nApplied via SET_TMC_FIELD (runtime only, edit printer.cfg to persist)')
    return 0
