"""Tests for the pluggable blob chart store and its 3 wired consumers.

NETWORK-FREE: azure is never imported here. ``blob_enabled`` is toggled by
setting ``SWING_BLOB_ACCOUNT_URL`` in the env (it keys off ``load_settings``),
and the upload/download seams are monkeypatched so no network call is made.
"""

import base64
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from swing_screener.config import StrategyConfig
from swing_screener.db.models import Signal
from swing_screener.db.session import get_engine
from swing_screener.notify import pdf as pdf_mod
from swing_screener.notify.pdf import PdfPick, build_digest_pdf
from swing_screener.pipeline import run
from swing_screener.storage import blob

# _firing's trigger sits past the freshness gate; these blob-plumbing tests disable
# the anti-chase gate so the firing fixture still produces a charted signal.
_NO_EXT_GATE = StrategyConfig(max_extension_atr=0.0)

# 1x1 transparent PNG -- valid bytes so reportlab can actually embed it.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _signals_for(session, run_date):
    """One run's signals, rank-ascending (direct query; the repo read-back wrapper
    was deleted as dead -- 2026-07-17 audit L2)."""
    return list(session.scalars(
        select(Signal).where(Signal.run_date == run_date).order_by(Signal.rank)))


# ---------------------------------------------------------------------------
# blob_enabled toggling
# ---------------------------------------------------------------------------
def test_blob_disabled_when_no_account_url(monkeypatch):
    monkeypatch.delenv("SWING_BLOB_ACCOUNT_URL", raising=False)
    assert blob.blob_enabled() is False


def test_blob_enabled_when_account_url_set(monkeypatch):
    monkeypatch.setenv("SWING_BLOB_ACCOUNT_URL", "https://acct.blob.core.windows.net")
    assert blob.blob_enabled() is True


# ---------------------------------------------------------------------------
# pipeline producer
# ---------------------------------------------------------------------------
def _firing(bars):
    rows = []
    p = 10.0
    for _ in range(60):
        rows.append({"open": p, "high": p + 1.2, "low": p, "close": p + 1.0})
        p += 1.0
    for _ in range(3):
        rows.append({"open": p, "high": p + 0.05, "low": p - 1.5, "close": p - 1.2})
        p -= 1.2
    rows.append({"open": p, "high": p + 6.0, "low": p, "close": p + 5.6})
    return bars(rows)


def _write_universe(tmp_path, tickers):
    p = tmp_path / "universe.csv"
    p.write_text(
        "ticker,name,exchange\n"
        + "\n".join(f"{t},{t} Inc,NYSE" for t in tickers)
        + "\n"
    )
    return p


def test_pipeline_blob_enabled_sets_key_and_uploads(tmp_path, bars, monkeypatch):
    """With blob enabled, chart_path is the EXACT key (not a local path) and
    upload_chart is called with (local_png_path, that key)."""
    monkeypatch.setenv("SWING_BLOB_ACCOUNT_URL", "https://acct.blob.core.windows.net")

    def fake_fetch(ticker, *, cache_dir, today, cfg):
        return {"1d": _firing(bars)} if ticker == "AAPL" else {}
    monkeypatch.setattr(run, "_fetch_all_timeframes", fake_fetch)

    # render_chart must produce a real local file (mplfinance is not exercised).
    rendered: dict[str, object] = {}

    def fake_render(frame, ctx, zone, out_path, *, lookback=80):
        from pathlib import Path
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(_PNG)
        rendered["path"] = out_path
        return out_path
    monkeypatch.setattr(run, "render_chart", fake_render)

    uploads: list[tuple] = []

    def fake_upload(local_path, key):
        uploads.append((local_path, key))
        return key
    monkeypatch.setattr(run, "upload_chart", fake_upload)

    today = date(2024, 4, 1)
    db = f"sqlite:///{tmp_path / 'db.sqlite'}"
    run.run_screen(
        universe_path=_write_universe(tmp_path, ["AAPL", "ZZZ"]), db_url=db,
        cache_dir=tmp_path / "cache", chart_dir=tmp_path / "charts", today=today,
        cfg=_NO_EXT_GATE,
    )

    expected_key = f"{today:%Y%m%d}/AAPL_1d_{today:%Y%m%d}.png"
    with Session(get_engine(db)) as s:
        sigs = _signals_for(s, today)
        charted = [x for x in sigs if x.chart_path is not None]
        assert charted, "expected at least one charted signal"
        aapl = next(x for x in charted if x.ticker == "AAPL")
        # The stored chart_path is the literal KEY, not a filesystem path.
        assert aapl.chart_path == expected_key

    # upload_chart called once with the local png path and that exact key.
    assert len(uploads) == 1
    local_path, key = uploads[0]
    assert key == expected_key
    assert str(local_path) == str(rendered["path"])


