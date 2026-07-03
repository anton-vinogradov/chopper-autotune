# chopper-autotune

**Closed-loop, measurement-driven auto-tuning of TMC stepper driver chopper registers for Klipper.**

[Русская версия](README_RU.md)

> **Status: design stage.** This document is a statement of intent. No working code yet — the measurement methodology is proven by the predecessor projects listed below; this project automates the decision-making around it.

## The problem

Chopper register values (`TBL`, `TOFF`, `HSTRT`, `HEND`, `TPFD`) dramatically affect stepper motor behavior: up to ~30% torque difference, up to 10x vibration difference, plus audible noise. The optimal values depend on the specific motor, driver, supply voltage and mechanics — datasheet defaults are a compromise.

Existing tools leave a gap:

- [chopper-resonance-tuner](https://github.com/MRX8024/chopper-resonance-tuner) and [tmc-chopper-tune](https://github.com/anton-vinogradov/tmc-chopper-tune) — brute-force sweep over the full register grid (~7000 combinations, ~2 hours, ~700 MB of CSV), after which a **human** reads an interactive plot and picks the best point. Semi-automatic at best.
- [klipper_tmc_autotune](https://github.com/andrewmcgr/klipper_tmc_autotune) — computes registers analytically from a motor database, **without any feedback from the actual hardware**.

## The intent

A tool that closes the loop on real hardware: *apply registers → move the axis → measure vibrations with the toolhead accelerometer → score → pick the next candidate*. Fully automatic, from "run one command" to "paste this block into `printer.cfg`".

### Planned workflow

1. **Baseline** — measure accelerometer noise floor at stand-still.
2. **Resonance speed detection** — sweep speeds with current config; find resonance peaks automatically (`scipy.signal.find_peaks`) instead of eyeballing an HTML plot.
3. **Register search** — optimize over the discrete register space at the resonant speed(s). Coordinate descent in the order Trinamic recommends in [AN-001](https://www.analog.com/en/app-notes/AN-001.html) (`TBL`+`TOFF` → `HSTRT`/`HEND` → `TPFD`) and/or Bayesian optimization (Optuna/TPE). Noisy measurements are handled by successive halving: cheap single runs to explore, repeated runs to confirm the leaders. CSV files are processed and deleted incrementally — no 700 MB disk requirement.
4. **Validation & output** — re-measure the top candidates at 2–3 speeds in both directions, print a ready-to-paste `printer.cfg` snippet, optionally apply it live via `SET_TMC_FIELD`.

Expected runtime: tens of minutes instead of hours, or the same time budget spent on a multi-speed objective.

### Datasheet-driven scoring, not just measurement

The accelerometer cannot hear the chopper (ADXL345 samples at 3.2 kHz), but the chopper frequency is *computable* from the registers and the driver clock. That makes the classic "low vibration but nasty audible whine" trade-off automatic: candidates whose chopper frequency falls into the audible range get penalized analytically.

Also datasheet-driven:

- search space constraints (`HSTRT`+`HEND` ≤ 16, `TOFF` = 0 forbidden, `TOFF` = 1 blank-time restrictions) — pruned before any motion;
- per-driver capability matrix (`TPFD` only on TMC2240/5160; forcing spreadCycle on TMC2208/2209 for the duration of the test);
- future: StallGuard readout as a torque-margin proxy to auto-tune motor current (quieter and cooler motors at a known safety margin).

## Planned stack

Python 3 on the printer host. Moonraker API for orchestration (no Jinja macro loops), `numpy`/`scipy` for PSD and peak detection, Klipper's own `shaper_calibrate` helpers where applicable, Optuna for the search, plotly for the final report.

## Requirements

- Klipper + Moonraker
- An accelerometer mounted on the toolhead ([Measuring Resonances](https://www.klipper3d.org/Measuring_Resonances.html))
- A supported TMC driver (see below)

## Roadmap

- [ ] Moonraker client + measurement primitive (apply registers, `FORCE_MOVE`, fetch & process CSV incrementally)
- [ ] Automatic resonance speed detection
- [ ] Coordinate-descent optimizer with datasheet constraints
- [ ] Chopper-frequency model and audible-range penalty
- [ ] Optuna-based search as an alternative strategy
- [ ] Validation phase and `printer.cfg` snippet output
- [ ] TPFD support (TMC2240/5160)
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
