"""Analysis phase: aggregate a dataset, rank configs, report, optionally apply the winner.

Works offline on a collected dataset; the printer is only needed for --apply.
"""
from __future__ import annotations

import configparser
import fnmatch
import functools
import hashlib
import json
import os
import posixpath
import re
import statistics
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from . import tmc
from .dataset import Dataset, RESULTS_HOME, load_json, measured_steppers, save_json
from .moonraker import Moonraker, MoonrakerError


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


def motor_runs(axis: str, runs: 'list[Path] | None' = None):
    """The (path, manifest) of each tuning run of the motor on `axis` among runs (default:
    all of them), newest first. A run stopped before its first measurement is skipped,
    named in the log."""
    from .collect import motor_label
    for path in reversed(dataset_dirs() if runs is None else runs):
        manifest = Dataset(path).manifest()
        if manifest.get('axis') != axis or 'search' not in manifest:
            continue
        if not manifest.get('winner') and not Dataset(path).moves():
            print('motor %s: skipping run %s: it has no measurements'
                  % (motor_label(axis), path.name))
            continue
        yield path, manifest


def latest_dataset(bases=None, axis: 'str | None' = None) -> str:
    """The run ANALYZE takes without DATASET=: the newest one, or with MOTOR= the newest
    run of that motor, as CHOPPER_SAVE takes it."""
    if axis:
        from .collect import motor_label
        for path, _ in motor_runs(axis, dataset_dirs(bases)):
            return str(path)
        # the action first: the display shows its first characters (failure_display)
        raise SystemExit('tune motor %s first: none of its tuning runs has measurements'
                         % motor_label(axis))
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


def _option_re(options) -> 're.Pattern':
    return re.compile(r'^\s*(%s)\s*[:=]' % '|'.join(map(re.escape, options)), re.IGNORECASE)


def option_lines(text: str, section: str, options) -> 'dict[str, list[str]]':
    """The active lines of each option in one config section, as written."""
    lines = text.splitlines(keepends=True)
    start, end = _section_span(lines, section)
    return {option: [line if line.endswith('\n') else line + '\n'
                     for line in lines[start + 1:end] if _option_re([option]).match(line)]
            for option in options}


def option_values(lines: 'list[str]') -> 'list[str]':
    """What Klipper reads from option lines: each value without its comment."""
    return [re.split(r'#|\s;', re.split(r'[:=]', line, maxsplit=1)[1])[0].strip() for line in lines]


def replaced_options(text: str, section: str, lines: 'dict[str, list[str]]') -> str:
    """Put the given lines in place of the options' active lines in one config section,
    keeping everything else intact: commented lines are left as history, the given ones
    go right under the section header."""
    text_lines = text.splitlines(keepends=True)
    start, end = _section_span(text_lines, section)
    drop = _option_re(lines)
    body = [line for line in text_lines[start + 1:end] if not drop.match(line)]
    inserted = [line for option in lines.values() for line in option]
    return ''.join(text_lines[:start + 1] + inserted + body + text_lines[end:])


def register_options(fields: dict) -> 'dict[str, str]':
    return {'driver_' + name.upper(): '%d' % value for name, value in fields.items()}


def updated_options(text: str, section: str, entries: 'dict[str, str]') -> str:
    """Rewrite options of one config section (driver_*, run_current): their active lines
    are dropped, the new values go right under the header."""
    return replaced_options(text, section, {name: ['%s: %s\n' % (name, value)]
                                            for name, value in entries.items()})


# a save copies each file it changes here. Klipper reads every file an [include] takes,
# and the options of a section met again win: a copy that [include config/*.cfg] takes
# puts its old values over the saved ones. No pattern ending in .cfg takes these names,
# in any folder, Kalico's recursive ** included
BACKUPS = 'chopper-autotune/backups/'
# where older versions kept the copy of x.cfg: beside it, as x.chopper-backup.cfg
OLD_BACKUP_SUFFIX = '.chopper-backup.cfg'


def backup_name(name: str) -> str:
    return BACKUPS + name + '.bak'


def backed_up(copy: str) -> str:
    """The file a copy in BACKUPS is of."""
    return copy[len(BACKUPS):-len('.bak')]


def is_backup(name: str) -> bool:
    return name.startswith(BACKUPS) and name.endswith('.bak')


def glob_takes(pattern: str, path: str) -> bool:
    """Whether glob, as Klipper and Kalico call it on an [include] pattern, takes the file
    at path; both are relative to the config folder. A wildcard stays inside one folder
    and skips names that start with a dot unless it starts with one too. A ** of its own
    takes any number of folders, as in Kalico; Klipper reads it as *, which takes less."""
    def takes(parts, names):
        if not parts:
            return not names
        if parts[0] == '**':
            return takes(parts[1:], names) or (bool(names) and not names[0].startswith('.')
                                               and takes(parts, names[1:]))
        return bool(names) and fnmatch.fnmatchcase(names[0], parts[0]) \
            and (parts[0].startswith('.') or not names[0].startswith('.')) \
            and takes(parts[1:], names[1:])
    return takes(pattern.split('/'), path.split('/'))


def config_chunks(listing: 'list[str]', read, name: str = 'printer.cfg',
                  stack: tuple = ()) -> 'list[tuple[str, str]]':
    """The config as Klipper reads it (configfile.py): (file, text) in its order. Each
    [include] line ends a piece of its file, the files its pattern takes follow in
    sorted order, then the rest of the file. read: the text of a file of the listing."""
    if name not in listing or name in stack:
        return []
    chunks, text = [], []
    for line in read(name).replace('\r\n', '\n').split('\n'):
        header = configparser.RawConfigParser.SECTCRE.match(line.split('#', 1)[0])
        if header and header.group('header').startswith('include '):
            chunks.append((name, '\n'.join(text)))
            text = []
            pattern = posixpath.normpath(posixpath.join(posixpath.dirname(name),
                                                        header.group('header')[8:].strip()))
            for taken in sorted(path for path in listing if glob_takes(pattern, path)):
                chunks += config_chunks(listing, read, taken, stack + (name,))
        else:
            text.append(line)
    return chunks + [(name, '\n'.join(text))]


