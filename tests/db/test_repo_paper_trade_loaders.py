from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from swing_screener.db import repo
from swing_screener.db.models import Base, PaperTrade


def _engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


def _pt(**kw):
    base = dict(ticker="AMD", timeframe="1d", horizon="medium", play_type="continuation",
                signal_score=0.8, rank=1, fill_status="filled", stop=9.0, target=12.0,
                risk=1.0, status="closed", realized_r=1.5, arm="baseline")
    base.update(kw)
    return PaperTrade(**base)


def test_load_closed_paper_trades_filters_status_fill_and_optional_facets():
    eng = _engine()
    with Session(eng) as s:
        s.add_all([
            _pt(),                                            # closed+filled baseline
            _pt(arm="partial33_cond"),                        # closed+filled other arm
            _pt(status="open", realized_r=None),              # open -> excluded
            _pt(fill_status="missed", status="closed",        # not filled -> excluded
                realized_r=None),
            _pt(play_type="reversal", strength="early",       # reversal (non-baseline arm
                arm="partial33_cond"),                        # so arm="baseline" -> 1)
            # No filled+closed+null-R row is constructed: the shadow writer never
            # produces one, so it's intentionally outside this fixture.
        ])
        s.commit()

        assert len(repo.load_closed_paper_trades(s)) == 3
        assert len(repo.load_closed_paper_trades(s, arm="baseline")) == 1
        assert len(repo.load_closed_paper_trades(s, play_type="reversal")) == 1
        assert len(repo.load_all_paper_trades(s)) == 5
