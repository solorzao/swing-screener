from swing_screener.config import StrategyConfig
from swing_screener.pipeline.replay import replay_ticker
from tests.pipeline._replay_fixtures import synthetic_daily


def test_replay_produces_a_book():
    book = replay_ticker("SYN", synthetic_daily(400), StrategyConfig(),
                         warmup_bars=250, seed=0)
    assert book, "expected some paper trades"
    plays = {t.play_type for t in book}
    assert "continuation" in plays
    for t in book:
        if t.status == "closed" and t.fill_status == "filled":
            assert t.realized_r is not None and t.risk and t.risk > 0