def klipper_reads(chunks: 'list[tuple[str, str]]') -> 'dict[str, tuple[str, dict]]':
    """Each section as Klipper reads it from the pieces of config_chunks: the file it first
    meets the section in, and the options as (value, the file Klipper takes it from). A
    section met again adds its options, and they win over the earlier ones."""
    reads = {}
    for name, text in chunks:
        parser = configparser.RawConfigParser(strict=False, inline_comment_prefixes=(';', '#'))
        parser.read_string('\n'.join(line.split('#', 1)[0] for line in text.split('\n')), name)
        for section in parser.sections():
            reads.setdefault(section, (name, {}))[1].update(
                (option, (value, name)) for option, value in parser.items(section))
    return reads


def active_config_files(mk) -> 'dict[str, str]':
    """Only the configs Klipper actually loads: printer.cfg and the files its [include]s
    take, in the order Klipper reads them. Dated backups (printer-YYYYMMDD.cfg) and
    SAVE_CONFIG leftovers sit in the same folder and carry the same sections, but no
    include takes them."""
    read = functools.lru_cache(maxsize=None)(mk.download_config)
    return {name: read(name) for name, _ in config_chunks(mk.list_config_files(), read)}


def old_copies(mk, dry_run: bool = False) -> 'tuple[dict[str, tuple[str, str]], list[str], dict]':
    """The copies older versions left beside their files, each with its place in BACKUPS
    and its text; the console line about them; and how Klipper reads its sections once
    they are gone: {section: None when it goes, else {option: the value then, None for
    none}}, for the sections that change. dry_run: the line says the copies would move."""
    listing = mk.list_config_files()
    old = [name for name in listing if name.endswith(OLD_BACKUP_SUFFIX)]
    if not old:
        return {}, [], {}
    read = functools.lru_cache(maxsize=None)(mk.download_config)
    chunks = config_chunks(listing, read)
    now = klipper_reads(chunks)
    without = klipper_reads(config_chunks([name for name in listing if name not in old], read))
    stale, sources = {}, set()
    for section, (first, options) in now.items():
        if section not in without:
            stale[section] = None
            sources |= {first} | {name for _, name in options.values()}
            continue
        after = without[section][1]
        changed = [option for option, (value, _) in options.items()
                   if after.get(option, (None,))[0] != value]
        if changed:
            stale[section] = {option: after[option][0] if option in after else None
                              for option in changed}
            sources |= {options[option][1] for option in changed}
    users = [name for name in dict(chunks) if name in sources]
    taken = [name for name in dict(chunks) if name in old]
    line = ('This command %s the backup copies that older versions of the tool left next to your '
            'files into %s: %s. Their new names end in .bak, so Klipper does not read them through '
            'an [include] of .cfg files'
            % ('would move' if dry_run else 'moves', BACKUPS[:-1], ', '.join(old)))
    if users:
        line = ('Klipper reads %s through an [include]. In %s, Klipper uses old values from %s '
                'instead of the values in your config. %s%s'
                % (', '.join(users), ', '.join('[%s]' % section for section in stale),
                   'it' if len(users) == 1 else 'them', line,
                   '' if dry_run else '. Klipper stops using the old values when it restarts'))
    elif taken:
        line = ('Klipper reads %s through an [include], but this changes none of the values Klipper '
                'uses. %s' % (', '.join(taken), line))
    return {name: (backup_name(name[:-len(OLD_BACKUP_SUFFIX)] + '.cfg'), read(name))
            for name in old}, [line], stale


def settings_without(settings: dict, stale: dict) -> dict:
    """Klipper's settings as Klipper reads the config once the old copies are gone; stale:
    the sections that change (old_copies)."""
    settings = dict(settings)
    for section, options in stale.items():
        if options is None:
            settings.pop(section.lower(), None)
        else:
            settings[section.lower()] = {option: value for option, value in dict(
                settings.get(section.lower(), {}), **options).items() if value is not None}
    return settings


def file_settings(listing: 'list[str]', read) -> dict:
    """The sections as Klipper reads these files (config_chunks): {section: {option: value}},
    lower-cased as in its settings."""
    return {section.lower(): {option: value for option, (value, _) in options.items()}
            for section, (_, options) in klipper_reads(config_chunks(listing, read)).items()}


def restart_settings(mk) -> dict:
    """file_settings of Klipper's next start, the old copies gone (move_old_copies). Klipper
    runs what it read at its last start: old copies it read then, copies moved since, or a
    file edited since set the two apart."""
    return file_settings([name for name in mk.list_config_files()
                          if not name.endswith(OLD_BACKUP_SUFFIX)],
                         functools.lru_cache(maxsize=None)(mk.download_config))


def section_file(files: 'dict[str, str]', section: str) -> 'str | None':
    """The file that holds the section, None when none does."""
    matches = [name for name in files
               if re.search(r'^\[%s\]' % re.escape(section), files[name], re.MULTILINE)]
    if len(matches) > 1:
        raise SystemExit('section [%s] found in several files: %s' % (section, ', '.join(matches)))
    return matches[0] if matches else None


# the lines the last save wrote and the ones they replaced (restore_last_save). Through
# Moonraker, as the copies, not into this host's RESULTS_HOME: `analyze --save` run on
# another machine writes the printer's files, and the restore on the printer reads it
LAST_SAVE = 'chopper-autotune/last-save.json'


