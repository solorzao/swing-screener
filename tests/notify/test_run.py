import logging
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.db.models import EmailLog, ExitEvent, ReversalFunnel, Signal, Universe
from swing_screener.db.session import get_engine
from swing_screener.notify import run

RUN = date(2026, 6, 15)


class _Block:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)]


class _FakeClient:
    @property
    def messages(self):
        class _M:
            def create(self, **kw):
                return _Resp("CORE: Strong daily continuation.\n\nClean setup, entry to target.")

        return _M()


def _sig(ticker, rank):
    return Signal(run_date=RUN, ticker=ticker, timeframe="1d", horizon="medium",
                  score=1.0 / rank, rank=rank, trigger_close=100.0, atr=4.0, rsi=55.0,
                  entry_floor=96.0, entry_ceiling=101.0, stop=95.0, target=110.0)


def _seed(url):
    engine = get_engine(url)
    with Session(engine) as s:
        s.add_all([_sig("AMD", 1), _sig("AEP", 2)])
        s.commit()


def test_send_digest_emails_with_pdf_and_is_idempotent(tmp_path, unpark_continuation,
                                                       no_conviction_floor):
    url = f"sqlite:///{tmp_path / 'd.sqlite'}"
    _seed(url)
    sent = []

    def recorder(**kwargs):
        sent.append(kwargs)

    res = run.send_digest(kind="daily", db_url=url, run_date=RUN, to="me@example.com",
                          pdf_dir=tmp_path / "digests", anthropic_client=_FakeClient(),
                          smtp_send=recorder)
    assert res.sent is True and res.n_picks == 2 and res.pdf_attached is True
    assert len(sent) == 1
    email = sent[0]
    assert email["to"] == "me@example.com"
    assert "AMD" in email["text"]
    # the company name (from the universe seed CSV) is threaded into the body
    assert "Advanced Micro Devices" in email["text"]
    assert "American Electric Power" in email["text"]
    assert "Advanced Micro Devices" in email["html"]
    assert len(email["attachments"]) == 1  # the PDF
    assert str(email["attachments"][0]).endswith(".pdf")

    # second run for the same day is a no-op (idempotent)
    res2 = run.send_digest(kind="daily", db_url=url, run_date=RUN, to="me@example.com",
                           pdf_dir=tmp_path / "digests", anthropic_client=_FakeClient(),
                           smtp_send=recorder)
    assert res2.sent is False
    assert len(sent) == 1  # recorder not called again


def _rev_sig(ticker, rank, strength):
    return Signal(run_date=RUN, ticker=ticker, timeframe="1d", horizon="medium",
                  play_type="reversal", strength=strength, score=1.0 / rank, rank=rank,
                  trigger_close=50.0, atr=2.0, rsi=22.0, entry_floor=50.0,
                  entry_ceiling=52.0, stop=47.0, target=58.0)


def test_daily_digest_surfaces_confirmed_reversals_with_funnel_line(tmp_path,
                                                                    no_conviction_floor):
    """Under the default config the CONFIRMED reversal surfaces (premium_only must not
    silently blank the list -- the 2026-06-28..07-01 drought) and the body carries the
    detected/confirmed/surfaced funnel so a filtered-empty day is visibly different
    from a no-signals day."""
    url = f"sqlite:///{tmp_path / 'rev.sqlite'}"
    _seed(url)  # 2 continuation picks
    engine = get_engine(url)
    with Session(engine) as s:
        s.add_all([_rev_sig("GME", 1, "confirmed"), _rev_sig("BBBY", 2, "early")])
        s.commit()
    sent = []
    res = run.send_digest(kind="daily", db_url=url, run_date=RUN, to="me@example.com",
                          pdf_dir=tmp_path / "digests", anthropic_client=_FakeClient(),
                          smtp_send=lambda **k: sent.append(k))
    assert res.sent is True and res.n_reversals == 1  # the confirmed pick surfaced
    body = sent[-1]["text"]
    assert "GME" in body                      # confirmed reversal is in the email
    assert "BBBY" not in body                 # early stays shadow-tracked, hidden
    # stage-attributed funnel: cooldown (fresh) and actionability stages are visible so a
    # wipeout names the bar that filtered the list (2026-07 rotation audit).
    assert ("Reversal funnel: 2 detected · 1 confirmed · 1 fresh · 1 actionable · "
            "1 surfaced") in body
    assert "Reversal funnel: 2 detected" in sent[-1]["html"]


