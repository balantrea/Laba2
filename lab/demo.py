import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

import psycopg
import pyarrow as pa
from confluent_kafka import Consumer, KafkaError
from pyiceberg.catalog import load_catalog

from history import FIELDS, decode, materialize

TOPIC_PREFIX = "p2"
CONNECTOR = "p2-postgres"
TOPICS = [f"{TOPIC_PREFIX}.public.{entity}" for entity in FIELDS]
CONNECT_URL = os.environ.get("CONNECT_URL", "http://connect:8083")

CHANGES = [
    """UPDATE accounts SET owner_name = 'Борис Иванов',
         effective_at = '2026-03-04T10:00:00Z'
       WHERE id = 101 AND owner_name = 'Анна Орлова'""",
    """UPDATE orders SET status = 'PAID', effective_at = '2026-03-02T11:00:00Z'
       WHERE id = 7001 AND status = 'CREATED'""",
    """UPDATE accounts SET effective_at = '2026-03-05T10:00:00Z'
       WHERE id = 101 AND effective_at = '2026-03-04T10:00:00Z'""",
    """UPDATE orders SET status = 'CANCELLED', effective_at = '2026-03-06T09:00:00Z'
       WHERE id = 7001 AND status = 'PAID'""",
    """UPDATE accounts SET owner_name = 'Вера Смирнова',
         effective_at = '2026-03-08T10:00:00Z'
       WHERE id = 101 AND owner_name = 'Борис Иванов'""",
    """INSERT INTO orders VALUES (7002, 'CREATED', '2026-03-01T12:00:00Z')
       ON CONFLICT DO NOTHING""",
    "DELETE FROM orders WHERE id = 7002",
]


def connect_api(path, config=None):
    body = json.dumps(config).encode() if config is not None else None
    request = Request(CONNECT_URL + path, body, {"Content-Type": "application/json"},
                      method="PUT" if config is not None else "GET")
    with urlopen(request, timeout=8) as response:
        return json.load(response)


def ensure_connector():
    config = {
        "connector.class": "io.debezium.connector.postgresql.PostgresConnector",
        "database.hostname": "postgres",
        "database.port": "5432",
        "database.user": "cdc",
        "database.password": "cdc",
        "database.dbname": "operations",
        "topic.prefix": TOPIC_PREFIX,
        "plugin.name": "pgoutput",
        "slot.name": "p2_cdc",
        "publication.name": "p2_publication",
        "publication.autocreate.mode": "disabled",
        "table.include.list": "public.accounts,public.orders",
        "snapshot.mode": "initial",
        "provide.transaction.metadata": "true",
        "key.converter": "org.apache.kafka.connect.json.JsonConverter",
        "key.converter.schemas.enable": "false",
        "value.converter": "org.apache.kafka.connect.json.JsonConverter",
        "value.converter.schemas.enable": "false",
        "tombstones.on.delete": "true",
        "topic.creation.default.replication.factor": "1",
        "topic.creation.default.partitions": "1",
    }
    deadline = time.monotonic() + 150
    last_error = None
    while time.monotonic() < deadline:
        try:
            try:
                connect_api(f"/connectors/{CONNECTOR}/config")
            except HTTPError as exc:
                if exc.code != 404:
                    raise
                connect_api(f"/connectors/{CONNECTOR}/config", config)

            status = connect_api(f"/connectors/{CONNECTOR}/status")
            for item in [status["connector"], *status["tasks"]]:
                if item["state"] == "FAILED":
                    raise RuntimeError(item.get("trace", status))
            if status["tasks"] and all(task["state"] == "RUNNING" for task in status["tasks"]):
                print(f"Debezium {CONNECTOR}: RUNNING, task RUNNING")
                return
        except HTTPError as exc:
            if exc.code not in (404, 409, 500, 502, 503):
                raise RuntimeError(exc.read().decode()) from exc
            last_error = exc
        except (URLError, TimeoutError) as exc:
            last_error = exc
        time.sleep(2)
    raise TimeoutError(f"Debezium did not start: {last_error}")


def apply_changes():
    with psycopg.connect(os.environ["POSTGRES_DSN"]) as connection:
        owner = connection.execute(
            "SELECT owner_name FROM accounts WHERE id = 101 FOR UPDATE"
        ).fetchone()[0]
        status = connection.execute("SELECT status FROM orders WHERE id = 7001").fetchone()[0]
        order_7002 = connection.execute("SELECT 1 FROM orders WHERE id = 7002").fetchone()
        if (owner, status) == ("Вера Смирнова", "CANCELLED") and order_7002 is None:
            return
        for statement in CHANGES:
            connection.execute(statement)


