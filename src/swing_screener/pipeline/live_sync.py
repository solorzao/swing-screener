"""Shared live-book sync: the ONE definition of *when the live book gets reconciled*.

Extracted from ``pipeline.run``'s screen-run posture (Task 11) so the evening screen
and the hourly exit job can never drift on it: reconcile when the execution mode is
``live`` with a buildable broker, OR whenever OPEN live exposure exists -- even
disarmed. Disarming used to stop the reconcile precisely when the operator was
trying to reduce risk, leaving the live book dark while positions sat at the venue
(2026-07 review). Broker construction for the on-demand path is guarded: missing
broker secrets must degrade to a loud warning, never kill the caller -- the screen's
core job is persisting the day's signals, the hourly job's is the exit alerts.
"""

import logging
from datetime import date

from sqlalchemy.orm import Session

from swing_screener.db import repo
from swing_screener.pipeline.broker import BrokerClient
from swing_screener.pipeline.broker_alpaca import build_broker
from swing_screener.pipeline.reconcile import reconcile_live
from swing_screener.settings import Settings, load_settings

log = logging.getLogger(__name__)


def maybe_reconcile_live(
    session: Session, *, today: date,
    broker: BrokerClient | None = None,
    settings: Settings | None = None,
) -> tuple[int, BrokerClient | None]:
    """Reconcile the live book when it matters; returns ``(changes, broker)``.

    "When it matters": ``execution_mode == "live"`` with a broker in hand or
    buildable, OR open live exposure exists (even disarmed -- the screen-run
    posture this extracts). A caller's ``broker`` (an injected test double, or
    the screen's already-built live-mode broker) always wins; otherwise one is
    built on demand from ``settings`` -- guarded, so a secrets/config gap
    degrades to a loud warning and a 0 count, never a raise. The change count is
    0 on every degraded path.

    THE BUILD GATE IS DELIBERATELY WIDER THAN THE SCREEN NEEDS: the ``mode ==
    "live"`` arm of the on-demand build exists for the HOURLY caller
    (``pipeline.exitcheck``), which has no pre-built broker of its own. It is
    unreachable from ``run_screen`` today -- live mode there builds its broker
    before this call (``run.py``'s "if broker is None and execution_mode ==
    'live'") -- so for the screen only the open-exposure arm can fire. Do not
    "simplify" it away on the strength of the screen path alone.

    The broker rides back in the tuple deliberately: when the on-demand build
    fired (mode off + open exposure), the caller's guardrails consult right
    after this must reuse it -- a fresh trip's sweep should run NOW, not defer a
    cycle because the extraction dropped the broker on the floor (the old inline
    block in ``run.py`` left the built broker in scope for the consult).
    """
    settings = settings if settings is not None else load_settings()
    open_exposure = bool(repo.load_open_live_trades(session))
    if (broker is None and settings.broker
            and (settings.execution_mode == "live" or open_exposure)):
        try:
            broker = build_broker(settings)
        except Exception:  # noqa: BLE001 -- a secrets/config gap must not abort the caller
            log.warning("live book needs a reconcile but the broker could not be "
                        "built; live book NOT reconciled this run", exc_info=True)
    if broker is not None and (settings.execution_mode == "live" or open_exposure):
        n_reconciled = reconcile_live(session, broker, today=today)
        log.info("live reconcile: %d change(s)", n_reconciled)
        return n_reconciled, broker
    return 0, broker
