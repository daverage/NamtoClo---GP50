# V4.2 historical V4 warm-start compatibility

`ENGINE_V2_RESEARCH_NORTH_STAR.md` remains the governing document.

## Why this exists

The first V4 trainer saved the final `A128`, `pk`, B filters and fit metrics, but it did not save the 24-point `controls_db` vector that generated A128. V4.2's coordinate optimizer works in that control-vector space, so the initial V4.2 warm-start implementation could not reuse historical V4 runs even though the expensive V4 basin search had already been completed.

That forced an unnecessary fallback to the full three-start/eight-round V4 search.

## Compatibility rule

`train_namtoclo_north_star_v42_fast.py` keeps the same warm-start priority:

```text
stored V4.2 controls
stored V4.1 controls
historical V4 A128 + P/K
full V4 multistart only if none are compatible
```

For historical V4 only, the saved A128 magnitude response is sampled at the same logarithmic A-control frequencies used by `controls_to_a()`. Those values form an approximate `controls_db` restart.

The projection is not treated as authoritative coefficients. V4.2 immediately:

1. regenerates A from the projected controls;
2. re-evaluates that A/P-K state on the current canonical T3K five-level NAM teacher evidence;
3. analytically solves B512 again;
4. continues the ordinary V4.2 bracket/refine search.

The stored V4 B512 and stored V4 score are never trusted for fitting.

## What does not change

This is runtime/search-state reuse only. It does not change:

- the NAM teacher;
- the T3K stimulus or five level variants;
- the waveform ESR objective;
- A128/P-K/POST/B512 DSP structure;
- P/K bounds;
- analytic B solving;
- guitar exclusion from fitting/candidate choice;
- output calibration.

The source NAM remains the behavioural authority. The purpose is only to avoid paying again for a basin search we already performed.
