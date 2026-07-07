# The science

The physics behind chopper tuning and the conclusions we've measured — the
topical reference. For *how and when* each result was found (the models tried,
the dead ends, the dates, the upstream exchange), see the companion dated
[CHRONICLE.md](CHRONICLE.md).

Each claim is tagged: **[datasheet]** comes from Trinamic documentation,
**[measured]** was observed on the reference printer (Ender-6 CoreXY, TMC2209,
24 V; run current 1.8 A through the early campaigns, 1.0 A since the current
tuning), **[model]** comes from our simulations, **[hypothesis]** is the current
best guess.

## What the registers physically do

A stepper phase is an RL coil. The driver chops the supply voltage to hold a
sine-shaped target current; the current slope obeys

```
dI/dt = (V_drive − I·R − V_bemf) / L
```

so everything depends on the specific motor (L, R), the supply voltage and the
back-EMF (∝ speed) — this is why one-size-fits-all defaults leave margin on the
table, and why the optimum is per-motor.

One spreadCycle chopper cycle **[datasheet]**:

1. **on-phase** — coil across the supply, current ramps up until it exceeds the
   target by the hysteresis offset;
2. **fast decay** — reversed voltage, current drops quickly below the target by
   the hysteresis band;
3. **slow decay** — coil shorted for a *fixed* time set by `TOFF`; then the cycle
   restarts. After every switch the comparator is blanked for `TBL` clocks so it
   does not trigger on the switching spike.

| register | physical knob | effect |
| --- | --- | --- |
| `TBL` | comparator blank time after each switch | too short → false triggers on the spike; too long → blind window, coarse regulation |
| `TOFF` | slow-decay duration | sets the chopper frequency: `f ≈ fclk / (2·(blank + 12 + 32·TOFF))` (first order, as in `tmc.py`); small `TOFF` = fast inaudible chopper, big `TOFF` = slow chopper that can drop into the audible band |
| `HSTRT`, `HEND` | hysteresis around the target | the current ripple amplitude; effective hysteresis `(HSTRT+1) + (HEND−3)` must stay ≤ 16 **[datasheet]** |
| `TPFD` | passive fast decay (2240/5160 only) | damps the mid-band velocity resonance |

## What we measure — and what we cannot see

- The toolhead ADXL345 samples at 3.2 kHz → it sees **vibration up to ~1.6 kHz**:
  the low-frequency shake that causes ringing in prints and tracks motor losses.
  The ear peaks at 2–5 kHz, a band the sensor is blind to — hence
  “vibration ≠ perceived loudness” (see the scoring caveat in the README) **[measured]**.
- The chopper itself (20–80 kHz) is far beyond the sensor; its frequency is
  *computed* from the registers instead and penalised when audible.
- Per-move score = **median** magnitude: robust against sample noise, but
  **blind to rare transients** — a config can win the median while producing
  audible clicks (see below) **[measured]**.
- The forward/reverse difference is real signal, not noise: configs are ranked
  by the **mean across directions**, so a config must be quiet both ways **[measured]**.

## Model vs measurement

