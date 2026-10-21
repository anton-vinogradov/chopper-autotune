"""Analysis phase: aggregate a dataset, rank configs, report, optionally apply the winner.

Works offline on a collected dataset; the printer is only needed for --apply.
"""
from __future__ import annotations

import os
import re
import statistics
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from . import tmc
from .dataset import Dataset, RESULTS_HOME, load_json, save_json
from .moonraker import Moonraker


def written(path: Path) -> float:
    """When a dataset last took data."""
    records = path / 'measurements.jsonl'
    return (records if records.is_file() else path / 'manifest.json').stat().st_mtime


def dataset_bases() -> 'tuple[Path, ...]':
    """Where the runs keep their datasets: under printer_data, or ./datasets on a machine
    without it (collect.default_dataset_root)."""
    return RESULTS_HOME / 'datasets', Path('datasets')


def dataset_dirs(bases=None) -> 'list[Path]':
    """Oldest to newest by actual data write time, not by name: a custom-named dataset
    would otherwise lexicographically shadow newer timestamped ones forever."""
    return sorted((path for base in bases or dataset_bases() if base.is_dir()
                   for path in base.iterdir() if (path / 'manifest.json').is_file()), key=written)


def latest_dataset(bases=None) -> str:
    for path in reversed(dataset_dirs(bases)):
        # scans and demos are not tuning results: analyzing (worse, SAVE-ing) a
        # two-combo demo dataset as "the latest run" would persist nonsense
        if Dataset(path).manifest().get('mode') not in ('find-speed', 'demo', 'resonance-map'):
            return str(path)
    raise SystemExit('no chopper datasets found, pass the dataset directory explicitly')


def aggregate(ds: Dataset, recompute: bool, trim_fraction: float) -> 'list[dict]':
    groups = defaultdict(list)
    clicks = defaultdict(int)
    for record in ds.moves():
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


def rank(aggregates: 'list[dict]', driver: tmc.Driver, hearing: tmc.Hearing,
         space: 'tmc.Space | None' = None) -> 'list[dict]':
    """Best first. Combos this driver's config cannot hold (a TMC2660 hysteresis sum
    measured before the limit existed), the audible ones the hearing skips and the ones
    outside the run's ranges (a resumed dataset measured under others) are left out:
    nothing may recommend them."""
    from .search import penalized_score
    aggregates = [a for a in aggregates if tmc.validate(a['chopper'], driver) is None
                  and not hearing.skips(a['chopper'], driver)
                  and not (space and space.outside(a['chopper']))]
    for a in aggregates:
        a['chopper_freq_hz'] = tmc.chopper_freq_hz(a['chopper'], driver)
        a['audible'] = hearing.audible(a['chopper'], driver)
        a['score'] = penalized_score(a['chopper'], [a['magnitude']], driver, hearing,
                                     a.get('clicks', 0) / a['n'])
    aggregates.sort(key=lambda a: a['score'])
    return aggregates


def print_table(ranked: 'list[dict]', top: int, recommended: 'dict | None' = None):
    """The top rows, and the recommended one at its own rank when it is below them."""
    # rank flush left: Klipper's console strips the leading spaces of every line
    print('%-4s %4s %5s %6s %5s %5s %10s %8s %3s %6s %8s %s'
          % ('rank', 'tbl', 'toff', 'hstrt', 'hend', 'tpfd', 'magnitude', 'spread', 'n',
             'clicks', 'f_chop', ''))
    rows = list(enumerate(ranked[:top], 1))
    if recommended is not None and recommended not in ranked[:top]:
        rows.append((ranked.index(recommended) + 1, recommended))
    for position, a in rows:
        c = a['chopper']
        print('%-4d %4d %5d %6d %5d %5s %10.1f %8.1f %3d %6d %5.1fkHz %s'
              % (position, c.tbl, c.toff, c.hstrt, c.hend,
                 c.tpfd if c.tpfd is not None else '-',
                 a['magnitude'], a['spread'], a['n'], a.get('clicks', 0),
                 a['chopper_freq_hz'] / 1000, 'audible!' if a['audible'] else ''))


