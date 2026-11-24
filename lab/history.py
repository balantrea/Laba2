import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

FIELDS = {"accounts": "owner_name", "orders": "status"}
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Event:
    topic: str
    partition: int
    offset: int
    entity: str
    key: int
    op: str
    source_position: tuple
    effective_at: datetime
    value: str


def decode(topic, partition, offset, key_json, value_json):
    if value_json is None:  # Kafka tombstone is distinct from CDC DELETE.
        return None

    payload = json.loads(value_json)
    source = payload["source"]
    op = payload["op"]
    entity = source["table"]
    key = json.loads(key_json)["id"]
    row = payload["before"] if op == "d" else payload["after"]
    if op == "d":
        # There is no new business timestamp in the deleted row.
        effective_at = EPOCH + timedelta(milliseconds=source["ts_ms"])
    else:
        effective_at = datetime.fromisoformat(row["effective_at"]).astimezone(timezone.utc)

    if op == "r":
        source_position = ("snapshot", entity, key)
    else:
        tx = payload["transaction"]
        source_position = (source["lsn"], tx["id"], tx["total_order"])

    return Event(topic, partition, offset, entity, key, op, source_position,
                 effective_at, row[FIELDS[entity]])


def materialize(events, entity):
    unique = {}
    for event in sorted(events, key=lambda e: (e.topic, e.partition, e.offset)):
        if event.entity == entity:
            unique.setdefault(event.source_position, event)

    by_key = {}
    for event in unique.values():
        by_key.setdefault(event.key, []).append(event)

    rows = []
    for key, changes in sorted(by_key.items()):
        # One partition per demo table. Last WAL event wins at the same instant.
        by_time = {event.effective_at: event for event in changes}
        current = None
        for effective_at, event in sorted(by_time.items()):
            if current is not None and event.op != "d" and current["value"] == event.value:
                continue
            if current is not None:
                current["valid_to"] = effective_at
                current["is_current"] = False
                current = None
            if event.op != "d":
                current = {
                    "id": key, "value": event.value, "valid_from": effective_at,
                    "valid_to": None, "is_current": True,
                }
                rows.append(current)
    return rows
