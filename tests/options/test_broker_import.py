from datetime import date
from pathlib import Path

from swing_screener.options.broker_import import (
    Episode,
    commit_episodes,
    pair_episodes,
    parse_activity_csv,
    store_fills,
)

FIXTURE = Path(__file__).parent / "fixtures" / "robinhood_sample.csv"


def _fills():
    return parse_activity_csv(FIXTURE.read_text(encoding="utf-8"))


# --- Task 10: parser -------------------------------------------------------


def test_noise_and_trailer_rows_are_skipped() -> None:
    fills = _fills()
    assert all(f.trans_code in {"BTO", "STC", "STO", "BTC", "OEXP"} for f in fills)
    assert len(fills) == 11  # 10 trade rows + 1 OEXP; ACH/RTP/blank/disclaimer skipped


def test_description_parses_to_occ_fields() -> None:
    f = next(f for f in _fills() if f.trans_code == "BTO" and f.quantity == 5)
    assert f.underlying == "AAA"
    assert f.expiry == date(2026, 7, 17)
    assert f.right == "C"
    assert f.strike == 13.0
    assert f.occ_symbol == "AAA   260717C00013000"


def test_money_parsing_handles_parens_and_commas() -> None:
    big = next(f for f in _fills() if f.price == 13.20)
    assert big.amount == -1320.04
    win = next(f for f in _fills() if f.price == 12.55)
    assert win.amount == 1254.92


def test_oexp_quantity_strips_suffix_and_has_no_price() -> None:
    oexp = next(f for f in _fills() if f.trans_code == "OEXP")
    assert oexp.quantity == 30
    assert oexp.price is None and oexp.amount is None
    assert oexp.expiry == date(2026, 7, 10)


def test_duplicate_lines_get_distinct_hashes() -> None:
    fills = _fills()
    dupes = [f for f in fills if f.trans_code == "STC" and f.quantity == 15]
    assert len(dupes) == 2
    assert dupes[0].import_hash != dupes[1].import_hash


def test_hashes_stable_when_newer_rows_are_prepended() -> None:
    # A later re-export prepends newer activity; existing rows must keep their
    # hashes or overlapping re-imports duplicate every fill.
    text = FIXTURE.read_text(encoding="utf-8")
    header, rest = text.split("\n", 1)
    new_row = '"7/11/2026","7/11/2026","7/14/2026","AAA","AAA 7/17/2026 Call $13.00","STC","5","$0.09","$44.80"'
    reexport = f"{header}\n{new_row}\n{rest}"
    old = {f.import_hash for f in parse_activity_csv(text)}
    new = {f.import_hash for f in parse_activity_csv(reexport)}
    assert old <= new
    assert len(new - old) == 1


def test_unrecognized_header_fails_loudly() -> None:
    import pytest
    with pytest.raises(ValueError, match="unrecognized"):
        parse_activity_csv('"Date","Stuff"\n"1/1/2026","x"\n')


# --- Task 11: episode pairing + fill persistence ---------------------------


def test_same_day_scalp_pairs_into_one_closed_episode() -> None:
    fills = _fills()
    episodes = {e.occ_symbol: e for e in pair_episodes(fills)}
    scalp = episodes["BBB   260821C00360000"]
    assert scalp.status == "closed"
    assert scalp.contracts == 1
    assert scalp.pnl == 1254.92 - 1320.04
    assert scalp.opened_on == scalp.closed_on == date(2026, 6, 25)


def test_scale_out_aggregates_to_one_episode_with_vwap() -> None:
    eps = [e for e in pair_episodes(_fills()) if e.occ_symbol == "AAA   260821C00015000"]
    assert len(eps) == 1
    e = eps[0]
    assert e.status == "closed"
    assert e.contracts == 30
    assert e.entry_premium == 0.14
    assert e.exit_premium == 0.13


def test_expiration_closes_episode_at_zero() -> None:
    e = next(e for e in pair_episodes(_fills()) if e.occ_symbol == "AAA   260710C00012000")
    assert e.status == "closed"
    assert e.exit_reason == "expired"
    assert e.exit_premium == 0.0


def test_unclosed_buy_is_an_open_episode() -> None:
    e = next(e for e in pair_episodes(_fills()) if e.occ_symbol == "AAA   260717C00013000")
    assert e.status == "open"


def test_short_open_is_flagged_needs_review() -> None:
    e = next(e for e in pair_episodes(_fills()) if e.occ_symbol == "CCC   260529C00016000")
    assert e.needs_review is True


def test_store_fills_is_idempotent() -> None:
    from sqlalchemy.orm import Session

    from swing_screener.db.session import get_engine

    engine = get_engine("sqlite:///:memory:")
    fills = _fills()
    with Session(engine) as s:
        first = store_fills(s, fills)
        again = store_fills(s, fills)
    assert first.added == len(fills)
    assert again.added == 0 and again.skipped == len(fills)


# --- Task 12: tagged episode commit ----------------------------------------


def test_commit_episodes_respects_tags_and_is_idempotent() -> None:
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from swing_screener.db.models import OptionPaperTrade
    from swing_screener.db.session import get_engine

    engine = get_engine("sqlite:///:memory:")
    episodes = pair_episodes(_fills())
    closed = [e for e in episodes if e.status == "closed"]
    tags = {e.import_key: "gex" for e in closed[:1]}
    tags.update({e.import_key: "other" for e in closed[1:2]})
    # everything else untagged -> skipped
    with Session(engine) as s:
        n = commit_episodes(s, episodes, tags)
        rows = list(s.scalars(select(OptionPaperTrade)))
        assert n == 2 and len(rows) == 2
        assert {r.strategy for r in rows} == {"gex", "other"}
        assert all(r.account == "robinhood" for r in rows)
        assert all(r.premium_pnl is not None for r in rows)
        # re-commit is a no-op
        assert commit_episodes(s, episodes, tags) == 0


assert Episode is not None  # exported dataclass (see pair_episodes)
