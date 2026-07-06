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

## Case study: clicks on the tuned config (July 2026 — resolved)

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

### Resolution (2026-07-06, on hardware)

The forensics analyzer ran on the real machine, followed by a hysteresis
ladder (their hstrt-first split, chopper fixed) and a retune:

- **fingerprint [measured]**: no lock to the electrical phase (R ≈ 0.1–0.4), a
  heavily damped broadband ~300 Hz thump (decay ~1 ms) — an electromechanical
  kick, not a per-cycle electrical glitch (both models were right to find
  nothing);
- **ladder [measured]**: clean at h_eff ≤ 6, sporadic clicks from ≈ 8,
  explosion at 16 (~5/move, peaks 65–69×); at h_eff −2 the chopper *chatters*
  (the anti-chatter floor their formula guards — observed live);
- **correction**: the earlier "their cap of 14 is vindicated" entry was wrong —
  h_eff 14 with the hstrt-first split clicks too (0.6/stroke); motor B's h14
  config had simply never been click-checked;
- **the split matters, not just the total [measured/hypothesis]**: the retuned
  winners with hend-heavy/balanced splits (A `0/2/2/12`, B `0/2/6/10`) measure
  zero clicks at h_eff 12–14, where hstrt-first splits click — consistent with
  the datasheet note that positive HEND improves the sine zero crossings.
  Open sub-question;
- **the fix (shipped)**: every measurement now counts transients (rising
  crossings above 15× the move median, over the whole capture — reversal
  clicks live outside the steady window) and one click per move is penalized
  like doubling the vibration. The retune moved both motors to click-free
  configs at **no vibration cost**: 1.86× less than defaults vs 1.81× of the
  clicky pair **[measured]**.

L/R refinement moved to the cross-check section (the chatter floor turned out
to be an in-situ inductance probe).

## Cross-check: klipper_tmc_autotune's model vs our measured grid (July 2026)

We transcribed the hysteresis formula from
[klipper_tmc_autotune](https://github.com/andrewmcgr/klipper_tmc_autotune)
(`motor_constants.hysteresis()`: hysteresis sized to the natural current change
over one blank + two slow-decay intervals) verbatim, fed it their own motor
database entries, and asked our measured 3540-combo grid (motor A, 58 mm/s,
TMC2209, 24 V, 1.8 A) how those predictions actually perform.

| finding | data |
| --- | --- |
| vibration falls **monotonically** with effective hysteresis (chopper fixed at tbl2/toff1) | 3915 @ h_eff −2 → 1180 @ h_eff 16, a 3.3× span **[measured]** |
| their formula + their own DB (Creality 42-xx, 24 V, 1.8 A) yields h_eff **−2…+1** | measured 2800–3915 — *worse than Klipper defaults* (2922), bottom 5 % of the grid **[measured]** |
| their hstrt-first **split** of a given total is near-optimal *on the median* | at h_eff 14 their split (regs hstrt7/hend9) is the best same-chopper combo (1227); motor B's independently measured winner was *exactly* that config **[measured]** |
| ~~their cap (h_eff 14) is vindicated~~ **corrected 2026-07-06**: the hstrt-first split clicks from h_eff ≈ 8 regardless of the cap | the click ladder (see the case study) shows the *split*, not only the total, governs clicking; hend-heavy splits measure clean at h_eff 12–14 **[measured]** |

Reading: their formula computes the **anti-chatter minimum** — just enough
hysteresis to cover the unavoidable current ripple — and then uses it as *the*
setting. The measured **vibration optimum sits near the cap**, 2–6× above that
minimum (for their formula to output h_eff 14 at 1.8 A the natural ripple would
have to be ~78 mA; their DB values for a 42-40 give ~12 mA). Plausible missing
pieces: small-signal DB inductance (saturation at run current lowers L and
raises the real ripple), peak-vs-RMS current convention, back-EMF at speed —
or simply a different objective (minimum viable ≠ minimum vibration). We are
taking these to the community as questions.

Caveats: their exact 2209 default chopper (tbl1/toff1) is not in our grid — we
exclude `TOFF=1` with `TBL<2` per the datasheet note (their code only bumps
TBL 0→1, another upstream question); the nearest tbl2/toff1 cells were used.
Grid cells are n=2, but the trend spans 19 hysteresis levels smoothly.

What we took from their code **[model]**:

- **our blank-time table was wrong for TMC2208/2209** (16/24/32/40 clocks, not
  16/24/36/54) — found by diffing their `_tblank_cycles()` against ours; fixed
  in `tmc.py` (slightly shifts the audible-frequency estimate);
- our simulator modeled one slow-decay phase per chopper cycle; the real
  spreadCycle sequence is on → slow decay → fast decay → **second slow decay**
  — a refinement for the next model iteration;
- a cheaper search: since vibration is near-monotonic in h_eff and their split
  is near-optimal, the hysteresis plane can first be swept as a **single h_eff
  line** (parametrized by their split), then refined locally;
- click-fix bounds sharpened: their formula is a **floor** (below it the
  chopper chatters), the datasheet cap is the **ceiling** — the search should
  live between them, not centered on the model value as we first assumed.

### Upstream response ([issue #339](https://github.com/andrewmcgr/klipper_tmc_autotune/issues/339), 2026-07-06)

The maintainer confirmed:

- the motor database carries **datasheet units**: RMS current and *small-signal*
  inductance — so saturation at run current is indeed un-modelled physics;
- **TOFF=1/TBL, the fclk fallback and the ×32/32 are bugs** (three of our six
  questions resolved on the spot);
- the formula deliberately targets the **minimum recommended hysteresis** —
  the floor reading is correct — because "there is a downside to too much
  hysteresis" (higher current ripple → copper losses/motor heating, and our
  clicks case at the very edge).

