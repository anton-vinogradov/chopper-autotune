"""Analysis phase: aggregate a dataset, rank configs, report, optionally apply the winner.

Works offline on a collected dataset; the printer is only needed for --apply.
"""
from __future__ import annotations

import re
import statistics
from collections import defaultdict
from pathlib import Path

from . import tmc
from .dataset import Dataset, RESULTS_HOME
from .moonraker import Moonraker


def dataset_dirs(bases=(RESULTS_HOME / 'datasets', Path('datasets'))) -> 'list[Path]':
    """Oldest to newest by actual data write time, not by name: a custom-named dataset
    would otherwise lexicographically shadow newer timestamped ones forever."""
    def written(path):
        records = path / 'measurements.jsonl'
        return (records if records.is_file() else path / 'manifest.json').stat().st_mtime

    return sorted((path for base in bases if base.is_dir() for path in base.iterdir()
                   if (path / 'manifest.json').is_file()), key=written)


def latest_dataset(bases=(RESULTS_HOME / 'datasets', Path('datasets'))) -> str:
    for path in reversed(dataset_dirs(bases)):
        # scans and demos are not tuning results: analyzing (worse, SAVE-ing) a
        # two-combo demo dataset as "the latest run" would persist nonsense
        if Dataset(path).manifest().get('mode') not in ('find-speed', 'demo', 'resonance-map'):
            return str(path)
    raise SystemExit('no chopper datasets found, pass the dataset directory explicitly')


def aggregate(ds: Dataset, recompute: bool, trim_fraction: float) -> 'list[dict]':
    groups = defaultdict(list)
    clicks = defaultdict(int)
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
        clicks[key] += record['score'].get('clicks', 0)
    # mean across moves, not median: the fwd/rev (and iteration) difference is real
    # signal — a config that is quiet one way and loud the other should not win on
    # its lucky direction. Per-move sample noise is already tamed by the inner median.
    return [{
        'chopper': tmc.Chopper(*key),
        'magnitude': statistics.mean(values),
        'spread': max(values) - min(values),
        'n': len(values),
        'clicks': clicks[key],
    } for key, values in groups.items()]


def rank(aggregates: 'list[dict]', driver: tmc.Driver, audible_weight: float) -> 'list[dict]':
    from .search import penalized_score
    for a in aggregates:
        a['chopper_freq_hz'] = tmc.chopper_freq_hz(a['chopper'], driver)
        a['audible'] = tmc.is_audible(a['chopper'], driver)
        a['score'] = penalized_score(a['chopper'], [a['magnitude']], driver, audible_weight,
                                     a.get('clicks', 0) / a['n'])
    aggregates.sort(key=lambda a: a['score'])
    return aggregates


def print_table(ranked: 'list[dict]', top: int):
    print('%4s %4s %5s %6s %5s %5s %10s %8s %3s %6s %7s %s'
          % ('rank', 'tbl', 'toff', 'hstrt', 'hend', 'tpfd', 'magnitude', 'spread', 'n',
             'clicks', 'f_chop', ''))
    for position, a in enumerate(ranked[:top], 1):
        c = a['chopper']
        print('%4d %4d %5d %6d %5d %5s %10.1f %8.1f %3d %6d %5.1fkHz %s'
              % (position, c.tbl, c.toff, c.hstrt, c.hend,
                 c.tpfd if c.tpfd is not None else '-',
                 a['magnitude'], a['spread'], a['n'], a.get('clicks', 0),
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


def _section_span(lines: 'list[str]', section: str) -> 'tuple[int, int]':
    header = re.compile(r'^\[%s\]\s*(?:[#;].*)?$' % re.escape(section))
    starts = [i for i, line in enumerate(lines) if header.match(line.strip('\r\n'))]
    if not starts:
        raise SystemExit('section [%s] not found' % section)
    if len(starts) > 1:
        raise SystemExit('section [%s] found %d times' % (section, len(starts)))
    start = starts[0]
    end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith('[')),
               len(lines))
    return start, end


def updated_config(text: str, section: str, fields: dict) -> str:
    """Rewrite the driver_* options of one config section, keeping everything else intact.

    Existing active lines for the given fields are dropped (commented ones are left
    as history), the new values are inserted right under the section header.
    """
    lines = text.splitlines(keepends=True)
    start, end = _section_span(lines, section)
    drop = re.compile(r'^\s*driver_(%s)\s*[:=]' % '|'.join(fields), re.IGNORECASE)
    body = [line for line in lines[start + 1:end] if not drop.match(line)]
    inserted = ['driver_%s: %d\n' % (name.upper(), value) for name, value in fields.items()]
    return ''.join(lines[:start + 1] + inserted + body + lines[end:])