def test_daily_digest_funnel_line_on_filtered_empty_day(tmp_path):
    """All-early day: the surfaced list is empty but the funnel line still reports what
    was detected, so the reader can tell 'filtered out' from 'nothing found'."""
    url = f"sqlite:///{tmp_path / 'revempty.sqlite'}"
    _seed(url)
    engine = get_engine(url)
    with Session(engine) as s:
        s.add_all([_rev_sig("GME", 1, "early"), _rev_sig("BBBY", 2, "early")])
        s.commit()
    sent = []
    res = run.send_digest(kind="daily", db_url=url, run_date=RUN, to="me@example.com",
                          pdf_dir=tmp_path / "digests", anthropic_client=_FakeClient(),
                          smtp_send=lambda **k: sent.append(k))
    assert res.sent is True and res.n_reversals == 0
    assert ("Reversal funnel: 2 detected · 0 confirmed · 0 fresh · 0 actionable · "
            "0 surfaced") in sent[-1]["text"]


def test_daily_digest_parks_continuation_by_default(tmp_path, monkeypatch,
                                                    no_conviction_floor):
    """Continuation parking (Q6 NULL completed the falsification, 2026-07-25;
    docs/plans/2026-07-25-q6-q7-sweep-results.md): under the DEFAULT config the daily
    digest OMITS the continuation section -- no title, no picks, so no continuation
    deep calls and (intents are built only for deep picks) no continuation intents --
    while the reversal section leads the email and the send stays clean. The section
    is omitted rather than rendered as 'No qualifying setups': a parked book must not
    read like a quiet market every day (the 2026-07-01 blank-digest ambiguity). The
    ATTACHMENT mirrors the same convention: the PDF builder receives picks=None (the
    placeholder-free omitted section), never [] (whose 'No picks.' page-1 placeholder
    is the same ambiguity in PDF form)."""
    url = f"sqlite:///{tmp_path / 'park.sqlite'}"
    _seed(url)  # 2 continuation picks that must NOT surface
    engine = get_engine(url)
    with Session(engine) as s:
        s.add(_rev_sig("GME", 1, "confirmed"))
        s.commit()
    sent = []
    pdf_calls = []
    real_pdf = run.build_digest_pdf

    def spy_pdf(picks, out_path, **kw):  # records, then builds the REAL pdf
        pdf_calls.append((picks, kw.get("reversal_picks")))
        return real_pdf(picks, out_path, **kw)

    monkeypatch.setattr(run, "build_digest_pdf", spy_pdf)
    res = run.send_digest(kind="daily", db_url=url, run_date=RUN, to="me@example.com",
                          pdf_dir=tmp_path / "digests", anthropic_client=_FakeClient(),
                          smtp_send=lambda **k: sent.append(k))
    assert res.sent is True and res.n_picks == 0 and res.n_reversals == 1
    body = sent[-1]["text"]
    assert "Continuation Plays" not in body           # section OMITTED, not "no setups"
    assert "AMD" not in body and "AEP" not in body    # parked picks appear nowhere
    assert "GME" in body                              # the confirmed reversal still leads
    assert "Continuation Plays" not in sent[-1]["html"]
    assert res.pdf_attached is True                   # the reversal PDF still ships
    [(pdf_picks_arg, rev_arg)] = pdf_calls
    assert pdf_picks_arg is None                      # parked -> no placeholder page
    assert [p.ticker for p in rev_arg] == ["GME"]     # reversal section intact


@pytest.mark.parametrize("kind,tf", [("weekly", "1wk"), ("monthly", "1mo")])
def test_parked_slow_cadence_digest_contract(tmp_path, kind, tf):
    """What a parked weekly/monthly digest IS: those cadences are continuation-only, so
    while parked they still SEND -- the always-on health footer (the 'is the cron
    alive' push) must keep arriving -- with n_picks=0, NO pick sections, NO PDF, and a
    body that opens clean (no leading blank line from an unconditional separator)."""
    url = f"sqlite:///{tmp_path / 'slowpark.sqlite'}"
    engine = get_engine(url)
    with Session(engine) as s:
        s.add(_sig_on(RUN, "NVDA", tf, 1, first_seen=RUN))  # would surface un-parked
        s.commit()
    sent = []
    res = run.send_digest(kind=kind, db_url=url, run_date=RUN, to="me@example.com",
                          pdf_dir=tmp_path / "d", anthropic_client=_FakeClient(),
                          smtp_send=lambda **k: sent.append(k))
    assert res.sent is True and res.n_picks == 0
    assert res.pdf_attached is False and sent[0]["attachments"] == []
    body = sent[0]["text"]
    assert "NVDA" not in body and "Plays" not in body   # no pick section at all
    assert not body.startswith("\n")                    # opens clean...
    assert body.strip()                                 # ...on the health footer