def tbl_toff_matrix(ranked: 'list[dict]', driver: tmc.Driver, hearing: tmc.Hearing):
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
            z_row.append(statistics.median(values) if values else None)
            audible = hearing.audible(tmc.Chopper(tbl, toff, 0, 0), driver)
            text_row.append('%.0f%s' % (z_row[-1], '!' if audible else '') if values else '')
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


def write_report(ranked: 'list[dict]', driver: tmc.Driver, hearing: tmc.Hearing, title: str,
                 path: str, top: int = 30):
    import plotly.graph_objects as go
    heat = {'colorscale': 'RdYlGn', 'reversescale': True, 'colorbar': {'title': 'magnitude'}}
    figures = []

    tbls, toffs, z, text = tbl_toff_matrix(ranked, driver, hearing)
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
    fig.add_vline(x=hearing.limit_hz / 1000, line_dash='dash', line_color='#d62728')
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


def _persist(mk, edits: 'list[tuple[str, object]]', what: str, lines: 'list[str]' = ()):
    """Apply per-section edit functions to the live config through the Moonraker
    files API: all edits in memory first, then every touched file is backed up,
    then the edits are uploaded and Klipper restarted once. A failure mid-upload
    can leave files from different items out of sync, but the originals are
    already backed up by that point. lines: what the console gets once every edit
    applies, before the first upload."""
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

    tell(mk, lines)
    for name in edited:
        mk.upload_config(name.rsplit('.', 1)[0] + BACKUP_SUFFIX, originals[name])
    for name in edited:
        mk.upload_config(name, files[name])
    mk.gcode('RESTART')
    print('\nSaved %s to %s (backups: *%s), Klipper is restarting'
          % (what, ', '.join(edited), BACKUP_SUFFIX))


def twin_refusal(settings: dict, stepper: str) -> 'str | None':
    """A register write to stepper_x/stepper_y alone leaves a twin on the same axis
    (stepper_x1...) with its old registers (#129)."""
    from .collect import rail_twins
    twins = rail_twins(settings, stepper.rsplit('_', 1)[-1])
    if twins:
        return ('refusing to write registers to %s only: %s share its axis and would keep their '
                'old registers (see issue #129)' % (stepper, ', '.join(twins)))
    return None


def refuse_twin_write(mk, stepper: str):
    why = twin_refusal(mk.settings(), stepper)
    if why:
        raise SystemExit(why)


def save_refusal(settings: dict, driver_name: str, stepper: str, fields: 'dict | None' = None,
                 autotune: 'str | None' = None) -> 'str | None':
    """Why registers may not be written into [tmc<driver_name> <stepper>], None when they
    may. fields: the registers, when they are known; autotune: the klipper_tmc_autotune
    goal the result was measured under. A combo Klipper refuses at load keeps it from
    starting, and the undo macros live inside Klipper."""
    from .collect import autotune_goal, autotune_refusal, measured_under_autotune
    if fields is not None:
        why = tmc.validate(tmc.Chopper(**fields), tmc.DRIVERS.get(driver_name))
        if why:
            return 'refusing to save %s to [tmc%s %s]: %s' % (tmc.Chopper(**fields).label(),
                                                              driver_name, stepper, why)
    why = twin_refusal(settings, stepper)
    if why:
        return why
    if autotune_goal(settings, stepper) is not None:
        return autotune_refusal(driver_name, stepper, settings)
    if autotune:
        return measured_under_autotune(driver_name, stepper)
    return None


def shares_enable(settings: dict, stepper: str) -> bool:
    """No enable_pin of its own (none, or one another stepper uses): Klipper switches such
    a driver off with toff=0 and puts its config toff back at every enable (tmc.py)."""
    def pin(name):
        value = (settings.get(name) or {}).get('enable_pin')
        return value.strip().lstrip('!^~').strip() if isinstance(value, str) else None
    own = pin(stepper)
    return not own or any(pin(name) == own for name in settings if name != stepper)