Note on units: with RMS current confirmed, converting to peak (×1.414) in the
counts term makes the predicted floor *lower* still — the RMS/peak convention
does not close the 2–6× gap, it widens it. That leaves **saturation** (a 42-40
driven at 1.8 A against a 1.0 A rating is deep in saturation; L can drop
severalfold) and the **objective difference** as the live explanations.

### Measured answers (2026-07-06, on hardware)

1. *Can saturation be modeled?* **Measured: saturation factor ≈ 2×.** The
   chatter floor turned out to be an in-situ inductance probe: at 1.8 A the
   chopper chatters at h_eff −2 and is clean from −1, which implies a natural
   ripple of ≈ 24 mA — their formula with the small-signal DB inductance
   predicts ≈ 12 mA. Effective L at 1.8 A on a 1.0 A-rated 42-40 is roughly
   **half the datasheet figure** (crude: ± half a hysteresis step; R taken
   from their DB — a multimeter measurement would refine it). A saturated-L
   field or derate factor in their motor DB would carry this.
2. *Does back-EMF change the chosen values?* **Second order.** The chatter
   floor does not visibly shift across 30/58/90 mm/s and the curve keeps its
   shape; what changes with speed is the *stakes* (at resonance hysteresis
   spans 3× of vibration, off resonance the curve is nearly flat).
3. *Is high hysteresis worth its downsides?* Partially answered by the click
   ladder: past h_eff ≈ 7 the hstrt-first split buys ~10 % of median at the
   price of audible clicks. The temperature comparison (copper losses) is
   protocoled but deferred — available on request.
4. Bonus **[measured]**: at 1.0 A the whole vibration-vs-hysteresis ladder is
   flat (~940 everywhere) and the audible clicks vanish — both the tuning
   gains and the clicks are *run-current* phenomena; tune at the current you
   print with.

## Practical rules distilled so far

- A winner on the edge of the datasheet-allowed region (effective hysteresis 16)
  is suspect — prefer the interior even at a small median cost.
- A median-based score needs a transient companion metric; “quiet on average”
  is not “clean”. (Implemented: clicks are counted per move and penalized.)
- The hysteresis **split** matters, not only the total: hend-heavy splits
  measured clean where hstrt-first splits click, at the same effective total.
- Tune at the run current and at the resonance speed you actually print with:
  at 1.0 A the whole hysteresis ladder is flat and the clicks vanish.
- A resonance scan must run on stock registers — a well-tuned chopper hides
  the very peak the scan is looking for (897 vs 2676 at the same speed).
- Analytic bounds and hardware measurement are not competitors: the model draws
  the fence, the measurement picks the spot inside it.
