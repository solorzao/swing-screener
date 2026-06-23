from swing_screener.pipeline.diversity import cap_by_sector


def test_cap_by_sector_limits_per_sector_and_preserves_order():
    items = [("AAPL", "Tech"), ("MSFT", "Tech"), ("NVDA", "Tech"),
             ("JPM", "Fin"), ("XOM", "Energy")]
    out = cap_by_sector(items, lambda x: x[1], max_per_sector=2, limit=5)
    # NVDA (the 3rd Tech) is dropped; rank order is otherwise preserved.
    assert [t for t, _ in out] == ["AAPL", "MSFT", "JPM", "XOM"]


def test_cap_by_sector_unknown_sector_is_never_capped():
    items = [("A", None), ("B", None), ("C", None), ("D", "Fin")]
    out = cap_by_sector(items, lambda x: x[1], max_per_sector=1, limit=10)
    # None/empty sector is fail-open: every such item stays eligible.
    assert [t for t, _ in out] == ["A", "B", "C", "D"]


def test_cap_by_sector_respects_the_limit():
    items = [("A", "X"), ("B", "Y"), ("C", "Z")]
    out = cap_by_sector(items, lambda x: x[1], max_per_sector=5, limit=2)
    assert [t for t, _ in out] == ["A", "B"]