def test_daily_digest_continuation_returns_when_unparked(tmp_path, monkeypatch,
                                                         no_conviction_floor):
    """The parking flag is read from config per run (never baked in): flipping
    surface_continuation back to True restores the continuation section -- the
    re-enable path for a future entry mechanic that clears the replay bar."""
    import dataclasses

    from swing_screener.config import StrategyConfig

    # min_conviction="" alongside the un-park: this test's subject is the PARKING flag,
    # and it never arms the insight engine, so the armed floor would drop both picks as
    # ungraded and the restored section would render empty (see the no_conviction_floor
    # fixture -- this call rebuilds the config from the real default, overriding it).
    unparked = dataclasses.replace(StrategyConfig(), surface_continuation=True,
                                   min_conviction="")
    monkeypatch.setattr(run, "StrategyConfig", lambda: unparked)
    url = f"sqlite:///{tmp_path / 'unpark.sqlite'}"
    _seed(url)
    sent = []
    res = run.send_digest(kind="daily", db_url=url, run_date=RUN, to="me@example.com",
                          pdf_dir=tmp_path / "digests", anthropic_client=_FakeClient(),
                          smtp_send=lambda **k: sent.append(k))
    assert res.sent is True and res.n_picks == 2
    assert "Top 3 - Continuation Plays" in sent[-1]["text"]
    assert "AMD" in sent[-1]["text"]


def test_send_digest_drops_already_ran_picks(tmp_path, unpark_continuation,
                                             no_conviction_floor):
    """A pick whose live price has run past its entry ceiling (or broken its stop) by
    digest time is dropped: the screen ran the prior evening, so a pick can leave its
    entry zone overnight. Mirrors the dashboard's 'hide already ran' filter so the email
    surfaces only what is still tradable."""
    url = f"sqlite:///{tmp_path / 'ran.sqlite'}"
    _seed(url)  # AMD (rank 1) + AEP (rank 2), entry zone 96-101, stop 95
    sent = []

    # AMD has run to 130 (well past the 101 ceiling -> extended); AEP sits at 100 (in zone).
    prices = {"AMD": 130.0, "AEP": 100.0}
    res = run.send_digest(kind="daily", db_url=url, run_date=RUN, to="me@example.com",
                          pdf_dir=tmp_path / "digests", anthropic_client=_FakeClient(),
                          smtp_send=lambda **k: sent.append(k),
                          latest_closes_fn=lambda tickers: prices)
    assert res.sent is True and res.n_picks == 1
    body = sent[-1]["text"]
    assert "American Electric Power" in body          # AEP still actionable -> kept
    assert "Advanced Micro Devices" not in body       # AMD already ran -> dropped


def test_reversal_pick_extended_is_kept_broken_is_dropped(tmp_path, no_conviction_floor):
    """Play-type-aware already-ran semantics: a REVERSAL pick is a resting limit with a
    multi-bar fill window, so sitting above its ceiling at digest time is its NORMAL
    state (a confirmed reversal closes above the flip high by definition) -- it must be
    KEPT. Only a broken stop drops it. The old drop-on-extended rule silently deleted
    every confirmed reversal during the 2026-07 rotation."""
    url = f"sqlite:///{tmp_path / 'revran.sqlite'}"
    _seed(url)
    engine = get_engine(url)
    with Session(engine) as s:
        # zone 50-52, stop 47: GME at 55 is extended (keep); BBBY at 46 broke the stop (drop)
        s.add_all([_rev_sig("GME", 1, "confirmed"), _rev_sig("BBBY", 2, "confirmed")])
        s.commit()
    sent = []
    prices = {"GME": 55.0, "BBBY": 46.0, "AMD": 100.0, "AEP": 100.0}
    res = run.send_digest(kind="daily", db_url=url, run_date=RUN, to="me@example.com",
                          pdf_dir=tmp_path / "digests", anthropic_client=_FakeClient(),
                          smtp_send=lambda **k: sent.append(k),
                          latest_closes_fn=lambda tickers: prices)
    assert res.sent is True and res.n_reversals == 1
    body = sent[-1]["text"]
    assert "GME" in body        # extended reversal: the resting limit is still working
    assert "BBBY" not in body   # broken stop: the setup failed before entry


def test_reversal_top5_sector_cap_backfills(tmp_path, no_conviction_floor):
    """A one-sector wave can't fill the reversal list: with reversal_max_per_sector=2 the
    third+ same-sector names give way to the next sectors' picks (the Jul-2 crowding)."""
    url = f"sqlite:///{tmp_path / 'revcap.sqlite'}"
    _seed(url)
    engine = get_engine(url)
    with Session(engine) as s:
        fins = ["JPM", "GS", "MS", "BAC", "C"]
        s.add_all([_rev_sig(t, i + 1, "confirmed") for i, t in enumerate(fins)])
        s.add_all([_rev_sig("CRM", 6, "confirmed"), _rev_sig("WDAY", 7, "confirmed")])
        s.add_all([Universe(ticker=t, sector="Financials") for t in fins])
        s.add_all([Universe(ticker=t, sector="Information Technology")
                   for t in ("CRM", "WDAY")])
        s.commit()
    sent = []
    res = run.send_digest(kind="daily", db_url=url, run_date=RUN, to="me@example.com",
                          pdf_dir=tmp_path / "digests", anthropic_client=_FakeClient(),
                          smtp_send=lambda **k: sent.append(k))
    # 2 financials keep their rank slots, the 3 others are capped out, and the first
    # software name (rank 6) backfills the last slot -> 3 surfaced from the 7 candidates
    assert res.sent is True and res.n_reversals == 3
    body = sent[-1]["text"]
    assert "CRM" in body
    # capped-out and over-limit names stay visible on the compact overflow line,
    # not as full picks (WDAY cleared every bar but lost the top-3 race)
    assert "Also confirmed (lost the top-3/sector race): MS, BAC, C, WDAY" in body


