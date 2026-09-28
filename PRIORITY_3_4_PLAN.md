# Priority 3 & 4 Development Plan

**Branch:** `priority-3-4-dev`

---

## Priority 3: Multi-Level Dynamics Metrics & Re-evaluation

### Goal
Measure Tone Match candidate quality across input levels (-24 to +6 dB), not just at one level, to catch failures that affect how the model responds to volume changes.

### What's already in the code
- `buildLevelClips()` — renders NAM at 6 input levels
- `sweepKAndSolveSharedB()` — solves B across multi-level gain sweep
- `gp5LevelClips` structure — holds pre-computed level clips
- P/K search infrastructure — can be re-enabled with better gating

### What needs to be built

#### 3.1 Multi-level Tone Match candidate selection
**Current:** Candidates compared only on fitting stimulus (single level)  
**Desired:** All candidates compared across 6-level sweep

**Work:**
- In `convertNamToClo` GP-5/GP-50 Tone Match section (around line 2350):
  - Build multi-level clips for BOTH fitting and selection references
  - Evaluate each candidate at all 6 levels
  - Score: RMS error + compression-curve error (not just spectral)
  - Only promote candidate if it improves at majority of levels

#### 3.2 Compression-curve metrics
**Current:** Only spectral loss (magnitude response)  
**Desired:** Also track compression accuracy across levels

**Work:**
- New function `computeCompressionError()`:
  - Input: 6 rendered levels from model vs. reference
  - Output: relative error in output levels (dB)
  - Should measure: does model compress the same way as reference?
  
**Example metric:**
```
Reference output levels:    [-24, -18, -12, -6, 0, +6] dB
Model output levels:        [-25, -19, -11, -5, +1, +7] dB
Compression error:          [1, 1, 1, 1, 1, 1] dB (avg = 1 dB)
```

#### 3.3 Re-enable P/K search with better gating
**Current:** `dynamicsAwareFitting` disabled by default (made amps quieter)  
**Desired:** Re-enable but with better success criteria

**Work:**
- Change P/K search from `zero-anchored RMS dynamics error` gate
- New gate: `multi-level compression-curve preservation`
- Only enable P/K search if:
  1. Base candidate has >1.5 dB compression error across levels
  2. P/K search improves it by >50%
  3. Spectral loss doesn't regress >10%
  4. Output level stays within ±2 dB of original

### Success criteria
- Multi-level candidates show measurable improvement on held-out dynamic playing
- P/K search re-enabled and produces audible improvements
- No quieter output (the original failure mode)

---

## Priority 4: Blended/Hybrid NAM Corpus Testing

### Goal
Validate that the converter handles NAMs with level-dependent tone shaping (blended amps that mix high/low distortion paths dynamically).

### What's needed

#### 4.1 Create or find test corpus
**Options:**
1. **Generate synthetic blended NAMs:**
   - Mix existing high/low-gain NAMs at different blend ratios
   - Create level-dependent crossfade versions
   - Test NAMs at blend points: 0%, 25%, 50%, 75%, 100%

2. **Use existing NAMs:**
   - Look for NAMs that already exhibit blend behavior
   - Characterize them as "hybrid" vs. "single-character"

3. **Reference real hardware:**
   - Document real amps with switching/blending (e.g., Marshall with lead/rhythm channels)
   - Create simplified NAM approximations

#### 4.2 Benchmark setup for blended NAMs
**Same as Priority 1 structure:**
- Blended NAM (source)
- Render through Full A2 (ground truth)
- Convert with Tone Match → CLO
- Compare: spectral + compression + held-out audio

**Metrics per NAM:**
- How well does CLO preserve blend point transitions?
- Does compression curve stay stable across blend points?
- Is there tonal discontinuity at blend points?

#### 4.3 Expected findings
**Likely outcomes:**
- Direct B512 solve handles gentle blends well (mostly linear problem)
- Extreme blends (high/low distortion) may expose CLO's fixed P/K limitation
- Multi-level solve may help by averaging across blend points

### Success criteria
- Corpus of 5-10 test blended NAMs characterized
- Benchmark results show how well CLO handles each blend type
- Clear guidance on "this type of NAM works well, this type struggles"

---

## Implementation Order

1. **Phase 1 (P3 setup):** Multi-level candidate selection framework
   - Modify Tone Match to evaluate candidates across 6 levels
   - Implement `computeCompressionError()`
   
2. **Phase 2 (P3 validation):** Test on existing corpus
   - Run benchmark with multi-level metrics
   - Document improvements
   
3. **Phase 3 (P3 refinement):** P/K search re-enablement
   - New gating logic
   - Hardware validation
   
4. **Phase 4 (P4 setup):** Blended NAM corpus
   - Identify or generate test cases
   - Characterize each one
   
5. **Phase 5 (P4 validation):** Blended NAM benchmark
   - Full comparison against baseline
   - Document findings

---

## Estimated scope

- **Priority 3:** 2-3 weeks (new metrics + testing + P/K tuning)
- **Priority 4:** 1-2 weeks (corpus creation + benchmarking)
- **Total:** 3-5 weeks of focused development

---

## Blockers

- **P3:** Need real hardware listening validation for P/K search re-enablement
- **P4:** Need source for blended NAMs or ability to generate them convincingly

---

## Not included (future work)

- Frequency-weighted dynamics error (Priority 3.5)
- Patch/preset rendering at different levels (Priority 3.7)
- Full CLO architecture redesign (Priority 5+)

