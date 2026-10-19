"""PENDING REAL ACCEPTANCE — intentionally hard-disabled pending review.

Import is inert. This source phase deliberately has no Docker subprocess/API
implementation; a future reviewed implementation must replace this gate and
must be authorized separately. Fake unit tests are not acceptance evidence.
"""
from __future__ import annotations

import sys
from typing import Sequence

REVIEW_GATE = "PENDING_INDEPENDENT_SOURCE_REVIEW"
ACCEPTANCE_STATUS = "NOT_RUN_NO_DOCKER_BACKEND"


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    print(
        "Container-pilot real acceptance is blocked: independent source review, "
        "a reviewed operator-pinned backend, and separate execution approval are required.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