def test_daily_digest_persists_reversal_funnel_row(tmp_path, no_conviction_floor):
    """The daily digest persists the funnel snapshot: fresh/actionable/surfaced are
    digest-time state (cooldown, live quotes, sector cap) and are unrecoverable later --
    the row is the only record. Sector-cap fixture: 5 financials + 2 software, all
    confirmed, cap 2/sector + top-3 -> 3 surfaced, 4 on the overflow line."""
    url = f"sqlite:///{tmp_path / 'funrow.sqlite'}"
    _seed(url)
    engine = get_engine(url)
    with Session(engine) as s:
        fins = ["JPM", "GS", "MS", "BAC", "C"]
        s.add_all([_rev_sig(t, i + 1, "confirmed") for i, t in enumerate(fins)])
        s.add_all([_rev_sig("CRM", 6, "confirmed"), _rev_sig("WDAY", 7, "confirmed")])
        s.add_all([Universe(ticker=t, sector="Financials") for t in fins])
        s.add_all([Universe(ticker=t, sector="Information Technology")
                   for t in ("CRM", "WDAY")])
        s.commit()
    res = run.send_digest(kind="daily", db_url=url, run_date=RUN, to="me@example.com",
                          pdf_dir=tmp_path / "digests", anthropic_client=_FakeClient(),
                          smtp_send=lambda **k: None,
                          latest_closes_fn=lambda tickers: {})  # fail-open: nothing dropped
    assert res.sent is True and res.n_reversals == 3
    with Session(get_engine(url)) as s:
        rows = list(s.scalars(select(ReversalFunnel)))
    assert len(rows) == 1
    row = rows[0]
    assert row.run_date == RUN  # the screen run's date, not "today"
    assert (row.detected, row.confirmed, row.fresh, row.actionable,
            row.surfaced) == (7, 7, 7, 7, 3)
    assert row.overflow_tickers == "MS,BAC,C,WDAY"  # cleared every bar, lost the top-3/sector race
    assert row.pool_n == 20
    assert row.confirmed_only is True and row.premium_only is False
    assert row.already_ran_checked is True  # latest_closes_fn was injected
    assert row.created_at is not None


def test_weekly_digest_persists_no_reversal_funnel(tmp_path):
    """The funnel block is daily-only: a weekly digest must not write a funnel row."""
    url = f"sqlite:///{tmp_path / 'funwk.sqlite'}"
    _seed(url)
    res = run.send_digest(kind="weekly", db_url=url, run_date=RUN, to="me@example.com",
                          pdf_dir=tmp_path / "digests", anthropic_client=_FakeClient(),
                          smtp_send=lambda **k: None)
    assert res.sent is True
    with Session(get_engine(url)) as s:
        assert list(s.scalars(select(ReversalFunnel))) == []


def _sig_on(run_date, ticker, tf, rank, first_seen=None):
    return Signal(run_date=run_date, ticker=ticker, timeframe=tf, horizon="medium",
                  score=1.0 / rank, rank=rank, trigger_close=100.0, atr=4.0, rsi=55.0,
                  entry_floor=96.0, entry_ceiling=101.0, stop=95.0, target=110.0,
                  first_seen_date=first_seen)