def backup_copies(mk) -> 'dict[str, str]':
    """The copies in BACKUPS, each with its text."""
    return {name: mk.download_config(name) for name in mk.list_config_files() if is_backup(name)}


def copies_digest(copies: 'dict[str, str]') -> str:
    """One digest of copies (name: text). The record of a save holds it for the copies in
    BACKUPS: a save that writes copies and no record changes it, and a record with another
    digest is not of the last save."""
    digest = hashlib.sha256()
    for name in sorted(copies):
        digest.update(('%s\0%s\0' % (name, copies[name])).encode())
    return digest.hexdigest()


def move_old_copies(mk, dry_run: bool = False) -> dict:
    """Move the copies older versions left beside their files into BACKUPS, after the
    console line about them (old_copies); dry_run: the line alone. Returns the sections
    Klipper reads otherwise once the copies are gone (old_copies)."""
    moves, line, stale = old_copies(mk, dry_run)
    if moves and not dry_run and mk.is_printing():
        raise SystemExit('printer is busy printing, not touching the config')
    tell(mk, line)
    if dry_run or not moves:
        return stale
    record = json.loads(mk.download_config(LAST_SAVE, missing_ok=True) or 'null')
    # an older version that writes a record takes its digest over these copies in their
    # old place. Each save and restore of this version moves them: a record with another
    # digest is older than the copies, so not of the last save
    current = record and record['copies'] == copies_digest({name: text for name, (_, text)
                                                            in moves.items()})
    if record and not current:
        mk.delete_config(LAST_SAVE)
    for name, (new, text) in moves.items():
        # the upload first: a failed delete leaves a copy in both places, never in none
        mk.upload_config(new, text)
        mk.delete_config(name)
    if current:
        record['copies'] = copies_digest(backup_copies(mk))
        mk.upload_config(LAST_SAVE, json.dumps(record, indent=1))
    return stale


def _persist(mk, writes: 'list[tuple[str, dict[str, str]]]', what: str, lines: 'list[str]' = ()):
    """Write options (driver_*, run_current) into config sections through the Moonraker
    files API: the old copies are moved first (move_old_copies), then all edits are made in
    memory, every touched file is backed up and the record of the save (LAST_SAVE)
    written, then the edits are uploaded and Klipper restarted once. A failure mid-upload
    can leave files from different items out of sync, but the originals and the record are
    already written by that point. lines: what the console gets once every edit applies,
    before the first upload of the save."""
    if mk.is_printing():
        raise SystemExit('printer is busy printing, not touching the config')
    move_old_copies(mk)
    files = active_config_files(mk)
    if not files:
        raise SystemExit('could not read printer.cfg via Moonraker')
    originals = dict(files)
    edited, record = [], {}
    for section, options in writes:
        name = section_file(files, section)
        if name is None:
            raise SystemExit('section [%s] not found in any config file' % section)
        before = option_lines(originals[name], section, options)
        record.setdefault(section, {}).update(
            {option: {'before': before[option], 'saved': value} for option, value in options.items()})
        files[name] = updated_options(files[name], section, options)
        if name not in edited:
            edited.append(name)

    tell(mk, lines)
    for name in edited:
        mk.upload_config(backup_name(name), originals[name])
    mk.upload_config(LAST_SAVE, json.dumps({'time': time.time(),
                                            'copies': copies_digest(backup_copies(mk)),
                                            'sections': record}, indent=1))
    for name in edited:
        mk.upload_config(name, files[name])
    mk.gcode('RESTART')
    print('\nSaved %s to %s. Copies of these files from before the save are in %s. '
          'Klipper is restarting' % (what, ', '.join(edited), BACKUPS[:-1]))


def rail_mismatch(settings: dict, steppers: 'list[str]', driver_name: str) -> 'str | None':
    """Why a dataset's registers may not go to the config's drivers, None when they may:
    they go to the whole rail of the axis (stepper_x and its twins, #129), and only when
    the dataset measured that rail, on drivers of the model it measured. A dataset of one
    motor on an axis that has a twin now, or of a rail changed since, would leave a driver
    on registers no run measured with. One motor's dataset keeps the checks it had before
    rails (decision 19): a section of another model then stops the save, naming it. The
    extruder has no twins: its rail is itself."""
    from .collect import driver_of, motor_label, rail_twins
    axis = steppers[0].rsplit('_', 1)[-1]
    rail = steppers[:1] + rail_twins(settings, axis)

    def drivers(names):
        return '%s alone' % names[0] if len(names) == 1 else ', '.join(names)
    # the action first: the display shows its first characters (failure_display)
    if steppers != rail:
        return ('tune motor %s again: its dataset measured %s, the config drives the axis with %s '
                'now, and the registers go to every driver of an axis or to none (see issue #129)'
                % (motor_label(axis), drivers(steppers), drivers(rail)))
    models = {stepper: driver_of(settings, stepper) for stepper in steppers}
    if len(steppers) > 1 and set(models.values()) != {driver_name}:
        return ('tune motor %s again: its dataset measured TMC%s drivers, the config gives %s now'
                % (motor_label(axis), driver_name, ', '.join(
                    '%s %s' % (stepper, 'TMC%s' % model if model else 'no TMC section')
                    for stepper, model in models.items() if model != driver_name)))
    return None


