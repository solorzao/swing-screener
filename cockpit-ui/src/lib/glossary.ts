import raw from './glossary.data.json'

/* The cockpit glossary — the ONE source of vocabulary truth. The 112 terms live
   in glossary.data.json (imported here, read by tests/cockpit/test_glossary.py);
   there is no second, forkable copy. Behavioral terms carry a `source` citation to
   the maintained truth that governs them (a North Star principle # and/or a source
   file) rather than a paraphrase of the mechanism — a link can't rot, a paraphrase
   does, and test_glossary.py fails if a citation stops resolving. */

export type TermCategory =
  | 'risk-metric'
  | 'status-lamp'
  | 'system-action'
  | 'provenance'
  | 'config'
  | 'strategy'
  | 'screen'

export interface TermDef {
  term: string
  cat: TermCategory
  /** Plain meaning — the definitional one-liner (no jargon). */
  short: string
  /** What the user actually decides from it. */
  why: string
  /** Where it shows in the UI (informational; drives no logic). */
  appears?: string[]
  /** Behavioral terms only: a citation to maintained truth, never a mechanism
   * paraphrase (e.g. "North Star #3 (kill switch) · pipeline/disarm.py"). */
  source?: string
  /** A rough authoring-time confusion estimate — a sort seed only, never a filter. */
  risk?: 'low' | 'medium' | 'high'
}

export const GLOSSARY: TermDef[] = raw as TermDef[]

/** Category display order + short tab labels (matches the app's dense uppercase
 * idiom). Only categories actually present in the data render as tabs. */
export const CATEGORIES: { key: TermCategory; label: string }[] = [
  { key: 'risk-metric', label: 'RISK' },
  { key: 'status-lamp', label: 'LAMP' },
  { key: 'system-action', label: 'ACTION' },
  { key: 'provenance', label: 'PROV' },
  { key: 'config', label: 'CONFIG' },
  { key: 'strategy', label: 'STRAT' },
  { key: 'screen', label: 'SCREEN' },
]

/** Look up a term for an inline HelpTerm: exact match first, then the first term
 * whose name contains the key (so `<HelpTerm term="gold">` finds
 * "Facet: gold / research / would_surface"). Undefined = not in the glossary, so
 * HelpTerm renders plain text rather than a dead affordance. */
export function lookupTerm(key: string): TermDef | undefined {
  const k = key.toLowerCase()
  return (
    GLOSSARY.find((t) => t.term.toLowerCase() === k) ??
    GLOSSARY.find((t) => t.term.toLowerCase().includes(k))
  )
}
