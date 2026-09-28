# Priority 3 Phase 3: Real-World Validation Plan

**Goal:** Prove P/K search with compression-error gating actually works on real hardware/listening material.

**Risk:** Original P/K search made amps quieter despite improving metrics. Don't repeat that mistake.

---

## Validation Protocol

### Test Amps (2 required)

**Marshall 800** — should ENABLE P/K
- Moderate-gain crunch amp
- Baseline compression error likely >1.5 dB
- P/K should run and improve dynamics

**Fender Deluxe** — should SKIP P/K  
- Clean amp, no-correction won Phase 2
- Baseline compression error likely <1.5 dB
- P/K should be skipped (already optimal)

### Test Material (3 required)

For each amp, need:
1. **Dynamic playing test:** Volume sweep (quiet to loud picking)
   - Test if P/K improves dynamics feel
   - Check for level consistency across range

2. **Tone comparison:** Same riff at consistent level
   - Listen for tone changes (P/K on vs off)
   - Check for phase/presence changes

3. **Quiet passage:** Very soft playing
   - Critical: Check if output gets quieter
   - Original failure mode detector

### Listening Criteria

For P/K to pass validation:

✅ **MUST:** No quieter output (if anything, slightly louder or same)  
✅ **MUST:** Dynamics feel more consistent (not all lows or all highs)  
✅ **SHOULD:** Tone stays clear (no phase smearing)  
✅ **SHOULD:** Compression feels natural (not artificial)  

❌ **FAIL IF:** Sounds quieter at any level  
❌ **FAIL IF:** Dynamics feel worse (compressed or pumping)  
❌ **FAIL IF:** Tone gets thin/phase-y  

---

## Execution Steps

1. **Convert Marshall 800 with P/K enabled**
   - Check if P/K search ran (status message)
   - Check if gates accepted it
   - Save both versions (P/K on, P/K off)

2. **Convert Fender Deluxe with P/K enabled**
   - Verify P/K skipped (compression error <1.5 dB)
   - Check status message explaining gate 1 rejection

3. **Listen to Marshall 800 CLOs**
   - Play dynamic test audio
   - Compare P/K on vs P/K off
   - Score each criterion above
   - If FAIL on any "MUST", reject Phase 3

4. **Listen to Fender Deluxe CLOs**
   - Verify P/K off version matches no-correction from Phase 2
   - Confirm no P/K search ran

5. **Document results**
   - Pass/fail for each criterion
   - Any anomalies or edge cases
   - Recommendation: ship Phase 3 or disable P/K again

---

## Success Definition

**Phase 3 passes if:**
- ✅ Marshall 800 P/K improves dynamics WITHOUT sounding quieter
- ✅ Fender Deluxe correctly skips P/K (compression error <1.5 dB)
- ✅ All "MUST" listening criteria pass
- ✅ No trade-offs (tone, level, dynamics all preserved/improved)

**Phase 3 fails if:**
- ❌ Any amp sounds quieter (original failure mode)
- ❌ Dynamics feel worse (compressed, pumping, loss of dynamics)
- ❌ Tone changes (phase, presence, clarity affected)
- ❌ Any "MUST" criterion fails

---

## If Phase 3 Fails

1. Document exactly what failed (tone, level, dynamics, etc.)
2. Disable P/K search again (keep it in code, but disable by default)
3. Leave as research code (not shipped)
4. Note in QUALITY.md: "P/K search with compression-error gating attempted and rejected due to [specific failure]"

---

## If Phase 3 Passes

1. Ship with P/K enabled by default for appropriate amps
2. Document that compression-error gating solved the original problem
3. Mark as production-ready
4. Can proceed to Priority 4 if time permits

---

## Timeline

**Critical:** Don't ship Phase 3 without this validation.  
**Effort:** ~1-2 hours (conversions + listening + documentation)  
**Blocker:** Real test material needed (actual audio files or hardware)

---

## Notes

- Metrics alone are not sufficient (original P/K proved this)
- Real listening is the only ground truth
- If any doubt, disable P/K (safe default)
- Better to be conservative than repeat the "sounds quieter" mistake
