/* Lamp — the ONE swatch + screen-reader dual-render primitive: an aria-hidden
   colored swatch (shape redundancy lives in the .plamp-* CSS — green filled
   circle / amber triangle / red square / dashed hollow, never hue alone) beside
   a .vh span carrying the same text to a screen reader, so a lamp is never
   title-only silence. UNKNOWN is never green.

   Consumers (one definition, not a copy per surface): PositionLamp (the
   positions table + Zone B risk strip), PickCard's ActionabilityLamp (the
   Candidates / Zone D picks), and Task 18's TierChip (forward_confirmed green /
   replay_screened amber / hunch gray) composes this too — extend LampColor +
   add the matching .plamp-* class rather than re-implementing the pattern. */

export type LampColor = 'green' | 'yellow' | 'red' | 'gray' | 'unknown'

export function Lamp({ color, title }: { color: LampColor; title: string }) {
  return (
    <>
      <span className={`plamp plamp-${color}`} title={title} aria-hidden="true" />
      <span className="vh">{title}</span>
    </>
  )
}
