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
        if base.is_dir():
            found = sorted(p for p in base.iterdir() if (p / 'manifest.json').is_file())
            if found:
                return str(found[-1])
    raise SystemExit('no datasets found, pass the dataset directory explicitly')


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


def write_report(ranked: 'list[dict]', title: str, path: str):
    import plotly.graph_objects as go
    palette = ('#2F4F4F', '#12B57F', '#9DB512', '#DF8816', '#1297B5', '#5912B5', '#B51284', '#127D0C')
    ordered = ranked[::-1]
    fig = go.Figure(go.Bar(
        x=[a['magnitude'] for a in ordered],
        y=[a['chopper'].label() for a in ordered],
        orientation='h',
        marker_color=['#d62728' if a['audible'] else palette[a['chopper'].toff % len(palette)]
                      for a in ordered],
        hovertext=['magnitude %.1f, spread %.1f, n=%d, f_chop %.1f kHz%s'
                   % (a['magnitude'], a['spread'], a['n'], a['chopper_freq_hz'] / 1000,
                      ', audible' if a['audible'] else '') for a in ordered],
    ))
    fig.update_layout(title=title, xaxis_title='median magnitude (red = audible chopper)',
                      height=max(500, 200 + 14 * len(ordered)))
    fig.write_html(path)


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
        write_report(ranked, 'tmc%s %s' % (driver.name, manifest['stepper']), path)
        print('\nReport: %s' % path)

    best = ranked[0]
    print('\nRecommended for printer.cfg:\n')
    print(tmc.cfg_snippet(driver, manifest['stepper'], best['chopper']))
    if args.apply:
        Moonraker(args.url).set_tmc_fields(manifest['stepper'], best['chopper'].fields())
        print('\nApplied via SET_TMC_FIELD (runtime only, edit printer.cfg to persist)')
    return 0