def run_apply(mk, stepper: str, chopper: tmc.Chopper):
    """Set a combo live (SET_TMC_FIELD), not persisted. The motor goes on first, in a
    request of its own (see collect.wake_stepper): toff written to a motor Klipper counts
    as off energizes it, and Klipper's own enable would then put its config toff back."""
    if mk.is_printing():
        raise SystemExit('printer is busy printing, not touching the drivers')
    refuse_twin_write(mk, stepper)
    mk.gcode('SET_STEPPER_ENABLE STEPPER=%s ENABLE=1' % stepper)
    mk.set_tmc_fields(stepper, chopper.fields())
    if shares_enable(mk.settings(), stepper):
        warning = ('WARNING: %s has no enable pin of its own: at the next motors off and on '
                   'Klipper puts toff back to the config value and keeps the other applied '
                   'registers; SAVE=1 keeps the whole set' % stepper)
        print(warning)
        # APPLY runs detached: the log alone is unseen. Not without [respond]: Klipper
        # answers an M118 it lacks with an 'Unknown command' line, KlipperScreen with an error
        if 'M118' in (mk.accepted_commands() or {'M118'}):
            try:
                mk.gcode('M118 ' + warning)
            except Exception:
                pass


def run_save(mk, items: 'list[tuple[dict, tmc.Chopper]]', extruder_state: 'dict | None' = None,
             lines: 'list[str]' = ()):
    """Persist chopper winners into the Klipper config, one restart for the batch;
    the extruder's stored winner (see extruder.save_winner_state) rides along. lines:
    what the console gets before the write (see _persist)."""
    settings = mk.settings()
    for manifest, combo in items:
        why = save_refusal(settings, manifest['driver'], manifest['stepper'], combo.fields(),
                           manifest.get('autotune'))
        if why:
            raise SystemExit(why)
    if extruder_state:
        why = save_refusal(settings, extruder_state['driver'], 'extruder', extruder_state['fields'],
                           extruder_state.get('autotune'))
        if why:
            raise SystemExit(why)
    edits = [('tmc%s %s' % (manifest['driver'], manifest['stepper']),
              lambda text, section, combo=combo: updated_config(text, section,
                                                                combo.fields()))
             for manifest, combo in items]
    if extruder_state:
        edits.append(('tmc%s extruder' % extruder_state['driver'],
                      lambda text, section: updated_config(text, section,
                                                           extruder_state['fields'])))
    _persist(mk, edits, 'the new registers', lines)


def run_save_currents(mk, items: 'list[tuple[str, str, float]]', lines: 'list[str]' = ()):
    """Persist (driver, stepper, amps) run currents, one restart for the batch. lines:
    what the console gets before the write (see _persist)."""
    _persist(mk, [('tmc%s %s' % (driver, stepper),
                   lambda text, section, amps=amps: updated_scalars(text, section,
                                                                    {'run_current': '%.2f' % amps}))
                  for driver, stepper, amps in items], 'the new run currents', lines)


TUNED_FIELD_RE = re.compile(r'^\s*driver_(tbl|toff|hstrt|hend|tpfd)\s*[:=]',
                            re.IGNORECASE | re.MULTILINE)


def tuned_tmc_sections(files: 'dict[str, str]') -> 'list[str]':
    """TMC sections of the tool's motors (X/Y twins included) that carry active
    driver_* tuning lines — the ones a defaults-restore must rewrite (a section without
    them already runs the Klipper defaults, adding explicit lines there would only be
    churn). Each gets its own driver's stock, so a pair ends up matched."""
    found = []
    for text in files.values():
        lines = text.splitlines(keepends=True)
        for match in re.finditer(r'^\[(tmc\w+ (?:stepper_[xy]\d*|extruder))\]',
                                 text, re.MULTILINE):
            section = match.group(1)
            start, end = _section_span(lines, section)
            if TUNED_FIELD_RE.search(''.join(lines[start + 1:end])):
                found.append(section)
    return found


