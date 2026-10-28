"""The dog runs off the dispatch loop, but never two at once.

Its tools change process-wide state (switch_wolt rewrites the active wolt's
directories and environment), so a dog for one connector must not run while
another connector's dog does.
"""

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "container"))
sys.path.insert(0, str(ROOT / "container" / "lib"))

import chat_lodge  # noqa: E402


def test_two_dogs_never_overlap(monkeypatch):
    running, overlaps = [0], []
    guard = threading.Lock()

    def slow_dog(*args, **kwargs):
        with guard:
            running[0] += 1
            overlaps.append(running[0])
        time.sleep(0.05)
        with guard:
            running[0] -= 1
        return {}

    monkeypatch.setattr(chat_lodge, "get_response", slow_dog)
    threads = [threading.Thread(target=chat_lodge._dog_response, args=("hi",)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(overlaps) == 4 and max(overlaps) == 1
