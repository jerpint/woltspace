# jerpint: we need docs / session to brainstorm on what sparks are anymore - they were useful pre current woltspace but still
# seem to be everywhere not sure how they tie back in now
"""Spark/digest storage."""

import json
from .config import SPARKS_DIR


async def list_sparks() -> list[dict]:
    try:
        sparks = []
        for f in SPARKS_DIR.iterdir():
            if not f.name.endswith(".json"):
                continue
            try:
                data = json.loads(f.read_text())
                sparks.append({
                    "id": data["id"],
                    "type": data.get("type"),
                    "title": data.get("title"),
                    "timestamp": data.get("timestamp"),
                    "parentId": data.get("parentId"),
                })
            except Exception:
                pass
        sparks.sort(key=lambda s: s.get("timestamp", ""), reverse=True)
        return sparks
    except Exception:
        return []


async def get_spark(spark_id: str) -> dict:
    raw = (SPARKS_DIR / f"{spark_id}.json").read_text()
    return json.loads(raw)


async def get_spark_with_chain(spark_id: str) -> dict:
    spark = await get_spark(spark_id)
    all_sparks = await list_sparks()

    children = [s for s in all_sparks if s.get("parentId") == spark_id]

    # Walk back to root
    chain = [spark_id]
    current = spark
    while current.get("parentId"):
        chain.insert(0, current["parentId"])
        try:
            current = await get_spark(current["parentId"])
        except Exception:
            break

    # Walk forward to end
    child_id = children[0]["id"] if children else None
    walk_id = child_id
    while walk_id:
        chain.append(walk_id)
        next_children = [s for s in all_sparks if s.get("parentId") == walk_id]
        walk_id = next_children[0]["id"] if next_children else None

    version_index = chain.index(spark_id) if spark_id in chain else 0
    return {
        "id": spark["id"],
        "type": spark.get("type"),
        "title": spark.get("title"),
        "timestamp": spark.get("timestamp"),
        "parentId": spark.get("parentId"),
        "childId": children[0]["id"] if children else None,
        "version": version_index + 1,
        "totalVersions": len(chain),
        "html": spark.get("html", ""),
    }