# a rollback restarts the plan: the panel's step marks and Results read these files,
# and their numbers were measured against the registers being rolled away (the skip
# threshold, the envelope ceilings and the map are chopper properties). The runs stay:
# the datasets and the extruder winner (extruder.json); a save skips them by RESTORED.
STALE_ON_RESTORE = ('belts.json', 'current.json', 'envelope.json', 'map.json', 'state.json')
RESTORED = 'restored.json'


def _record_restore():
    for name in STALE_ON_RESTORE:
        try:
            os.remove(RESULTS_HOME / name)
        except OSError:
            pass
    save_json(RESULTS_HOME / RESTORED, {'time': time.time()})


def last_restore() -> float:
    """When CHOPPER_RESTORE last rolled the config back, 0 if never. A run measured before
    it is not saved: the next CHOPPER_SAVE would write the rolled-back registers again."""
    return load_json(RESULTS_HOME / RESTORED).get('time', 0.0)


def run_restore_config(args) -> int:
    """The panel's undo: DEFAULTS=1 writes the stock chopper registers into every
    tuned TMC section (run_current stays — it is a measured safety setting);
    BACKUP=1 puts back the pre-save snapshots wholesale, currents included."""
    mk = Moonraker(args.url)
    if args.defaults == args.backup:
        raise SystemExit('pick one: DEFAULTS=1 (stock chopper registers) or '
                         'BACKUP=1 (the pre-save %s snapshots)' % BACKUP_SUFFIX)
    if args.backup:
        if mk.is_printing():
            raise SystemExit('printer is busy printing, not touching the config')
        backups = [name for name in mk.list_config_files() if name.endswith(BACKUP_SUFFIX)]
        if not backups:
            raise SystemExit('no %s snapshots found — this tool has not saved anything yet'
                             % BACKUP_SUFFIX)
        for name in backups:
            original = name[:-len(BACKUP_SUFFIX)] + '.cfg'
            mk.upload_config(original, mk.download_config(name))
            print('restored %s from %s' % (original, name))
        _record_restore()
        mk.gcode('RESTART')
        print('Klipper is restarting on the pre-save config (registers AND currents)')
        return 0

    files = {name: content for name, content in active_config_files(mk).items()
             if not name.endswith(BACKUP_SUFFIX)}
    sections = tuned_tmc_sections(files)
    if not sections:
        raise SystemExit('no tuned TMC sections found — the config already runs stock registers')
    # stock registers are a property of the driver: a 2240 restored to the 2209 values
    # would sit in a third state that is neither stock nor tuned
    defaults = {section: tmc.driver_default(section.split()[0]) for section in sections}
    print('Writing Klipper default registers into: %s (run_current untouched)'
          % ', '.join('%s (%s)' % (s, d.label()) for s, d in defaults.items()))
    _persist(mk, [(section,
                   lambda text, s, d=default: updated_config(text, s, d.fields()))
                  for section, default in defaults.items()], 'the Klipper default registers')
    _record_restore()
    return 0


MOTOR_AXES = {'A': 'x', 'B': 'y'}


def configured(settings: dict, driver_name: str, stepper: str, fields: dict) -> bool:
    """Whether the config already gives [tmc<driver_name> <stepper>] these registers; a
    register without its driver_* line runs at the driver's stock value."""
    section = settings.get('tmc%s %s' % (driver_name, stepper), {})
    stock = tmc.DRIVERS[driver_name].default.fields()
    return all(int(section.get('driver_' + field, stock[field])) == value
               for field, value in fields.items())


