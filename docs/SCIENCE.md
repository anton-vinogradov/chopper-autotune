# The science

The physics behind chopper tuning, the models we build, and the open questions —
kept current as the investigation moves. Each claim is tagged: **[datasheet]** comes
from Trinamic documentation, **[measured]** was observed on the reference printer
(Ender-6 CoreXY, TMC2209, 24 V, 1.8 A), **[model]** comes from our simulations,
**[hypothesis]** is the current best guess.

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
  audible clicks (see the case study) **[measured]**.
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

## Case study: clicks on the tuned config (July 2026, open)

The tuned configs won the median score convincingly (~2× less vibration than
Klipper defaults **[measured]**), but motor A's winner sits at effective
hysteresis **16 — the datasheet maximum** — and produces sporadic audible clicks
the scoring never saw.

Facts **[measured]**:

- clicks: accelerometer peak ~65× the median, ~2 clicks per 1 s move ≈ one per
  ~36 electrical cycles — rare and intermittent, not per-cycle;
- Klipper defaults are clean under the identical move;
- single-motor `FORCE_MOVE` clicks too → it is the *config*, not the diagonal
  showcase trajectory.

Models built, hypotheses falsified:

| model | question | outcome |
| --- | --- | --- |
| time-domain RL + spreadCycle state machine | is the current overshoot at the sine zero-crossing bigger on the tuned config? | reproduces the vibration ordering (tuned < default ripple), but the zero-crossing overshoot is the same across configs → **zero-crossing hypothesis rejected [model]** |
| cycle-to-cycle stability (Floquet multiplier at constant current) | does the chopper loop go subharmonic at high hysteresis? | multipliers ≈ 1.0 for all configs, no config-specific instability; a fixed-off-time regulator is stable by construction → **loop-instability hypothesis rejected [model]** |

Working hypothesis **[hypothesis]**: the click is *electromechanical* — the
wide-hysteresis current ripple (or a transient at direction reversal)
occasionally excites a mechanical resonance that rings; a purely electrical
chopper model cannot reproduce it by construction.

Next steps (need the printer):

1. **Click forensics** on raw accelerometer data: where clicks happen in the
   stroke (reversal vs steady motion), inter-click timing vs the electrical
   cycle, the ring frequency/decay (= which mechanical mode), and phase lock to
   the electrical cycle. The analyzer is built and validated on synthetic data.
2. Measure the motor's **L and R** (V and I are known) to replace the estimates
   in the models and compute the analytic hysteresis bound for this motor.
3. The engineering fix, mechanism-agnostic: **bound the hysteresis search** to
   the model-derived window instead of the full 0..16, and add a **transient
   penalty** (peak/median or click rate) to the scoring so the winner must be
   clean, not just quiet on median. Then re-tune and verify.

## Practical rules distilled so far

- A winner on the edge of the datasheet-allowed region (effective hysteresis 16)
  is suspect — prefer the interior even at a small median cost.
- A median-based score needs a transient companion metric; “quiet on average”
  is not “clean”.
- Analytic bounds and hardware measurement are not competitors: the model draws
  the fence, the measurement picks the spot inside it.
