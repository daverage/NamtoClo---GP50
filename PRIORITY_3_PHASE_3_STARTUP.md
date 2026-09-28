# Priority 3 Phase 3: Re-Enable P/K Search with Multi-Level Gating - Startup Guide

**Status:** Ready to start  
**Prerequisite:** Phase 1 & 2 complete (multi-level validation passed)  
**Dependency:** computeCompressionError() already implemented

---

## Objective

Re-enable P/K dynamics-aware search (currently disabled by default) with better gating criteria based on multi-level compression error, instead of the original zero-anchored RMS metrics that made amps quieter.

**Goal:** Enable P/K improvements only when they genuinely improve dynamics tracking without artifacts.

---

## Background

### Why P/K Search Was Disabled
- Original gating: `dynamicsAwareFitting` config with `dynamicsSearchThresholdDb`
- Original problem: improved RMS dynamics error, but made amps **quieter** on real material
- Result: disabled by default (see QUALITY.md)

### Why Phase 3 is Different
- Phase 2 proved multi-level selection works (picks best candidate per level)
- We now have compression error metric (`computeCompressionError()`)
- Better gating: only enable if base candidate has measurable dynamics deficiency

---

## Implementation Plan

### Step 1: Add Compression Error Gating

In `native_converter.cpp` near line 2399 (where P/K search currently gated):

```cpp
// NEW: Multi-level compression error gating for P/K search
if(trainer.dynamicsAwareFitting && !gp5LevelClips.empty() && 
   gp5DirectSolveWon && !gp5DirectSolveB44.empty()) {
    
    // Measure compression error of current winning candidate
    std::vector<double> winningRenderedDb, targetDb;
    for(std::size_t i=0; i<gp5LevelClips.size(); ++i) {
        std::vector<float> rendered;
        if(renderCloWithOverrideOnSignal(gp5PreToneMatchClo,
            gp5Chosen->pk.pp, gp5Chosen->pk.pn, 
            gp5Chosen->pk.kp, gp5Chosen->pk.kn,
            gp5DirectSolveB44, gp5LevelClips[i].input44100, 
            rendered, levelError)) {
            winningRenderedDb.push_back(rmsDb(rendered));
        }
        targetDb.push_back(rmsDb(gp5LevelClips[i].target44100));
    }
    
    double compressionError = computeCompressionError(targetDb, winningRenderedDb);
    
    // Gate 1: Only run P/K if compression error > 1.5 dB
    if(compressionError > 1.5) {
        // Run P/K search here...
        // Accept only if compression improves >50% and spectral doesn't regress >10%
    }
}
```

### Step 2: Modify P/K Search Acceptance Gate

Current code accepts based on `dynamicsSearchThresholdDb` comparison.  
**New logic:**

```cpp
// After P/K search completes, measure new compression error
double pkCompressionError = computeCompressionError(pkTargetDb, pkRenderedDb);

// Accept P/K if:
bool acceptPk = 
    (compressionError > 0) &&  // Avoid division by zero
    (pkCompressionError / compressionError < 0.5) &&  // >50% improvement
    (pkSpectralLoss / originalSpectralLoss < 1.1) &&  // <10% regression
    (std::abs(pkOutputGainDb - originalGainDb) < 2.0);  // ±2dB level
```

### Step 3: Test Amps for Phase 3

From Phase 2 results, good candidates are:
- **Marshall 800:** Has room for improvement (direct B solve good but maybe P/K helps)
- **Peavey 5150:** High-gain, dynamics critical
- **Skip Fender:** No-correction already winning, P/K would be overkill

---

## Success Criteria

✅ **P/K search re-enabled and runs when criteria met**
✅ **No quieter output** (>2 dB level change rejected)
✅ **Compression error improves >50%** when P/K accepted
✅ **Spectral loss doesn't regress >10%** when P/K accepted
✅ **Hardware listening test confirms improvements** on at least one amp

---

## Files to Modify

1. **src/core/native_converter.cpp** (~100 lines)
   - Add compression error measurement around line 2399
   - Add gating criteria
   - Modify P/K search acceptance logic

2. **src/core/native_converter.hpp**
   - If needed: expose compression error thresholds as config (optional)

---

## Testing Strategy

### Unit Level
1. Build and verify no compilation errors
2. Run on Marshall 800: should enable P/K (compression error likely >1.5 dB)
3. Run on Fender Deluxe: might skip P/K (no-correction already winning)

### Validation Level
1. Convert amps with P/K enabled
2. Measure compression error before/after P/K
3. Verify >50% improvement when accepted
4. Spot-check spectral loss (should not regress)

### Real-World Level
1. Listen to dynamic playing test (volume sweep)
2. Compare with P/K off
3. Listen for artifacts, quietness, improved dynamics

---

## Edge Cases to Handle

### Case 1: P/K Search Fails
If `searchPkForDynamics` returns no acceptable solution:
- Fall back to base candidate (already selected)
- Log "P/K search did not improve results"

### Case 2: Compression Error = 0
- Skip P/K (dynamics already perfect)
- This would mean no correction needed

### Case 3: Both Fender-style (no-corr winning) and High-Gain amps
- Fender: skip P/K (no-correction already optimal)
- Marshall: run P/K (direct B solve might benefit)
- Both tested to confirm behavior

---

## Reference Implementation Checklist

- [ ] Add compression error measurement for winning candidate
- [ ] Implement >1.5 dB gating (only run P/K if needed)
- [ ] Modify acceptance: >50% improvement, <10% spectral regression
- [ ] Modify acceptance: ±2 dB output level tolerance
- [ ] Build and test on Marshall 800
- [ ] Build and test on Fender Deluxe (should skip P/K)
- [ ] Verify no output quieter than baseline
- [ ] Document results (new measurements vs. original)
- [ ] Real-world listening test (if time permits)

---

## Estimated Scope

- Implementation: 1-1.5 hours (add gating + modify acceptance)
- Unit testing: 0.5 hours (build, Marshall/Fender verification)
- Validation: 1-2 hours (compress measurement, spot-check spectral)
- Listening test: 0.5-1 hour (optional but recommended)
- Documentation: 0.5 hours

**Total: 3.5-5 hours**

---

## Success Indicators

### If Phase 3 Succeeds
→ P/K search re-enabled with real, measured improvements
→ No regressions in output level or spectral fidelity
→ Real-world dynamics better with P/K on

### If Phase 3 Struggles
→ Compression error metric doesn't correlate to real improvement
→ Try different threshold (1.0 dB instead of 1.5 dB)
→ Or conclude P/K search genuinely can't improve without trade-offs
→ Document finding and leave disabled

---

## Next Steps After Phase 3

If successful:
→ **Phase 4: Blended NAM Corpus Testing**
- Test on level-dependent tone shapes
- Validate multi-level selection handles hybrid amps
- Document capability for future reference

If unsuccessful:
→ Keep P/K disabled, document why in QUALITY.md
→ Skip Phase 4, consider project stable

---

## Starting Prompt for Next Session

```
Start Priority 3 Phase 3: Re-enable P/K dynamics search with multi-level gating.

Current state: P/K search disabled (makes amps quieter)
Goal: Re-enable with compression-error based gating

Implementation:
1. Measure compression error of winning candidate (across 6 levels)
2. Gate 1: Only run P/K if error > 1.5 dB
3. Gate 2: Only accept if error improves >50% AND spectral <10% regress AND level within ±2dB
4. Test on Marshall 800 (should enable), Fender Deluxe (should skip)
5. Real-world listening validation

See PRIORITY_3_PHASE_3_STARTUP.md for full details.
Estimated time: 3.5-5 hours.
```
