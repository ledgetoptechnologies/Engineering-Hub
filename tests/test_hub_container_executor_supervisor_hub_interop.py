"""Interop: real supervisor-signed bytes vs Codex's Hub verifiers (vendored verbatim from d7cb732).

Test-only. The vendored modules in tests/interop_hub_d7cb732/ are fixtures, never production code.
"""
import json

import pytest

pytest.importorskip("fcntl", reason="Route journal requires actual Linux kernel ownership")

from hub_container_executor.supervisor_route import challenge_expectation
from tests.interop_hub_d7cb732.supervisor_challenge_verifier import PinnedChallengeVerifier
from tests.interop_hub_d7cb732.supervisor_reservation_verifier import PinnedReservationVerifier
from tests.test_hub_container_executor_supervisor_route import LINUX, command, world  # noqa: F401
from tests.test_hub_container_executor_supervisor_reservation import make, two  # noqa: F401
from tests.test_hub_container_executor_supervisor_dispatch_proof import proof  # noqa: F401
from tests.test_hub_container_executor_supervisor_transport import env, unix_clients  # noqa: F401


def hub_verifiers(state):
    """Pin and epoch from the independently enrolled values, never from the receipt."""
    pin, epoch = state["pin"].as_dict(), state["epoch"].as_dict()
    reservations = PinnedReservationVerifier(public_key=state["signer"].public_key, key_id=state["signer"].key_id,
                                             pin=pin, epoch=epoch)
    challenges = PinnedChallengeVerifier(public_key=state["challenger"].public_key,
                                         key_id=state["challenger"].key_id, pin=pin, epoch=epoch)
    return reservations, challenges


def check(state, cmd, receipt, challenge):
    reservations, challenges = hub_verifiers(state)
    got = reservations.reservation(receipt)
    assert got is not None, "Hub reservation verifier refused a real supervisor receipt"
    assert (got["command_id"], got["dispatch_counter"]) == (cmd.command_id, json.loads(receipt)["payload"]["counter"])
    assert reservations.reservation(receipt.replace(b'"counter":', b'"counter": ')) is None  # Non-canonical.
    expected = challenge_expectation(receipt, state["pin"], state["epoch"])
    assert challenges.verify(challenge, expected=dict(expected)) is not None, "Hub refused a real challenge"
    assert challenges.verify(challenge, expected=dict(expected, counter=expected["counter"] + 1)) is None


@LINUX
def test_hub_verifiers_accept_real_supervisor_bytes_and_refuse_tamper(two):
    route = make(two)
    try:
        cmd = command(two["epoch"])
        receipt = route.reserve(cmd).receipt
        check(two, cmd, receipt, route.issue_dispatch_challenge(cmd.command_id))
    finally:
        route.close()


@LINUX
def test_hub_verifiers_accept_bytes_fetched_over_the_transport(env):
    listener, path = env["unix_server"]()
    hub, _ = unix_clients(env, path)
    cmd = command(env["proof"]["epoch"])
    reserved = hub.reserve(cmd)
    issued = hub.challenge(cmd.command_id)
    assert reserved.unverified is not None and issued.unverified is not None
    check(env["proof"], cmd, reserved.raw, issued.raw)
