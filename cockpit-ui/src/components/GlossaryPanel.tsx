import { useMemo, useState } from 'react'
import { CATEGORIES, GLOSSARY } from '../lib/glossary'
import type { TermCategory, TermDef } from '../lib/glossary'
import { Segmented } from './Segmented'

/* GlossaryPanel — the browse-everything vocabulary surface (screen 10). Instant
   search + category tabs over the single-source glossary. STATIC: the data is a
   bundled import, so there is no polling, no debounce, and no PanelBody — it can
   never be stale-from-a-fetch. Behavioral terms show their `source` citation
   (the maintained truth they point at) instead of a mechanism paraphrase. */

type CatFilter = 'all' | TermCategory

export function GlossaryPanel() {
  const [q, setQ] = useState('')
  const [cat, setCat] = useState<CatFilter>('all')
  const query = q.trim().toLowerCase()

  const rows = useMemo(
    () =>
      GLOSSARY.filter(
        (t) =>
          (cat === 'all' || t.cat === cat) &&
          (query === '' || `${t.term} ${t.short} ${t.why}`.toLowerCase().includes(query)),
      ),
    [cat, query],
  )

  // Tabs: ALL plus each category actually present, with a live count. Built once
  // (the data is static), so this never recomputes.
  const options = useMemo(() => {
    const counts = new Map<TermCategory, number>()
    for (const t of GLOSSARY) counts.set(t.cat, (counts.get(t.cat) ?? 0) + 1)
    return [
      { value: 'all' as CatFilter, label: `ALL ${GLOSSARY.length}` },
      ...CATEGORIES.filter((c) => counts.has(c.key)).map((c) => ({
        value: c.key as CatFilter,
        label: `${c.label} ${counts.get(c.key) ?? 0}`,
      })),
    ]
  }, [])

  return (
    <section className="panel">
      <div className="panel-head">
        GLOSSARY
        <span className="panel-caption">
          your cockpit&rsquo;s vocabulary, decoded &middot; {GLOSSARY.length} terms
        </span>
      </div>
      <div className="gl-toolbar">
        <input
          className="gl-search mono"
          value={q}
          maxLength={48}
          placeholder="search terms…"
          aria-label="search glossary"
          onChange={(e) => setQ(e.target.value)}
        />
        <Segmented className="gl-cats" options={options} value={cat} onChange={setCat} title="category" />
      </div>
      <div className="gl-count">
        {rows.length} {rows.length === 1 ? 'term' : 'terms'}
      </div>
      {rows.length === 0 ? (
        <div className="panel-wait">no terms match</div>
      ) : (
        <div className="gl-list">
          {rows.map((t) => (
            <GlossaryRow key={t.term} t={t} />
          ))}
        </div>
      )}
    </section>
  )
}

function GlossaryRow({ t }: { t: TermDef }) {
  return (
    <div className="gl-row">
      <div className="gl-row-head">
        <span className="gl-term">{t.term}</span>
        <span className="gl-cat">{t.cat}</span>
      </div>
      <div className="gl-means">{t.short}</div>
      <div className="gl-why">{t.why}</div>
      {t.source !== undefined && <div className="gl-src">{t.source}</div>}
      {t.appears !== undefined && (
        <div className="gl-appears">
          {t.appears.map((a) => (
            <span key={a} className="gl-achip">
              {a}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}