[klipper_tmc_autotune](https://github.com/andrewmcgr/klipper_tmc_autotune)
computes the registers analytically: the hysteresis is sized to the natural
current decay over one blank + slow-decay interval, from L, R, V and the run
current — no feedback from the hardware. We do the opposite: measure every
candidate on the real machine. The two are complementary:

- the analytic model bounds the *sane region* (in particular a hysteresis cap
  around effective 14);
- measurement finds the optimum *inside* it for the specific motor and mechanics.

A measured optimum that lands on the datasheet edge is a warning sign, not a
triumph — see below.

## The clicks: the split governs them, not the total

A config can win the median while producing sporadic **audible clicks** the
median never sees. On the reference rig the datasheet-edge winner (effective
hysteresis 16) peaked ~65× the median, ~2 clicks per one-second move, and
clicked even under a single-motor `FORCE_MOVE` — so it is the *config*, not the
showcase trajectory; Klipper defaults are clean under the identical move
**[measured]**.

It is **electromechanical, not electrical.** Neither electrical mechanism
reproduces it — both were modeled and ruled out (the modeling is in the
[chronicle](CHRONICLE.md)): the current overshoot at the sine zero crossing is
the same across configs, and the chopper loop is stable by construction (fixed
off-time; Floquet multipliers ≈ 1) **[model]**. On hardware the click has no
lock to the electrical phase (R ≈ 0.1–0.4) and rings as a heavily damped
broadband ~300 Hz thump (decay ~1 ms) — a mechanical kick **[measured]**.

**The split governs it, not the total [measured].** Sweeping effective
hysteresis with the hstrt-first split: clean at h_eff ≤ 6, sporadic clicks from
≈ 8, explosion at 16 (~5/move, peaks 65–69×); yet hend-heavy/balanced splits —
the retuned winners A `0/2/2/12`, B `0/2/6/10` — measure zero clicks at the same
h_eff 12–14. Consistent with the datasheet note that positive HEND improves the
sine zero crossings; *why* is an open sub-question **[hypothesis]**. (This
corrected an earlier guess that the datasheet cap of 14 was inherently clean — it
clicks too, with the wrong split.)

**The fix.** Every capture counts transients — rising crossings above 15× the
move median, over the whole capture, since reversal clicks live outside the
steady window — and one click per move is penalized like doubling the vibration.
The retune landed both motors click-free at **no vibration cost**: 1.86× less
than defaults, vs 1.81× for the clicky pair **[measured]**.

## Cross-check against the analytic model

`motor_constants.hysteresis()` in
[klipper_tmc_autotune](https://github.com/andrewmcgr/klipper_tmc_autotune) sizes
the hysteresis to the natural current change over one blank + two slow-decay
intervals, from L, R, V and the run current. Transcribed verbatim, fed its own
motor-database entries, and run against our measured 3540-combo grid (motor A,
58 mm/s, 24 V, 1.8 A):

| finding | data |
| --- | --- |
| vibration falls **monotonically** with effective hysteresis (chopper fixed at tbl2/toff1) | 3915 @ h_eff −2 → 1180 @ h_eff 16, a 3.3× span **[measured]** |
| the formula + its own DB (Creality 42-xx, 24 V, 1.8 A) yields h_eff **−2…+1** | measured 2800–3915 — *worse than Klipper defaults* (2922), bottom 5 % of the grid **[measured]** |
| the hstrt-first **split** of a given total is near-optimal on the median | at h_eff 14 the split (regs hstrt7/hend9) is the best same-chopper combo (1227); motor B's independent winner was exactly it **[measured]** |
| the datasheet cap alone is not the clean/clicky boundary | the hstrt-first split clicks from h_eff ≈ 8 regardless of the cap; the *split* governs clicking (see above), not the total **[measured]** |

The formula computes the **anti-chatter floor** — just enough hysteresis to
cover the unavoidable ripple — and uses it as the setting; the measured
**vibration optimum sits near the cap**, 2–6× above that floor (for the formula
to output h_eff 14 at 1.8 A the natural ripple would have to be ~78 mA; its DB
gives ~12 mA for a 42-40).

**The gap is saturation, and it is ~2× [measured].** The chatter floor is an
in-situ inductance probe: at 1.8 A the chopper chatters at h_eff −2 and is clean
from −1, implying a natural ripple ≈ 24 mA against the ≈ 12 mA the small-signal
DB inductance predicts — effective L at run current is about **half** the
datasheet figure (a 42-40 rated 1.0 A driven at 1.8 A RMS is deep in
saturation). The maintainer confirmed the DB carries datasheet units (RMS
current, small-signal L), so this is genuinely un-modelled physics; a
saturated-L / derate field would carry it. The RMS→peak convention would only
*widen* the gap, and **back-EMF is second order** (the chatter floor does not
shift across 30/58/90 mm/s — what changes with speed is the stakes, not the
choice). The full exchange — including three of our six questions acknowledged
as bugs (the TOFF=1/TBL guard, the fclk fallback, the cancelling ×32/32) — is in
[issue #339](https://github.com/andrewmcgr/klipper_tmc_autotune/issues/339) and
walked through in the [chronicle](CHRONICLE.md).

Caveat: the exact 2209 default chopper (tbl1/toff1) is not in our grid — we
exclude `TOFF=1` with `TBL<2` per the datasheet — so the nearest tbl2/toff1 cells
were used; cells are n=2 but the trend spans 19 hysteresis levels smoothly.

What the cross-check put into our own code **[model]**:

- **the blank-time table was wrong for TMC2208/2209** (16/24/32/40 clocks, not
  16/24/36/54) — found by diffing their `_tblank_cycles()`; fixed in `tmc.py`
  (slightly shifts the audible-frequency estimate);
- our simulator modeled one slow-decay phase per chopper cycle; the real
  spreadCycle sequence is on → slow decay → fast decay → **second slow decay**
  — a refinement for the next model iteration;
- a cheaper search: since vibration is near-monotonic in h_eff and the split is
  near-optimal, the hysteresis plane can be swept first as a **single h_eff
  line** (parametrized by the split), then refined locally;
- the click-fix bounds: the analytic formula is a **floor** (below it the
  chopper chatters), the datasheet cap is the **ceiling** — the search lives
  between them.

## Run current: the master knob

Why 1.8 A on 1.0 A-rated motors? Because at lower currents the printer used to
skip steps — so we measured the skip threshold instead of guessing at it:

- **The referee.** A skipped step is quantized — one electrical cycle = 4 full
  steps ≈ 0.8 mm of belt — and always lands as a position offset. Creeping
  toward an endstop in 0.2 mm moves while polling `QUERY_ENDSTOPS` measures
  that offset deterministically (`SET_KINEMATIC_POSITION` widens the legal
  range to ±13 mm; a per-run calibration absorbs the systematic bias).
- **The stress.** On coupled-XY kinematics a pure X move splits the load
  between both motors — the honest single-motor worst case is the X=Y
  (motor A) / X=−Y (motor B) diagonal at full machine acceleration through the
  speed band.
- **Silent slips exist [measured].** The default chopper at 0.65 A lost
  14.8 mm almost inaudibly (accelerometer barely above baseline) while at
  0.4 A the tuned one roared at 5× the healthy p99. The accelerometer is
  theater; the endstop is the judge.

Measured skip thresholds (worst case: belt to 200 mm/s at 10 000 mm/s²):

| chopper | skip threshold |
| --- | --- |
| Klipper default | **1.0–1.2 A** (at 1.0 A it slips with a roar — the rig's historical skipped steps, explained) |
| tuned | **0.40–0.45 A** |

**Chopper tuning bought ~2.5× of torque margin [measured]** — and margin is
spendable. We dropped `run_current` 1.8 → 1.0 A (2.2× over the measured
threshold, verified to also hold at 0.7 A), retuned the chopper at the new
current, and landed on the quietest state this rig has ever measured: at 1.0 A
the vibration ladder is flat (~940) regardless of registers, the motors run
3.2× cooler (I²R), and the saturation that broke the analytic model is gone.

The distilled causal chain: **tune the chopper → buy torque margin → spend it
on lower current → end up cooler and quieter than any register combo at high
current could make you.** The `current` command (`CHOPPER_CURRENT`) automates
the ladder — endstop referee, bisection to the threshold, a safety margin
(default 2×), optional save — followed by a chopper retune, since the optimum
depends on the current.

## Practical rules distilled so far

- A winner on the edge of the datasheet-allowed region (effective hysteresis 16)
  is suspect — prefer the interior even at a small median cost.
- A median-based score needs a transient companion metric; “quiet on average”
  is not “clean”. (Implemented: clicks are counted per move and penalized.)
- The hysteresis **split** matters, not only the total: hend-heavy splits
  measured clean where hstrt-first splits click, at the same effective total.
- Tune at the run current and at the resonance speed you actually print with:
  at 1.0 A the whole hysteresis ladder is flat and the clicks vanish.
- **Run current is the master knob**: registers matter most when the current is
  high; if tuning buys you torque margin, spend it on lowering the current.
- Position loss must be measured by an endstop, not inferred from sound —
  silent slips exist.
- A resonance scan must run on stock registers — a well-tuned chopper hides
  the very peak the scan is looking for (897 vs 2676 at the same speed).
- Analytic bounds and hardware measurement are not competitors: the model draws
  the fence, the measurement picks the spot inside it.