def updated_scalars(text: str, section: str, entries: 'dict[str, str]') -> str:
    """Rewrite plain options of one config section (e.g. run_current), same rules
    as updated_config: active lines dropped, new values under the header."""
    lines = text.splitlines(keepends=True)
    start, end = _section_span(lines, section)
    drop = re.compile(r'^\s*(%s)\s*[:=]' % '|'.join(map(re.escape, entries)), re.IGNORECASE)
    body = [line for line in lines[start + 1:end] if not drop.match(line)]
    inserted = ['%s: %s\n' % (name, value) for name, value in entries.items()]
    return ''.join(lines[:start + 1] + inserted + body + lines[end:])


BACKUP_SUFFIX = '.chopper-backup.cfg'
INCLUDE_RE = re.compile(r'^\[include\s+(.+?)\]\s*$', re.MULTILINE)


def active_config_files(mk, root: str = 'printer.cfg') -> 'dict[str, str]':
    """Only the configs Klipper actually loads: root plus its transitive [include]s.

    Dated backups (printer-YYYYMMDD.cfg) and SAVE_CONFIG leftovers sit in the same
    directory and carry the same sections, but are not included anywhere — resolving
    the include closure keeps the search on the live config only.
    """
    import posixpath
    from fnmatch import fnmatch
    all_files = mk.list_config_files()
    active, stack = {}, [root]
    while stack:
        name = stack.pop()
        if name in active or name not in all_files:
            continue
        content = mk.download_config(name)
        active[name] = content
        base = posixpath.dirname(name)
        for pattern in INCLUDE_RE.findall(content):
            full = posixpath.normpath(posixpath.join(base, pattern.strip()))
            stack += [f for f in all_files if fnmatch(f, full)]
    return active


def _persist(mk, edits: 'list[tuple[str, object]]', what: str):
    """Apply per-section edit functions to the live config through the Moonraker
    files API: all edits in memory first, then every touched file is backed up,
    then the edits are uploaded and Klipper restarted once. A failure mid-upload
    can leave files from different items out of sync, but the originals are
    already backed up by that point."""
    if mk.is_printing():
        raise SystemExit('printer is busy printing, not touching the config')
    files = {name: content for name, content in active_config_files(mk).items()
             if not name.endswith(BACKUP_SUFFIX)}
    if not files:
        raise SystemExit('could not read printer.cfg via Moonraker')
    originals = dict(files)
    edited = []
    for section, edit in edits:
        matches = [name for name in files
                   if re.search(r'^\[%s\]' % re.escape(section), files[name], re.MULTILINE)]
        if not matches:
            raise SystemExit('section [%s] not found in any config file' % section)
        if len(matches) > 1:
            raise SystemExit('section [%s] found in several files: %s'
                             % (section, ', '.join(matches)))
        name = matches[0]
        files[name] = edit(files[name], section)
        if name not in edited:
            edited.append(name)

    for name in edited:
        mk.upload_config(name.rsplit('.', 1)[0] + BACKUP_SUFFIX, originals[name])
    for name in edited:
        mk.upload_config(name, files[name])
    mk.gcode('RESTART')
    print('\nSaved %s to %s (backups: *%s), Klipper is restarting'
          % (what, ', '.join(edited), BACKUP_SUFFIX))


def run_save(mk, items: 'list[tuple[dict, tmc.Chopper]]', extruder_state: 'dict | None' = None):
    """Persist chopper winners into the Klipper config, one restart for the batch;
    the extruder's stored winner (see extruder.save_winner_state) rides along."""
    edits = [('tmc%s %s' % (manifest['driver'], manifest['stepper']),
              lambda text, section, combo=combo: updated_config(text, section,
                                                                combo.fields()))
             for manifest, combo in items]
    if extruder_state:
        edits.append(('tmc%s extruder' % extruder_state['driver'],
                      lambda text, section: updated_config(text, section,
                                                           extruder_state['fields'])))
    _persist(mk, edits, 'the new registers')


def run_save_currents(mk, items: 'list[tuple[str, str, float]]'):
    """Persist (driver, stepper, amps) run currents, one restart for the batch."""
    _persist(mk, [('tmc%s %s' % (driver, stepper),
                   lambda text, section, amps=amps: updated_scalars(text, section,
                                                                    {'run_current': '%.2f' % amps}))
                  for driver, stepper, amps in items], 'the new run currents')


