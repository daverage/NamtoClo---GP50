# Priority 3 Phase 1: Complete ✅

**Date:** September 28, 2026  
**Status:** Implemented and tested successfully

---

## What was implemented

Multi-level candidate selection for Tone Match refinement. Previously, candidates were evaluated only at the fitting stimulus level (single operating point). Now they're evaluated across all 6 input levels (-24 to +6 dB) to ensure robustness across the full dynamic range.

### Code changes

1. **clo_refiner.hpp/cpp**
   - Added `computeCompressionError()` function to measure compression curve accuracy
   - Computes RMS error across multiple output levels

2. **native_converter.cpp** (~70 lines added)
   - Build multi-level clips (gp5LevelClips) always, using default stimulus if reference unavailable
   - Evaluate all three candidates at each of 6 levels:
     - No-correction (baseline model)
     - Direct B solve
     - Multi-level B solve (when available)
   - Track wins per candidate per level
   - Select candidate that wins majority of levels
   - Report multi-level wins in status output: `[multi-level: no-correction=X, direct=X, ml=X wins]`

---

## Testing results

### Marshall JCM 800 (moderate/high-gain)
```
loss before=1.67543, direct B solve=0.639953
[multi-level: no-correction=0, direct=6, ml=0 wins]
```
- Direct B solve wins all 6 levels ✅
- 62% loss improvement (1.67543 → 0.639953)

### Vox AC30 (clean)
```
loss before=1.37769, direct B solve=0.180604
[multi-level: no-correction=0, direct=6, ml=0 wins]
```
- Direct B solve wins all 6 levels ✅
- 87% loss improvement (1.37769 → 0.180604)

---

## Key findings

1. **Direct B solve is universally robust** — wins across entire gain range on both amps, not just at fitting stimulus level

2. **Multi-level solve doesn't improve on direct** — direct B solve is sufficient, multi-level doesn't add value in these cases

3. **No-correction never wins** — baseline model is consistently outperformed at all levels

4. **Phase 1 complete** — system now validates candidates across full dynamic range before selection

---

## Next steps

### Phase 2 (Phase 1 validation)
- Run comprehensive benchmark using multi-level evaluation
- Compare before/after with held-out dynamic playing content
- Document improvements in PRIORITY_1_BENCHMARK.md

### Phase 3 (P/K search re-enablement)
- Use multi-level metrics to gate P/K dynamics search
- Set better success criteria (compression curve preservation, not just dynamics RMS)
- Only enable when base candidate has >1.5 dB compression error

### Phase 4 (Blended NAM corpus)
- Create test corpus of level-dependent tone shapes
- Validate multi-level selection on these challenging cases
- Document how well CLO handles blend transitions

---

## Files modified

- `src/core/clo_refiner.hpp` — added function declaration
- `src/core/clo_refiner.cpp` — added implementation (~10 lines)
- `src/core/native_converter.cpp` — added multi-level evaluation logic (~70 lines)

---

## Build status

✅ Builds without errors on macOS  
✅ CLI runs successfully  
✅ macOS SwiftUI app compiles and launches  
✅ Real-world testing validates output

---

## Commit

Hash: `65bb397`  
Message: "Priority 3 Phase 1: Implement multi-level candidate selection for Tone Match"

Includes commit for real-world testing validation.
