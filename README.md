# chopper-autotune

**Closed-loop, measurement-driven auto-tuning of TMC stepper driver chopper registers for Klipper.**

[Русская версия](README_RU.md)

[![tests](https://github.com/anton-vinogradov/chopper-autotune/actions/workflows/ci.yml/badge.svg)](https://github.com/anton-vinogradov/chopper-autotune/actions/workflows/ci.yml)

> **Status: hardware-validated.** The full pipeline — chopper tuning with click-aware scoring plus measured run-current tuning — runs on a real printer (CoreXY, TMC2209, ADXL345); broader driver and printer coverage is still early.

## Contents

- [Why](#why)
- [The problem](#the-problem)
- [The approach](#the-approach) · [how it works](#how-it-works-today) · [datasheet-driven scoring](#datasheet-driven-scoring-not-just-measurement)
- [The science](#the-science)
- [Two runs by design](#two-runs-by-design)
- [Usage](#usage) · [one command](#the-simple-way--one-command) · [touchscreen](#from-the-touchscreen--klipperscreen) · [step by step](#the-manual-way--step-by-step) · [command reference](#command-reference)
- [Stack](#stack) · [Prerequisites](#prerequisites) · [Roadmap](#roadmap)
- [Prior art](#prior-art--credits) · [Datasheets](#datasheets) · [License](#license)

## Why

- **One command.** `CHOPPER_TUNE SAVE=1` finds each motor's resonance speed, searches the register space and writes the winner into `printer.cfg` in ~20 minutes — no graphs to read, no numbers to copy.
- **Measured on *your* hardware, not guessed.** Every candidate is scored from real toolhead-accelerometer data on your motors, belts and supply voltage — not computed from a database.
- **Real numbers.** On the reference printer (CoreXY, TMC2209): ~2× less measured vibration than Klipper defaults at the resonance speed, zero audible clicks — and the tuned chopper survives the worst-case torque test at **2.5× less current** than the default one. That margin let us drop `run_current` 1.8 → 1.0 A: motors 3.2× cooler, at the quietest state the rig has ever measured.
- **What tuning spreadCycle is for.** Lower measured vibration and — the part nobody advertises — **torque margin**: on the reference rig the tuned chopper holds the worst-case stress at 0.42 A where the default needs ~1.1 A. Margin you can spend on a lower, cooler run current; `CHOPPER_CURRENT` measures exactly how much. It targets *vibration*, not perceived loudness — see the [caveat](#datasheet-driven-scoring-not-just-measurement).
- **Won't trade silence for a whine — or for clicks.** The chopper frequency is derived from the registers, so configs that would slip into the audible band are penalised automatically; and every measurement counts transient clicks, so a "quiet on average, clicking in fact" config cannot win either.
- **Built for a real printer.** Resumable runs, live progress on the KlipperScreen, a config backup before anything is written, and a `--csv` fallback if streaming misbehaves.

## The problem

Chopper register values (`TBL`, `TOFF`, `HSTRT`, `HEND`, `TPFD`) dramatically affect stepper motor behavior: up to ~30% torque difference, up to 10x vibration difference, plus audible noise. The optimal values depend on the specific motor, driver, supply voltage and mechanics — datasheet defaults are a compromise.

Existing tools leave a gap:

- [chopper-resonance-tuner](https://github.com/MRX8024/chopper-resonance-tuner) and [tmc-chopper-tune](https://github.com/anton-vinogradov/tmc-chopper-tune) — brute-force sweep over the full register grid (~7000 combinations, ~2 hours, ~700 MB of CSV), after which a **human** reads an interactive plot and picks the best point. Semi-automatic at best.
- [klipper_tmc_autotune](https://github.com/andrewmcgr/klipper_tmc_autotune) — computes registers analytically from a motor database, **without any feedback from the actual hardware**.

## The approach

Close the loop on real hardware: *apply registers → move the motor → measure vibrations with the toolhead accelerometer → score → pick the next candidate*. Fully automatic, from "run one command" to "paste this block into `printer.cfg`".

### How it works today

`tune` chains everything below into a single command; each piece is also available separately:

1. **`find-speed`** sweeps the speed range on stock (Klipper default) registers — a well-tuned chopper suppresses the very resonance peak the scan is looking for, so the tuned registers are set aside and restored afterwards — builds the magnitude(speed) curve, finds resonance peaks (prominence-based) and recommends the speed for the main run.
2. **`collect`** reads everything it needs from the printer config over the klippy API socket (driver type, current registers, accelerometer, kinematics, axis limits), builds a register/speed plan pruned by datasheet constraints, checks the travel length against the axis span, prints an ETA and asks for confirmation.
3. The printer homes XY, parks at the bed center and disables motors. For every combination the tool applies registers via `SET_TMC_FIELD`, runs `FORCE_MOVE` back and forth, and streams accelerometer samples straight from the klippy socket. The end of each move is taken from `toolhead.print_time`, so the metric sees exactly the cruise phase — acceleration and deceleration transients are cut analytically, not by guesswork.
4. Every measurement is appended to an on-disk dataset immediately; an interrupted run resumes from where it stopped.
5. **`analyze`** aggregates the dataset (mean across directions/iterations/speeds — the fwd/rev difference is real, so a config must be quiet *both* ways to win), penalizes configurations whose chopper frequency falls into the audible range and configurations that produced transient clicks, prints a ranking table (with a clicks column), writes an interactive plotly report and a ready-to-paste `printer.cfg` snippet; `--apply` sets the winner live without restarting Klipper.

Besides the default full-grid sweep, `--search descent` (`SEARCH=descent`) runs a **multi-start** coordinate descent in the AN-001 tuning order — `TBL`+`TOFF` jointly, then `HSTRT`, `HEND`, then `TPFD` — evaluating a few percent of the grid (minutes instead of hours), re-measuring the top candidates before recommending. Several seeds spread across the `TOFF`×`HEND` plane keep the greedy search from getting trapped: phase A sweeps `TOFF` at a fixed `HEND`, so a single low-`HEND` start hides the low-`TOFF`/high-`HEND` valley — starting from a few `HEND` levels lets some run find it. The objective includes the audible-chopper penalty, so the descent does not trade a barely lower vibration for a 15 kHz whine. For the second motor, `SEED_FROM=<dataset>` starts the descent from the winner of the first one — the seed only positions the search, every candidate is still measured on the target motor, so belt tension and mechanics differences are accounted for; a good seed converges in a couple of minutes, a bad one just costs the usual descent time. Any recorded grid dataset doubles as an offline benchmark: `simulate <dataset>` replays the descent against it and reports the gap to the true optimum.

### Datasheet-driven scoring, not just measurement

The accelerometer cannot hear the chopper (ADXL345 samples at 3.2 kHz), but the chopper frequency is *computable* from the registers and the driver clock. That makes the classic "low vibration but nasty audible whine" trade-off automatic: candidates whose chopper frequency falls into the audible range get penalized analytically (`--audible-weight`).

**It optimises vibration, not perceived loudness.** Sampling at 3.2 kHz, the accelerometer only sees vibration up to ~1.6 kHz — the low-frequency growl/resonance that causes ringing in prints, shakes the frame and tracks motor efficiency and heat. Your ear hears much higher (peak sensitivity ~2–5 kHz), a band the sensor is blind to. So "−N% vibration" means less *measured* low-frequency vibration; it usually but not always sounds quieter — a config can shake the toolhead less yet emit a higher-pitched hiss the ear reads as louder (no whine, since the chopper itself is ultrasonic). Optimising true acoustic loudness would need a microphone.

**It refuses clicky winners.** The per-move median is blind to rare transients: a config can win the median while audibly clicking (measured — the datasheet-edge winner clicked ~5× per move at 65× the median). Every capture therefore also counts clicks over the whole move, with a hardware-calibrated threshold (15× the move median: real clicks measure 22–69×, threshold noise stays under ~13×), and one click per move costs as much as doubling the vibration.

Also datasheet-driven:

- search space constraints (effective `HSTRT`+`HEND` ≤ 16 per datasheet, `TOFF` = 0 forbidden, `TOFF` = 1 blank-time restrictions) — pruned before any motion;
- per-driver capability matrix: `TPFD` enters the grid only on TMC2240/5160, clock frequencies and blank-time tables match the driver datasheets;
- when `stealthchop_threshold` is configured, spreadCycle is forced for the duration of the test and restored afterwards — chopper registers only act in spreadCycle, stealthChop would measure noise;
- run current is measured too: `CHOPPER_CURRENT` stresses one motor at a time and bisects to the skip threshold with an endstop referee — see the [command reference](#command-reference).

## The science

Tuning is an experiment, and we keep the lab notebook public:
[docs/SCIENCE.md](docs/SCIENCE.md) explains what each register physically does
(the spreadCycle cycle, coil RL physics, why the optimum is per-motor), what the
accelerometer can and cannot see, and hosts the investigations *with their
outcomes*: the clicks case (resolved — the click penalty came out of it, and the
hysteresis **split** turned out to govern clicking), the cross-check against
klipper_tmc_autotune's analytic model (bugs found on both sides, coil saturation
≈2× measured in-situ, ongoing discussion in
[their issue #339](https://github.com/andrewmcgr/klipper_tmc_autotune/issues/339)),
and the run-current campaign (chopper tuning buys ~2.5× of torque margin —
spend it on a lower current). Negative results included; updated as the
investigation moves.

For the story rather than the physics, [docs/CHRONICLE.md](docs/CHRONICLE.md) is
the same research as a dated log — three directions, each with a spoiler that
walks from the first symptom to the shipped result.

## Two runs by design

The tool is deliberately split into two commands sharing one on-disk dataset (`manifest.json` + `measurements.jsonl` + gzipped raw accelerometer CSVs):

1. **`collect`** — the slow hardware part. Streams samples from the klippy API socket (no CSV churn in `/tmp`, no SD-card wear; `--csv` falls back to the classic `ACCELEROMETER_MEASURE` path). Interrupted or extended runs resume from the same dataset directory: finished measurements are skipped.
2. **`analyze`** — offline and instant. Raw data is kept in the dataset, so scoring can be reworked and replayed (`--recompute`) without touching the printer.

The multi-start descent already picks its next point online, and the dataset stays append-only and complete — analysis remains replayable offline.

## Usage

Install on the printer host (Klipper restarts at the end):

```
cd ~ && git clone https://github.com/anton-vinogradov/chopper-autotune && bash ./chopper-autotune/install.sh
```

### The simple way — one command

```
CHOPPER_TUNE SAVE=1     ; both motors: resonance speed + register descent, ~20 min
CHOPPER_CURRENT SAVE=1  ; measure the skip threshold, drop run_current with a 2x margin
CHOPPER_TUNE SAVE=1     ; re-tune the chopper at the new current (its optimum depends on it)
```

The first tune finds the resonance speed of each motor, runs the register descent at it, seeds the second motor with the first one's winner and persists the winners (with a backup). It also buys **torque margin** — which `CHOPPER_CURRENT` then converts into a lower, cooler `run_current` (measured skip threshold × safety margin). The final tune adapts the registers to the new current. Progress shows on the printer display; `CHOPPER_STATUS` prints it in the console. Just `CHOPPER_TUNE SAVE=1` alone is still a fine day one.

### From the touchscreen — KlipperScreen

If you run [KlipperScreen](https://github.com/KlipperScreen/KlipperScreen), `install.sh` adds a **Chopper** button to its **More** menu (it merges with your existing menu, nothing is rewritten). One tap opens a panel with:

- **Tune A** / **Tune B** — tune one motor (A = `stepper_x`, B = `stepper_y`; the chopper is a motor property, so it's the same on any kinematics — and on CoreXY those two steppers literally are motors A and B);
- **Tune both** — tune both motors in one run, seeding the second from the first winner;
- **Belts** — measure belt tension by plucking: follow the display, pluck each belt's long front span hard, twice; the accelerometer hears the tension;
- **Motor A** / **Motor B** — jog just that motor for a moment so you can see which physical motor and belt it is, then release the motors so you can reach in;
- **Save** — write the latest tuning result for each motor into the config (backup first, one restart);
- **Show** — set the defaults, then the tuned registers, on **both** motors and do coordinated moves (both run together, like printing) so you can *hear* the whole printer change; it reports the combined drop in vibration;
- **Stop** — abort a running job; the tool restores the registers and re-homes before it exits.

Every action confirms before it moves the printer. While a job runs the panel shows live progress; when idle it shows, per motor, the **default → tuned** registers and how much **less it vibrates** (measured by the last Show). The buttons drive the same `CHOPPER_*` macros, so anything you can do from the console you can do from the screen. (`CHOPPER_CURRENT` is console-only for now.)

![The Chopper panel on KlipperScreen](docs/klipperscreen-panel.png)

### The manual way — step by step

```
CHOPPER_FIND_SPEED                   ; 1. locate the resonance speeds of the motor
CHOPPER_COLLECT SPEED=55 DRY_RUN=1   ; check the plan and ETA without moving anything
CHOPPER_COLLECT SPEED=55             ; 2. sweep the full grid at the resonance speed (hours)
CHOPPER_COLLECT SPEED=55 SEARCH=descent  ; ...or multi-start descent (minutes)
CHOPPER_COLLECT MOTOR=B SPEED=52 SEARCH=descent SEED_FROM=<A dataset>  ; fast second motor
CHOPPER_STATUS                       ; progress and ETA of the running collection
CHOPPER_ANALYZE                      ; 3. rank the latest dataset, write the report
CHOPPER_ANALYZE APPLY=1              ; apply the winner live via SET_TMC_FIELD
CHOPPER_ANALYZE SAVE=1               ; persist it into the config and restart Klipper
CHOPPER_DEMO                         ; play defaults vs the tuned registers so you can hear it
CHOPPER_CURRENT SAVE=1               ; 4. measured run-current: skip threshold x 2.0 margin
```

The same over SSH: `chopper-autotune tune|collect|analyze|…`. Every macro parameter maps 1:1 to a CLI flag (`MEASURE_TIME=1.5` → `--measure-time 1.5`); boolean flags take `1`/`0`. Progress is reported two ways: `M117` sets `display_status.message` (the Mainsail/Fluidd header, LCDs, and the KlipperScreen status line), and a prefixed `RESPOND` echoes each update to the console (Mainsail/Fluidd/KlipperScreen console) — with a `Chopper:` prefix rather than `echo:`, so KlipperScreen does not raise a dismissable notification for every line and swallow taps on the panel. Each channel self-disables if the printer lacks it. The final recommendation stays in the display message.

Datasets and HTML reports land in `~/printer_data/config/chopper-autotune/datasets/` — visible in the web file manager. `collect`/`tune` must run on the printer host (they talk to the klippy unix socket); `analyze` runs anywhere. `uninstall.sh` removes the integration and keeps the datasets.

### Command reference

**CHOPPER_TUNE** — the whole pipeline; no parameters needed.

| parameter | default | meaning |
|---|---|---|
| `MOTOR` | `AB` | `A`, `B`, or `AB` = both (A = `stepper_x`, B = `stepper_y`), the second seeded with the first one's winner; `x`/`y`/`xy` also accepted |
| `SPEED` | auto | skip the resonance scan and tune at this speed (mm/s) |
| `SAVE` | `0` | write the winners into the Klipper config (backup first) and restart |
| `ITERATIONS` | `1` | repeats per candidate — raise on noisy mechanics |
| `AUDIBLE_WEIGHT` | `0.25` | penalty multiplier for audible chopper frequency |
| `DRY_RUN` | `0` | print the plan and ETA, do not move anything |

**CHOPPER_FIND_SPEED** — resonance speed scan on stock registers (the tuned ones are set aside for the sweep and restored afterwards).

| parameter | default | meaning |
|---|---|---|
| `MOTOR` | `A` | motor to scan: `a`/`b` (a = `stepper_x`, b = `stepper_y`); `x`/`y` also accepted |
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
| `MOTOR` | `A` | motor to tune: `a`/`b` (a = `stepper_x`, b = `stepper_y`); `x`/`y` also accepted |
| `SEARCH` | `grid` | `grid` = full sweep (hours), `descent` = multi-start coordinate descent (minutes) |
| `TBL` / `TOFF` / `HSTRT` / `HEND` | `0:3` / `1:8` / `0:7` / `0:15` | register ranges (`lo:hi` or a single value) |
| `TPFD` | off | TPFD range, TMC2240/5160 only |
| `SEED_FROM` | — | start the descent from another dataset's winner (fast second motor) |
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

**CHOPPER_DEMO** — plays the driver defaults against the saved/tuned registers, alternating so you can *hear* the difference and announcing each on the display and console. `MOTOR` (a/b, or **ab** = the default: **both motors together** in coordinated moves, like printing — a whole-printer before/after), `SPEED`, `ROUNDS`, `REPEATS`. `REPORT=1` prints the measured numbers (how much less vibration, per motor, with bars) one motor at a time instead of the audible show; `DEFAULT=tbl,toff,hstrt,hend` (default `2,3,5,0`) and `ITERATIONS` apply to the report.

**CHOPPER_SAVE** — write the latest tuning result for each motor into the config in one batched restart (with a backup); logs which winner it saves per motor and from which dataset. Save what the last tuning achieved, whether the motors were tuned separately or together.

**CHOPPER_CURRENT** — find the minimal safe `run_current` per motor: a worst-case single-motor stress pattern (full machine accel, belt speeds through 200 mm/s) with an **endstop referee** — skipped steps land as a position offset that an endstop creep measures deterministically, because a stall can be nearly silent (measured; see [docs/SCIENCE.md](docs/SCIENCE.md)). Bisects to the skip threshold and recommends `threshold × MARGIN` (default `2.0`); `SAVE=1` writes `run_current` (backup first) and restarts. Re-run `CHOPPER_TUNE` afterwards — the chopper optimum depends on the current. Parameters: `MOTOR`, `MARGIN`, `MIN_CURRENT`, `RESOLUTION`, `ACCEL`, `SAVE`, `DRY_RUN`.

**CHOPPER_ENVELOPE** — the motor's **torque ceiling** at the configured current: how fast and how hard each motor can be pushed before it skips a step. Climbs a speed ladder (default `150→350` mm/s at full accel) and an acceleration ladder (`1–4×` `max_accel` at a moderate speed), same single-motor stress and **endstop referee** as `CHOPPER_CURRENT`; the safe ceiling is the last rung before the first skip, reported with a `1.3×` margin. This is the *motor* limit only — which speeds are quiet vs ringy is `CHOPPER_MAP`, and the real top-speed limit is usually hotend flow, not the motor. Read-only (nothing to save). Parameters: `MOTOR`, `MIN_SPEED`, `MAX_SPEED`, `STEP`, `ACCEL_PROBE_SPEED`, `ACCEL`, `DRY_RUN`.

**CHOPPER_MAP** — the **resonance map**: vibration vs speed on the registers you actually print with, over a wide range (default `20→250` mm/s). Where `CHOPPER_FIND_SPEED` scans on *stock* registers to expose the peak it needs for tuning, this scans on the *current* (tuned) ones — the vibration you actually feel — and reports the **resonance peaks** (cruising there causes **VFAs**, the fine vertical banding) and the **quiet dips** between them, as a bar table plus an HTML plot. Pass `PRINT_SPEED=` and it tells you whether your speed sits on a resonance and names the quieter speeds nearby. Honest scope: a constant-velocity sweep measures the motor/chopper vibration signature, **not** your top print speed — the motor holds torque far past any commanded speed (`CHOPPER_ENVELOPE`), the real ceiling is hotend flow, and corner ringing is the input shaper's job (`SHAPER_CALIBRATE`). Read-only. Parameters: `MOTOR`, `MIN_SPEED`, `MAX_SPEED`, `STEP`, `PRINT_SPEED`, `ITERATIONS`, `ACCEL`, `DRY_RUN`.

**CHOPPER_BELTS** — **belt tension by guided pluck**: you pluck belt spans like guitar strings on the display's cue, and the toolhead accelerometer listens. The pluck excites the **transverse string mode** — the one that *is* tension (f ∝ √T) — and the tool identifies the fundamental by its **(f, 2f) pair**: the ringing span shakes its anchor laterally at f and axially at 2f (the string's tension pulses twice per cycle), so a lone unpaired line is flagged as a possible harmonic of a weak pluck (measured: a 4f line masquerading at 400 Hz). Pluck the **longest free span** of each belt (across the front on most CoreXY), mid-span, hard — field-tested: near-head spans are too short and stiff to ring usefully. A belt is accepted once **two plucks agree** on the paired fundamental (within 2 %) — repeatability is the control. Verdict: tension ratio (f²) between belts; with `SPAN=` (plucked span length, cm) also **absolute newtons**, T = μ·(2·L·f)²; `MU=` overrides the belt's linear density (default 7.7 g/m, GT2 6 mm). `SHOW=A`/`SHOW=B` just jogs that belt — on CoreXY that diagonal moves only that belt's loop — so you can see which one is which (also the **Motor A**/**Motor B** touchscreen buttons). Parameters: `SPAN`, `MU`, `PLUCKS`, `TOLERANCE`, `SHOW`, `SWEEP`, `DRY_RUN`.

**CHOPPER_BELTS SWEEP=1** — the **diagonal response comparison**, kept as a *structural-change diagnostic*, not a tension gauge: it excites each belt's diagonal with Klipper's swept-sine `TEST_RESONANCES` and reports the response frequencies, the gap, and the per-belt change since the previous run. **Measured caveat:** a persistent gap can be structural asymmetry — on the reference rig a heavy overtension moved the response by 0 Hz — so it never orders "tighten belt X", and if nothing moved since the last run it says so. Use it to notice mechanics drifting over time; use the pluck (default) for tension. Needs `[resonance_tester]`; CoreXY/H-bot only. Parameters: `MIN_FREQ`, `MAX_FREQ`, `HZ_PER_SEC`, `TOLERANCE`.

**CHOPPER_EXTRUDER** — tune the **extruder's** chopper. On a direct-drive head the E motor sits right next to the accelerometer, and its chopper matters exactly where the A/B motors' does: at the mid-band resonance (measured: filament 5 mm/s rang 3× above the neighbours and separated register configs by 27 %; off-resonance the field is flat). **Heats the hotend first** (default `TEMP=200` — right for PLA and its composites, workable for PETG; ABS owners pass `TEMP=240`) so the loaded filament can move — no unloading. The motion is a net-zero oscillation of a few mm of filament (never chews one spot, never drags melt into the cold zone; `FORCE_MOVE` bypasses Klipper's cold-extrusion guard, so the tool enforces its own). Flow: resonance scan over filament speeds on stock registers → register descent at the peak → top-3 validation → `SAVE=1` writes `[tmcXXXX extruder]` (backup first). The heater is switched off on every exit path. Parameters: `TEMP`, `SPEED`, `MIN_SPEED`, `MAX_SPEED`, `AUDIBLE_WEIGHT`, `SAVE`, `DRY_RUN`.

**CHOPPER_STOP** — abort a running tuning/show job; the tool restores the registers, leaves spreadCycle and re-homes before it exits.

**CHOPPER_STATUS** — progress of the most recent (or `DATASET=`) run; `TOTAL=` supplies the planned move count for old datasets.

CLI-only extras: `chopper-autotune simulate <grid-dataset>` (replay the descent offline, report the gap to the true optimum) and `chopper-autotune compare <A> <B>` (winners, rank correlation, top overlap). Expert flags `SOCKET=`/`URL=` override the klippy socket path and the Moonraker URL.

## Stack

Python 3.9+ on the printer host. The klippy API socket for orchestration and sample streaming (no Jinja macro loops; Moonraker HTTP only for applying/saving configs), `numpy` for metrics, plotly for reports; peak picking and the search are self-contained — no scipy.

## Prerequisites

- Klipper + Moonraker (Mainsail, Fluidd or any other frontend).
- A supported TMC driver on the motor being tuned (see the datasheet list below).
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
- [x] One-command `CHOPPER_TUNE` pipeline (speed scan → descent per motor → batched `SAVE=1`)
- [x] Multi-start coordinate-descent search (`--search descent`: AN-001 order, TOFF×HEND-spread seeds to escape the non-separable blind spot, audible-penalty objective, offline `simulate` replay; measured within ~1% of the full-grid optimum, which retired the Optuna idea)
- [x] Validation phase: top candidates re-measured with extra runs before recommending (grid and descent)
- [x] Click-aware scoring: transient clicks counted per move and penalized — the median alone is blind to them (measured)
- [x] Resonance scan on stock registers — a well-tuned chopper masks the very peak the scan needs
- [x] Measured run-current tuning (`CHOPPER_CURRENT`): worst-case single-motor stress + endstop referee, bisection to the skip threshold — chosen over StallGuard, which on TMC2209 only works in stealthChop and would miss the (measured) silent slips
- [x] Motor torque envelope (`CHOPPER_ENVELOPE`): speed and acceleration ceilings before skipped steps, same endstop referee — caps the top of the resonance map and separates the motor limit from the (usually binding) hotend-flow limit
- [x] Resonance map (`CHOPPER_MAP`): wide vibration-vs-speed sweep on the current registers — which speeds ring vs stay quiet, framed honestly as the motor's vibration signature rather than a print-speed limit (flow and the shaper set that)
- [x] Belt-diagonal response comparison (`CHOPPER_BELTS`): swept-sine per diagonal, PSD in the tool's own venv, per-belt deltas between runs — reframed after a measured falsification: on the reference rig the response does not track tension (a heavy overtension moved it 0 Hz), so it diagnoses response asymmetry and never orders a tighten (see docs/SCIENCE.md)
- [x] Belt tension proper (`CHOPPER_BELTS PLUCK=1`): display-cued finger plucks heard by the toolhead accelerometer — the transverse string mode that *is* tension, identified by its (f, 2f) pair; tension ratio from f², absolute newtons with a span length
- [ ] Ringing-vs-acceleration ceiling: the surface-quality accel limit the envelope defers to the shaper, measured as residual vibration vs acceleration — pairs with the torque ceiling
- [x] Extruder chopper tuning (`CHOPPER_EXTRUDER`): heated, filament-in, net-zero oscillation — the E motor resonates in the same mid-band as A/B (measured at 5 mm/s filament) and its registers separate there
- [ ] The split question: why hend-heavy hysteresis splits stay click-free where hstrt-first splits click (open science, see docs/SCIENCE.md)
- [ ] Motors beyond `stepper_x`/`stepper_y` (dual Y, IDEX)

## Prior art & credits

- [MRX8024/chopper-resonance-tuner](https://github.com/MRX8024/chopper-resonance-tuner) — the original measurement methodology
- [anton-vinogradov/tmc-chopper-tune](https://github.com/anton-vinogradov/tmc-chopper-tune) — simplified fork, direct predecessor
- [andrewmcgr/klipper_tmc_autotune](https://github.com/andrewmcgr/klipper_tmc_autotune) — the analytic (no-measurement) approach; our measured cross-check of its model (and the resulting exchange of bug reports and data) lives in [their issue #339](https://github.com/andrewmcgr/klipper_tmc_autotune/issues/339) and [docs/SCIENCE.md](docs/SCIENCE.md)
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
