# Priority 2 Implementation Guide

## Overview

Priority 2 refactors Tone Match to separate fitting and selection logic, preventing candidates from winning merely because they fit the fitting stimulus particularly well.

**Status:** Config changes done ✅ | Converter logic: TODO

---

## Problem Statement

**Current behavior:**
1. All Tone Match candidates are fit against fitting reference clip
2. All candidates are selected against the **same** fitting reference clip
3. Any candidate can win if it happens to fit that particular clip well
4. Corrective IR is incorrectly treated as a Tone Match candidate (should be post-processing)

**Desired behavior:**
1. Candidates fit against fitting reference clip
2. Candidates selected against **separate** selection reference clip
3. Selection is based on generalization, not overfitting to fitting stimulus
4. Corrective IR clearly marked as optional post-processing, applied after selection

---

## Changes Completed

### ✅ 1. CloRefineConfig (src/core/clo_refiner.hpp)

Added `selectionReferenceWav` field to `CloRefineConfig`:
- Separate from `referenceWav` (fitting reference)
- When both are populated, fitting uses one, selection uses the other
- When only one is available, falls back to using same clip for both
- When `referenceMode` is Auto/Clean/Moderate/High/Bass, auto-selects second bundled clip from same bucket

---

## Changes Still Needed

### 2. GP-5/GP-50 Tone Match Refactoring (src/core/native_converter.cpp)

**Location:** Lines 2233-2450 (GP-5/GP-50 device-specific Tone Match block)

#### Step 1: Remove Correction-IR candidate (lines 2287-2295)

**Current code:**
```cpp
std::vector<float> candidateIr;
if(computeToneMatchCorrectionIr(gp5PreToneMatchClo,refineStimulusPath,refineTargetWavPath,
                                 candidateIr,gp5Error,status)){
    Model postM=preM;
    if(applyCorrectiveIrToB44(postM.B,candidateIr,0.0,applyErr)){
        const double lossPost=evaluateModelLoss(postM,analysisInput,analysisTarget,44100.0);
        os<<L", correction-IR="<<lossPost;
        if(lossPost<bestLoss){bestLoss=lossPost;gp5ToneMatchIr=candidateIr;...}
    }
}
```

**Action:** Delete this entire block. Correction-IR will be moved to post-processing.

#### Step 2: Resolve selection reference clip (after line 2303, new code)

**Purpose:** Get a separate clip for candidate selection (different from fitting clip)

**Pseudo-code:**
```cpp
// Resolve SELECTION reference clip (separate from fitting reference)
fs::path selectionStimulusPath;
fs::path selectionTargetWavPath;
bool hasSelectionClip = false;

if(!refine.selectionReferenceWav.empty()){
    // User provided explicit selection clip (Custom mode with both clips)
    selectionStimulusPath = work/L"selection_input_wav.wav";
    // Build stimulus from selectionReferenceWav
    // Render through NAM Full
    // Store in selectionTargetWavPath
    hasSelectionClip = true;
}
else if(refine.referenceMode == ToneMatchReferenceMode::Auto ||
        refine.referenceMode == ToneMatchReferenceMode::Clean ||
        refine.referenceMode == ToneMatchReferenceMode::Moderate ||
        refine.referenceMode == ToneMatchReferenceMode::High ||
        refine.referenceMode == ToneMatchReferenceMode::Bass){
    // Auto-selected or explicit named clip: try to find second bundled clip
    fs::path clipsDir = resolveReferenceClipsDir();
    std::wstring bucketPrefix = /* derive from refine.referenceMode or classifyGainBucket */;
    fs::path secondClip = secondClipWithPrefix(clipsDir, bucketPrefix);
    
    if(!secondClip.empty()){
        selectionStimulusPath = work/L"selection_input_wav.wav";
        // Build stimulus from secondClip
        // Render through NAM Full
        // Store in selectionTargetWavPath
        hasSelectionClip = true;
    }
}

// Fallback: if no selection clip available, use fitting reference for both
if(!hasSelectionClip){
    selectionTargetWavPath = refineTargetWavPath;
    // Log: "No separate selection clip available; using fitting reference for both"
}
```

**Key logic:**
- If `selectionReferenceWav` is set: use it
- Else if bundled clip is available: use second bundled clip from same bucket
- Else: fall back to fitting reference (same clip for both)

#### Step 3: Evaluate candidates against selection reference (lines 2284-2327)

