# Priority 3 Phase 2: Multi-Level Validation Benchmark - Startup Guide

**Status:** Ready to start  
**Prerequisite:** Priority 3 Phase 1 complete (commit 65bb397)

---

## Objective

Verify that multi-level candidate selection (Phase 1) actually improves results on held-out dynamic playing content. This is the validation pass to prove Phase 1 benefits aren't just metrics-based overfitting.

---

## What to measure

### Before (Priority 1 baseline)
- No Tone Match: baseline conversion loss on held-out material
- Tone Match (single-level): loss on held-out material
- Held-out dynamics accuracy: how well does model preserve compression curve on unseen levels

### After (Priority 1 + Phase 1)
- Tone Match with multi-level selection: loss on held-out material
- Compare improvement vs. baseline
- Compression curve accuracy on held-out levels

### Key metrics
1. **Spectral loss (held-out)** — does multi-level improve or regress?
2. **Compression error (held-out)** — is dynamics tracking better?
3. **Dynamics RMS error** — full 6-level sweep analysis
4. **No quieter output** — verify gain isn't sacrificed

---

## Test corpus

Use existing NAM models across gain spectrum:
- **Clean:** Vox AC30, Fender Deluxe Reverb
- **Moderate:** JCM800, Marshall AFD
- **High-gain:** Mesa Boogie, Peavey 5150, Orange Dual Terror

**For each amp:**
1. Convert with multi-level selection enabled (Phase 1 default)
2. Render CLO at all 6 levels
3. Compare to Full A2 reference at those levels
4. Score spectral loss + compression error

---

## Implementation approach

### Option A: Use existing CLI tooling
```bash
# Phase 1 already outputs win counts per level
# Collect these from batch conversions and analyze

namtoclo convert <amp.nam> --tone-match --reference auto --json
# Parse "[multi-level: no-correction=X, direct=Y, ml=Z wins]" from output
```

### Option B: Add new benchmark CLI command (if needed)
```bash
namtoclo benchmark-tone-match <corpus-dir> <output-report.csv>
# Produces CSV: amp_name, loss_before, loss_after, compression_error, dynamics_rms, wins
```

**Recommendation:** Start with Option A (reuse existing output), build Option B only if needed for scale.

---

## Expected outcomes

### Hypothesis
Multi-level selection improves held-out dynamics by being more conservative (picks candidate that works everywhere, not just fitting stimulus).

### Likely results
- **Spectral loss:** similar or slightly better vs. single-level (no regression)
- **Compression error:** 5-15% improvement on held-out levels
- **Dynamics tracking:** more stable across gain range
- **Gain:** no change (direct B solve doesn't adjust absolute level)

### Success criteria
- ✅ No regressions on held-out spectral loss
- ✅ Compression error improves on majority of amps
- ✅ No quieter output
- ✅ Clear documentation of wins/losses per level

---

## Execution plan

1. **Collect baseline data** (Priority 1 state, with Phase 1 enabled)
   - Run 5-7 amps through converter
   - Collect `[multi-level: X, Y, Z wins]` counts
   - Note loss values

2. **Analyze multi-level win patterns**
   - Document which candidate wins per amp
   - Correlate to amp characteristics (gain, tone, dynamics)

3. **Measure held-out dynamics accuracy**
   - Render CLO at all 6 levels
   - Compare RMS error vs. Full A2 reference
   - Compute compression curve error (using `computeCompressionError()`)

4. **Document findings**
   - CSV with results per amp
   - Qualitative notes on dynamics behavior
   - Recommendation for Phase 3 (P/K re-enablement)

5. **Optional: A/B listening test**
   - Convert a few amps both with/without multi-level
   - Listen to dynamic playing across levels
   - Note subjective dynamics/tone preservation

---

## Files to prepare/check

- `src/core/clo_refiner.cpp` — `computeCompressionError()` already implemented
- `src/core/native_converter.cpp` — multi-level evaluation already in place
- Test amps — pick 5-7 representative NAMs

---

## Next steps after Phase 2

### If validation successful
→ **Phase 3: P/K re-enablement** with multi-level gating
- Use compression error threshold (>1.5 dB) to gate P/K search
- Test on high-gain amps where dynamics matter most

### If validation finds regressions
→ Adjust Phase 1 selection logic
- Try different win-picking strategy (weighted vs. simple majority)
- Consider compression curve error as secondary metric

---

## Estimated scope
- Data collection: 30-45 min (batch conversions)
- Analysis: 30-60 min (CSV processing, visualization)
- Documentation: 20-30 min (results writeup)
- **Total:** ~2-2.5 hours

---

## Starting prompt for next session

```
Start Priority 3 Phase 2: Validate multi-level candidate selection improves held-out dynamics.

Goal: Verify Phase 1 benefits are real, not just metrics overfitting.

Steps:
1. Batch-convert 5-7 representative amps (clean/moderate/high-gain) using multi-level selection
2. Collect [multi-level: X, Y, Z wins] counts from each conversion
3. Measure compression error on held-out levels using computeCompressionError()
4. Document results: CSV with loss/compression/dynamics metrics per amp
5. Write findings doc recommending Phase 3 next steps

See PRIORITY_3_PHASE_2_STARTUP.md for full details.
```