def save_refusal(settings: dict, driver_name: str, *steppers: str, fields: 'dict | None' = None,
                 autotune: 'str | None' = None) -> 'str | None':
    """Why registers may not be written into [tmc<driver_name> <stepper>] of each of the
    steppers, None when they may: the motor's drivers, a rail's all of them (rail_mismatch).
    fields: the registers, when they are known; autotune: the klipper_tmc_autotune goal the
    result was measured under. A combo Klipper refuses at load keeps it from starting, and
    the undo macros live inside Klipper."""
    from .collect import (autotune_goal, autotune_refusal, measured_under_autotune,
                          tmc_sections)
    if fields is not None:
        why = tmc.validate(tmc.Chopper(**fields), tmc.DRIVERS.get(driver_name))
        if why:
            return 'refusing to save %s to %s: %s' % (tmc.Chopper(**fields).label(),
                                                      tmc_sections(driver_name, *steppers), why)
    why = rail_mismatch(settings, list(steppers), driver_name)
    if why:
        return why
    managed = [stepper for stepper in steppers if autotune_goal(settings, stepper) is not None]
    if managed:
        return autotune_refusal(driver_name, *managed, settings=settings)
    if autotune:
        return measured_under_autotune(driver_name, *steppers)
    return None


def shares_enable(settings: dict, stepper: str) -> bool:
    """No enable_pin of its own (none, or one another stepper uses): Klipper switches such
    a driver off with toff=0 and puts its config toff back at every enable (tmc.py)."""
    def pin(name):
        value = (settings.get(name) or {}).get('enable_pin')
        return value.strip().lstrip('!^~').strip() if isinstance(value, str) else None
    own = pin(stepper)
    return not own or any(pin(name) == own for name in settings if name != stepper)


def run_apply(mk, driver_name: str, steppers: 'list[str]', chopper: tmc.Chopper):
    """Set a combo live (SET_TMC_FIELD), not persisted, into every driver of the motor's rail.
    The motors go on first, in a request of their own (see collect.wake_stepper): toff
    written to a motor Klipper counts as off energizes it, and Klipper's own enable would
    then put its config toff back."""
    if mk.is_printing():
        raise SystemExit('printer is busy printing, not touching the drivers')
    settings = mk.settings()
    why = rail_mismatch(settings, steppers, driver_name)
    if why:
        raise SystemExit(why)
    mk.gcode('\n'.join('SET_STEPPER_ENABLE STEPPER=%s ENABLE=1' % stepper for stepper in steppers))
    if len(steppers) > 1:
        apply_rail(mk, settings, steppers, chopper)
    else:
        mk.set_tmc_fields(steppers[0], chopper.fields())
    for stepper in steppers:
        if not shares_enable(settings, stepper):
            continue
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


def applied_line(mk, manifest: dict, dataset: str, chopper: tmc.Chopper, args) -> str:
    """The console line of APPLY on a rail: the registers, the run, the drivers; what
    CHOPPER_DEMO makes of them, which plays the config's registers against the defaults;
    how to keep them: this ANALYZE on this run with SAVE=1 for APPLY=1, its hearing and
    trim choosing the same set."""
    from .cli import command_line
    from .collect import autotune_manages, driver_config, motor_label
    settings = mk.settings()
    steppers = measured_steppers(manifest)
    line = ('Motor %s: %s from run %s is set on %s'
            % (motor_label(manifest['axis']), chopper.label(), Path(dataset).name,
               ' and '.join(steppers)))
    # klipper_tmc_autotune writes its own registers at every start, whatever printer.cfg says
    if manifest.get('autotune') or autotune_manages(settings, manifest):
        return line + ' until Klipper restarts'
    if stays(mk, settings, manifest['driver'], steppers, chopper.fields()):
        return line + ('. These are the registers Klipper loaded from its config at its last '
                       'start. The run found nothing to change')
    line += ' until Klipper restarts'
    saved = [tmc.baseline_chopper(dict(own['baseline'], **chopper.fields()),
                                  default=own['driver'].default) == own['driver'].default
             for own in (driver_config(settings, stepper) for stepper in steppers)]
    hear = ('These are the Klipper defaults. After you save them, CHOPPER_DEMO has nothing to '
            'play against the defaults' if all(saved) else
            'CHOPPER_DEMO plays the registers Klipper loaded from its config at its last start. '
            'It plays the registers from this run after you save them')
    return '%s. %s. To save them: %s' % (line, hear, command_line(
        'analyze', args, dataset_opt=dataset, axis=None, apply=False, save=True))


def apply_rail(mk, settings: dict, steppers: 'list[str]', chopper: tmc.Chopper):
    """The set into every driver of a rail in one script, as a run writes a candidate: all
    of them switch at one print time. Klipper stops a script at its first line that fails,
    so a driver may have the set and its twin not: then each driver gets back the registers
    its config gives it, as a restart would. Under klipper_tmc_autotune only a restart
    knows them."""
    from .collect import autotune_goal, driver_config, motor_label
    try:
        mk.gcode('\n'.join(tmc.set_fields_script(stepper, chopper.fields())
                            for stepper in steppers))
        return
    except MoonrakerError as failure:
        why = ' '.join(str(failure).split())
    motor = motor_label(steppers[0].rsplit('_', 1)[-1])
    if any(autotune_goal(settings, stepper) is not None for stepper in steppers):
        stuck = list(steppers)
    else:
        stuck = []
        for stepper in steppers:
            own = driver_config(settings, stepper)
            try:
                mk.set_tmc_fields(stepper, dict(own['driver'].default.fields(), **own['baseline']))
            except MoonrakerError:
                stuck.append(stepper)
    # the action first: the display shows its first characters (failure_display)
    if stuck:
        raise SystemExit('restart Klipper (RESTART): setting %s on motor %s failed (%s), and %s '
                         'could not be put back, so the drivers of the motor may run different '
                         'registers' % (chopper.label(), motor, why, ', '.join(stuck)))
    raise SystemExit('check the drivers of motor %s (DUMP_TMC): setting %s failed (%s), so each of '
                     '%s is back on the registers of its config' % (motor, chopper.label(), why,
                                                                    ', '.join(steppers)))


