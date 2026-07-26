from dataclasses import dataclass, field


@dataclass(frozen=True)
class StrategyConfig:
    ema_fast: int = 20
    ema_slow: int = 50
    atr_period: int = 14
    rsi_period: int = 14
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9

    # HA classification
    wick_frac: float = 0.05        # "no wick" tolerance as fraction of range
    zone_body_frac: float = 0.30   # doji/zone: body <= frac * range

    # pullback
    min_pullback_bars: int = 1
    max_pullback_bars: int = 4

    # entry zone / risk
    ceiling_atr_mult: float = 0.35   # ceiling = trigger_close + mult * ATR
    floor_buffer_atr: float = 0.10   # floor = swing_low + buffer * ATR
    stop_buffer_atr: float = 0.25    # stop  = swing_low - buffer * ATR

    # freshness / anti-chase (continuation): reject a trigger whose close already sits
    # more than this many ATR above the fast EMA -- the move has "already run", so the
    # entry zone would be a chase. Empirically, fresh AMD pullback triggers fire at
    # < 1 ATR of extension while a late chase fires at ~2.7 ATR. 0 disables the gate.
    max_extension_atr: float = 2.0

    # entry-trigger kind (experiment). "ha_flip" = the incumbent bullish Heiken-Ashi flip;
    # "outside_bar" = a raw-candle bullish outside/engulfing bar at the trigger (its range
    # breaks BOTH the prior bar's high and low AND it closes up). A raw-price commitment
    # signal vs the smoothed HA flip. DETECTION-only (computed from the shared frame, no new
    # indicator), so it is a legal screen variant.
    trigger_kind: str = "ha_flip"
    # entry-depth gate (experiment). When True, require the pullback to have reached into the
    # EMA20-EMA50 band (swing_low <= ema_fast), not merely stayed above ema_slow -- i.e. price
    # actually pulled back to value rather than barely dipping while still extended. Off = the
    # incumbent behavior. DETECTION-only, so it is a legal screen variant.
    require_band_touch: bool = False
    # Breakout-confirmation trigger window (experiment, 2026-07 queue). 0 = the incumbent
    # trigger (fire ON the bullish flip bar out of the pullback). N >= 1 = fire instead on
    # the FIRST close above the flip bar's high within N bars of the flip -- the
    # continuation analog of reversal_confirm_window (whose late-confirm cohort graded
    # +0.110R): pullback/uptrend/quality structure is measured AS-OF the flip bar, the
    # trigger price/extension at the confirmation bar. A strictly different entry-timing
    # cohort, single-fire by construction. DETECTION-only -> a legal screen variant.
    cont_confirm_window: int = 0

    # --- continuation edge tournament, round 1 (Tier-A quality gates) -------------------
    # All DETECTION-only screen-variant levers, default no-op (0/False). Each targets the
    # diagnosed entry-fade leak by rejecting a low-quality slice of triggers. Computed from
    # the existing enriched frame (no new indicator periods). See
    # docs/plans/2026-06-25-continuation-edge-tournament-design.md.
    vol_thrust_min: float = 0.0        # reject if trigger volume / mean(volume, vol_avg_window) < this
    vol_avg_window: int = 20           # rolling window for the volume baseline (excludes the trigger bar)
    # Thrust-denominator A/B (2026-07 queue): the baseline above INCLUDES the pullback's
    # own dried-up volume, flattering thrust ratios on longer pullbacks (and doubly so
    # when combined with the dry-up gate, whose baseline EXCLUDES it). True measures the
    # baseline over the vol_avg_window bars BEFORE the pullback, matching the dry-up gate.
    vol_thrust_excl_pullback: bool = False
    min_ema_sep_atr: float = 0.0       # reject if (ema_fast - ema_slow) / atr < this (flat-trend filter)
    require_macd_hook: bool = False    # reject unless macd_hist > 0 AND macd_hist_rising
    rsi_min_trigger: float = 0.0       # reject if trigger rsi < this (bull-range floor)
    min_atr_pct: float = 0.0           # reject if atr / close < this (drops low-vol names -- worst cohort)
    min_trigger_body_frac: float = 0.0   # reject if trigger HA body_frac < this (weak-flip filter)
    max_trigger_lower_wick_frac: float = 0.15  # ... unless shaved_bottom or lower wick <= this
    require_value_band: bool = False   # pullback low must reach the EMA20 band (tol) AND hold above EMA50 (buf)
    band_touch_tol_atr: float = 0.10   # low may sit up to this many ATR above EMA20 and still count as a touch
    band_floor_buf_atr: float = 0.25   # low must stay at least this many ATR above EMA50
    require_orderly_pullback: bool = False  # reject violent pullbacks (a big single bar or a deep total drop)
    max_pullback_bar_atr: float = 1.5  # no single pullback bar's range may exceed this many ATR
    max_pullback_drop_atr: float = 2.5  # the whole pullback drop may not exceed this many ATR

    # --- edge-discovery wave 1 (volume footprint) --------------------------------------
    # volume DRY-UP: reject unless the pullback bars' mean volume <= this fraction of the
    # pre-pullback baseline (mean over vol_avg_window bars before the pullback). The orthogonal
    # other half of the volume thrust -- supply must exhaust on the dip, not just demand return.
    pullback_vol_dryup_max: float = 0.0   # 0 = off; e.g. 0.85 = pullback at <=85% of baseline
    # POCKET PIVOT: a self-normalizing thrust -- the up trigger bar's volume must EXCEED the
    # largest down-day volume of the prior pocket_pivot_lookback bars (demand > worst supply).
    require_pocket_pivot: bool = False
    pocket_pivot_lookback: int = 10

    # staleness / cooldown: drop a digest pick once its setup has been on the list for
    # more than this many days (by first_seen_date) so the same play isn't re-pitched
    # day after day. A freshly-appearing setup (first_seen == run_date) always shows.
    # None disables the cooldown; legacy rows with no first_seen_date are never dropped.
    digest_repeat_cooldown_days: int | None = 1

    # already-ran filter: re-check each digest pick's entry zone against the latest close
    # at send time and drop picks that have run past the entry ceiling ("extended") or broken
    # the stop ("broken"). The screen runs the prior evening, so a pick can leave its zone
    # overnight; this keeps the email to what is still tradable (mirrors the dashboard's
    # "hide already ran" filter). True = on; fail-open if live quotes are unavailable.
    digest_drop_already_ran: bool = True

    # diversity cap: in the DAILY digest keep at most this many picks per GICS sector, so one
    # hot sector can't fill every slot. Reads Universe.sector (yfinance-populated); a pick
    # with no known sector is never capped (fail-open). None disables the cap.
    daily_max_per_sector: int | None = 2

    # structure-aware continuation target (Step A)
    target_lookback: int = 30        # bars to search for overhead resistance
    target_pivot_width: int = 2      # a swing high tops this many bars on each side
    target_atr_mult: float = 2.0     # measured-move fallback when no resistance overhead
    min_target_r: float = 1.5        # floor: target is at least this many R above the reference

    # categorization tag thresholds (tunable for the dry-run)
    penny_price_max: float = 5.0            # price < this -> "penny"
    speculative_dollar_vol_max: float = 5e6   # avg $vol < this -> "speculative"
    mid_dollar_vol_max: float = 50e6        # avg $vol < this -> "mid", else "reputable"
    low_vol_atr_pct_max: float = 0.02       # atr% < this -> "low"
    med_vol_atr_pct_max: float = 0.05       # atr% < this -> "med", else "high"
    oversold_rsi_max: float = 35.0          # rsi < this -> oversold
    avg_dollar_vol_window: int = 20         # bars used for avg dollar volume

    # reversal screener (Heiken-Ashi reversal off a downtrend -> the "Reversal Plays" list).
    # HA structure is the gate; RSI is only a scoring confirmation (NOT a hard filter).
    reversal_min_bearish_bars: int = 3        # HA red bars required in the decline (downtrend gate)
    reversal_oversold_rsi_max: float = 25.0   # RSI scoring floor: how deep is "deeply oversold"
    reversal_oversold_lookback: int = 5       # bars to look back when measuring oversold depth
    reversal_decline_bars: int = 6            # window for the decline low (the bounce origin)
    reversal_target_lookback: int = 30        # bars for the prior decline high (the target/breakdown)
    # Reversal entry is a PULLBACK into the bounce (a limit below the chase price), so
    # there's room to both stop and target. The zone is this retracement band of the
    # bounce (reversal_low -> bounce_high):
    reversal_pullback_shallow: float = 0.382  # shallow end of the pullback entry zone
    reversal_pullback_deep: float = 0.618     # deep end of the pullback entry zone
    # Target = this retracement of the prior decline. DEFAULT 1.0 (the full breakdown
    # level) since the 2026-07-03 target-geometry sweep: same-sample across ~44k closed
    # fills the gradient is MONOTONE (0.5 -> -0.027R, 0.618 -> -0.001R, 0.786 -> +0.024R,
    # 1.0 -> +0.043R full book at 0.05 ATR slippage; confirmed cohort +0.057R, corrected
    # low +0.029) -- the old 0.786 capped winners early. At 0.10 slippage the 1.0 bound
    # is -0.002 (the same fragility profile the shipped confirmed+no-flip edge had). The
    # legacy 0.786 stays measured forward as the `rev_retrace786` screen variant.
    reversal_retrace_frac: float = 1.0
    reversal_target_r_multiple: float = 1.0   # measured-move fallback if the target sits below entry
    # edge-discovery wave 1: A/B the reversal flip-bar volume sign. The scorer rewards HIGH flip
    # volume, but Wyckoff/MR theory says the confirming test should be LOW volume (supply gone).
    # These gates isolate each cohort to settle it empirically (0 = off).
    reversal_max_flip_rvol: float = 0.0   # low-vol confirmation: reject if flip volume_ratio > this
    reversal_min_flip_rvol: float = 0.0   # high-vol confirmation: reject if flip volume_ratio < this
    # VIX-rank regime gate (edge-discovery exp 15): suppress REVERSAL fills when the
    # point-in-time VIX percentile-rank (trailing 252d) exceeds this (panic states where
    # oversold keeps falling). 0 = off. Needs ^VIX daily threaded into the replay (vix_daily).
    max_vix_rank: float = 0.0
    # relative-strength-vs-SPY leadership gate for the reversal book (edge-discovery wave 2):
    # require the RS line (close/spy_close, added to the frame when SPY is provided) to be above
    # its own MA at the bounce -- buy oversold names HOLDING UP vs the index, not the weakest
    # laggards. 0/off when no rs column. Needs spy_close threaded into build_frame.
    require_rs_leader: bool = False
    rs_ma_window: int = 21
    # --- rotation-capture entry mechanics (2026-07: the Jul 1-2 software rotation was
    # detected -- CRM/WDAY/INTU/PTC all fired -- but was structurally untradeable: EARLY
    # hidden on the thrust day, CONFIRMED born with price above its own pullback ceiling,
    # and a V-move never retraces 38.2% to fill the resting limit. Three default-off knobs
    # let replay variants A/B the entry mechanics; the defaults reproduce legacy exactly. ---
    # A flip may confirm up to this many bars after the flip bar: the trailing HA-green run
    # must start green-out-of-red and today's close is the FIRST close above the flip bar's
    # high. 1 = the legacy next-bar-only confirmation. For late confirms (>1 bar after the
    # flip) the decline gates are measured as-of the FLIP bar, where the downtrend claim
    # belongs (green recovery bars otherwise erode the red-run count).
    # DEFAULT 3 (replay-screened 2026-07-03, 511 names / 5y): the late-confirm increment --
    # flips that pause a bar or two before confirming, permanently invisible at window=1 --
    # graded +0.110R (clustered 95%low +0.065) net of 0.05 ATR slippage and HELD at 0.10
    # (+0.079R, low +0.034), while the legacy-only confirmed book no longer clears the bar
    # (+0.004R at 0.05, negative at 0.10). The legacy rule stays measured forward as the
    # `rev_confirm1` screen variant. See docs/plans/2026-07-03-reversal-rotation-capture.md.
    reversal_confirm_window: int = 3
    # Anchor the CONFIRMED entry band on the top of the bounce-so-far (max high of the
    # trailing green run) instead of the flip bar's high. The flip anchor leaves every
    # confirmed signal born above its own ceiling by construction (confirmation REQUIRES
    # a close above the flip high while the ceiling sits 38.2% below it).
    reversal_anchor_confirmation: bool = False
    # Entry ceiling at the trigger close -- a "buy at yesterday's close or better" resting
    # limit -- instead of the shallow-retracement band ceiling. The floor keeps the
    # deep-retrace bound. Catches V-shaped bounces that never pull back into the band.
    reversal_ceiling_at_close: bool = False

    # Reversal fill window: how many completed bars a reversal's resting-limit entry may
    # wait to fill before expiring to "missed" (the stop breaking first invalidates it).
    # 1 = the legacy next-bar-only fill, which the 2026-07 audit found left ~95% of
    # confirmed reversals unfillable (the zone needs a >38% bounce retrace on the very
    # next bar) and adversely selected the rest (live -0.86R vs replay +0.125R). A real
    # trader's limit order rests; the shadow book now measures that. Continuation is
    # unaffected (its zones fill on the next bar ~97% of the time).
    reversal_fill_window_bars: int = 5
    # Wyckoff spring trigger (edge-discovery exp 11): require the bounce bar to UNDERCUT a recent
    # support low (over the lookback window ending spring_gap bars back) then CLOSE back above it
    # -- a shakeout. 0/off. NOTE: our reversal flip bar opens near the decline bottom and closes
    # up, so most reversals already qualify -- expect this to be near-no-op.
    require_spring: bool = False
    spring_lookback: int = 15
    spring_gap: int = 3

    # Reversal score weights, lifted from inline literals (2026-07-03) so the replay
    # machinery can sweep them: the score is the ONLY ordering behind the digest's top-N
    # reversal picks. The LEGACY vector (0.35 bounce / 0.20 downtrend / 0.15 volume /
    # 0.15 confirmed / 0.15 depth, no lag term) graded NON-PREDICTIVE on the fixed
    # confirmed book -- flat quintile ladder, top-quintile-vs-rest clustered delta bound
    # -0.059 -- as did every individual component. The only ordering that separates is
    # CONFIRMATION LAG (how many bars after the flip the confirmation printed; the
    # ranking-space echo of the late-confirm edge) plus flip volume. This DEFAULT vector
    # was validated on the same book: top-quintile +0.097R (own bound +0.043), q5-vs-rest
    # delta bound +0.011, holds at 0.10 slippage (+0.017), and posts the best per-day
    # top-5 simulation of every key tested (+0.073R, bound +0.019). Weights are
    # normalized by their sum, so only the RATIOS matter. NOTE: score semantics changed
    # here -- edge/reversal.md score-band history before 2026-07-03 measures the old
    # definition. See docs/plans/2026-07-03-reversal-score-overhaul.md.
    reversal_score_w_lag: float = 0.40         # confirmation lag / confirm window (late = better)
    reversal_score_w_volume: float = 0.30      # flip-bar volume vs average
    reversal_score_w_bounce: float = 0.10      # HA flip-bar quality (body + shaved bottom)
    reversal_score_w_downtrend: float = 0.10   # red-run depth of the decline
    reversal_score_w_confirmed: float = 0.10   # confirmed follow-through (orders above EARLY)
    reversal_score_w_depth: float = 0.0        # oversold RSI depth: no predictive power found

    # --- item 1: tiered reversal surfacing + conviction sizing -------------------------
    # conviction tier (reversal_conviction_tier): premium = high-vol bounce AND spring (the
    # additive +0.25R edge); strong = any single conviction; base = none.
    reversal_premium_min_rvol: float = 1.3   # bounce volume_ratio at/above this counts as high-vol
    # 1a surfacing: when True, the digest's reversal list shows only the PREMIUM tier (high-vol +
    # spring). DEMOTED to False after the 2026-07-01 audit: premium fires ~0.2% live (2 signals in
    # 15 days) and its +0.25R was measured on a 250-name subset never confirmed at the full
    # universe -- turning it on silently blanked the reversal list from 06-28 (it overrides
    # confirmed_only in reversal_picks). Premium stays computed/shadow-booked as a tier; it may
    # regain surfacing only with a full-universe result AND a documented expected picks/week.
    reversal_surface_premium_only: bool = False
    # 1b conviction sizing weights live in analytics/performance.py's
    # _DEFAULT_CONVICTION_WEIGHTS (consumed by size_weighted_expectancy / the sizing
    # replay) -- the conviction_sizing/conviction_weight_* knobs that used to shadow
    # them here were never read and were deleted (2026-07-17 audit L9).

    # --- market screener (weekly macro "Market Weather" report) ------------------------
    vix_spike_rank: float = 80.0   # VIX percentile rank at/above this = a panic spike (flag it)
    # LLM deep analysis for the weekly market report. ON by default -- it is a ONCE-A-WEEK report,
    # so one Opus call + a few web searches is trivially cheap, and the full macro read is the
    # point. Falls back to the deterministic facts read if the LLM is unavailable (never blocks).
    # Set False to force the deterministic read.
    market_report_enabled: bool = True
    market_model: str = "claude-opus-4-8"
    market_reasoning: str = "high"
    market_max_searches: int = 6
    # surface only CONFIRMED-strength reversals in the digest (drop EARLY). A 503-name replay
    # found the cost-robust edge concentrates entirely in CONFIRMED reversals (+0.125R net of
    # 0.05 ATR slippage, 95%low >0) while EARLY is breakeven-to-negative and ~92% of the book.
    # EARLY is still detected, scored, and shadow-booked (learning loop intact) -- just hidden.
    # False restores the full list. See docs/plans/2026-06-25-reversal-confirmed-noflip-design.md.
    reversal_surface_confirmed_only: bool = True
    # Sector-diversity cap for the reversal list (the daily_max_per_sector counterpart).
    # 2026-07-02: 31 same-day confirmations crowded every software rotation name out of the
    # score-ranked top-N. Unknown sectors are never capped (fail-open); None disables.
    reversal_max_per_sector: int | None = 2

    # exits
    # momentum-flip exit: close a trade when the HA candle flips bearish (a shaved head).
    # True = on (the historical behavior). False powers the `no_flip` experiment arm, which
    # the offline edge study found is a mild, consistent drag (it cuts trades at ~-0.3R that
    # average ~-0.13R if held) -- so it's A/B'd in the live shadow book rather than switched.
    momentum_flip_exit: bool = True
    # Breakeven ratchet (the be_1r ARM, 2026-07 queue): once the PRIOR bar's high-water
    # clears entry + this many R, raise the stop to breakeven (never down; same
    # prior-bar/no-intra-bar-lookahead discipline as the Chandelier trail). 0 = off.
    # Pre-partial trades otherwise ride FULL initial risk to the end. Arm-tested only
    # (exit knob): the variants guard rejects it from screen variants by design.
    breakeven_after_r: float = 0.0
    # per-play-type override for the REVERSAL book: the same 503-name replay found the eager
    # momentum-flip exit is a net drag on reversals (it converts +0.77R time-stops and target
    # runs into ~-0.19R early cuts). Default OFF for reversal; continuation keeps the flip via
    # momentum_flip_exit above. evaluate_exit picks by trade.play_type. True re-enables it.
    reversal_momentum_flip_exit: bool = False
    partial_frac: float = 0.0   # fraction scaled out at the first target (0.0 = feature OFF; Step C turns it on)
    # When True, scale out at the target ONLY if HA momentum is softening
    # (not shaved_bottom, or a shrinking HA body); a strong target-touch holds the
    # full position and lets the winner run. False = unconditional partial (Step B).
    partial_require_softening: bool = False
    # Post-partial runner trail (Step D bake-off). "breakeven" leaves the stop at the
    # fill (the incumbent); "chandelier" ratchets it up to high_water - m*ATR (never
    # down, never below breakeven). Only active once a partial has been booked.
    trail_mode: str = "breakeven"
    chandelier_atr_mult: float = 3.0   # m in the Chandelier trail (flat)
    # Fill-pessimism haircut on LEVEL exits (stop/target/partial), as a fraction of ATR.
    # Models that a level fill executes slightly worse than the exact level (slippage/
    # gap-through). Default 0.05 since 2026-07: with 0.0 the LIVE shadow book (the
    # evidence base for reflection/calibration) and the scheduled optimizer judged
    # everything GROSS of costs while the replay tiers were haircut -- an asymmetry the
    # audit flagged. Fixed a-priori from a microstructure rule -- NEVER added to the
    # optimizer grid. momentum_flip/time_stop exits use the bar close and are NOT haircut.
    fill_slippage_atr: float = 0.05
    time_stop_factor: float = 1.0    # time stop = factor * max_hold_bars[tf]
    max_hold_bars: dict[str, int] = field(default_factory=lambda: {
        "4h": 18,   # ~3 trading days of 4h bars
        "1d": 10,
        "1wk": 8,
        "1mo": 6,
    })