def test_pipeline_blob_disabled_keeps_local_path(tmp_path, bars, monkeypatch):
    """Blob disabled -> chart_path is the local path string; upload not called."""
    monkeypatch.delenv("SWING_BLOB_ACCOUNT_URL", raising=False)

    def fake_fetch(ticker, *, cache_dir, today, cfg):
        return {"1d": _firing(bars)} if ticker == "AAPL" else {}
    monkeypatch.setattr(run, "_fetch_all_timeframes", fake_fetch)

    def fake_render(frame, ctx, zone, out_path, *, lookback=80):
        from pathlib import Path
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(_PNG)
        return out_path
    monkeypatch.setattr(run, "render_chart", fake_render)

    calls: list = []
    monkeypatch.setattr(run, "upload_chart", lambda *a, **k: calls.append(a))

    today = date(2024, 4, 1)
    db = f"sqlite:///{tmp_path / 'db.sqlite'}"
    run.run_screen(
        universe_path=_write_universe(tmp_path, ["AAPL"]), db_url=db,
        cache_dir=tmp_path / "cache", chart_dir=tmp_path / "charts", today=today,
        cfg=_NO_EXT_GATE,
    )

    expected_local = str(tmp_path / "charts" / f"AAPL_1d_{today:%Y%m%d}.png")
    with Session(get_engine(db)) as s:
        sigs = _signals_for(s, today)
        aapl = next(x for x in sigs if x.ticker == "AAPL" and x.chart_path)
        assert aapl.chart_path == expected_local
    assert calls == []  # upload never called when disabled


# ---------------------------------------------------------------------------
# pdf consumer
# ---------------------------------------------------------------------------
def _pick(ticker="AMD", chart=None):
    return PdfPick(
        ticker=ticker, name="Advanced Micro Devices", trade_type="medium", score=0.92,
        chart_path=chart,
        entry_floor=96.0, entry_ceiling=101.0, stop=95.0, target=110.0, risk_reward=1.5,
        quality_tier="reputable", volatility_tier="high", oversold=False, mtf_aligned=True,
        atr_pct=0.04,
        rationale="AMD daily uptrend intact; shallow pullback held EMA50.",
    )


def test_pdf_blob_branch_downloads_key_without_stat(tmp_path, monkeypatch):
    """Blob enabled: a key-shaped chart_path is fetched via download_bytes and
    embedded; the filesystem is NOT stat'd for it."""
    monkeypatch.setenv("SWING_BLOB_ACCOUNT_URL", "https://acct.blob.core.windows.net")

    seen: list[str] = []

    def fake_download(key):
        seen.append(key)
        return _PNG
    monkeypatch.setattr(pdf_mod, "download_bytes", fake_download)

    # Guard: if pdf stats the (non-existent) key path, it would skip the image.
    key = "20240401/AMD_1d_20240401.png"
    out = build_digest_pdf([_pick("AMD", key)], tmp_path / "digest.pdf")
    assert out.exists()
    assert out.read_bytes()[:4] == b"%PDF"
    assert seen == [key]  # download_bytes called with the exact key


