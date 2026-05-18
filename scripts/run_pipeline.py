"""
run_pipeline.py — Integration demo for Phases 1+2+3.
Runs detection → tracking → memory for each frame of a video.
"""
from __future__ import annotations
import sys
sys.path.insert(0, ".")

import cv2, time, logging
from services.detection.detector import Detector
from services.tracking.tracker   import Tracker
from services.tracking.visualizer import draw_tracks
from services.memory.memory       import MemoryStore
from services.memory.pipeline     import process_tracked_frame

logging.basicConfig(level=logging.INFO, format="%(name)s | %(levelname)s | %(message)s")
logger = logging.getLogger("pipeline")

SOURCE = "data/sample_videos/sample.mp4"

cap      = cv2.VideoCapture(SOURCE)
fps      = cap.get(cv2.CAP_PROP_FPS) or 30
detector = Detector()
tracker  = Tracker(fps=fps)
store    = MemoryStore()

frame_id = 0
while True:
    ret, frame = cap.read()
    if not ret:
        break

    # Phase 1 — Detection
    det_frame     = detector.detect(frame, frame_id=frame_id)

    # Phase 2 — Tracking
    tracked_frame = tracker.update(det_frame, frame)

    # Phase 3 — Memory
    events        = process_tracked_frame(tracked_frame, store)

    # Log sequences every 90 frames (~3s)
    if frame_id % 90 == 0:
        for track in tracked_frame.tracks:
            seq = store.get_sequence(track.track_id)
            logger.info(
                f"Track #{track.track_id} | events={len(seq.events)} | "
                f"summary={seq.action_summary} | dwell={seq.total_dwell:.1f}s"
            )

    annotated = draw_tracks(frame, tracked_frame)
    cv2.imshow("Agentic Vision — Phase 1+2+3", annotated)
    if cv2.waitKey(1) & 0xFF == ord("q"):
        break

    frame_id += 1

def process_frames(frames, detector=None, tracker=None, memory_service=None, show: bool = False):
    """Process a list of frames through detection, tracking, and memory.

    Returns a simple result object with attributes used by tests.
    """
    from dataclasses import dataclass

    @dataclass
    class Result:
        processed_frames: int = 0
        events: list = None
        action_summary: str = ""

    det = detector or Detector()
    trk = tracker or Tracker(fps=30)
    mem = memory_service or MemoryStore()

    res = Result(processed_frames=0, events=[])

    frame_id = 0
    for frame in frames:
        det_frame = det.detect(frame, frame_id=frame_id)
        tracked_frame = trk.update(det_frame, frame)
        # If memory_service implements handle_lifecycle_event (MemoryService),
        # use lifecycle events emitted by the tracker. Otherwise fall back to
        # the legacy process_tracked_frame which expects a MemoryStore.
        if hasattr(mem, "handle_lifecycle_event"):
            # Drain lifecycle events from tracker and store via MemoryService
            for evt in trk.drain_lifecycle_events():
                mem.handle_lifecycle_event(evt, embedding=None)
                res.events.append(evt)
        else:
            evts = process_tracked_frame(tracked_frame, mem)
            res.events.extend(evts or [])
        res.processed_frames += 1
        if tracked_frame.tracks:
            # simple action summary from first track sequence
            first_tid = tracked_frame.tracks[0].track_id
            if hasattr(mem, "get_sequence"):
                seq = mem.get_sequence(first_tid)
            elif hasattr(mem, "_r"):
                # MemoryService: create a temporary MemoryStore using same Redis
                from services.memory.memory import MemoryStore

                tmp = MemoryStore(redis_client=mem._r)
                seq = tmp.get_sequence(first_tid)
            else:
                seq = None

            if seq and getattr(seq, "action_summary", ""):
                res.action_summary = seq.action_summary
            else:
                # Fallback: build a simple summary from lifecycle events
                if res.events:
                    actions = [e.event.value for e in res.events]
                    unique = []
                    for a in actions:
                        if not unique or unique[-1] != a:
                            unique.append(a)
                    res.action_summary = " -> ".join(unique)
        frame_id += 1
    return res


if __name__ == "__main__":
    cap.release()
    cv2.destroyAllWindows()