def run_save(mk, items: 'list[tuple[dict, tmc.Chopper]]', extruder_state: 'dict | None' = None,
             lines: 'list[str]' = ()):
    """Persist chopper winners into the Klipper config, each into every section of the
    rail its dataset measured, one restart for the batch; the extruder's stored winner
    (see extruder.save_winner_state) rides along. lines: what the console gets before the
    write (see _persist)."""
    settings = mk.settings()
    for manifest, combo in items:
        why = save_refusal(settings, manifest['driver'], *measured_steppers(manifest),
                           fields=combo.fields(), autotune=manifest.get('autotune'))
        if why:
            raise SystemExit(why)
    if extruder_state:
        why = save_refusal(settings, extruder_state['driver'], 'extruder',
                           fields=extruder_state['fields'], autotune=extruder_state.get('autotune'))
        if why:
            raise SystemExit(why)
    writes = [('tmc%s %s' % (manifest['driver'], stepper), register_options(combo.fields()))
              for manifest, combo in items for stepper in measured_steppers(manifest)]
    if extruder_state:
        writes.append(('tmc%s extruder' % extruder_state['driver'],
                       register_options(extruder_state['fields'])))
    _persist(mk, writes, 'the new registers', lines)


def run_save_currents(mk, items: 'list[tuple[str, str, float]]', lines: 'list[str]' = ()):
    """Persist (driver, stepper, amps) run currents, one restart for the batch. lines:
    what the console gets before the write (see _persist)."""
    _persist(mk, [('tmc%s %s' % (driver, stepper), {'run_current': '%.2f' % amps})
                  for driver, stepper, amps in items], 'the new run currents', lines)


TUNED_FIELD_RE = re.compile(r'^\s*driver_(tbl|toff|hstrt|hend|tpfd)\s*[:=]',
                            re.IGNORECASE | re.MULTILINE)
# the TMC sections of the tool's motors, X/Y twins included
TMC_SECTION_RE = re.compile(r'^\[(tmc\w+ (?:stepper_[xy]\d*|extruder))\]', re.MULTILINE)


def tuned_tmc_sections(files: 'dict[str, str]') -> 'list[str]':
    """TMC sections of the tool's motors (X/Y twins included) that carry active
    driver_* tuning lines — the ones a defaults-restore must rewrite (a section without
    them already runs the Klipper defaults, adding explicit lines there would only be
    churn). Each gets its own driver's stock, so a pair ends up matched."""
    found = []
    for text in files.values():
        lines = text.splitlines(keepends=True)
        for match in TMC_SECTION_RE.finditer(text):
            section = match.group(1)
            start, end = _section_span(lines, section)
            if TUNED_FIELD_RE.search(''.join(lines[start + 1:end])):
                found.append(section)
    return found


# a rollback of motor A or B restarts the plan: the panel's step marks and Results read
# these files, and their numbers were measured on these motors against the registers being
# rolled away (the skip threshold, the envelope ceilings and the map are chopper
# properties). The runs stay: the datasets and the extruder winner (extruder.json); a save
# skips them by RESTORED.
STALE_ON_RESTORE = ('belts.json', 'current.json', 'envelope.json', 'map.json', 'state.json')
RESTORED = 'restored.json'


def _record_restore(sections: 'list[str]'):
    """Note the rollback of the motors of these TMC sections (last_restore); one of motor A
    or B restarts the plan."""
    motors = {section_motor(section) for section in sections}
    if motors & set('AB'):
        for name in STALE_ON_RESTORE:
            try:
                os.remove(RESULTS_HOME / name)
            except OSError:
                pass
    now = time.time()
    save_json(RESULTS_HOME / RESTORED, {motor: now if motor in motors else last_restore(motor)
                                        for motor in 'ABE'})


def last_restore(motor: str) -> float:
    """When CHOPPER_RESTORE last rolled back this motor (A, B or E), 0 if never; an older
    version kept one time for all motors. A run of the motor measured before it is not
    saved: the next CHOPPER_SAVE would write the rolled-back registers again."""
    restored = load_json(RESULTS_HOME / RESTORED)
    return restored.get(motor, restored.get('time', 0.0))


def rollback_text(name: str, section: str, back: 'dict[str, list[str]]') -> str:
    """The lines a section gets back, as the console shows them: an option that had no
    line before the save loses its line again and runs at Klipper's default."""
    parts = [', '.join('%s: %s' % (option, value) for option, before in back.items()
                       for value in option_values(before))]
    stock = [option for option, before in back.items() if not before]
    if stock:
        parts.append("Klipper's default%s for %s" % ('s' if len(stock) > 1 else '', ', '.join(stock)))
    return '%s [%s] gets back %s' % (name, section, ' and '.join(part for part in parts if part))


