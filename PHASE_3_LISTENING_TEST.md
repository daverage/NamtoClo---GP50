# Phase 3 Real-World Listening Test Protocol

**Goal:** Validate P/K search with compression-error gating doesn't make amps quieter.

## Problem Statement

Original P/K search failed: improved metrics but **made amps sound quieter on real material**. Phase 3's gating adds ±2 dB level check, but real-world listening is the only ground truth.

## Test Protocol

### Enable P/K for Testing

To enable Phase 3 P/K search, modify `src/core/native_converter.cpp` line 2441:

```cpp
// Change from:
if(trainer.dynamicsAwareFitting&&!gp5LevelClips.empty()){

// To:
if(true&&!gp5LevelClips.empty()){  // Force P/K enabled for testing
```

Then rebuild:
```bash
cd /Users/andrzejmarczewski/Documents/GitHub/NamtoClo---GP50
rm -rf build-macos
cmake --preset macos-arm64
cmake --build build-macos --parallel
bash scripts/build_macos_app.sh
```

### Test Material

Use real guitar playing:
1. **Dynamic sweep:** Quarter notes, volume increasing from barely audible to full
2. **Riff:** Consistent-level playing, various articulations
3. **Quiet passage:** Soft picking to detect if quieter

### Test Amps

**Marshall 800** (should enable P/K):
```bash
namtoclo convert /path/to/Marshall_800_cg.nam --output /tmp/p3_on --tone-match
namtoclo convert /path/to/Marshall_800_cg.nam --output /tmp/p3_off --tone-match
# First has P/K enabled, second (after disabling in code) without
```

**Fender Deluxe** (should skip P/K):
```bash
namtoclo convert /path/to/Fender_Deluxe_Reverb_Head_2.nam --output /tmp/fender --tone-match
# Should see "compression error < 1.5dB" message, P/K skipped
```

### Listening Checklist

For Marshall 800 P/K on vs off:

**Volume (CRITICAL):**
- [ ] Same level or louder with P/K on
- [ ] Not quieter at any input level
- [ ] Consistent across dynamic range

**Dynamics:**
- [ ] Feels more consistent (not compressed or pumping)
- [ ] Clearer dynamic response
- [ ] No loss of pick dynamics

**Tone:**
- [ ] Clear and open (no phase issues)
- [ ] No presence/treble loss
- [ ] Natural compression feel

**Pass/Fail:**
- ✅ **PASS** if: same/louder volume AND better/same dynamics AND clear tone
- ❌ **FAIL** if: any quieter OR dynamics worse OR tone thin/phased

## Expected Results

**If P/K works correctly:**
- Marshall 800: P/K on should have better dynamics, same/louder level
- Fender Deluxe: P/K skipped (compression error <1.5 dB), same as Phase 2

**If P/K fails (original problem repeats):**
- Marshall 800: P/K on sounds quieter OR dynamics feel worse
- Disable P/K again, keep as research code

## Success Criteria

Phase 3 passes real-world validation ONLY if:
- ✅ P/K improves dynamics tracking
- ✅ NO quieter output (critical)
- ✅ NO tone quality loss
- ✅ Fender correctly skips P/K

## Next Steps

1. Modify native_converter.cpp line 2441 to force P/K enabled
2. Rebuild macOS app
3. Convert Marshall 800 & Fender Deluxe
4. Listen to CLO files with test audio
5. Check all items above
6. Document: PASS or FAIL
7. If FAIL: disable P/K again (it's in code, just disabled)
8. If PASS: consider shipping Phase 3

## Notes

- Don't modify for production yet (P/K disabled by default is correct)
- This is research/validation phase only
- If any doubt, disable P/K (safe conservative choice)
- Trust real listening over metrics
