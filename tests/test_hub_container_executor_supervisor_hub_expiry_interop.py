"""Interop: a real SupervisorRoute expired statement verified by the Hub verifier vendored from 819d5aa."""
import pytest

pytest.importorskip("fcntl", reason="Route journal requires actual Linux kernel ownership")

from tests.test_hub_container_executor_supervisor_route import command, world  # noqa: F401
from tests.test_hub_container_executor_supervisor_reservation import WINDOW_NS, nonce_of, two  # noqa: F401
from tests.test_hub_container_executor_supervisor_dispatch_proof import proof, route_for  # noqa: F401
from tests.interop_hub_819d5aa.supervisor_expired_statement_verifier import (
    PinnedExpiredStatementVerifier, RefusingExpiredStatementVerifier,
)


def test_hub_verifier_accepts_real_expired_statement_and_refuses_mismatch(proof):
    route = route_for(proof)
    cmd = command(proof["epoch"])
    receipt = route.reserve(cmd).receipt
    nonce = nonce_of(route.issue_dispatch_challenge(cmd.command_id))
    proof["clock"].now += WINDOW_NS + 1
    assert route.expire_challenges() == 1
    statement = route.expired_statement(nonce)
    counter = route.inspect_reservation(cmd.command_id)[1]
    expected = {"nonce": nonce, "command_id": cmd.command_id, "counter": counter,
                "boot_id": proof["clock"].boot_id}

    hub = PinnedExpiredStatementVerifier(public_key=proof["statements"].public_key,
                                         key_id=proof["statements"].key_id)
    payload = hub.verify(statement, expected=expected)
    assert payload is not None, "Hub verifier refused a real supervisor expired statement"
    assert (payload["status"], payload["reservation_status"]) == ("expired_unconsumed", "retired")

    other_nonce = nonce_of(route.issue_dispatch_challenge(route.reserve(command(proof["epoch"])).command_id))
    assert hub.verify(statement, expected=dict(expected, nonce=other_nonce)) is None
    assert hub.verify(statement, expected=dict(expected, counter=counter + 1)) is None
    assert hub.verify(statement, expected=dict(expected, command_id=command(proof["epoch"]).command_id)) is None
    assert hub.verify(statement, expected=dict(expected, boot_id="00000000-0000-4000-8000-000000000000")) is None
    assert hub.verify(statement + b" ", expected=expected) is None
    other = PinnedExpiredStatementVerifier(public_key=proof["challenger"].public_key,
                                           key_id=proof["statements"].key_id)
    assert other.verify(statement, expected=expected) is None  # Wrong pinned key.
    assert RefusingExpiredStatementVerifier().verify(statement, expected=expected) is None
    assert receipt  # The reservation the statement retired.
    route.close()