def restore_last_save(mk, record: dict) -> int:
    """BACKUP=1 by the record of the last save: the lines it wrote go back to what they
    were before it, everything else stays. A line changed since the save goes back too,
    and the console says so."""
    files = active_config_files(mk)
    when = datetime.fromtimestamp(record['time']).strftime('%Y-%m-%d %H:%M')
    lines, edited, gone = [], [], []
    for section, options in record['sections'].items():
        name = section_file(files, section)
        if name is None:
            gone.append(section)
            lines.append('[%s] is not in the config any more: nothing to put back there' % section)
            continue
        now = {option: option_values(found)
               for option, found in option_lines(files[name], section, options).items()}
        back = {option: entry['before'] for option, entry in options.items()
                if now[option] != option_values(entry['before'])}
        if not back:
            continue
        files[name] = replaced_options(files[name], section, back)
        if name not in edited:
            edited.append(name)
        lines.append(rollback_text(name, section, back))
        lines += ['[%s] %s changed after the save: the save wrote %s, the config has %s. The '
                  'rollback still puts back %s'
                  % (section, option, options[option]['saved'],
                     ', '.join(now[option]) or 'no such line',
                     ', '.join(option_values(back[option])) or "Klipper's default")
                  for option in back if now[option] != [options[option]['saved']]]
    # a section a hand already put back counts too: a run of its motor from before the
    # rollback would write the save's registers again
    rolled = [section for section in record['sections'] if section not in gone]
    if not edited:
        # the first rollback of a save restarts the plan even when a hand already put its
        # lines back; a repeated one keeps what was measured after the first
        if any(last_restore(section_motor(section)) < record['time'] for section in rolled):
            _record_restore(rolled)
        nothing = ('Nothing to put back from the last save, made %s. Klipper keeps running' if gone
                   else 'Nothing to put back from the last save, made %s: the lines it wrote '
                        'already have the values they had before it. Klipper keeps running')
        tell(mk, lines + [nothing % when])
        return 0
    tell(mk, ['Rolling back the last save, made %s:' % when] + lines
         + ['The rest of the config stays as it is, including what SAVE_CONFIG wrote. '
            'Klipper restarts now'])
    for name in edited:
        mk.upload_config(name, files[name])
    _record_restore(rolled)
    mk.gcode('RESTART')
    return 0


def rails_apart(mk, copies: 'dict[str, str]') -> 'list[str]':
    """A console line for each motor with a twin (#129) whose drivers get different registers
    once the copies (backup_copies) are back over their files."""
    from .collect import driver_config, driver_of, motor_label, rail_steppers
    settings = mk.settings()
    rails = [steppers for steppers in (rail_steppers(settings, axis) for axis in 'xy')
             if len(steppers) > 1]
    if not rails:
        return []
    back = {backed_up(copy): text for copy, text in copies.items()}
    read = functools.lru_cache(maxsize=None)(mk.download_config)
    after = file_settings(sorted(set(mk.list_config_files()) | set(back)),
                          lambda name: back[name] if name in back else read(name))
    lines = []
    for steppers in rails:
        sets = {}
        for stepper in steppers:
            if driver_of(after, stepper):
                own = driver_config(after, stepper)
                sets[stepper] = tmc.baseline_chopper(own['baseline'],
                                                     default=own['driver'].default).label()
        if len(set(sets.values())) > 1:
            lines.append('After the files go back, the drivers of motor %s get different '
                         'registers: %s. CHOPPER_DEMO refuses such a motor. To give its drivers '
                         'one set, copy the driver_* lines of one section into the others and '
                         'restart Klipper'
                         % (motor_label(steppers[0].rsplit('_', 1)[-1]),
                            ', '.join('%s %s' % pair for pair in sets.items())))
    return lines


def restore_whole_files(mk) -> int:
    """BACKUP=1 without a record of the last save (some older versions keep none): the
    copies go back over their files whole. Each copy is of its file before the last save
    that changed it, so the sections of a rail in two files may get registers of different
    saves: the console names such a motor."""
    copies = backup_copies(mk)
    if not copies:
        raise SystemExit('no copies in %s: this tool has not saved anything yet, or someone '
                         'deleted the copies' % BACKUPS[:-1])
    tell(mk, ['The last save left no record of the lines it changed, so whole files go back: '
              + ', '.join('%s from %s' % (backed_up(copy), copy) for copy in copies),
              'This also undoes anything written to these files after the tool copied them, such '
              'as the input shaper, bed mesh and Z offset from SAVE_CONFIG. Klipper restarts now']
         + rails_apart(mk, copies))
    for copy, text in copies.items():
        mk.upload_config(backed_up(copy), text)
    _record_restore([match.group(1) for text in copies.values()
                     for match in TMC_SECTION_RE.finditer(text)])
    mk.gcode('RESTART')
    return 0


def run_restore_config(args) -> int:
    """The panel's undo: DEFAULTS=1 writes the stock chopper registers into every
    tuned TMC section (run_current stays — it is a measured safety setting);
    BACKUP=1 puts back the lines the last save wrote, currents included."""
    mk = Moonraker(args.url)
    if args.defaults == args.backup:
        raise SystemExit('pick one: DEFAULTS=1 (stock chopper registers) or BACKUP=1 (the old '
                         'values of the registers and currents that the last save changed)')
    if args.backup and args.motor:
        raise SystemExit('leave MOTOR out: BACKUP=1 puts back the whole last save, every motor '
                         'in it. To set only %s to Klipper\'s default registers: CHOPPER_RESTORE '
                         'DEFAULTS=1 MOTOR=%s' % (motors_text(args.motor), args.motor))
    if mk.is_printing():
        raise SystemExit('printer is busy printing, not touching the config')
    move_old_copies(mk)
    if args.backup:
        record = json.loads(mk.download_config(LAST_SAVE, missing_ok=True) or 'null')
        if record and record['copies'] == copies_digest(backup_copies(mk)):
            return restore_last_save(mk, record)
        return restore_whole_files(mk)

    motors = args.motor or 'ABE'
    files = active_config_files(mk)
    sections = [section for section in tuned_tmc_sections(files) if section_motor(section) in motors]
    if not sections:
        raise SystemExit('no tuned TMC sections found for %s: %s Klipper\'s default registers'
                         % (motors_text(motors), 'it already runs' if len(motors) == 1
                            else 'they already run'))
    # stock registers are a property of the driver: a 2240 restored to the 2209 values
    # would sit in a third state that is neither stock nor tuned
    defaults = {section: tmc.driver_default(section.split()[0]) for section in sections}
    print('Writing Klipper default registers into: %s (run_current untouched)'
          % ', '.join('%s (%s)' % (s, d.label()) for s, d in defaults.items()))
    _persist(mk, [(section, register_options(default.fields()))
                  for section, default in defaults.items()], 'the Klipper default registers')
    _record_restore(sections)
    return 0