def axis_choice(args, axis: str, settings: dict, since: float,
                runs: 'list[Path] | None' = None) -> 'tuple[str, tuple | None, bool]':
    """What CHOPPER_SAVE does with a motor of the gantry: its console line, the (manifest,
    registers) to write or None, and whether the line is a refusal. The motor's run is
    its newest tuning dataset among runs (default: all of them); one stopped before its
    first measurement does not count."""
    from .collect import BACK_TO_BACK_ROUNDS, motor_label
    from .tune import winner_of
    motor = 'motor ' + motor_label(axis)
    for path in reversed(runs or dataset_dirs()):
        manifest = Dataset(path).manifest()
        if manifest.get('axis') != axis or 'search' not in manifest:
            continue
        winner = manifest.get('winner')
        if not winner and not Dataset(path).moves():
            print('%s: skipping run %s: it has no measurements' % (motor, path.name))
            continue
        if written(path) < since:
            return ('%s: not saving run %s: it is older than the last CHOPPER_RESTORE. Saving it '
                    'would undo that rollback. To save it anyway: CHOPPER_ANALYZE DATASET=%s SAVE=1'
                    % (motor, path.name, path)), None, False
        why = save_refusal(settings, manifest.get('driver', 'XXXX'), 'stepper_' + axis,
                           autotune=manifest.get('autotune'))
        if why:
            return '%s: %s' % (motor, why), None, True
        if not winner:
            return ('%s: not saving run %s: it was stopped before the end, or it was a full sweep '
                    '(SEARCH=grid) with VALIDATE=0. So repeat measurements have not confirmed its '
                    'top-ranked registers. To save them anyway: CHOPPER_ANALYZE DATASET=%s SAVE=1'
                    % (motor, path.name, path)), None, False
        if manifest.get('kept') and configured(settings, manifest['driver'], 'stepper_' + axis,
                                               winner):
            return ('%s: your registers %s stay. In run %s, neither the winner nor Klipper '
                    'defaults was quieter than them in all %d rounds'
                    % (motor, tmc.Chopper(**winner).label(), path.name, BACK_TO_BACK_ROUNDS)), \
                None, False
        try:
            manifest, combo = winner_of(str(path), args)
        except SystemExit as reason:
            return '%s: %s' % (motor, reason), None, True
        return ('%s: %s %s from run %s' % (motor, 'would save' if args.dry_run else 'saving',
                                           combo.label(), path.name)), (manifest, combo), False
    return '%s: no tuning run to save' % motor, None, False


def extruder_choice(args, settings: dict, since: float) -> 'tuple[str, dict | None, bool]':
    """As axis_choice for the extruder, whose run is the winner CHOPPER_EXTRUDER stored."""
    from .extruder import STATE, load_winner_state
    state = load_winner_state()
    if not state:
        return 'motor E: no CHOPPER_EXTRUDER run to save', None, False
    taken = os.path.getmtime(STATE)
    if taken < since:
        return ('motor E: not saving the last CHOPPER_EXTRUDER run: it is older than the last '
                'CHOPPER_RESTORE. Saving it would undo that rollback', None, False)
    why = save_refusal(settings, state['driver'], 'extruder', state['fields'], state.get('autotune')) \
        or inaudible_refusal(state, tmc.Hearing.of(args, state))
    if why:
        return 'motor E: ' + why, None, True
    return ('motor E: %s %s from the CHOPPER_EXTRUDER run of %s'
            % ('would save' if args.dry_run else 'saving',
               tmc.Chopper(**state['fields']).label(),
               datetime.fromtimestamp(taken).strftime('%Y-%m-%d %H:%M')), state, False)


def tell(mk, lines: 'list[str]'):
    """Print the lines and, from a detached run, send them to the Klipper console, where its
    log is not seen. Not from a foreground one (run.sh --sync): its macro holds Klipper's
    G-code queue, and what it prints reaches the console anyway."""
    from .collect import Screen, console_text
    for line in lines:
        print(line)
    if not lines or os.environ.get('CHOPPER_SYNC') \
            or 'RESPOND' not in (mk.accepted_commands() or {'RESPOND'}):
        return
    try:
        mk.gcode('\n'.join('RESPOND PREFIX="%s" MSG="%s"' % (Screen.CONSOLE_PREFIX, console_text(line))
                           for line in lines))
    except Exception:
        pass


