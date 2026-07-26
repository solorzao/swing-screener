"""Message Batches transport for the deep-analysis path.

The daily/weekly/monthly digest is a scheduled email job with no user waiting, so
its per-pick analyst calls can go through the Message Batches API: 50% off ALL
tokens (input + thinking output) for a turnaround that is usually under an hour.
Server tools (``web_search``) run inside a batch exactly as in the synchronous API
-- the batch worker runs the same server-side agentic loop -- so the request shape
is byte-identical (see ``notify.analysis._analyst_kwargs``).

This module is ONLY the transport: submit N requests as one batch and poll for the
result messages. Parsing and the per-pick deterministic fallback live in
``notify.analysis`` (``analyze_convictions_batched``), which is where the sync path's
fallback already lives.
"""

import logging
import time
from collections.abc import Callable

import anthropic

log = logging.getLogger(__name__)

# Poll cadence + hard wall-clock cap. The digest ACA job's replica timeout MUST
# exceed ``max_wait_s`` (see the batch integration note in the PR) or the container
# is killed mid-poll. On timeout we return whatever ended and the caller falls back
# to the deterministic narrator for the rest. 65 min covers the "<1h" typical batch
# turnaround with headroom.
_POLL_INTERVAL_S = 30.0
_MAX_WAIT_S = 3900.0


def submit_and_poll(
    client: anthropic.Anthropic,
    requests: list[tuple[str, dict]],
    *,
    poll_interval_s: float = _POLL_INTERVAL_S,
    max_wait_s: float = _MAX_WAIT_S,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, object | None]:
    """Submit ``requests`` (each ``(custom_id, messages.create kwargs)``) as ONE batch,
    poll until it ends or ``max_wait_s`` elapses, and return ``{custom_id: result
    message}``.

    A value is ``None`` when that request errored / expired / was canceled, or when the
    batch timed out before ending -- the caller then uses its deterministic fallback for
    those ids. A per-request failure never raises. A submit/transport failure DOES
    propagate, so the caller can fall back for the whole run.
    """
    if not requests:
        return {}
    # params is the messages.create kwargs dict (same shape _analyst_kwargs builds); the
    # SDK accepts the dict at runtime -- the TypedDict cast is only to satisfy the checker.
    batch = client.messages.batches.create(
        requests=[
            {"custom_id": cid, "params": kw}  # type: ignore[typeddict-item]
            for cid, kw in requests
        ]
    )
    results: dict[str, object | None] = {cid: None for cid, _ in requests}
    deadline = monotonic() + max_wait_s
    while True:
        status = client.messages.batches.retrieve(batch.id).processing_status
        if status == "ended":
            break
        if monotonic() >= deadline:
            log.warning(
                "batch %s still %s after %.0fs; %d request(s) fall back to the narrator",
                batch.id, status, max_wait_s, len(requests),
            )
            return results
        sleep(poll_interval_s)
    for r in client.messages.batches.results(batch.id):
        if r.result.type == "succeeded":
            results[r.custom_id] = r.result.message
        else:  # errored / expired / canceled -> None -> caller falls back
            log.warning("batch request %s ended %s", r.custom_id, r.result.type)
    return results