def test_pdf_blob_branch_degrades_when_download_raises(tmp_path, monkeypatch):
    """Blob enabled but the blob is missing/aged-out: degrade to a chartless
    section without crashing."""
    monkeypatch.setenv("SWING_BLOB_ACCOUNT_URL", "https://acct.blob.core.windows.net")

    def boom(key):
        raise RuntimeError("blob 404")
    monkeypatch.setattr(pdf_mod, "download_bytes", boom)

    out = build_digest_pdf(
        [_pick("AMD", "20240401/AMD_1d_20240401.png")], tmp_path / "digest.pdf"
    )
    assert out.exists()
    assert out.read_bytes()[:4] == b"%PDF"  # built fine, just no image


def test_pdf_local_branch_unchanged_when_blob_disabled(tmp_path, monkeypatch):
    """Blob disabled: existing local-path .exists() behavior is preserved."""
    monkeypatch.delenv("SWING_BLOB_ACCOUNT_URL", raising=False)
    chart = tmp_path / "AMD.png"
    chart.write_bytes(_PNG)
    out = build_digest_pdf(
        [_pick("AMD", str(chart)), _pick("AEP", None)], tmp_path / "digest.pdf"
    )
    assert out.exists()
    assert out.read_bytes()[:4] == b"%PDF"


# ---------------------------------------------------------------------------
# byte resolvers (shared by the dashboard and the cockpit API)
# ---------------------------------------------------------------------------
def test_resolve_chart_bytes_blob_enabled_downloads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWING_BLOB_ACCOUNT_URL", "https://acct.blob.core.windows.net")
    seen: list[str] = []
    monkeypatch.setattr(blob, "download_bytes", lambda key: seen.append(key) or _PNG)

    key = "20240401/AMD_1d_20240401.png"
    assert blob.resolve_chart_bytes(key) == _PNG
    assert seen == [key]


def test_resolve_chart_bytes_blob_disabled_reads_local_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SWING_BLOB_ACCOUNT_URL", raising=False)
    # Existing local file -> its BYTES (FastAPI serves bytes, unlike st.image which
    # also took a path); missing local file -> None (skip image).
    chart = tmp_path / "AMD.png"
    chart.write_bytes(_PNG)
    assert blob.resolve_chart_bytes(str(chart)) == _PNG
    assert blob.resolve_chart_bytes(str(tmp_path / "missing.png")) is None
    assert blob.resolve_chart_bytes(None) is None
    assert blob.resolve_chart_bytes("") is None


def test_resolve_chart_bytes_blob_enabled_degrades_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SWING_BLOB_ACCOUNT_URL", "https://acct.blob.core.windows.net")

    def boom(key: str) -> bytes:
        raise RuntimeError("blob 404")

    monkeypatch.setattr(blob, "download_bytes", boom)
    assert blob.resolve_chart_bytes("20240401/AMD_1d_20240401.png") is None


def test_resolve_pdf_bytes_blob_enabled_downloads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWING_BLOB_ACCOUNT_URL", "https://acct.blob.core.windows.net")
    seen: list[str] = []
    monkeypatch.setattr(blob, "download_bytes", lambda key: seen.append(key) or b"%PDF-fake")

    key = "reports/AMD_20240401.pdf"
    assert blob.resolve_pdf_bytes(key) == b"%PDF-fake"
    assert seen == [key]


def test_resolve_pdf_bytes_blob_disabled_reads_local_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SWING_BLOB_ACCOUNT_URL", raising=False)
    pdf = tmp_path / "AMD.pdf"
    pdf.write_bytes(b"%PDF-fake")
    assert blob.resolve_pdf_bytes(str(pdf)) == b"%PDF-fake"
    assert blob.resolve_pdf_bytes(str(tmp_path / "missing.pdf")) is None
    assert blob.resolve_pdf_bytes(None) is None


def test_resolve_pdf_bytes_blob_enabled_degrades_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SWING_BLOB_ACCOUNT_URL", "https://acct.blob.core.windows.net")

    def boom(key: str) -> bytes:
        raise RuntimeError("blob 404")

    monkeypatch.setattr(blob, "download_bytes", boom)
    assert blob.resolve_pdf_bytes("reports/AMD_20240401.pdf") is None