@pytest.mark.parametrize("kind,tf", [("weekly", "1wk"), ("monthly", "1mo")])
def test_slow_cadence_digest_keeps_setups_older_than_the_daily_cooldown(
        tmp_path, caplog, kind, tf, unpark_continuation, no_conviction_floor):
    """Per-kind cooldown horizon: a 1wk/1mo setup first seen 4 daily runs ago is still
    fresh on its OWN cadence (slow-timeframe setups persist across many daily screens by
    nature -- first_seen_date inherits the streak start), so it must appear in the
    weekly/monthly digest even though the daily cooldown (1 run) drops it from the
    daily. Under the old shared horizon those digests showed only setups born in the
    last ~2 daily screens, so a cooldown-blanked one was indistinguishable from a quiet
    market. The monthly leg also exercises the _COOLDOWN_RUNS["monthly"] lookup on the
    real send path (a misspelled key would KeyError only in the monthly prod job) and
    the fewer-runs-than-horizon fallback: 5 stored runs < the 21-run monthly window, so
    the cutoff falls back to the oldest run available and the old pick is kept."""
    url = f"sqlite:///{tmp_path / 'slow.sqlite'}"
    engine = get_engine(url)
    with Session(engine) as s:
        # Establish the run calendar the run-denominated cooldown walks back over.
        for d in (date(2026, 6, 9), date(2026, 6, 10), date(2026, 6, 11), date(2026, 6, 12)):
            s.add(_sig_on(d, "FILL", "1d", 9))
        # Streak started 4 daily runs back (june 10); re-detected on today's run.
        s.add(_sig_on(RUN, "NVDA", tf, 1, first_seen=date(2026, 6, 10)))
        # Born today: fresh on every cadence.
        s.add(_sig_on(RUN, "MSFT", tf, 2, first_seen=RUN))
        s.commit()

    sent = []
    kw = {"db_url": url, "run_date": RUN, "to": "me@example.com", "pdf_dir": tmp_path / "d",
              "anthropic_client": _FakeClient(), "smtp_send": lambda **k: sent.append(k)}

    with caplog.at_level(logging.INFO, logger="swing_screener.notify.run"):
        daily = run.send_digest(kind="daily", **kw)
        slow = run.send_digest(kind=kind, **kw)

    # Daily (cooldown = 1 run): only the fresh setup survives.
    assert daily.n_picks == 1
    assert "MSFT" in sent[0]["text"] and "NVDA" not in sent[0]["text"]
    # Own cadence (weekly 5-run / monthly 21-run horizon): the 4-runs-old setup surfaces.
    assert slow.n_picks == 2
    assert "NVDA" in sent[1]["text"] and "MSFT" in sent[1]["text"]
    # Nothing outlived the cadence's horizon, so no drop line fires.
    assert [r for r in caplog.records if "cooldown dropped" in r.getMessage()] == []


def test_weekly_cooldown_drop_is_logged_with_true_count(tmp_path, caplog, unpark_continuation,
                                                        no_conviction_floor):
    """A setup that outlives even the weekly horizon (first seen before the last 5 runs)
    is still dropped -- and the drop is LOGGED with the true surfaced-set count, so a
    cooldown-blanked weekly digest is diagnosable from the job log instead of reading
    as a quiet market."""
    url = f"sqlite:///{tmp_path / 'wkdrop.sqlite'}"
    engine = get_engine(url)
    with Session(engine) as s:
        # 6 prior runs + today's = 7 distinct run dates; the weekly horizon (5 runs)
        # cuts off at june 6, so a june-5 streak start is genuinely stale.
        for d in (5, 6, 9, 10, 11, 12):
            s.add(_sig_on(date(2026, 6, d), "FILL", "1d", 9))
        s.add(_sig_on(RUN, "AAPL", "1wk", 1, first_seen=date(2026, 6, 5)))  # stale
        s.add(_sig_on(RUN, "MSFT", "1wk", 2, first_seen=RUN))               # fresh
        s.commit()

    sent = []
    with caplog.at_level(logging.INFO, logger="swing_screener.notify.run"):
        res = run.send_digest(kind="weekly", db_url=url, run_date=RUN, to="me@example.com",
                              pdf_dir=tmp_path / "d", anthropic_client=_FakeClient(),
                              smtp_send=lambda **k: sent.append(k))

    assert res.n_picks == 1
    assert "MSFT" in sent[0]["text"] and "AAPL" not in sent[0]["text"]
    drops = [r.getMessage() for r in caplog.records if "cooldown dropped" in r.getMessage()]
    assert drops == ["cooldown dropped 1 pick(s) for weekly digest"]


def test_parked_continuation_never_logs_cooldown_drops(tmp_path, caplog):
    """With continuation PARKED (the default), a weekly digest's empty list is parking,
    not staleness -- the 'cooldown dropped' diagnostic must NOT fire. The un-parked
    comparison query would misattribute every would-have-surfaced pick to the cooldown,
    poisoning the very log line that exists to diagnose blanked slow-cadence digests."""
    url = f"sqlite:///{tmp_path / 'parkwk.sqlite'}"
    engine = get_engine(url)
    with Session(engine) as s:
        for d in (5, 6, 9, 10, 11, 12):  # the run calendar (same shape as the drop test)
            s.add(_sig_on(date(2026, 6, d), "FILL", "1d", 9))
        s.add(_sig_on(RUN, "AAPL", "1wk", 1, first_seen=date(2026, 6, 5)))  # stale
        s.add(_sig_on(RUN, "MSFT", "1wk", 2, first_seen=RUN))               # fresh
        s.commit()

    sent = []
    with caplog.at_level(logging.INFO, logger="swing_screener.notify.run"):
        res = run.send_digest(kind="weekly", db_url=url, run_date=RUN, to="me@example.com",
                              pdf_dir=tmp_path / "d", anthropic_client=_FakeClient(),
                              smtp_send=lambda **k: sent.append(k))

    assert res.sent is True and res.n_picks == 0  # parked: nothing surfaces
    assert "MSFT" not in sent[0]["text"]
    assert [r for r in caplog.records if "cooldown dropped" in r.getMessage()] == []