def section_motor(section: str) -> str:
    """The motor of a section TMC_SECTION_RE finds: A for stepper_x and its twins on the
    axis, B for stepper_y and its twins, E for the extruder."""
    from .collect import motor_label
    stepper = section.split()[1]
    return 'E' if stepper == 'extruder' else motor_label(stepper[len('stepper_')])


def motors_text(motors: str) -> str:
    """'motor B', 'motors A and E', 'motors A, B and E'."""
    if len(motors) == 1:
        return 'motor ' + motors
    return 'motors %s and %s' % (', '.join(motors[:-1]), motors[-1])


MOTOR_AXES = {'A': 'x', 'B': 'y'}


def configured(settings: dict, driver_name: str, stepper: str, fields: dict) -> bool:
    """Whether the config already gives [tmc<driver_name> <stepper>] these registers; a
    register without its driver_* line runs at the driver's stock value."""
    section = settings.get('tmc%s %s' % (driver_name, stepper), {})
    stock = tmc.DRIVERS[driver_name].default.fields()
    return all(int(section.get('driver_' + field, stock[field])) == value
               for field, value in fields.items())


def stays(mk, settings: dict, driver_name: str, steppers: 'list[str]', fields: dict) -> bool:
    """Whether each of the steppers keeps these registers with nothing saved: the config
    Klipper loaded at its last start gives them (settings), and so do the files it reads at
    the next one (restart_settings, read through mk once the first holds). Only then does
    a save write nothing and restart nothing."""
    def held(view):
        return all(configured(view, driver_name, stepper, fields) for stepper in steppers)
    return held(settings) and held(restart_settings(mk))


def axis_choice(args, axis: str, settings: dict, since: float, runs: 'list[Path] | None',
                mk, loaded: dict) -> 'tuple[str, tuple | None, bool]':
    """What CHOPPER_SAVE does with a motor of the gantry: its console line, the (manifest,
    registers) to write or None, and whether the line is a refusal. The motor's run is
    its newest tuning dataset among runs (all of them when None); one stopped before its
    first measurement does not count. settings: the config as Klipper reads it once the
    old copies are gone; loaded: Klipper's settings, what it runs; mk: the files (stays)."""
    from .cli import command_line
    from .collect import BACK_TO_BACK_ROUNDS, motor_label, tmc_sections
    from .tune import winner_of
    motor = 'motor ' + motor_label(axis)
    for path, manifest in motor_runs(axis, runs):
        winner = manifest.get('winner')
        driver_name, steppers = manifest.get('driver', 'XXXX'), measured_steppers(manifest)
        why = save_refusal(settings, driver_name, *steppers, autotune=manifest.get('autotune'))
        if why:
            return '%s: %s' % (motor, why), None, True
        if not winner:
            return ('%s: not saving run %s: it was stopped before the end, or it was a full sweep '
                    '(SEARCH=grid) with VALIDATE=0. So repeat measurements have not confirmed its '
                    'top-ranked registers. To save them anyway: CHOPPER_ANALYZE DATASET=%s SAVE=1'
                    % (motor, path.name, path)), None, False
        # a rail's registers go to every section of it: the line names them
        sections = tmc_sections(driver_name, *steppers) if len(steppers) > 1 else ''

        def stay(fields):
            # one motor keeps main's check (decision 19)
            return stays(mk, loaded, driver_name, steppers, fields) if sections \
                else configured(settings, driver_name, steppers[0], fields)
        if manifest.get('kept') and stay(winner):
            return ('%s: your registers %s stay%s. In run %s, neither the winner nor Klipper '
                    'defaults was quieter than them in all %d rounds'
                    % (motor, tmc.Chopper(**winner).label(), ' in ' + sections if sections else '',
                       path.name, BACK_TO_BACK_ROUNDS)), None, False
        try:
            manifest, combo = winner_of(str(path), args)
        except SystemExit as reason:
            return '%s: %s' % (motor, reason), None, True
        if stay(combo.fields()):
            return ('%s: the config already has %s, the result of run %s%s, so there is nothing '
                    'to write' % (motor, combo.label(), path.name,
                                  ', in ' + sections if sections else '')), None, False
        if written(path) < since:
            return ('%s: not saving run %s: it is older than the last CHOPPER_RESTORE that rolled '
                    'this motor back. Saving it would undo that rollback. To save it anyway: '
                    'CHOPPER_ANALYZE DATASET=%s SAVE=1'
                    % (motor, path.name, path)), None, False
        return save_line(motor, combo, 'run ' + path.name, args.dry_run,
                         command_line('tune', axis=axis), sections), (manifest, combo), False
    return '%s: no tuning run to save' % motor, None, False


