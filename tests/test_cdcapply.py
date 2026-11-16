import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from cdcapply.apply import CDCApplier
from cdcapply.cli import run
from cdcapply.events import parse_events
from cdcapply.store import CDCStore, PHYSICAL_DELETE, SOFT_DELETE


def event(event_id, op, primary_key, txn, ts, after=None, before=None):
    value = {
        "event_id": event_id,
        "op": op,
        "primary_key": primary_key,
        "txn": txn,
        "ts": ts,
    }
    if before is not None:
        value["before"] = before
    if after is not None:
        value["after"] = after
    return value


def insert(event_id, key, txn, ts, name=None, value=None):
    return event(
        event_id,
        "insert",
        key,
        txn,
        ts,
        after={"name": name or key, "value": value if value is not None else int(key[1:])},
    )


def twelve_events():
    return [
        insert("e-ins-k1", "k1", "t1", 10),
        insert("e-ins-k2", "k2", "t2", 10),
        insert("e-ins-k3", "k3", "t3", 10),
        insert("e-ins-k4", "k4", "t4", 10),
        insert("e-ins-k5", "k5", "t5", 10),
        insert("e-ins-k6", "k6", "t6", 10),
        event(
            "e-upd-k1",
            "update",
            "k1",
            "t7",
            12,
            before={"name": "k1", "value": 1},
            after={"name": "k1-updated", "value": 11},
        ),
        event(
            "e-upd-k7",
            "update",
            "k7",
            "t8",
            12,
            after={"name": "k7", "value": 7},
        ),
        event("e-del-k2", "delete", "k2", "t9", 13, before={"name": "k2", "value": 2}),
        event("e-del-k3", "delete", "k3", "t10", 14, before={"name": "k3", "value": 3}),
        insert("e-ins-k1", "k1", "t11", 15, name="duplicate-must-not-apply", value=999),
        event(
            "e-late-k4",
            "update",
            "k4",
            "t12",
            9,
            after={"name": "late-k4", "value": 400},
        ),
    ]


def apply_payload(payload, temp_dir, db_name="cdc.db", delete_mode=SOFT_DELETE):
    db_path = os.path.join(temp_dir, db_name)
    store = CDCStore(db_path, delete_mode=delete_mode)
    applier = CDCApplier(store)
    report = applier.apply(parse_events(payload))
    return store, report


class CDCApplyTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def test_twelve_event_fixture_soft_delete_counts_and_rows(self):
        store, report = apply_payload(twelve_events(), self.temp_dir.name)
        self.addCleanup(store.close)

        rows = store.all_rows()
        self.assertEqual([json.loads(row["row_json"])["primary_key"] for row in rows], [f"k{i}" for i in range(1, 8)])
        self.assertEqual(store.row_count(), 7)
        by_key = {json.loads(row["row_json"])["primary_key"]: json.loads(row["row_json"]) for row in rows}
        self.assertTrue(by_key["k2"]["deleted"])
        self.assertTrue(by_key["k3"]["deleted"])
        self.assertFalse(by_key["k1"]["deleted"])
        self.assertEqual(report.summary.applied, 10)
        self.assertEqual(report.summary.skipped, 2)

    def test_duplicate_event_id_does_not_change_row(self):
        store, report = apply_payload(twelve_events(), self.temp_dir.name)
        self.addCleanup(store.close)

        row = json.loads(store.get_row("k1")["row_json"])
        self.assertEqual(row["name"], "k1-updated")
        self.assertEqual(row["value"], 11)
        self.assertEqual(report.summary.duplicates, 1)
        duplicate_entry = next(item for item in report.entries if item.event_id == "e-ins-k1" and not item.applied)
        self.assertEqual(duplicate_entry.table_rows, 7)
        self.assertIn("duplicate", duplicate_entry.reason)

    def test_out_of_order_event_is_dropped_and_counted(self):
        store, report = apply_payload(twelve_events(), self.temp_dir.name)
        self.addCleanup(store.close)

        row = json.loads(store.get_row("k4")["row_json"])
        self.assertEqual(row["name"], "k4")
        self.assertEqual(row["value"], 4)
        self.assertEqual(report.summary.out_of_order_dropped, 1)
        late_entry = next(item for item in report.entries if item.event_id == "e-late-k4")
        self.assertFalse(late_entry.applied)
        self.assertIn("out of order", late_entry.reason)

    def test_invalid_transaction_is_atomically_rolled_back(self):
        store, report = apply_payload(twelve_events(), self.temp_dir.name)
        self.addCleanup(store.close)
        before_rows = {row["primary_key"]: row["row_json"] for row in store.all_rows()}
        before_count = store.row_count()

        invalid_transaction = [
            event(
                "e-txn-valid",
                "insert",
                "k8",
                "bad-txn",
                20,
                after={"name": "k8", "value": 8},
            ),
            event(
                "e-txn-invalid",
                "insert",
                "k9",
                "bad-txn",
                21,
                after={"name": "k9"},
            ),
        ]
        rollback_report = CDCApplier(store).apply(parse_events(invalid_transaction))

        self.assertEqual(store.row_count(), before_count)
        self.assertEqual({row["primary_key"]: row["row_json"] for row in store.all_rows()}, before_rows)
        self.assertIsNone(store.get_row("k8"))
        self.assertIsNone(store.get_row("k9"))
        self.assertEqual(rollback_report.summary.rolled_back_txns, 1)
        self.assertEqual(rollback_report.summary.applied, 0)
        self.assertEqual(rollback_report.summary.skipped, 2)
        self.assertTrue(all(not item.applied for item in rollback_report.entries))
        self.assertTrue(all("事务被回滚" in item.reason for item in rollback_report.entries))

    def test_physical_and_soft_delete_modes(self):
        physical_store, physical_report = apply_payload(
            twelve_events(), self.temp_dir.name, db_name="physical.db", delete_mode=PHYSICAL_DELETE
        )
        self.addCleanup(physical_store.close)
        soft_store, soft_report = apply_payload(
            twelve_events(), self.temp_dir.name, db_name="soft.db", delete_mode=SOFT_DELETE
        )
        self.addCleanup(soft_store.close)

        self.assertEqual(physical_store.row_count(), 5)
        self.assertIsNone(physical_store.get_row("k2"))
        self.assertIsNone(physical_store.get_row("k3"))
        self.assertEqual(physical_report.summary.applied, 10)

        self.assertEqual(soft_store.row_count(), 7)
        soft_k2 = json.loads(soft_store.get_row("k2")["row_json"])
        soft_k3 = json.loads(soft_store.get_row("k3")["row_json"])
        self.assertTrue(soft_k2["deleted"])
        self.assertTrue(soft_k3["deleted"])
        self.assertEqual(soft_report.summary.applied, 10)

    def test_different_arrival_orders_produce_byte_identical_table(self):
        payload = twelve_events()
        order_a = [payload[index] for index in [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]]
        order_b = [payload[index] for index in [5, 4, 3, 2, 1, 0, 7, 6, 9, 8, 10, 11]]

        store_a, report_a = apply_payload(order_a, self.temp_dir.name, db_name="a.db")
        self.addCleanup(store_a.close)
        store_b, report_b = apply_payload(order_b, self.temp_dir.name, db_name="b.db")
        self.addCleanup(store_b.close)

        hash_a = hashlib.sha256(store_a.snapshot_json_lines()).hexdigest()
        hash_b = hashlib.sha256(store_b.snapshot_json_lines()).hexdigest()
        self.assertEqual(hash_a, hash_b)
        self.assertEqual(store_a.snapshot_json_lines(), store_b.snapshot_json_lines())
        self.assertEqual(report_a.summary.out_of_order_dropped, 1)
        self.assertEqual(report_b.summary.out_of_order_dropped, 1)
        self.assertEqual(report_a.summary.applied, 10)
        self.assertEqual(report_b.summary.applied, 10)

    def test_every_report_entry_has_exactly_the_required_fields(self):
        store, report = apply_payload(twelve_events(), self.temp_dir.name)
        self.addCleanup(store.close)

        required = {"event_id", "op", "applied", "reason", "table_rows"}
        self.assertTrue(report.entries)
        for entry in report.entries:
            self.assertEqual(set(entry.__dict__.keys()), required)

    def test_cli_runs_demo_and_writes_report(self):
        db_path = os.path.join(self.temp_dir.name, "cli.db")
        report_path = os.path.join(self.temp_dir.name, "report.json")
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = run(
                [
                    "--events",
                    "demo/events.jsonl",
                    "--db",
                    db_path,
                    "--out",
                    report_path,
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertIn("events=12", output.getvalue())
        self.assertIn("applied=10", output.getvalue())
        self.assertIn("skipped=2", output.getvalue())
        self.assertIn("final_rows=7", output.getvalue())
        with open(report_path, "r", encoding="utf-8") as source:
            persisted = json.load(source)
        self.assertEqual(persisted["summary"]["applied"], 10)
        self.assertEqual(persisted["summary"]["skipped"], 2)
        self.assertTrue(os.path.exists(db_path))

    def test_duplicate_event_id_within_same_input_is_idempotent(self):
        payload = [
            insert("e-same-id", "k1", "t1", 10, name="first", value=1),
            insert("e-same-id", "k1", "t2", 11, name="second", value=2),
        ]
        store, report = apply_payload(payload, self.temp_dir.name)
        self.addCleanup(store.close)

        row = json.loads(store.get_row("k1")["row_json"])
        self.assertEqual(row["name"], "first")
        self.assertEqual(row["value"], 1)
        self.assertEqual(store.row_count(), 1)
        self.assertEqual(report.summary.applied, 1)
        self.assertEqual(report.summary.skipped, 1)
        self.assertEqual(report.summary.duplicates, 1)

    def test_reused_transaction_id_after_another_txn_is_separate_segment(self):
        payload = [
            insert("e-a", "k1", "shared", 10, name="a", value=1),
            insert("e-b", "k2", "other", 11, name="b", value=2),
            insert("e-c", "k3", "shared", 12, name="c", value=3),
        ]
        store, report = apply_payload(payload, self.temp_dir.name)
        self.addCleanup(store.close)

        self.assertEqual(store.row_count(), 3)
        self.assertEqual(report.summary.applied, 3)
        self.assertEqual(report.summary.rolled_back_txns, 0)

    def test_idempotency_survives_applier_restart_with_same_database(self):
        db_path = os.path.join(self.temp_dir.name, "restart.db")
        payload = twelve_events()

        first_store = CDCStore(db_path)
        first_report = CDCApplier(first_store).apply(parse_events(payload))
        first_store.close()

        second_store = CDCStore(db_path)
        self.addCleanup(second_store.close)
        second_report = CDCApplier(second_store).apply(parse_events(payload))

        self.assertEqual(first_report.summary.applied, 10)
        self.assertEqual(second_store.row_count(), 7)
        self.assertEqual(second_report.summary.applied, 0)
        self.assertEqual(second_report.summary.skipped, 12)
        self.assertEqual(second_report.summary.duplicates, 11)
        self.assertEqual(second_report.summary.out_of_order_dropped, 1)

    def test_update_without_before_inserts_missing_primary_key(self):
        payload = [
            event(
                "e-upsert",
                "update",
                "k-new",
                "t-new",
                5,
                after={"name": "new", "value": 42},
            )
        ]
        store, report = apply_payload(payload, self.temp_dir.name)
        self.addCleanup(store.close)

        self.assertEqual(store.row_count(), 1)
        row = json.loads(store.get_row("k-new")["row_json"])
        self.assertEqual(row["name"], "new")
        self.assertEqual(row["value"], 42)
        self.assertFalse(row["deleted"])
        self.assertEqual(report.summary.applied, 1)

    def test_missing_primary_key_rolls_back_entire_transaction(self):
        payload = [
            insert("e-valid", "k1", "bad-txn", 1, name="valid", value=1),
            {
                "event_id": "e-missing-key",
                "op": "delete",
                "txn": "bad-txn",
                "ts": 2,
            },
        ]
        store, report = apply_payload(payload, self.temp_dir.name)
        self.addCleanup(store.close)

        self.assertEqual(store.row_count(), 0)
        self.assertEqual(report.summary.rolled_back_txns, 1)
        self.assertTrue(all(not entry.applied for entry in report.entries))
        self.assertTrue(all("事务被回滚" in entry.reason for entry in report.entries))


if __name__ == "__main__":
    unittest.main()
