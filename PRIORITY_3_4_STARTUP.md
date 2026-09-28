# Priority 3 & 4 Implementation Startup Guide

**Branch:** `priority-3-4-dev` (ready to work on)

---

## Quick Status

✅ **Done:**
- Priority 1 benchmark merged to main (Tone Match improves high-gain by 1-2 dB RMS)
- Priority 2 merged to main (separate fitting/selection clips)
- Priority 3 & 4 plan documented (PRIORITY_3_4_PLAN.md)
- Dev branch created and ready

---

## Phase 1: Multi-Level Candidate Selection (Starting Point)

### What to implement

In `src/core/native_converter.cpp`, lines ~2350-2380 (GP-5/GP-50 Tone Match section):

**Current state:**
```cpp
// Candidates evaluated at ONE level (fitting reference only)
double bestLoss = evaluateModelLoss(preM, analysisInput, analysisTarget, 44100.0);
// ... all candidates compared here
```

**Desired state:**
```cpp
// 1. Build multi-level clips for BOTH fitting and selection references
// 2. Score each candidate across all 6 levels
// 3. Pick candidate that wins majority of levels (or best average)
```

### Files to modify

1. **src/core/clo_refiner.hpp** (if needed)
   - Add `computeCompressionError()` declaration
   - Struct for multi-level metrics

2. **src/core/clo_refiner.cpp** (new function)
   - `computeCompressionError(const std::vector<double>& modelLevels, const std::vector<double>& refLevels) -> double`
   - Returns: RMS error in compression curve (dB)

3. **src/core/native_converter.cpp** (modify existing)
   - Lines 2350-2380: Modify candidate comparison logic
   - Use `gp5LevelClips` (already exists) for multi-level evaluation
   - Score: spectral loss + compression error

### Key insight

The code ALREADY HAS multi-level clip building (`buildLevelClips`) and multi-level solving (`sweepKAndSolveSharedB`). The missing piece is:
- Using those clips to SELECT the winner
- Currently we only build them for P/K search
- We need to build them ALWAYS and score candidates on them

### Success criteria for Phase 1

- [ ] Candidates evaluated on 6-level sweep (not just 1 level)
- [ ] Compression error metric computed
- [ ] Candidate that wins most levels is selected
- [ ] Build compiles without errors
- [ ] Test on existing NAM corpus shows improvement

---

## Phase 2 (After Phase 1)

Re-benchmark with Priority 2 + Phase 1 changes:
- Use separate selection clip (Priority 2)
- Evaluate on 6 levels (Phase 1)
- Measure: does holding out dynamics improve held-out listening?

---

## Resources

- **PRIORITY_3_4_PLAN.md** — Full plan with all phases
- **PRIORITY_2_IMPLEMENTATION.md** — Reference for how to add new logic
- **src/core/clo_refiner.cpp** — See `buildLevelClips()` and `sweepKAndSolveSharedB()` for examples

---

## Next session

Start with Phase 1 implementation in `native_converter.cpp` around line 2350.

Good starting prompt:
> "Implement Phase 1 of Priority 3: modify Tone Match candidate selection to evaluate candidates across 6-level sweep, not just fitting stimulus. Use gp5LevelClips that's already being built. See PRIORITY_3_4_PLAN.md Phase 1 section for details."