def read_until(consumer, events, tombstones, ready):
    deadline = time.monotonic() + 100
    while time.monotonic() < deadline:
        message = consumer.poll(1)
        if message is None:
            continue
        if message.error():
            if message.error().code() in (KafkaError.UNKNOWN_TOPIC_OR_PART, KafkaError._UNKNOWN_TOPIC):
                continue
            raise RuntimeError(message.error())
        record = decode(message.topic(), message.partition(), message.offset(),
                        message.key(), message.value())
        if record is None:
            tombstones.add((message.topic(), json.loads(message.key())["id"]))
            print(f"Kafka {message.topic()} p{message.partition()}:{message.offset()} tombstone")
        else:
            events.append(record)
            print(f"Kafka {record.topic} p{record.partition}:{record.offset} "
                  f"op={record.op} id={record.key} {record.value}")
        if ready():
            return
    raise TimeoutError(f"Expected Kafka records did not arrive (read {len(events)} events)")


def write_history(events):
    root = Path(os.environ.get("WAREHOUSE_DIR", "/warehouse")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    catalog = load_catalog("lab", type="sql", uri=f"sqlite:///{root / 'catalog.db'}",
                           warehouse=(root / "iceberg").as_uri())
    catalog.create_namespace_if_not_exists("history")

    tables = {}
    for entity, value_name in FIELDS.items():
        schema = pa.schema([
            pa.field("id", pa.int64(), nullable=False),
            pa.field(value_name, pa.string(), nullable=False),
            pa.field("valid_from", pa.timestamp("us", tz="UTC"), nullable=False),
            pa.field("valid_to", pa.timestamp("us", tz="UTC")),
            pa.field("is_current", pa.bool_(), nullable=False),
        ])
        rows = materialize(events, entity)
        for row in rows:
            row[value_name] = row.pop("value")
        table_name = f"history.{entity}_scd2"
        table = catalog.create_table_if_not_exists(table_name, schema=schema)
        actual = table.scan().to_arrow().to_pylist()
        key = lambda row: (row["id"], row["valid_from"])
        if sorted(actual, key=key) != rows:
            batch = pa.Table.from_pylist(rows, schema=schema)
            if table.current_snapshot() is None:
                table.append(batch)
            else:
                table.overwrite(batch)
        table = catalog.load_table(table_name)
        snapshot = table.current_snapshot()
        print(f"Iceberg {table_name}: snapshot={snapshot.snapshot_id}")
        tables[entity] = sorted(table.scan().to_arrow().to_pylist(), key=key)
    return tables


def verify(tables):
    for entity, rows in tables.items():
        print(f"\n{entity}: id | {FIELDS[entity]} | valid_from (UTC) | valid_to (UTC) | is_current")
        for row in rows:
            print(" | ".join(str(row[field]) for field in
                  ("id", FIELDS[entity], "valid_from", "valid_to", "is_current")))

    def at(entity, key, timestamp):
        return [row[FIELDS[entity]] for row in tables[entity]
                if row["id"] == key and row["valid_from"] <= timestamp
                and (row["valid_to"] is None or timestamp < row["valid_to"])]

    march_1 = datetime.fromisoformat("2026-03-01T00:00:00+00:00")
    cancelled = datetime.fromisoformat("2026-03-06T09:00:00+00:00")
    owner = at("accounts", 101, march_1)
    before = at("orders", 7001, cancelled - timedelta(microseconds=1))
    after = at("orders", 7001, cancelled)
    assert len(tables["accounts"]) == 3
    assert len(tables["orders"]) == 4
    assert owner == ["Анна Орлова"]
    assert before == ["PAID"] and after == ["CANCELLED"]
    assert len([row for row in tables["orders"] if row["id"] == 7002 and row["is_current"]]) == 0
    print(f"\nНа 1 марта счёт 101: {owner[0]}")
    print(f"Заказ 7001 до отмены: {before[0]}; в момент отмены: {after[0]}")
    print("Проверки SCD Type 2 пройдены.")


def main():
    ensure_connector()
    consumer = Consumer({
        "bootstrap.servers": os.environ.get("KAFKA_BOOTSTRAP", "kafka:9092"),
        "group.id": f"p2-replay-{uuid4()}",
        "auto.offset.reset": "earliest",
        "enable.auto.commit": False,
    })
    events, tombstones = [], set()
    try:
        consumer.subscribe(TOPICS)
        read_until(consumer, events, tombstones, lambda: {
            (e.entity, e.key) for e in events if e.op == "r"
        } >= {("accounts", 101), ("orders", 7001)})
        apply_changes()

        def scenario_observed():
            return ((TOPICS[1], 7002) in tombstones
                    and any(e.entity == "accounts" and e.value == "Вера Смирнова" for e in events)
                    and any(e.entity == "orders" and e.value == "CANCELLED" for e in events))

        read_until(consumer, events, tombstones, scenario_observed)
    finally:
        consumer.close()

    # Replay the same input twice to verify that a retry yields the same history.
    for entity in FIELDS:
        assert materialize(events + events, entity) == materialize(events, entity)
    verify(write_history(events))


if __name__ == "__main__":
    main()