def run_save_latest(args, motors: 'str | None' = None, runs: 'list[Path] | None' = None) -> int:
    """CHOPPER_SAVE: write the newest result of each motor (motors: letters of A, B and E,
    MOTOR= by default) into the config, one restart for the batch. Before it the console
    gets a line per motor: the registers and their run, or why the motor is not saved.
    runs: the datasets to take the motors' runs from instead of all; CHOPPER_TUNE SAVE=1
    passes its own, newer than any CHOPPER_RESTORE whatever the clock says."""
    mk = Moonraker(args.url)
    settings = mk.settings()
    since = 0.0 if runs else last_restore()
    lines, refusals, items, extruder_state = [], [], [], None
    for motor in motors or args.motor:
        if motor == 'E':
            line, extruder_state, refused = extruder_choice(args, settings, since)
        else:
            line, item, refused = axis_choice(args, MOTOR_AXES[motor], settings, since, runs)
            if item:
                items.append(item)
        lines.append(line)
        if refused:
            refusals.append(line)
    if not items and not extruder_state and refusals:
        # the refusal is the failure: the display shows what to change, and the console
        # gets it once, as the failure (M118 from a detached run, stderr from a dry run)
        tell(mk, [line for line in lines if line != refusals[0]])
        raise SystemExit(refusals[0])
    if not items and not extruder_state:
        tell(mk, lines + ['Nothing to save, Klipper keeps running'])
    elif args.dry_run:
        tell(mk, lines + ['DRY_RUN=1: nothing written, Klipper keeps running'])
    else:
        run_save(mk, items, extruder_state, lines)
    return 0


def hearing_text(hearing: tmc.Hearing) -> str:
    return '%g kHz, weight %g%s' % (hearing.limit_hz / 1000, hearing.weight,
                                    ', skip' if hearing.skip else '')


def inaudible_refusal(state: dict, hearing: tmc.Hearing) -> 'str | None':
    """Why a stored extruder winner the hearing skips is not saved, or None."""
    combo = tmc.Chopper(**state['fields'])
    if hearing.skips(combo, tmc.DRIVERS[state['driver']]):
        # the action first: the display shows its first characters (failure_display)
        return ('re-run CHOPPER_EXTRUDER with AUDIBLE_KHZ=%g: SKIP_AUDIBLE skips its stored '
                'winner %s, which runs below %g kHz'
                % (hearing.limit_hz / 1000, combo.label(), hearing.limit_hz / 1000))
    return None


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
        manifest = ds.manifest()
        driver = tmc.DRIVERS[manifest['driver']]
        aggregates = aggregate(ds, False, 0.25)
        if not aggregates:
            raise SystemExit('no successful measurements in %s' % path)
        hearing = tmc.Hearing.of(args, manifest)
        space = tmc.Space.recorded(manifest)
        ranked = rank(aggregates, driver, hearing, space)
        if not ranked:
            raise SystemExit('no measured combination in %s fits the tmc%s limits%s%s'
                             % (path, driver.name,
                                ' and the ranges of the dataset\'s last run' if space else '',
                                ' and SKIP_AUDIBLE' if hearing.skip else ''))
        winner = ranked[0]
        sides.append({'path': path, 'winner': winner, 'hearing': hearing,
                      'magnitudes': {a['chopper']: a['magnitude'] for a in aggregates}})

    for tag, side in zip('AB', sides):
        print('%s: %s (%d combos)  winner %s -> %.1f'
              % (tag, side['path'], len(side['magnitudes']),
                 side['winner']['chopper'].label(), side['winner']['magnitude']))
    if sides[0]['hearing'] != sides[1]['hearing']:
        # each dataset ranks as its run heard: the winners may differ by that alone
        print('The winners are ranked under different hearing (A %s, B %s): pass --audible-khz, '
              '--audible-weight and --skip-audible to rank both alike'
              % tuple(hearing_text(side['hearing']) for side in sides))

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


def newest_dataset(bases=None) -> Path:
    """Most recently written dataset of any kind, for progress reporting."""
    candidates = [path for path in dataset_dirs(bases)
                  if (path / 'measurements.jsonl').is_file()]
    if not candidates:
        raise SystemExit('no datasets found')
    return max(candidates, key=lambda p: (p / 'measurements.jsonl').stat().st_mtime)