def test_bounded_overflow_never_cuts_mid_ticker():
    """The 512-char overflow bound must truncate at a COMMA: a naive slice could leave
    a phantom fragment ("...,WDA") that the cockpit's comma-splitting reader would
    render as a real ticker."""
    assert run._bounded_overflow(["AAPL", "MSFT"]) == "AAPL,MSFT"  # under the bound
    tickers = [f"TICK{i:04d}" for i in range(80)]  # joined length 719 > 512
    out = run._bounded_overflow(tickers)
    assert len(out) <= 512
    parts = out.split(",")
    assert parts == tickers[: len(parts)]  # every persisted name is a REAL ticker
    # a bound too tight for even one full name persists nothing, not a fragment
    assert run._bounded_overflow(["ABCDEFGH"], limit=4) == ""


def test_send_digest_keeps_picks_when_quotes_unavailable(tmp_path, unpark_continuation,
                                                         no_conviction_floor):
    """Fail-open: when live quotes can't be fetched, no pick is dropped -- a quote outage
    must never silence the digest."""
    url = f"sqlite:///{tmp_path / 'noq.sqlite'}"
    _seed(url)
    sent = []
    res = run.send_digest(kind="daily", db_url=url, run_date=RUN, to="me@example.com",
                          pdf_dir=tmp_path / "digests", anthropic_client=_FakeClient(),
                          smtp_send=lambda **k: sent.append(k),
                          latest_closes_fn=lambda tickers: {})  # no quotes available
    assert res.n_picks == 2  # both picks kept


def test_send_digest_force_resends_without_duplicate_log(tmp_path):
    # force=True re-sends a digest that already went out today (a manual override
    # for ad-hoc verification), but it must NOT add a second EmailLog marker -- on
    # SQL Server a duplicate (kind, run_date, NULL alert_key) would violate the
    # dedup unique constraint, so a forced resend reuses the existing day marker.
    url = f"sqlite:///{tmp_path / 'f.sqlite'}"
    _seed(url)
    sent = []

    def recorder(**kwargs):
        sent.append(kwargs)

    kw = {"kind": "daily", "db_url": url, "run_date": RUN, "to": "me@example.com",
              "pdf_dir": tmp_path / "digests", "anthropic_client": _FakeClient(), "smtp_send": recorder}

    assert run.send_digest(**kw).sent is True and len(sent) == 1
    assert run.send_digest(**kw).sent is False and len(sent) == 1  # normal re-run: no-op
    assert run.send_digest(**kw, force=True).sent is True and len(sent) == 2  # forced resend

    with Session(get_engine(url)) as s:
        rows = list(s.scalars(select(EmailLog).where(EmailLog.kind == "daily")))
    assert len(rows) == 1  # no duplicate marker from the forced resend


def test_send_digest_defaults_to_latest_screen_run_date(tmp_path, unpark_continuation,
                                                        no_conviction_floor):
    # No explicit run_date: the morning digest must summarize the LATEST screen run
    # (seeded under RUN, not today's date). With the old date.today() default it would
    # query a run_date with no signals and send an empty digest.
    url = f"sqlite:///{tmp_path / 'latest.sqlite'}"
    _seed(url)  # AMD, AEP under run_date=RUN (2026-06-15), not the real "today"
    sent = []

    res = run.send_digest(kind="daily", db_url=url, to="me@example.com",  # run_date omitted
                          pdf_dir=tmp_path / "d", anthropic_client=_FakeClient(),
                          smtp_send=lambda **kw: sent.append(kw))

    assert res.sent is True and res.n_picks == 2  # found the latest screen's signals
    assert len(sent) == 1 and "AMD" in sent[0]["text"]


def test_email_log_sent_at_is_stamped_in_utc(tmp_path):
    # sent_at must be the UTC wall-clock, not the host's local time, so container
    # timestamps are unambiguous. The stored value is naive but represents UTC;
    # comparing against now-in-UTC catches a regression to naive local
    # datetime.now() on any host whose local tz != UTC.
    url = f"sqlite:///{tmp_path / 'utc.sqlite'}"
    _seed(url)
    run.send_digest(kind="daily", db_url=url, run_date=RUN, to="me@example.com",
                    pdf_dir=tmp_path / "digests", anthropic_client=_FakeClient(),
                    smtp_send=lambda **kw: None)

    with Session(get_engine(url)) as s:
        row = s.scalars(select(EmailLog).where(EmailLog.kind == "daily")).first()
    assert row is not None
    stored = row.sent_at.replace(tzinfo=None) if row.sent_at.tzinfo else row.sent_at
    now_utc = datetime.now(UTC).replace(tzinfo=None)
    assert abs((now_utc - stored).total_seconds()) < 300