def extruder_choice(args, settings: dict, since: float) -> 'tuple[str, dict | None, bool]':
    """As axis_choice for the extruder, whose run is the winner CHOPPER_EXTRUDER stored."""
    from .cli import command_line
    from .extruder import STATE, load_winner_state
    state = load_winner_state()
    if not state:
        return 'motor E: no CHOPPER_EXTRUDER run to save', None, False
    taken = os.path.getmtime(STATE)
    why = save_refusal(settings, state['driver'], 'extruder', fields=state['fields'],
                       autotune=state.get('autotune')) \
        or inaudible_refusal(state, tmc.Hearing.of(args, state))
    if why:
        return 'motor E: ' + why, None, True
    combo, run = tmc.Chopper(**state['fields']), datetime.fromtimestamp(taken).strftime('%Y-%m-%d %H:%M')
    if configured(settings, state['driver'], 'extruder', state['fields']):
        return ('motor E: the config already has %s, the result of the CHOPPER_EXTRUDER run of %s, '
                'so there is nothing to write' % (combo.label(), run)), None, False
    if taken < since:
        return ('motor E: not saving the last CHOPPER_EXTRUDER run: it is older than the last '
                'CHOPPER_RESTORE that rolled this motor back. Saving it would undo that rollback'), \
            None, False
    return save_line('motor E', combo, 'the CHOPPER_EXTRUDER run of ' + run, args.dry_run,
                     command_line('extruder')), state, False


def save_line(motor: str, combo: tmc.Chopper, run: str, dry_run: bool, search: str,
              sections: str = '') -> str:
    """The console line of a save that writes `combo`, the result of `run`, for `motor`
    ('motor A'); sections: a rail's, each of which gets it. Registers on an edge the default
    search leaves out (tmc.default_edges) get a warning with `search`, the command that
    searches again without the edges."""
    line = '%s: %s %s%s from %s' % (motor, 'would save' if dry_run else 'saving', combo.label(),
                                    ' into ' + sections if sections else '', run)
    edges = tmc.default_edges(combo)
    if not edges:
        return line
    these = 'these edges' if len(edges) > 1 else 'this edge'
    both = 'both edges' if len(edges) > 1 else 'both TBL 0 and effective hysteresis 16'
    return line + ('. These registers have %s, %s the search leaves out by default. On two '
                   'printers, registers with %s were noisy at 5 and 10 mm/s. We do not know which '
                   'of the two makes the noise. To search without %s: %s'
                   % (' and '.join(edges), 'edges' if len(edges) > 1 else 'an edge', both, these,
                      search))


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
    loaded = mk.settings()
    # Klipper read the old copies at its last start: each motor is weighed against the
    # config as Klipper reads it once they are gone
    settings = settings_without(loaded, move_old_copies(mk, args.dry_run))
    lines, refusals, items, extruder_state = [], [], [], None
    for motor in motors or args.motor:
        since = 0.0 if runs else last_restore(motor)
        if motor == 'E':
            line, extruder_state, refused = extruder_choice(args, settings, since)
        else:
            line, item, refused = axis_choice(args, MOTOR_AXES[motor], settings, since, runs,
                                              mk, loaded)
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
    axis = getattr(args, 'axis', None)
    dataset = args.dataset or latest_dataset(axis=axis)
    ds = Dataset.open(dataset)
    manifest = ds.manifest()
    if axis and manifest.get('axis') != axis:
        from .collect import motor_label
        # the action first: the display shows its first characters (failure_display)
        raise SystemExit('drop DATASET= or MOTOR=: %s is a run of motor %s, not of motor %s'
                         % (dataset, motor_label(manifest.get('axis') or '?'), motor_label(axis)))
    driver = tmc.DRIVERS[manifest['driver']]
    aggregates = aggregate(ds, args.recompute, args.trim)
    if not aggregates and not args.dataset and not axis and len(measured_steppers(manifest)) > 1:
        # the action first: the display shows its first characters (failure_display). One
        # motor's refusal stays main's (decision 19)
        raise SystemExit('add MOTOR=A or MOTOR=B: the newest run, %s, has no successful '
                         'measurements. With MOTOR=, CHOPPER_ANALYZE takes the newest run of that '
                         'motor that has them' % Path(dataset).name)
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

    steppers = measured_steppers(manifest)
    print('%s: %d configurations, driver tmc%s on %s\n'
          % (dataset, len(ranked), driver.name, ', '.join(steppers)))
    saved = manifest.get('winner')
    recorded = tmc.Chopper(saved['tbl'], saved['toff'], saved['hstrt'], saved['hend'],
                           saved.get('tpfd')) if saved else None
    print_table(ranked, args.top, next((a for a in ranked if a['chopper'] == recorded), None))

    if not args.no_html:
        path = args.html or str(Path(dataset) / 'report.html')
        write_report(ranked, driver, hearing, 'tmc%s %s' % (driver.name, ', '.join(steppers)), path)
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
    from .collect import AUTOTUNE_MEASURED, rail_snippet
    if manifest.get('autotune'):
        print('\nBest measured (not for saving: %s):\n' % AUTOTUNE_MEASURED)
    else:
        print('\nRecommended for printer.cfg:\n')
    print(rail_snippet(driver, steppers, best['chopper']))
    if args.apply and not args.save:
        mk = Moonraker(args.url)
        run_apply(mk, driver.name, steppers, best['chopper'])
        if len(steppers) > 1:
            # APPLY runs detached: the console sees nothing else of it. One motor's APPLY
            # stays main's (decision 19)
            tell(mk, [applied_line(mk, manifest, dataset, best['chopper'], args)])
        else:
            print('\nApplied via SET_TMC_FIELD (runtime only%s)'
                  % ('' if manifest.get('autotune') else ', use SAVE=1 to persist'))
    if args.save:
        from .cli import command_line
        from .collect import motor_label, tmc_sections
        axis = manifest['stepper'].rsplit('_', 1)[-1]
        run_save(Moonraker(args.url), [(manifest, best['chopper'])], lines=[save_line(
            'motor ' + motor_label(axis), best['chopper'], 'run ' + Path(dataset).name, False,
            command_line('tune', axis=axis),
            tmc_sections(driver.name, *steppers) if len(steppers) > 1 else '')])
    return 0
