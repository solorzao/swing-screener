import type { ScoreboardCard, ScoreboardCombined } from '../lib/api'
import { dashOr, fmtCountPct, fmtSignedUsd } from '../lib/fmt'
import { Lamp } from './Lamp'
import type { LampColor } from './Lamp'
import { StatChip } from './StatChip'

/* One Metrics-scoreboard tile — the R / $ / honest-empty faces of a single book
   (or the combined real-money pool). It reads ONLY the fields shared by
   ScoreboardCard and ScoreboardCombined, so a per-book card and the cross-book
   aggregate render through the same component; the parent (MetricsScreen, Task 8)
   owns the book→title/caption mapping — never this tile.

   StatChip stays the ONLY R renderer (design rule 1): an R book WITH closes shows
   its expectancy Stat + CI track (StatChip self-handles the n<5 "no read" and 5–11
   THIN faces); a $-only book shows formatted realized dollars with no chip; an empty
   book (n_closed === 0), or the shouldn't-happen R-book-with-null-expectancy, shows a
   muted em dash — an honest-empty face, never a fabricated zero. No per-tile equity
   curve here: the combined curve is a MetricsScreen footer (design decision). */

export function BookMetricCard({
  card,
  title,
  caption,
}: {
  card: ScoreboardCard | ScoreboardCombined
  title: string
  caption?: string
}) {
  const empty = card.n_closed === 0
  // An R book whose expectancy is null despite closes shouldn't occur, but the
  // union types it Stat | null — treat it as the honest-empty face too.
  const noRead = empty || (card.unit === 'R' && card.expectancy === null)

  // The status dot is a display cue, not a claim: gray for an empty/unknown book,
  // else the headline sign — expectancy drives an R book, realized dollars a $
  // book. Exactly flat reads yellow (neither win nor loss to assert).
  const headline =
    card.unit === '$' ? card.realized_usd ?? 0 : card.expectancy?.value ?? 0
  let lamp: LampColor
  let lampTitle: string
  if (noRead) {
    lamp = 'gray'
    lampTitle = 'no closed trades'
  } else if (headline > 0) {
    lamp = 'green'
    lampTitle = 'net positive'
  } else if (headline < 0) {
    lamp = 'red'
    lampTitle = 'net negative'
  } else {
    lamp = 'yellow'
    lampTitle = 'flat'
  }

  return (
    <div className="bmc">
      <div className="bmc-head">
        <span className="bmc-title">{title}</span>
        <span className="bmc-lamp">
          <Lamp color={lamp} title={lampTitle} />
        </span>
        <span className="bmc-unit">{card.unit}</span>
      </div>

      <div className="bmc-value">
        {noRead ? (
          <span className="bmc-empty" title="no closed trades">
            —
          </span>
        ) : card.unit === 'R' && card.expectancy !== null ? (
          <StatChip stat={card.expectancy} />
        ) : (
          <span
            className={`bmc-usd${(card.realized_usd ?? 0) < 0 ? ' neg' : ''}`}
          >
            {dashOr(card.realized_usd, fmtSignedUsd)}
          </span>
        )}
      </div>

      {!noRead && (
        <div className="bmc-sub">
          win {fmtCountPct(card.win_rate)} · {card.n_wins}W–{card.n_losses}L
          {card.profit_factor !== null
            ? ` · PF ${card.profit_factor.toFixed(2)}`
            : card.unit === 'R' && card.n_wins > 0
              ? ' · PF ∞' // all-winner book — parity with PerformancePanel's ∞
              : ''}
        </div>
      )}

      {!noRead && card.unit === 'R' && card.realized_usd !== null && (
        <div className="bmc-realized">realized {fmtSignedUsd(card.realized_usd)}</div>
      )}

      {caption !== undefined && <div className="bmc-caption">{caption}</div>}
    </div>
  )
}
