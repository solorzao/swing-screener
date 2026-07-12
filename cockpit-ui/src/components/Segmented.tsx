import type { ReactNode } from 'react'

/* The one segmented control (masthead + panel heads): buttons in a bordered
   span, selection blue-tinted and never green (.seg / .seg-on in index.css).
   Extracted from the five hand-rolled Phase-2 instances — same markup shape,
   same class semantics, zero visual drift. Disabled options keep the Phase-2
   "visible commitments, dead controls beat absent ones" treatment: rendered,
   labeled with an honest title, unclickable. */

export type SegmentedOption<T extends string> = {
  value: T
  /** Button text; the value itself when omitted (the breakdown-tabs case). */
  label?: ReactNode
  /** Per-option hover title (facet definitions, the cost-haircut caption). */
  title?: string
  disabled?: boolean
}

export function Segmented<T extends string>({
  options,
  value,
  onChange,
  title,
  className,
}: {
  options: readonly SegmentedOption<T>[]
  value: T
  onChange: (value: T) => void
  /** Group-level hover title (e.g. "play type"). */
  title?: string
  /** Extra classes on the wrapping span (e.g. "perf-tabs"). */
  className?: string
}) {
  return (
    <span className={className === undefined ? 'seg' : `seg ${className}`} title={title}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          className={o.value === value ? 'seg-on' : undefined}
          aria-pressed={o.value === value}
          title={o.title}
          disabled={o.disabled}
          onClick={() => onChange(o.value)}
        >
          {o.label ?? o.value}
        </button>
      ))}
    </span>
  )
}
