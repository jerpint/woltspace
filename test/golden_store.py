"""Read and write a golden transcript file.

Many scenarios share one expected result (a voice note routes like a photo,
the dog changes nothing on most paths). The file stores each distinct result
once: a later scenario with the same result is {"same_as": "<earlier id>"}.
Every scenario keeps its own key, so a behaviour change still shows up as a
diff on the scenario it touches.
"""

import json
from pathlib import Path


def load(path: Path) -> dict:
    """Every scenario's full expected result."""
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {sid: raw[entry["same_as"]] if set(entry) == {"same_as"} else entry
            for sid, entry in raw.items()}


def save(path: Path, data: dict) -> None:
    """Write the results, each distinct one once, in scenario-id order."""
    out, first = {}, {}
    for sid in sorted(data):
        key = json.dumps(data[sid], sort_keys=True, ensure_ascii=False)
        if key in first:
            out[sid] = {"same_as": first[key]}
        else:
            first[key] = sid
            out[sid] = data[sid]
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(out, indent=1, ensure_ascii=False, sort_keys=True) + "\n")
