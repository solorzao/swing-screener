# Cockpit Glossary + Inline Help — Design (v1)

**Date:** 2026-07-12 · **Branch:** `feat/cockpit-glossary` · **Status:** validated, building

## Problem

The cockpit accreted ~112 jargon terms across 14 screens as it grew. Returning to it
after time away, the builder can't recall what half of them mean or do. We want an
in-app way to re-learn the vocabulary — without creating a *second description of the
system that drifts from the system* (which would reproduce the original problem with a
friendlier face).

Explored via a 13-agent workflow (five UX approaches, adversarial critique). Chosen
scope confirmed with the user:

- **v1 = a Glossary panel + a few inline `HelpTerm` cards.**
- **Behavioral terms link, don't paraphrase** — cite the maintained truth (North Star
  principle # / source file), never re-describe how the code works.
- **Cut:** ambient "explain mode", LLM "ask this screen", guided tours, a hand-kept
  "new since you were away" field (that's `git log`), coverage burndown.

## Anti-staleness (the crux)

A confidently-stale glossary is *worse than none*. Three commitments keep content honest:

1. **One source, never forked.** The 112 terms live in a single JSON file
   (`cockpit-ui/src/lib/glossary.data.json`). TypeScript imports it; a Python test reads
   the same file. There is no second copy to drift.
2. **Behavioral terms link, don't paraphrase.** The ~16 mechanism terms carry a `source`
   citation to durable truth (`North Star #3 · pipeline/disarm.py: ensure_stop_protection`),
   not a hand-written description of the mechanism. A link can't rot; a paraphrase does.
3. **A CI test that citations resolve.** `tests/cockpit/test_glossary.py` fails if a
   `source` cites a North Star principle number that doesn't exist or a file that isn't in
   the tree. Rot becomes a red test, not a silent lie.

Live, per-cell state ("why is *this* pick extended right now") stays on the call-site
lamps where the computing code is. The glossary owns **vocabulary**; call sites own **state**.

## Components

| File | What it is |
|---|---|
| `cockpit-ui/src/lib/glossary.data.json` | **Single source** — 112 `{term,cat,short,why,appears?,source?,risk?}`. |
| `cockpit-ui/src/lib/glossary.ts` | Types (`TermDef`, `TermCategory`), typed import, `CATEGORIES` + label helpers. |
| `cockpit-ui/src/lib/useAnchoredPopover.ts` | **Extracted** from `ProvenancePopover` — portal + `position:fixed` + measure/flip/clamp + Escape/outside/scroll dismissal. Returns `{ ref, pos }`. |
| `cockpit-ui/src/components/HelpTerm.tsx` | A dotted-underlined label → anchored popover (means / why / source). Uses `useAnchoredPopover`. Neutral blue affordance, never green/amber. |
| `cockpit-ui/src/components/GlossaryPanel.tsx` | Instant search + `Segmented` category tabs + term list + `source` citations. Static (no polling). |
| `cockpit-ui/src/screens/ReferenceScreen.tsx` | Adds `GlossaryPanel` as the first panel (screen 10 = the lookup surface). |
| `tests/cockpit/test_glossary.py` | Contract + citation-resolution guard (pytest, import-free file reads). |

`ProvenancePopover` is **refactored onto `useAnchoredPopover`** as a behavior-preserving
move — it stays the regression oracle. `DisarmControl` is untouched (its `.dz-pop` is
CSS-positioned with deliberately different dismissal — not the same machinery).

## Data model (minimal)

```ts
type TermCategory = 'risk-metric'|'status-lamp'|'system-action'|'provenance'|'config'|'strategy'|'screen'
interface TermDef {
  term: string
  cat: TermCategory
  short: string        // plain meaning — the definitional one-liner
  why: string          // why it matters
  appears?: string[]   // where it shows (informational, not load-bearing)
  source?: string      // behavioral terms only: citation to maintained truth
  risk?: 'low'|'medium'|'high'  // rough sort seed only; drives no filter UX
}
```

## Scope decisions (defaults, per the user's greenlight)

- Definitions **imported from the single JSON**, not codegen'd into a `.ts` (one source).
- `source` citations lead with **North Star # + module file** (durable), symbol as hint.
- Citations are **text**, not clickable, in v1 (no "open in what?" in the pywebview shell).

## Deferred to v2 (documented, not lost)

- **Cross-screen "open in glossary" deep-link** from a `HelpTerm` popover (needs App-level
  screen-switch + target state, like the existing `prefillSignalId` hand-off).
- **Wiring `HelpTerm` into the busy masthead live controls** (gate chip, DISARM, cost/facet
  toggles) — retrofitting interactive elements is risky with no JS test runner; do it
  opportunistically when already editing those components. The 100 existing `title=`
  strings stay valid fallbacks.
- v1 proves the inline pattern at low-risk **display-only** sites (e.g. `FacetCaption`).

## Verification

- `cd cockpit-ui && npm run build` (tsc + vite) clean; `npm run lint` (oxlint) clean.
- `pytest tests/cockpit/test_glossary.py` green (contract + citations resolve).
- Drive the real cockpit: open screen 10, search/filter the glossary, click an inline
  `HelpTerm`, confirm the popover positions and cites correctly.
- Rebuild the committed static artifact and commit it (`.gitattributes` pins `static/** -text`).