def run_save_latest(args) -> int:
    """Persist the most recent tuning result for each motor into the config, batched into
    one restart. Backs the panel's Save button: save what the last tuning achieved, whether
    the motors were tuned separately (Tune A, Tune B) or together (Tune both)."""
    from .collect import motor_label
    from .extruder import load_winner_state
    from .tune import winner_of
    seen, items = set(), []
    for path in reversed(dataset_dirs()):
        info = Dataset(str(path)).manifest()
        axis = info.get('axis')
        if axis in ('x', 'y') and axis not in seen and 'search' in info:
            try:
                manifest, combo = winner_of(str(path), args.audible_weight)
            except SystemExit as reason:
                # an aborted-at-start dataset has no measurements — fall back to an
                # older complete one instead of blocking Save entirely
                print('motor %s: skipping %s (%s)' % (motor_label(axis), Path(path).name, reason))
                continue
            seen.add(axis)
            items.append((manifest, combo))
            print('motor %s: saving %s (from %s)' % (motor_label(axis), combo.label(), Path(path).name))
    extruder_state = load_winner_state()
    if extruder_state:
        print('extruder: saving %s (last CHOPPER_EXTRUDER winner)' % extruder_state['fields'])
    if not items and not extruder_state:
        raise SystemExit('no tuning datasets to save — run CHOPPER_TUNE first')
    run_save(Moonraker(args.url), items, extruder_state)
    return 0


def spearman(xs: 'list[float]', ys: 'list[float]') -> float:
    def ranks(values):
        order = sorted(range(len(values)), key=values.__getitem__)
        result = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            for k in range(i, j + 1):
                result[order[k]] = (i + j) / 2
            i = j + 1
        return result

    # Pearson over the ranks: exact also in the presence of ties
    rx, ry = ranks(xs), ranks(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    return cov / (vx * vy) ** 0.5 if vx and vy else 0.0


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
    candidates = [path for path in dataset_dirs(bases)
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

    # pace over the recent window only, so pauses and resumed runs do not skew it
    recent = moves[-50:]
    last = datetime.fromisoformat(moves[-1]['ts'])
    elapsed = (last - datetime.fromisoformat(recent[0]['ts'])).total_seconds()
    rate = (len(recent) - 1) / elapsed if elapsed > 0 else 0
    print('Pace: %.1f s/move (last %d moves)' % (1 / rate if rate else 0, len(recent)))

    age = (datetime.now(timezone.utc) - last).total_seconds()
    if age > 120:
        print('Last measurement %dm ago — the run looks finished or stalled' % (age // 60))

    total = args.total or manifest.get('total_moves')
    if not total and manifest.get('search') == 'grid' and 'ranges' in manifest:
        from .collect import Range, VALIDATE_EXTRA_ITERATIONS, build_plan
        ranges = manifest['ranges']
        plan = build_plan(tmc.DRIVERS[manifest['driver']],
                          Range(*ranges['tbl']), Range(*ranges['toff']),
                          Range(*ranges['hstrt']), Range(*ranges['hend']),
                          Range(*ranges['tpfd']) if ranges.get('tpfd') else None,
                          manifest.get('speeds', [0]),
                          manifest.get('skip_audible', False))
        total = (len(plan) * manifest.get('iterations', 1) * 2
                 + manifest.get('validate', 0) * VALIDATE_EXTRA_ITERATIONS
                 * len(manifest.get('speeds', [0])) * 2)
    if total and rate:
        from .collect import eta_text
        remaining = max(0, total - len(moves))
        print('Progress: %d/%d (%.0f%%), ETA %s'
              % (len(moves), total, 100 * len(moves) / total, eta_text(remaining / rate)))
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
    saved = manifest.get('winner')
    if saved:
        validated = tmc.Chopper(saved['tbl'], saved['toff'], saved['hstrt'], saved['hend'],
                                saved.get('tpfd'))
        if validated != best['chopper']:
            # recommend what the run validated, not an unvalidated re-rank topper
            print('\nUsing the validated winner recorded by the run: %s '
                  '(top-ranked %s was never re-measured)'
                  % (validated.label(), best['chopper'].label()))
            best = next((a for a in ranked if a['chopper'] == validated),
                        {'chopper': validated})
    print('\nRecommended for printer.cfg:\n')
    print(tmc.cfg_snippet(driver, manifest['stepper'], best['chopper']))
    if args.apply and not args.save:
        Moonraker(args.url).set_tmc_fields(manifest['stepper'], best['chopper'].fields())
        print('\nApplied via SET_TMC_FIELD (runtime only, use SAVE=1 to persist)')
    if args.save:
        run_save(Moonraker(args.url), [(manifest, best['chopper'])])
    return 0