**Current logic:**
```cpp
double bestLoss=evaluateModelLoss(preM,analysisInput,analysisTarget,44100.0);
// ... fit candidates ...
if(lossDirect<bestLoss){bestLoss=lossDirect;...}
if(lossMultiLevel<bestLoss){bestLoss=lossMultiLevel;...}
```

**Change required:**
Replace `analysisInput` and `analysisTarget` (fitting reference) with selection reference:
```cpp
// Load selection reference for candidate comparison
std::vector<float> selectionInput, selectionTarget;
if(!loadClipAsMono44100(selectionTargetWavPath, selectionTarget, readErr) ||
   !loadClipAsMono44100(selectionStimulusPath, selectionInput, readErr)){
    // Log warning and fall back to fitting reference
    selectionInput = analysisInput;
    selectionTarget = analysisTarget;
}

double bestLoss=evaluateModelLoss(preM,selectionInput,selectionTarget,44100.0);
// ... fit candidates ...
if(lossDirect<bestLoss){bestLoss=lossDirect;...}
if(lossMultiLevel<bestLoss){bestLoss=lossMultiLevel;...}
```

#### Step 4: Update comments

Update the large comment block (lines 2233-2254) to reflect:
- TWO candidates now (direct B solve, multi-level B solve)
- Correction-IR is NOT a candidate
- Selection is based on separate selection reference (when available)
- Remove references to "three candidate corrections"

Example update:
```cpp
// GP-5/GP-50 device-specific Tone Match: measure and correct the ACTUAL chosen
// 512-tap model's own response against the same target. Two candidate
// corrections are tried -- a direct least-squares solve for B against the
// Tone Match target (solveBlockBLeastSquares) and a direct solve of B
// jointly across a six-level gain sweep (sweepKAndSolveSharedB).
// Candidates are SELECTED based on a separate selection reference clip
// (when available from the same gain bucket), preventing any candidate
// from winning merely because it fits the fitting clip particularly well.
```

#### Step 5: Move Corrective IR application to after Tone Match

**Current location:** Lines 2287-2295 (as candidate comparison)
**New location:** After Tone Match winner is selected (after lines 2416, new block)

**New block structure:**
```cpp
// Optional post-processing: apply Corrective IR after Tone Match selection
// (if enabled). This is NOT part of Tone Match candidate comparison.
if(correction.enabled && !correctiveIr.empty()){
    report(status,L"GP-5/GP-50: applying Corrective IR post-Tone-Match...");
    // Apply correction IR to the winning candidate's B
    // Log the result
}
```

**Documentation update:**
Make clear in comments that Corrective IR is:
- Optional post-processing (user chooses whether to apply)
- Applied AFTER Tone Match candidate selection
- Not part of what determines which candidate wins
- Useful for embedding cabinet IRs or other frequency corrections

---

## Testing & Validation

After implementation, verify:

1. **Code compiles without errors**
   ```bash
   cmake --preset macos-arm64
   cmake --build build-macos --parallel
   ```

2. **CLI runs with Tone Match enabled**
   ```bash
   ./build-macos/namtoclo convert input.nam output_dir --tone-match auto
   ```

3. **Selection clip is used when available**
   - Add logging to verify selection clip path is different from fitting clip
   - Verify "No separate selection clip available" message when fallback occurs

4. **Corrective IR is now post-processing**
   - Verify Corrective IR is applied after Tone Match selection
   - Verify conversion succeeds without Corrective IR (it's optional)

5. **GUI still works**
   ```bash
   scripts/build_macos_app.sh
   open build-macos-app/NamToClo.app
   ```

---

## Documentation Updates Needed

After code changes:

1. **CLAUDE.md:** Update "Tone Match" section to clarify fitting vs. selection clips
2. **QUALITY.md:** Update to note Tone Match now uses separate selection clip
3. **Code comments:** Update large comment block in native_converter.cpp (lines 2233-2254)
4. **Commit message:** Document the separation of fitting/selection/post-processing

---

## Why This Matters

**Before:** Any candidate could win if it happened to fit the fitting clip really well, even if it wouldn't generalize to other clips.

**After:** Candidates are selected based on generalization to a separate clip, preventing overfitting to the fitting stimulus and improving robustness across different playing styles and input levels.

This aligns with benchmark plan section 16 (separate fit/selection/validation material) and improves conversion quality on held-out material.
