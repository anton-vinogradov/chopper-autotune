# chopper-autotune

**Closed-loop, measurement-driven auto-tuning of TMC stepper driver chopper registers for Klipper.**

[Русская версия](README_RU.md)

[![tests](https://github.com/anton-vinogradov/chopper-autotune/actions/workflows/ci.yml/badge.svg)](https://github.com/anton-vinogradov/chopper-autotune/actions/workflows/ci.yml)

> **Status: working skeleton.** `collect` and `analyze` are implemented (full-grid sweep first, smart search next), not yet validated on real hardware.

## The problem

Chopper register values (`TBL`, `TOFF`, `HSTRT`, `HEND`, `TPFD`) dramatically affect stepper motor behavior: up to ~30% torque difference, up to 10x vibration difference, plus audible noise. The optimal values depend on the specific motor, driver, supply voltage and mechanics — datasheet defaults are a compromise.

Existing tools leave a gap:

- [chopper-resonance-tuner](https://github.com/MRX8024/chopper-resonance-tuner) and [tmc-chopper-tune](https://github.com/anton-vinogradov/tmc-chopper-tune) — brute-force sweep over the full register grid (~7000 combinations, ~2 hours, ~700 MB of CSV), after which a **human** reads an interactive plot and picks the best point. Semi-automatic at best.
- [klipper_tmc_autotune](https://github.com/andrewmcgr/klipper_tmc_autotune) — computes registers analytically from a motor database, **without any feedback from the actual hardware**.

## The approach

Close the loop on real hardware: *apply registers → move the axis → measure vibrations with the toolhead accelerometer → score → pick the next candidate*. Fully automatic, from "run one command" to "paste this block into `printer.cfg`".

### How it works today

`tune` chains everything below into a single command; each piece is also available separately:

1. **`find-speed`** sweeps the speed range with the current registers, builds the magnitude(speed) curve, finds resonance peaks (prominence-based) and recommends the speed for the main run.
2. **`collect`** reads everything it needs from the printer config over the klippy API socket (driver type, current registers, accelerometer, kinematics, axis limits), builds a register/speed plan pruned by datasheet constraints, checks the travel length against the axis span, prints an ETA and asks for confirmation.
3. The printer homes XY, parks at the bed center and disables motors. For every combination the tool applies registers via `SET_TMC_FIELD`, runs `FORCE_MOVE` back and forth, and streams accelerometer samples straight from the klippy socket. The end of each move is taken from `toolhead.print_time`, so the metric sees exactly the cruise phase — acceleration and deceleration transients are cut analytically, not by guesswork.
4. Every measurement is appended to an on-disk dataset immediately; an interrupted run resumes from where it stopped.
5. **`analyze`** aggregates the dataset (median across directions/iterations/speeds), penalizes configurations whose chopper frequency falls into the audible range, prints a ranking table, writes an interactive plotly report and a ready-to-paste `printer.cfg` snippet; `--apply` sets the winner live without restarting Klipper.

Besides the default full-grid sweep, `--search descent` (`SEARCH=descent`) runs a coordinate descent in the AN-001 tuning order — `TBL`+`TOFF` jointly, then `HSTRT`, `HEND`, then `TPFD` — evaluating a few percent of the grid (minutes instead of hours), re-measuring the top candidates before recommending. The objective includes the audible-chopper penalty, so the descent does not trade a barely lower vibration for a 15 kHz whine. For the second axis, `SEED_FROM=<dataset>` starts the descent from the winner of the first one — the seed only positions the search, every candidate is still measured on the target axis, so belt tension and mechanics differences are accounted for; a good seed converges in a couple of minutes, a bad one just costs the usual descent time. Any recorded grid dataset doubles as an offline benchmark: `simulate <dataset>` replays the descent against it and reports the gap to the true optimum.

### Datasheet-driven scoring, not just measurement

The accelerometer cannot hear the chopper (ADXL345 samples at 3.2 kHz), but the chopper frequency is *computable* from the registers and the driver clock. That makes the classic "low vibration but nasty audible whine" trade-off automatic: candidates whose chopper frequency falls into the audible range get penalized analytically (`--audible-weight`).

Also datasheet-driven:

- search space constraints (effective `HSTRT`+`HEND` ≤ 16 per datasheet, `TOFF` = 0 forbidden, `TOFF` = 1 blank-time restrictions) — pruned before any motion;
- per-driver capability matrix: `TPFD` enters the grid only on TMC2240/5160, clock frequencies match the Klipper driver code;
- when `stealthchop_threshold` is configured, spreadCycle is forced for the duration of the test and restored afterwards — chopper registers only act in spreadCycle, stealthChop would measure noise;
- planned: StallGuard readout as a torque-margin proxy to auto-tune motor current.

## Two runs by design

The tool is deliberately split into two commands sharing one on-disk dataset (`manifest.json` + `measurements.jsonl` + gzipped raw accelerometer CSVs):

1. **`collect`** — the slow hardware part. Streams samples from the klippy API socket (no CSV churn in `/tmp`, no SD-card wear; `--csv` falls back to the classic `ACCELEROMETER_MEASURE` path). Interrupted or extended runs resume from the same dataset directory: finished measurements are skipped.
2. **`analyze`** — offline and instant. Raw data is kept in the dataset, so scoring can be reworked and replayed (`--recompute`) without touching the printer.

Smarter search strategies will live inside `collect` and pick the next point online, but the dataset stays append-only and complete — analysis remains replayable offline.

## Usage

Install on the printer host (Klipper restarts at the end):

```
cd ~ && git clone https://github.com/anton-vinogradov/chopper-autotune && bash ./chopper-autotune/install.sh
```

### The simple way — one command

```
CHOPPER_TUNE            ; both axes: resonance speed + register descent, ~20 min
CHOPPER_TUNE SAVE=1     ; ...and write the winners into the config (with a backup)
```

That is the whole workflow: the tool finds the resonance speed of each axis, runs the register descent at it, seeds the second axis with the first one's winner, prints both `printer.cfg` blocks and — with `SAVE=1` — persists them and restarts Klipper. Progress shows on the printer display; `CHOPPER_STATUS` prints it in the console.

### The manual way — step by step

```
CHOPPER_FIND_SPEED                   ; 1. locate the resonance speeds of the axis
CHOPPER_COLLECT SPEED=55 DRY_RUN=1   ; check the plan and ETA without moving anything
CHOPPER_COLLECT SPEED=55             ; 2. sweep the full grid at the resonance speed (hours)
CHOPPER_COLLECT SPEED=55 SEARCH=descent  ; ...or coordinate descent (minutes)
CHOPPER_COLLECT AXIS=Y SPEED=52 SEARCH=descent SEED_FROM=<X dataset>  ; fast second axis
CHOPPER_STATUS                       ; progress and ETA of the running collection
CHOPPER_ANALYZE                      ; 3. rank the latest dataset, write the report
CHOPPER_ANALYZE APPLY=1              ; apply the winner live via SET_TMC_FIELD
CHOPPER_ANALYZE SAVE=1               ; persist it into the config and restart Klipper
```

The same over SSH: `chopper-autotune tune|collect|analyze|…`. Every macro parameter maps 1:1 to a CLI flag (`MEASURE_TIME=1.5` → `--measure-time 1.5`); boolean flags take `1`/`0`. Progress is mirrored to the printer display (KlipperScreen / LCD / web header) via `M117`, with the final recommendation left on screen.

![Tuning progress on the KlipperScreen display](docs/klipperscreen.svg) Datasets and HTML reports land in `~/printer_data/config/chopper-autotune/datasets/` — visible in the web file manager. `collect`/`tune` must run on the printer host (they talk to the klippy unix socket); `analyze` runs anywhere. `uninstall.sh` removes the integration and keeps the datasets.

### Command reference

**CHOPPER_TUNE** — the whole pipeline; no parameters needed.

| parameter | default | meaning |
|---|---|---|
| `AXIS` | `XY` | `X`, `Y`, or `XY` = both, the second seeded with the first one's winner |
| `SPEED` | auto | skip the resonance scan and tune at this speed (mm/s) |
| `SAVE` | `0` | write the winners into the Klipper config (backup first) and restart |
| `ITERATIONS` | `1` | repeats per candidate — raise on noisy mechanics |
| `AUDIBLE_WEIGHT` | `0.25` | penalty multiplier for audible chopper frequency |
| `DRY_RUN` | `0` | print the plan and ETA, do not move anything |

**CHOPPER_FIND_SPEED** — resonance speed scan at the current registers.

| parameter | default | meaning |
|---|---|---|
| `AXIS` | `X` | axis to scan |
| `MIN_SPEED` / `MAX_SPEED` | `20` / `120` | scan range, mm/s |
| `STEP` | `2` | speed increment, mm/s |
| `ITERATIONS` | `1` | repeats per speed |
| `MEASURE_TIME` | `1.0` | target cruise seconds per move (shrinks at high speeds to fit the axis) |
| `DATASET` | new | pass an existing directory to resume it |
| `DRY_RUN` | `0` | plan and ETA only |

**CHOPPER_COLLECT** — register search at a given speed.

| parameter | default | meaning |
|---|---|---|
| `SPEED` | required | resonance speed, mm/s (or a `lo:hi` range) |
| `AXIS` | `X` | axis to tune |
| `SEARCH` | `grid` | `grid` = full sweep (hours), `descent` = coordinate descent (minutes) |
| `TBL` / `TOFF` / `HSTRT` / `HEND` | `0:3` / `1:8` / `0:7` / `0:15` | register ranges (`lo:hi` or a single value) |
| `TPFD` | off | TPFD range, TMC2240/5160 only |
| `SEED_FROM` | — | start the descent from another dataset's winner (fast second axis) |
| `SKIP_AUDIBLE` | `0` | exclude audibly-whining combos instead of just penalizing them |
| `AUDIBLE_WEIGHT` | `0.25` | descent-objective penalty for audible chopper frequency |
| `ITERATIONS` | `1` | repeats per combination |
| `VALIDATE` | `3` | re-measure top N candidates with extra runs before recommending (`0` = off) |
| `MEASURE_TIME` | `1.25` | cruise seconds per move |
| `ACCEL` | `max_accel/10` | move acceleration |
| `TRIM` | `0.1` | guard fraction of the cruise window (with `CSV=1`: `0.25` of the whole capture) |
| `DATASET` | new | pass an existing directory to resume it |
| `NO_RAW` | `0` | do not keep raw samples (saves space, disables `RECOMPUTE`) |
| `CSV` | `0` | classic `ACCELEROMETER_MEASURE`+`/tmp` capture instead of streaming |
| `DRY_RUN` | `0` | plan and ETA only |

**CHOPPER_ANALYZE** — offline ranking of a dataset.

| parameter | default | meaning |
|---|---|---|
| `DATASET` | latest | dataset directory to analyze |
| `TOP` | `15` | rows in the console table |
| `AUDIBLE_WEIGHT` | `0.25` | ranking penalty for audible chopper frequency |
| `RECOMPUTE` | `0` | recompute metrics from raw samples instead of stored scores |
| `HTML` / `NO_HTML` | `<dataset>/report.html` | report path / skip the report |
| `APPLY` | `0` | apply the winner live via `SET_TMC_FIELD` (until reboot) |
| `SAVE` | `0` | rewrite the `driver_*` lines in the config (backup first) and restart |

**CHOPPER_STATUS** — progress of the most recent (or `DATASET=`) run; `TOTAL=` supplies the planned move count for old datasets.

CLI-only extras: `chopper-autotune simulate <grid-dataset>` (replay the descent offline, report the gap to the true optimum) and `chopper-autotune compare <A> <B>` (winners, rank correlation, top overlap). Expert flags `SOCKET=`/`URL=` override the klippy socket path and the Moonraker URL.

## Stack

Python 3.9+ on the printer host. The klippy API socket for orchestration and sample streaming (no Jinja macro loops; Moonraker HTTP only for `analyze --apply`), `numpy` for metrics, plotly for reports; `scipy` peak detection and Optuna search are planned.

## Prerequisites

- Klipper + Moonraker (Mainsail, Fluidd or any other frontend).
- A supported TMC driver on the axis being tuned (see the datasheet list below).
- **An accelerometer on the toolhead** — the measuring instrument of the whole tool:
  - any chip supported by Klipper's resonance stack works: ADXL345 (the classic), LIS2DW, the MPU-9250 family; USB sticks (KUSBA, FYSETC PIS) and CAN toolhead boards with an onboard chip (EBB36/42, SB2209, …) count too;
  - mount it **rigidly on the printhead** (screwed down, not taped) — exactly as for input-shaper calibration;
  - wiring and configuration (`[adxl345]` + `[resonance_tester]`) are covered by Klipper's [Measuring Resonances](https://www.klipper3d.org/Measuring_Resonances.html) guide; config reference: [adxl345](https://www.klipper3d.org/Config_Reference.html#adxl345), [resonance_tester](https://www.klipper3d.org/Config_Reference.html#resonance_tester). The tool picks the chip from `[resonance_tester] accel_chip` automatically (default `adxl345`);
  - sanity check before tuning: `ACCELEROMETER_QUERY` returns readings and `MEASURE_AXES_NOISE` stays around or below ~100;
  - unlike Klipper's own shaper tools, chopper-autotune does **not** need numpy inside klippy-env — samples are streamed out and processed in the tool's own venv.

## Roadmap

- [x] Two-run design: `collect` (hardware, resumable dataset) / `analyze` (offline, replayable)
- [x] Measurement primitive over the klippy API socket (registers → `FORCE_MOVE` → streamed samples)
- [x] Grid sweep with datasheet constraints, TPFD included on TMC2240/5160
- [x] Chopper-frequency model and audible-range penalty (first-order)
- [x] Web-console macros (`CHOPPER_COLLECT`/`CHOPPER_ANALYZE`), installer, Moonraker update_manager
- [x] Streaming capture with exact cruise-phase slicing (`--csv` fallback)
- [x] Hardware validation on a real printer (CoreXY, TMC2209, ADXL345: streaming and CSV paths agree)
- [x] Automatic resonance speed detection (`find-speed`, prominence-based peak picking)
- [x] Forcing spreadCycle during the test when `stealthchop_threshold` is configured; `CHOPPER_STATUS` progress/ETA
- [x] One-command `CHOPPER_TUNE` pipeline (speed scan → descent per axis → batched `SAVE=1`)
- [x] Coordinate-descent search (`--search descent`: AN-001 order, audible-penalty objective, top-3 re-measurement, offline `simulate` replay)
- [ ] Optuna/TPE strategy, early abort of bad candidates mid-move
- [x] Validation phase: top candidates re-measured with extra runs before recommending (grid and descent)
- [ ] StallGuard-based current tuning

## Prior art & credits

- [MRX8024/chopper-resonance-tuner](https://github.com/MRX8024/chopper-resonance-tuner) — the original measurement methodology
- [anton-vinogradov/tmc-chopper-tune](https://github.com/anton-vinogradov/tmc-chopper-tune) — simplified fork, direct predecessor
- [andrewmcgr/klipper_tmc_autotune](https://github.com/andrewmcgr/klipper_tmc_autotune) — the analytic (no-measurement) approach
- Trinamic [AN-001: Parameterization of spreadCycle](https://www.analog.com/en/app-notes/AN-001.html)

## Datasheets

- TMC2130 [datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/TMC2130_datasheet_rev1.15.pdf) · Klipper [config](https://www.klipper3d.org/Config_Reference.html#tmc2130)
- TMC2208 [datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/TMC2202_TMC2208_TMC2224_datasheet_rev1.14.pdf) · Klipper [config](https://www.klipper3d.org/Config_Reference.html#tmc2208)
- TMC2209 [datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/TMC2209_datasheet_rev1.09.pdf) · Klipper [config](https://www.klipper3d.org/Config_Reference.html#tmc2209)
- TMC2660 [datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/TMC2660C_Datasheet_Rev1.01.pdf) · Klipper [config](https://www.klipper3d.org/Config_Reference.html#tmc2660)
- TMC2240 [datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/tmc2240_datasheet.pdf) · Klipper [config](https://www.klipper3d.org/Config_Reference.html#tmc2240)
- TMC5160 [datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/TMC5160A_datasheet_rev1.17.pdf) · Klipper [config](https://www.klipper3d.org/Config_Reference.html#tmc5160)

## License

[MIT](LICENSE.TXT)