def test_exit_alert_delivered_on_rerun_after_event(tmp_path):
    # an exit event that fires AFTER the digest already went out must still be
    # delivered on a re-run, even though the digest itself is a no-op.
    url = f"sqlite:///{tmp_path / 'e.sqlite'}"
    _seed(url)
    sent = []

    def recorder(**kwargs):
        sent.append(kwargs)

    kw = {"kind": "daily", "db_url": url, "run_date": RUN, "to": "me@example.com",
              "pdf_dir": tmp_path / "digests", "anthropic_client": _FakeClient(), "smtp_send": recorder}
    run.send_digest(**kw)  # first run: digest only (no exit events yet)
    assert len(sent) == 1

    with Session(get_engine(url)) as s:  # a hard stop fires intraday
        s.add(ExitEvent(created_date=RUN, is_paper=False, trade_id=1, tier="hard",
                        reason="stop", message="AMD stopped @ 95"))
        s.commit()

    res = run.send_digest(**kw)  # re-run: digest no-op, but exit alert still sent
    assert res.sent is False
    assert len(sent) == 2
    assert "Exit" in sent[1]["subject"] and sent[1]["attachments"] == []


def _real_open_trade(ticker):
    from swing_screener.db.models import Trade
    return Trade(ticker=ticker, timeframe="1d", horizon="medium", entry_date=date(2026, 6, 10),
                 entry_price=100.0, size=10.0, stop=95.0, target=110.0, status="open")


def test_kind_exit_produces_and_sends_exit_alert(tmp_path):
    # the exit kind PRODUCES today's real exit events (via run_exit_check) and
    # then SENDS the exit-alert email -- end to end, no network.
    url = f"sqlite:///{tmp_path / 'k.sqlite'}"
    engine = get_engine(url)
    with Session(engine) as s:
        s.add(_real_open_trade("AMD"))  # a real, open trade that will hard-stop
        s.commit()

    sent = []

    def recorder(**kwargs):
        sent.append(kwargs)

    # fake live bar source: AMD's low breaches its stop -> hard exit.
    def fake_bars(tickers, timeframe):
        bar = {"low": 93.0, "high": 100.0, "close": 95.0, "shaved_head": False, "bearish": True}
        return {t: bar for t in tickers}

    result = run.run_exit_check_and_alert(db_url=url, run_date=RUN, to="me@example.com",
                                          smtp_send=recorder, latest_bars_fn=fake_bars)

    assert result.n_open == 1 and result.n_exited == 1
    assert len(sent) == 1
    email = sent[0]
    assert email["to"] == "me@example.com"
    assert "Exit" in email["subject"]
    assert email["attachments"] == []  # exit alerts carry no PDF
    assert "AMD" in email["text"]

    # the produced event is a real (is_paper=False) exit event.
    with Session(engine) as s:
        events = list(s.scalars(select(ExitEvent).where(ExitEvent.is_paper.is_(False))))
        assert len(events) == 1 and events[0].reason == "stop"


def _exit_event(ticker, tier="hard", reason="stop"):
    return ExitEvent(created_date=RUN, is_paper=False, trade_id=1, tier=tier, reason=reason,
                     message=f"{ticker} stopped @ 95")


def test_emit_skips_a_manual_close_only_day(tmp_path):
    # the cockpit's manual close writes ExitEvent(reason='manual_close'); with ONLY
    # that event pending the hourly emit path must send NOTHING and log NOTHING --
    # Oliver performed the close himself seconds ago.
    url = f"sqlite:///{tmp_path / 'mc.sqlite'}"
    engine = get_engine(url)
    with Session(engine) as s:
        s.add(_exit_event("AMD", tier="", reason="manual_close"))
        s.commit()

    sent = []
    with Session(engine) as s:
        assert run._emit_pending_exit_alert(
            s, RUN, "me@example.com", lambda **kw: sent.append(kw)) is False
    assert sent == []
    with Session(engine) as s:
        assert list(s.scalars(select(EmailLog).where(EmailLog.kind == "exit"))) == []


def test_exit_alert_key_distinguishes_event_sets(tmp_path):
    # the key is a deterministic hash over the SET of event ids, so {1,2},
    # {1,2,3}, and {1,3} all map to distinct keys (and order doesn't matter).
    url = f"sqlite:///{tmp_path / 'key.sqlite'}"
    engine = get_engine(url)
    with Session(engine) as s:
        s.add_all([_exit_event("AMD"), _exit_event("AEP"), _exit_event("MSFT")])
        s.commit()
        evs = list(s.scalars(select(ExitEvent).order_by(ExitEvent.id)))
    e1, e2, e3 = evs

    assert run._exit_alert_key([e1, e2]) == run._exit_alert_key([e2, e1])  # order-free
    assert run._exit_alert_key([e1, e2]) != run._exit_alert_key([e1, e2, e3])
    assert run._exit_alert_key([e1, e2]) != run._exit_alert_key([e1, e3])
    assert len(run._exit_alert_key([e1, e2, e3])) == 40  # sha1 hexdigest fits String(64)