def run_status(args) -> int:
    from datetime import timezone
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
        from .collect import VALIDATE_EXTRA_ITERATIONS, build_plan
        plan = build_plan(tmc.DRIVERS[manifest['driver']], tmc.Space.recorded(manifest),
                          manifest.get('speeds', [0]), tmc.Hearing.of(recorded=manifest))
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
    hearing = tmc.Hearing.of(args, manifest)
    space = tmc.Space.recorded(manifest)
    ranked = rank(aggregates, driver, hearing, space)
    unloadable = sum(1 for a in aggregates if tmc.validate(a['chopper'], driver) is not None)
    if unloadable:
        print('%d measured combos are left out: Klipper would not load them on tmc%s'
              % (unloadable, driver.name))
    outside = sum(1 for a in aggregates if space and space.outside(a['chopper'])
                  and tmc.validate(a['chopper'], driver) is None)
    if outside:
        print('%d measured combos are left out: they are outside the ranges of the last run '
              'on this dataset, %s' % (outside, space.text()))
    if len(aggregates) - unloadable - outside > len(ranked):
        print('%d measured combos are left out: their chopper runs below %g kHz (SKIP_AUDIBLE)'
              % (len(aggregates) - unloadable - outside - len(ranked), hearing.limit_hz / 1000))
    if not ranked:
        raise SystemExit('no measured combination in %s fits the tmc%s limits%s%s'
                         % (dataset, driver.name,
                            ' and the ranges of the dataset\'s last run' if outside else '',
                            ' and SKIP_AUDIBLE' if hearing.skip else ''))

    print('%s: %d configurations, driver tmc%s on %s\n'
          % (dataset, len(ranked), driver.name, manifest['stepper']))
    saved = manifest.get('winner')
    recorded = tmc.Chopper(saved['tbl'], saved['toff'], saved['hstrt'], saved['hend'],
                           saved.get('tpfd')) if saved else None
    print_table(ranked, args.top, next((a for a in ranked if a['chopper'] == recorded), None))

    if not args.no_html:
        path = args.html or str(Path(dataset) / 'report.html')
        write_report(ranked, driver, hearing, 'tmc%s %s' % (driver.name, manifest['stepper']), path)
        print('\nReport: %s' % path)

    best = ranked[0]
    if recorded:
        validated = recorded
        if tmc.validate(validated, driver) is not None:
            print('\nThe winner recorded by the run, %s, is past the tmc%s limits: '
                  'recommending the best loadable combo instead' % (validated.label(), driver.name))
        elif hearing.skips(validated, driver):
            print('\nThe winner recorded by the run, %s, runs below %g kHz (SKIP_AUDIBLE): '
                  'recommending the best inaudible combo instead'
                  % (validated.label(), hearing.limit_hz / 1000))
        elif validated != best['chopper']:
            # recommend what the run validated, not an unvalidated re-rank topper
            print('\nKeeping the registers the run kept: %s (no combo was quieter than them '
                  'in each back-to-back round)' % validated.label() if manifest.get('kept')
                  else '\nUsing the validated winner recorded by the run: %s '
                  '(top-ranked %s was never re-measured)'
                  % (validated.label(), best['chopper'].label()))
            best = next((a for a in ranked if a['chopper'] == validated),
                        {'chopper': validated})
    if manifest.get('autotune'):
        from .collect import AUTOTUNE_MEASURED
        print('\nBest measured (not for saving: %s):\n' % AUTOTUNE_MEASURED)
    else:
        print('\nRecommended for printer.cfg:\n')
    print(tmc.cfg_snippet(driver, manifest['stepper'], best['chopper']))
    if args.apply and not args.save:
        run_apply(Moonraker(args.url), manifest['stepper'], best['chopper'])
        print('\nApplied via SET_TMC_FIELD (runtime only%s)'
              % ('' if manifest.get('autotune') else ', use SAVE=1 to persist'))
    if args.save:
        run_save(Moonraker(args.url), [(manifest, best['chopper'])])
    return 0
