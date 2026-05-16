"""
Run the Phase 1 -> Phase 2 -> Phase 3 Eagle pipeline.

The production entry point can be expanded for video sources, but the core
``process_frames`` function is intentionally dependency-injected so tests can
exercise the pipeline with synthetic frames and mocked model/tracker output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import numpy as np

from libs.schemas.tracking import TrackLifecycleEvent

EmbeddingFactory = Callable[[TrackLifecycleEvent, np.ndarray], np.ndarray | None]


@dataclass
class PipelineEvent:
    frame_id: int
    track_id: int
    event: str
    global_id: str | None
    action_summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_id": self.frame_id,
            "track_id": self.track_id,
            "event": self.event,
            "global_id": self.global_id,
            "action_summary": self.action_summary,
        }


@dataclass
class PipelineResult:
    processed_frames: int = 0
    events: list[PipelineEvent] = field(default_factory=list)

    @property
    def action_summary(self) -> str:
        names = [event.event.lower() for event in self.events]
        return " -> ".join(names) if names else "no events"

    def to_dict(self) -> dict[str, Any]:
        return {
            "processed_frames": self.processed_frames,
            "action_summary": self.action_summary,
            "events": [event.to_dict() for event in self.events],
        }


def process_frames(
    frames: Iterable[np.ndarray],
    *,
    detector: Any,
    tracker: Any,
    memory_service: Any,
    embedding_factory: EmbeddingFactory | None = None,
) -> PipelineResult:
    """
    Process frames through detection, tracking, and memory persistence.

    Args:
        frames: Synthetic or real BGR frames.
        detector: Object exposing ``detect(frame, frame_id=...)``.
        tracker: Object exposing ``update(...)`` and ``drain_lifecycle_events()``.
        memory_service: Object exposing ``handle_lifecycle_event(...)``.
        embedding_factory: Optional callback that supplies ReID embeddings.

    Returns:
        PipelineResult containing lifecycle events observed by the memory layer.
    """
    result = PipelineResult()

    for frame_id, frame in enumerate(frames):
        detection_frame = detector.detect(frame, frame_id=frame_id)
        tracker.update(detection_frame, frame)

        for lifecycle_event in tracker.drain_lifecycle_events():
            embedding = (
                embedding_factory(lifecycle_event, frame) if embedding_factory is not None else None
            )
            global_id = memory_service.handle_lifecycle_event(
                lifecycle_event,
                embedding=embedding,
            )
            result.events.append(
                PipelineEvent(
                    frame_id=lifecycle_event.frame_id,
                    track_id=lifecycle_event.track_id,
                    event=lifecycle_event.event.value,
                    global_id=global_id,
                    action_summary=lifecycle_event.event.value.lower(),
                )
            )

        result.processed_frames += 1

    return result
