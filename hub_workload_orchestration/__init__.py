"""Additive, source-only workload orchestration proposal."""

from .intents import (
    ProviderQueueObservation,
    ViewerProcessingIntent,
    observe_provider_info,
)

__all__ = [
    "ProviderQueueObservation",
    "ViewerProcessingIntent",
    "observe_provider_info",
]
