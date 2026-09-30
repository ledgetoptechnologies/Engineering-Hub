from __future__ import annotations

from dataclasses import dataclass

from .database import MessageRepository


@dataclass(frozen=True)
class RetentionPolicy:
    acknowledged_days: int = 30
    rejected_days: int = 90

    def safe(self) -> "RetentionPolicy":
        return RetentionPolicy(max(1, self.acknowledged_days), max(1, self.rejected_days))


def cleanup(repository: MessageRepository, policy: RetentionPolicy) -> dict[str, int]:
    safe_policy = policy.safe()
    repository.expire_claims()
    return repository.cleanup(acknowledged_days=safe_policy.acknowledged_days,
                              rejected_days=safe_policy.rejected_days)
