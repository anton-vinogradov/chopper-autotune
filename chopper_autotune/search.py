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


def _spanning_starts(tbl: Range, toff: Range, hstrt: Range, hend: Range) -> 'list[tmc.Chopper]':
    """Seeds spread across the (toff, hend) plane. Phase A of the descent sweeps
    (tbl, toff) at a *fixed* hend, so which toff looks best depends on the starting
    hend — a single low-hend start hides the low-toff/high-hend valley. Starting
    from a few hend (and toff) levels lets some run see it."""
    def levels(r: Range):
        return sorted({r.lo, (r.lo + r.hi) // 2, r.hi})
    tbl0, hstrt0 = tbl.lo, (hstrt.lo + hstrt.hi) // 2
    seeds = [tmc.Chopper(tbl0, o, hstrt0, e) for o in levels(toff) for e in levels(hend)]
    return [c for c in seeds if tmc.validate(c) is None]


def multi_start_descent(driver: tmc.Driver, tbl: Range, toff: Range, hstrt: Range, hend: Range,
                        tpfd: 'Range | None', baseline: tmc.Chopper, evaluate,
                        rounds: int = 2) -> tmc.Chopper:
    """Coordinate descent from several seeds; the best result wins. evaluate caches,
    so seeds that converge to the same region cost nothing extra."""
    starts = [baseline] + [c for c in _spanning_starts(tbl, toff, hstrt, hend) if c != baseline]
    best, best_score = None, float('inf')
    for start in starts:
        candidate = coordinate_descent(driver, tbl, toff, hstrt, hend, tpfd, start, evaluate, rounds)
        score = evaluate(candidate)
        if best is None or score < best_score:
            best, best_score = candidate, score
    return best


def descent_budget(driver: tmc.Driver, tbl: Range, toff: Range, hstrt: Range, hend: Range,
                   tpfd: 'Range | None', rounds: int = 2) -> int:
    """Rough upper bound of unique candidates the multi-start descent evaluates.

    Phase A's (tbl, toff) sweep is re-done once per distinct starting hend, so the
    cost scales with the number of hend seed levels; seeds sharing a hend converge
    into the same cached region and add little."""
    pairs = sum(1 for t, o in product(tbl.values(), toff.values()) if not (o == 1 and t < 2))
    per_round = pairs + len(hstrt.values()) + len(hend.values())
    tpfd_count = len(tpfd.values()) if driver.has_tpfd and tpfd is not None else 0
    hend_levels = len({hend.lo, (hend.lo + hend.hi) // 2, hend.hi})
    return hend_levels * rounds * per_round + tpfd_count


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
    # mean across moves (see analyze.aggregate): direction asymmetry is signal
    magnitude = statistics.mean(magnitudes)
    return magnitude * (1 + audible_weight) if tmc.is_audible(combo, driver) else magnitude


def seed_start(ds: Dataset, driver: tmc.Driver, audible_weight: float) -> tmc.Chopper:
    """Best combo of a previously collected dataset, adapted to the target driver.

    Used to start the descent for one motor from the winner of another: the seed
    only positions the search, every candidate is still measured on this motor.
    """
    history = dataset_history(ds)
    if not history:
        raise SystemExit('no successful measurements in the seed dataset %s' % ds.root)
    best = min(history, key=lambda combo: penalized_score(combo, history[combo], driver,
                                                          audible_weight))
    if not driver.has_tpfd and best.tpfd is not None:
        best = replace(best, tpfd=None)
    return best


def run_simulate(args) -> int:
    """Replay the descent against a recorded grid dataset: no printer involved."""
    ds = Dataset.open(args.dataset)
    manifest = ds.manifest()
    driver = tmc.DRIVERS[manifest['driver']]
    lookup = {combo: penalized_score(combo, mags, driver, args.audible_weight)
              for combo, mags in dataset_history(ds).items()}
    if not lookup:
        raise SystemExit('no register measurements in %s — simulate needs a grid dataset'
                         % args.dataset)

    combos = list(lookup)
    ranges = {field: Range(min(getattr(c, field) for c in combos),
                           max(getattr(c, field) for c in combos))
              for field in ('tbl', 'toff', 'hstrt', 'hend')}
    tpfd_values = [c.tpfd for c in combos if c.tpfd is not None]
    tpfd = Range(min(tpfd_values), max(tpfd_values)) if tpfd_values else None

    registers = manifest.get('baseline_registers') or manifest.get('registers') \
        or {'tbl': 2, 'toff': 3, 'hstrt': 5, 'hend': 0}
    start = tmc.Chopper(registers['tbl'], registers['toff'], registers['hstrt'],
                        registers['hend'], registers.get('tpfd'))

    lookups = []
    missing = set()

    def evaluate(combo: tmc.Chopper) -> float:
        if combo not in lookup:
            # a dataset collected under narrower ranges (or the old raw<=16 rule)
            # cannot answer for this combo — steer the replay away from it
            missing.add(combo)
            return float('inf')
        lookups.append(combo)
        return lookup[combo]

    best = multi_start_descent(driver, ranges['tbl'], ranges['toff'], ranges['hstrt'],
                               ranges['hend'], tpfd, start, evaluate)
    if best not in lookup:
        raise SystemExit('replay could not reach any measured combo from start %s' % start.label())
    global_best = min(lookup, key=lookup.get)
    gap = (lookup[best] / lookup[global_best] - 1) * 100

    print('Dataset: %d combos; descent evaluated %d unique (%d lookups), %.1f%% of the grid'
          % (len(lookup), len(set(lookups)), len(lookups), 100 * len(set(lookups)) / len(lookup)))
    if missing:
        print('Warning: %d candidates were outside the dataset and treated as worst-case'
              % len(missing))
    print('Descent best: %s -> %.1f' % (best.label(), lookup[best]))
    print('Global best:  %s -> %.1f' % (global_best.label(), lookup[global_best]))
    print('Gap to global optimum: %.1f%%' % gap)
    return 0