def test_emit_exit_alert_per_event_set_idempotency(tmp_path):
    # a NEW exit event in a later hour triggers a SECOND alert (hourly cadence),
    # while a re-run with the SAME event set is a no-op.
    url = f"sqlite:///{tmp_path / 'set.sqlite'}"
    engine = get_engine(url)
    with Session(engine) as s:
        s.add_all([_exit_event("AMD"), _exit_event("AEP")])
        s.commit()

    sent = []

    def recorder(**kwargs):
        sent.append(kwargs)

    # event set {ev1, ev2} present -> one alert + one EmailLog(kind="exit") row
    # whose alert_key is the key over those two events.
    with Session(engine) as s:
        assert run._emit_pending_exit_alert(s, RUN, "me@example.com", recorder) is True
    assert len(sent) == 1
    with Session(engine) as s:
        evs = list(s.scalars(select(ExitEvent).order_by(ExitEvent.id)))
        logs = list(s.scalars(select(EmailLog).where(EmailLog.kind == "exit")))
    assert len(logs) == 1
    assert logs[0].alert_key == run._exit_alert_key(evs)

    # re-run with the SAME set -> NO-OP (no second send, no new log row).
    with Session(engine) as s:
        assert run._emit_pending_exit_alert(s, RUN, "me@example.com", recorder) is False
    assert len(sent) == 1
    with Session(engine) as s:
        assert len(list(s.scalars(select(EmailLog).where(EmailLog.kind == "exit")))) == 1

    # a NEW exit (ev3) fires intraday -> a SECOND alert IS sent (cadence works),
    # with a DIFFERENT alert_key.
    with Session(engine) as s:
        s.add(_exit_event("MSFT"))
        s.commit()
    with Session(engine) as s:
        assert run._emit_pending_exit_alert(s, RUN, "me@example.com", recorder) is True
    assert len(sent) == 2
    with Session(engine) as s:
        logs = list(s.scalars(select(EmailLog).where(EmailLog.kind == "exit")
                              .order_by(EmailLog.id)))
    assert len(logs) == 2
    assert logs[0].alert_key != logs[1].alert_key


def test_emit_exit_alert_tolerates_integrity_error(tmp_path, monkeypatch):
    # the SEND-then-LOG insert races the unique constraint: pre-insert the exact
    # (kind="exit", run_date, alert_key) row and bypass the pre-check, so the code
    # reaches the colliding insert. It must NOT raise and must leave the session
    # usable (the transaction is rolled back, not leaked).
    url = f"sqlite:///{tmp_path / 'race.sqlite'}"
    engine = get_engine(url)
    with Session(engine) as s:
        s.add_all([_exit_event("AMD"), _exit_event("AEP")])
        s.commit()
        evs = list(s.scalars(select(ExitEvent).order_by(ExitEvent.id)))
    key = run._exit_alert_key(evs)

    # pre-insert the row that the orchestrator's insert will collide with.
    with Session(engine) as s:
        s.add(EmailLog(sent_at=datetime.now(UTC), kind="exit", subject="dup",
                       run_date=RUN, alert_key=key))
        s.commit()

    # force the code past the pre-check so it reaches the insert + commit.
    monkeypatch.setattr(run, "_exit_already_sent", lambda *a, **k: False)

    sent = []

    def recorder(**kwargs):
        sent.append(kwargs)

    with Session(engine) as s:
        # does NOT raise despite the IntegrityError on commit.
        run._emit_pending_exit_alert(s, RUN, "me@example.com", recorder)
        # the send happened first (urgent: never lose an alert).
        assert len(sent) == 1
        # session is left usable after the rollback (no leaked transaction).
        s.add(_exit_event("NVDA"))
        s.commit()  # would raise PendingRollbackError if the txn had leaked
    with Session(engine) as s:
        # still exactly one exit log row -- the collision was swallowed.
        assert len(list(s.scalars(select(EmailLog).where(EmailLog.kind == "exit")))) == 1


def test_digest_warns_when_a_surfaced_pick_has_no_chart(tmp_path, caplog, unpark_continuation):
    """A surfaced pick with no rendered chart reaches the PDF as a chartless section --
    the render/selection gap must be LOUD at digest time (it was invisible for weeks)."""
    import logging

    url = f"sqlite:///{tmp_path / 'nochart.sqlite'}"
    _seed(url)  # AMD + AEP continuation picks, chart_path unset in the fixture
    engine = get_engine(url)
    with Session(engine) as s:
        s.add(_rev_sig("GME", 1, "confirmed"))  # surfaced reversal, also chartless
        s.commit()
    sent = []
    with caplog.at_level(logging.WARNING, logger="swing_screener.notify.run"):
        run.send_digest(kind="daily", db_url=url, run_date=RUN, to="me@example.com",
                        pdf_dir=tmp_path / "digests", anthropic_client=_FakeClient(),
                        smtp_send=lambda **k: sent.append(k))
    msgs = [r.message for r in caplog.records if "NO chart" in r.message]
    assert any("daily" in m and "AMD" in m for m in msgs)
    assert any("daily-reversal" in m and "GME" in m for m in msgs)
