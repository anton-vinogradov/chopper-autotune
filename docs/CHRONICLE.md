# Chronicle

How chopper-autotune grew from "it picks registers" into a measuring instrument
with ears and a torque meter. This is the narrative by date; the physics and the
numbers live in [SCIENCE.md](SCIENCE.md), and the upstream conversation in
[issue #339](https://github.com/andrewmcgr/klipper_tmc_autotune/issues/339).

Tags: **[measured]** on the reference printer (Ender-6 CoreXY, TMC2209, 24 V),
**[model]** from simulations, **[hypothesis]** a working explanation.

## Bird's-eye view

| date | direction | milestone |
| --- | --- | --- |
| Jul 4 | Clicks | spotted on the tuned config; two models falsified; forensics analyzer built |
| Jul 5 | Model ↔ measurement | cross-check of their formula vs our grid; **our** blank-time bug found; issue #339 opened |
| Jul 6, 04:16 | Model ↔ measurement | maintainer replies: three bugs acknowledged, the "floor" reading confirmed |
| Jul 6 | Current | saturation ≈2×; back-EMF second order; endstop referee; skip thresholds; current 1.8→1.0 A |
| Jul 6 | Clicks | resolution on hardware: the **split** governs it; click penalty; clean retune |
| Jul 6 | Everything | data reply to the maintainer; **v0.2.0 release** |
| Jul 7 | Print speed | tune verified at 200 mm/s: flat, neutral, click-free |
| Jul 8 | Tuner | flat-region blind spot found → safety tie-breaker → auto re-tune off the edge |
| Jul 8 | Print speed | motion envelope: no skip to 350 mm/s / 40k accel — the motor isn't the limit |

---

## Direction I — The clicks mystery

**Outcome.** The click turned out to be **electromechanical** (a broadband
~300 Hz thump, no lock to the electrical phase), and it is governed by the
**split** of the hysteresis between HSTRT and HEND, not by their total. A
transient penalty went into the scoring; the retune landed a clean pair at no
vibration cost.

<details>
<summary>How we got there — 4 moves, 2 falsified hypotheses</summary>

**Jul 4 — discovery [measured].** The tuned config won the median convincingly
(~2× quieter than Klipper defaults) but clicked: accelerometer peaks ~65× the
median, ~2 clicks per one-second move. A single-motor `FORCE_MOVE` clicked too →
it's the *config*, not the diagonal showcase trajectory. The median score was
blind to it by construction.

**Jul 4 — model #1: time-domain RL + spreadCycle state machine.** Hypothesis:
the tuned config overshoots more at the sine zero crossing. The model reproduced
the vibration ordering, but the zero-crossing overshoot was the same across
configs → **zero-crossing hypothesis rejected [model]**.

**Jul 4 — model #2: cycle-to-cycle stability (Floquet multiplier).** Hypothesis:
the loop goes subharmonic at high hysteresis. Multipliers ≈1.0 for all configs;
a fixed-off-time regulator is stable by construction → **loop-instability
hypothesis rejected [model]**. Conclusion: the click is not an electrical
chopper-loop effect — it's electromechanical, and needs hardware data.

**Jul 4 — the instrument.** Built `click_forensics.py`: from raw accelerometer
it fingerprints where the click happens (reversal / steady motion), its timing,
the ring frequency and decay, and phase lock to the electrical cycle. Validated
on synthetic data (distinguishes two injected signatures, nails the frequency).

**Jul 6 — resolution on hardware [measured].** Fingerprint: R≈0.1–0.4 (no
electrical-phase lock), a heavily damped broadband ~300 Hz thump (decay ~1 ms).
Hysteresis ladder (their hstrt-first split, chopper fixed): clean at h_eff ≤ 6,
explosion at h16 (~5/move, peaks 65–69×). **An earlier claim corrected**: "the
cap of 14 is clean" was wrong — h14 with that split clicks too; motor B's h14
config had simply never been click-checked.

**⚡ The key finding.** The **split** governs clicking, not the total:
hend-heavy and balanced splits measure zero clicks at h_eff 12–14 where the
hstrt-first split clicks. Consistent with the datasheet note that positive HEND
improves the sine zero crossings.

**Jul 6 — the fix.** Every capture now counts clicks (rising crossings above 15×
the move median, over the whole capture — reversal clicks live outside the
steady window); one click per move is penalized like doubling the vibration. The
retune moved both motors to clean configs **at no vibration cost**: A `0/2/2/12`,
B `0/2/6/10`, zero clicks, 1.86× quieter than defaults (vs 1.81× for the clicky
pair).

</details>

---

## Direction II — Model vs measurement

**Outcome.** The analytic hysteresis formula of
[klipper_tmc_autotune](https://github.com/andrewmcgr/klipper_tmc_autotune)
computes the anti-chatter **floor**, not the vibration optimum; its split of a
given total is near-optimal. Along the way we found **our own** bug (the
blank-time tables for 2208/2209) and opened a live dialogue with the maintainer —
three of our questions he acknowledged on the spot as bugs on his side.

<details>
<summary>How we got there</summary>

**Jul 5 — the cross-check.** We transcribed their `hysteresis()` verbatim and ran
it against our measured etalon (3540 combos, motor A @58 mm/s). Vibration falls
**monotonically** with effective hysteresis (3915 @ h_eff −2 → 1180 @ h_eff 16).
Their formula with their own motor DB yields h_eff −2…+1 — which our grid
measures *worse than Klipper defaults* (bottom 5% of all combos). Reading: the
formula computes the **floor** (just enough hysteresis to cover the unavoidable
ripple), while the vibration optimum sits near the cap.

**Jul 5 — our bug.** Diffing their `_tblank_cycles()` against our code: our
blank-time table for TMC2208/2209 was 16/24/36/54 clocks instead of
16/24/32/40. Fixed (PR #42); it slightly shifts the audible-frequency estimate.

**Jul 5 — issue #339.** Six questions posted: three inconsistencies/bugs (the
TOFF=1/TBL guard, the fclk fallback, the cancelling ×32/32) and three "harder"
ones (what the formula optimizes, saturation, back-EMF). Tone: "some of this
confirms your choices, some raises questions."

**Jul 6, 04:16 — the maintainer replies.** Acknowledged all three bugs;
confirmed the DB semantics (RMS current, *small-signal* inductance — so
saturation really is un-modelled); confirmed the formula deliberately targets the
**minimum recommended hysteresis** (the "floor" reading is right). His questions
back: how to model saturation? does back-EMF change the chosen values?

**Jul 6 — the data reply.** We answered with measurements from the hardware
campaign (see Direction III): saturation ≈2×, back-EMF second order, and the
"downside of too much hysteresis" is clicks and tracks the split. We offered a PR
adding a saturated-L / derate field to their DB — awaiting his word on the
semantics he wants.

</details>

---

## Direction III — Saturation and run current

**Outcome.** The threshold below which the chopper starts *chattering* turned out
to be a **direct in-situ inductance probe** → coil saturation ≈2× at run
current. And chopper tuning buys ~**2.5× of torque margin** — which we spent on
dropping the current 1.8 → 1.0 A: motors 3.2× cooler, at the quietest state the
rig has ever measured. The `CHOPPER_CURRENT` command was born from this.

<details>
<summary>How we got there</summary>

**Jul 6 — saturation (M2) [measured].** The chatter floor is a measurement: the
chopper chatters when the hysteresis is below the unavoidable per-interval
current ripple, so the threshold is a direct read of that ripple → effective L.
Measured ΔI ≈ 24 mA vs the ≈ 12 mA their formula predicts from the small-signal
DB inductance → **saturation factor ≈ 2×** (a 42-40 rated at 1.0 A driven at
1.8 A RMS is deep in saturation). Bonus: at 1.0 A the whole vibration ladder is
flat (~940 everywhere) and the audible clicks vanish — both the tuning gains and
the clicks are *run-current* phenomena.

**Jul 6 — back-EMF (M3) [measured].** Hysteresis ladder at 30/58/90 mm/s: the
curve keeps its shape and the chatter floor **does not shift** with speed → for
choosing the hysteresis, back-EMF is a **second-order** effect. What changes with
speed is the *stakes* (at resonance hysteresis spans 3× of vibration, off
resonance nearly flat).

**Jul 6 — the endstop referee (M7a).** To measure current we need a reliable
skipped-step detector. A skip is **quantized** (one electrical cycle = 4 full
steps ≈ 0.8 mm of belt) and always lands as a position offset; creeping toward an
endstop in 0.2 mm steps polling `QUERY_ENDSTOPS` measures it deterministically.
Lesson: **silent slips exist** — the default chopper at 0.65 A lost 14.8 mm
almost inaudibly. The accelerometer is theater; the endstop is the judge.

**Jul 6 — skip thresholds (M7b) [measured].** Worst-case single-motor stress
(belt to 200 mm/s, full accel): the tuned chopper holds down to **0.42 A**, the
default one slips already at **1.0–1.2 A** (at 1.0 A with a roar, p99 41k). These
were the historical skipped steps at the default current — not "too little
current," but a bad chopper eating torque at resonance. **Chopper tuning bought
~2.5× of torque margin.**

**Jul 6 — the decision.** Margin is spendable: `run_current` 1.8 → 1.0 A (2.2×
over the measured threshold, verified to also hold at 0.7 A), then a chopper
retune at the new current. The result is the quietest, coolest state the rig has
ever measured. The causal chain: **tune the chopper → buy torque margin → spend
it on a lower current → end up cooler and quieter than any register combo at high
current could make you.** Productized as `CHOPPER_CURRENT` (PR #46).

</details>

---

## Direction IV — Print speed and the flat-region blind spot

**Outcome.** Verifying the tune at the real print speed (200 mm/s) showed the
chopper landscape is **flat** there — nothing to gain, nothing lost. That
flatness exposed a tuner blind spot: at a low run current the vibration objective
is nearly flat, so with nothing to distinguish configs the descent had landed
motor A on a datasheet-edge config *at random*. A small **safety tie-breaker** in
the score now makes the tuner pick the safe config on its own. A motion-envelope
tool measures where the motor actually runs out of torque, in speed and
acceleration.

<details>
<summary>How we got there</summary>

**Jul 7 — the worry.** The user prints at 200 mm/s; we tuned at resonance
(58/34). Legitimate question: did we optimise a regime the printer never cruises
in? Torque and skipping were already validated to 200 (Direction III); vibration
was not. A two-point check (default vs tuned) at 200 came out equal (1521 vs
1526), zero clicks — suggestive but not proof.

**Jul 8 — the full landscape at 200 [measured].** Swept 16 configs (4 chopper
frequencies × 4 hysteresis levels) directly at 200 mm/s: spread **9 %**, within
measurement noise, zero clicks everywhere. So at print speed the chopper choice
genuinely does not matter — flat. Resonance tuning is the right target (the
machine crosses 58/34 on every accel/decel); the print-speed "gain" is nil
because there is nothing to gain.

**Jul 8 — the blind spot.** That flatness (also the rule at 1.0 A generally)
means the tuner's vibration objective is nearly flat, so the descent had picked
motor A's `0/8/3/15` — effective hysteresis 16 (the datasheet edge) and 21 kHz
(barely ultrasonic) — essentially at random. It does not click, but it is exactly
the edge our own practical rules say to avoid.

**Jul 8 — the fix, automatic (not a user choice).** `tmc.edge_penalty()` adds a
small tie-breaker toward safe configs: a chopper frequency comfortably above the
audible band, and interior hysteresis. Weighted ≤10 %, so any real vibration win
overrides it; it only decides when the field is flat. The re-tune then moved
motor A off the edge on its own — `0/8/3/15` (h16, 21 kHz) → `0/2/4/7` (h9,
65 kHz); motor B stayed at its already-interior `1/6/4/0` (PR #53).

**Jul 8 — the motion envelope [measured].** A skip-threshold sweep in speed and
acceleration (the same endstop referee, at the saved 1.0 A) found **no skip on
either motor** through the whole testable range — belt to 350 mm/s (near the MCU
step-rate limit) and acceleration to 40 000 mm/s² (4× the configured max, far past
any print use). So at the chosen current the motors are nowhere near their torque
ceiling: the print-speed limit is not the motion system but the hotend's flow
rate — which is not a motion measurement. (Prototype; the referee's fine creep
makes it slow, to be sped up before shipping as `CHOPPER_ENVELOPE`.)

</details>

---

## Still open

- **The split question [hypothesis].** Why do hend-heavy splits stay clean where
  hstrt-first splits click? Working guess — positive HEND improves the sine zero
  crossings (datasheet); the hysteresis-decrementer model isn't built yet.
- **Upstream PR.** A saturated-L / derate field for the klipper_tmc_autotune
  motor DB — after the maintainer settles on the semantics he wants.
- **Optional measurements.** Phase R with a multimeter (would sharpen the
  saturation number); floor-vs-cap temperature — *cancelled*, at 1.0 A the
  hysteresis heat effect sinks below the noise.
- **`CHOPPER_ENVELOPE`.** Productize the motion envelope (speed + acceleration
  torque ceilings) with a *fast* endstop referee — the prototype's 0.2 mm creep
  is correct but slow. Pair it with a wide `find-speed` for the resonance map, so
  one report gives the motor's usable speed band and its hard ceiling.
- **Real-print verification.** Motor temperature (1.0 A vs 1.8 A, thermal
  camera), a defaults-vs-tuned surface A/B, and no layer shifts at 200 mm/s.

## Tooling lessons along the way

Incidental, but recorded so we don't step on them twice:

- the median is blind to rare transients — it needs a companion metric (done);
- position loss is measured by an endstop, not inferred from sound — silent
  slips are real;
- a resonance scan must run on **stock** registers: a well-tuned chopper hides
  the very peak the scan is looking for (897 vs 2676 at the same speed) — this is
  why TUNE on an already-tuned printer first failed to find resonance;
- run current is the **master knob**: registers matter most when the current is
  high.
