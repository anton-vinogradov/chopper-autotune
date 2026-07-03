"""Search strategies over the chopper register space and their offline replay."""
from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import replace
from itertools import product

from . import tmc
from .collect import Range
from .dataset import Dataset


def coordinate_descent(driver: tmc.Driver, tbl: Range, toff: Range, hstrt: Range, hend: Range,
                       tpfd: 'Range | None', start: tmc.Chopper, evaluate, rounds: int = 2) -> tmc.Chopper:
    """Greedy descent in the AN-001 tuning order: TBL+TOFF jointly, then HSTRT, HEND, then TPFD.

    evaluate(combo) -> penalized score, lower is better; expected to cache, so
    re-visiting a combo is free. TBL and TOFF interact strongly (both set the
    chopper frequency), hence the joint sweep; the hysteresis pair is separable
    enough for per-field passes, repeated while the optimum keeps moving.
    """
    best = start
    best_score = evaluate(best)

    def consider(candidate: tmc.Chopper):
        nonlocal best, best_score
        if tmc.validate(candidate) is not None:
            return
        score = evaluate(candidate)
        if score < best_score:
            best, best_score = candidate, score

    for _ in range(rounds):
        previous = best
        for t, o in product(tbl.values(), toff.values()):
            consider(replace(best, tbl=t, toff=o))
        for value in hstrt.values():
            consider(replace(best, hstrt=value))
        for value in hend.values():
            consider(replace(best, hend=value))
        if best == previous:
            break

    if driver.has_tpfd and tpfd is not None:
        for value in tpfd.values():
            consider(replace(best, tpfd=value))
    return best


def descent_budget(driver: tmc.Driver, tbl: Range, toff: Range, hstrt: Range, hend: Range,
                   tpfd: 'Range | None', rounds: int = 2) -> int:
    """Upper bound of unique candidates a descent may evaluate."""
    pairs = sum(1 for t, o in product(tbl.values(), toff.values()) if not (o == 1 and t < 2))
    per_round = pairs + len(hstrt.values()) + len(hend.values())
    tpfd_count = len(tpfd.values()) if driver.has_tpfd and tpfd is not None else 0
    return rounds * per_round + tpfd_count


def dataset_history(ds: Dataset) -> 'dict[tmc.Chopper, list[float]]':
    history = defaultdict(list)
    for record in ds.records():
        if record.get('kind') == 'move' and record.get('status') == 'ok':
            combo = tmc.Chopper(record['tbl'], record['toff'], record['hstrt'], record['hend'],
                                record.get('tpfd'))
            history[combo].append(record['score']['median_magnitude'])
    return history


def penalized_score(combo: tmc.Chopper, magnitudes: 'list[float]', driver: tmc.Driver,
                    audible_weight: float) -> float:
    magnitude = statistics.median(magnitudes)
    return magnitude * (1 + audible_weight) if tmc.is_audible(combo, driver) else magnitude


def run_simulate(args) -> int:
    """Replay the descent against a recorded grid dataset: no printer involved."""
    ds = Dataset.open(args.dataset)
    manifest = ds.manifest()
    driver = tmc.DRIVERS[manifest['driver']]
    lookup = {combo: penalized_score(combo, mags, driver, args.audible_weight)
              for combo, mags in dataset_history(ds).items()}
    if not lookup:
        raise SystemExit('no successful measurements in %s' % args.dataset)

    combos = list(lookup)
    ranges = {field: Range(min(getattr(c, field) for c in combos),
                           max(getattr(c, field) for c in combos))
              for field in ('tbl', 'toff', 'hstrt', 'hend')}
    tpfd_values = [c.tpfd for c in combos if c.tpfd is not None]
    tpfd = Range(min(tpfd_values), max(tpfd_values)) if tpfd_values else None

    registers = manifest['baseline_registers']
    start = tmc.Chopper(registers['tbl'], registers['toff'], registers['hstrt'],
                        registers['hend'], registers.get('tpfd'))

    lookups = []

    def evaluate(combo: tmc.Chopper) -> float:
        if combo not in lookup:
            raise SystemExit('%s is missing from the dataset; simulate needs a grid dataset '
                             'covering the whole descent range' % combo.label())
        lookups.append(combo)
        return lookup[combo]

    best = coordinate_descent(driver, ranges['tbl'], ranges['toff'], ranges['hstrt'],
                              ranges['hend'], tpfd, start, evaluate)
    global_best = min(lookup, key=lookup.get)
    gap = (lookup[best] / lookup[global_best] - 1) * 100

    print('Dataset: %d combos; descent evaluated %d unique (%d lookups), %.1f%% of the grid'
          % (len(lookup), len(set(lookups)), len(lookups), 100 * len(set(lookups)) / len(lookup)))
    print('Descent best: %s -> %.1f' % (best.label(), lookup[best]))
    print('Global best:  %s -> %.1f' % (global_best.label(), lookup[global_best]))
    print('Gap to global optimum: %.1f%%' % gap)
    return 0
