"""The cross-book Metrics scoreboard endpoint -- a thin HTTP wrapper over the pure
``cockpit.scoreboard.build_scoreboard`` aggregation. Read-only (no cockpit-header
guard, matching /api/stats/performance): it serves stats and takes no action. All the
math -- the per-book cards, the ONE sanctioned real-money cross-book aggregate -- lives
in the pure module; this router only closes over the ``_session`` seam and serializes."""

from collections.abc import Callable, Iterator
from typing import Literal

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from swing_screener.cockpit.scoreboard import build_scoreboard


def build_scoreboard_router(*, _session: Callable[[], Iterator[Session]]) -> APIRouter:
    """The /api/stats/scoreboard read endpoint, closed over the app's session seam."""
    router = APIRouter()

    @router.get("/api/stats/scoreboard")
    def scoreboard(
        window: Literal["all", "90", "180", "365"] = "all",
        session: Session = Depends(_session),
    ) -> dict[str, object]:
        """Cross-book real-money scoreboard. The ONE sanctioned place a cross-book
        aggregate exists (manual_equity + live, both R, both real money) -- every other
        surface stays per-book firewalled. R cards carry a full Stat; $-only (robinhood)
        and empty (live) books carry expectancy null. See docs/plans/2026-07-12-*."""
        return build_scoreboard(session, window=window)

    return router
