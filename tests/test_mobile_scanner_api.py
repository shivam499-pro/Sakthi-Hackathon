"""
Phase 1 Unit Tests for Smart Export Packing Mobile Scanner.
Tests:
- Payload parsing and validation (pipe format, JSON format, booleans, decimals, malformed inputs)
- Persistent nonce idempotency and duplicate request protection
- Mobile database isolation and initial order setup
- Zero writes to production databases
- Atomic transaction rollback on simulated persistence failure
- Persistence and idempotency across database close/reopen cycles
"""

from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from contextlib import closing

import database
import main
import mobile_scanner_core


def compute_file_hash(path: Path) -> str:
    if not path.exists():
        return ""
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest().upper()


class TestMobileScannerPhase1(unittest.TestCase):

    def setUp(self):
        # Always use an isolated temporary directory for test databases
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_db_path = Path(self.temp_dir.name) / "packing_verification_mobile_test.db"

        # Baseline hashes of existing database files to verify isolation
        self.prod_db_path = Path("packing_verification.db")
        self.backup_db_path = Path("packing_verification_backup.db")
        self.hw_test_db_path = Path("packing_verification_hardware_test.db")
        self.ng_test_db_path = Path("packing_verification_ng_test.db")

        self.baseline_hashes = {
            self.prod_db_path: compute_file_hash(self.prod_db_path),
            self.backup_db_path: compute_file_hash(self.backup_db_path),
            self.hw_test_db_path: compute_file_hash(self.hw_test_db_path),
            self.ng_test_db_path: compute_file_hash(self.ng_test_db_path),
        }

    def tearDown(self):
        self.temp_dir.cleanup()

        # Verify that no test touched existing databases
        for path, baseline_hash in self.baseline_hashes.items():
            if baseline_hash:
                current_hash = compute_file_hash(path)
                self.assertEqual(
                    current_hash,
                    baseline_hash,
                    f"CRITICAL: Database {path} was modified during test!",
                )

    # ----------------------------------------------------
    # Payload Parsing & Strict Validation Tests
    # ----------------------------------------------------

    def test_parse_valid_pipe_format(self):
        part_id, variant, qty = mobile_scanner_core.parse_scan_payload("PART-A|LH|1")
        self.assertEqual(part_id, "PART-A")
        self.assertEqual(variant, "LH")
        self.assertEqual(qty, 1)

    def test_parse_valid_pipe_format_with_spaces(self):
        part_id, variant, qty = mobile_scanner_core.parse_scan_payload(" PART-B | RH | 2 ")
        self.assertEqual(part_id, "PART-B")
        self.assertEqual(variant, "RH")
        self.assertEqual(qty, 2)

    def test_parse_valid_json_string(self):
        payload = '{"part_id": "PART-C", "variant": "Standard", "quantity": 4}'
        part_id, variant, qty = mobile_scanner_core.parse_scan_payload(payload)
        self.assertEqual(part_id, "PART-C")
        self.assertEqual(variant, "Standard")
        self.assertEqual(qty, 4)

    def test_parse_valid_dict(self):
        payload = {"part_id": "PART-A", "variant": "LH", "quantity": 2}
        part_id, variant, qty = mobile_scanner_core.parse_scan_payload(payload)
        self.assertEqual(part_id, "PART-A")
        self.assertEqual(variant, "LH")
        self.assertEqual(qty, 2)

    def test_parse_rejects_boolean_quantity(self):
        # In Python, isinstance(True, int) is True, so explicit bool check is required
        with self.assertRaises(ValueError):
            mobile_scanner_core.parse_scan_payload({"part_id": "PART-A", "variant": "LH", "quantity": True})

        with self.assertRaises(ValueError):
            mobile_scanner_core.parse_scan_payload({"part_id": "PART-A", "variant": "LH", "quantity": False})

    def test_parse_rejects_float_or_decimal_quantity(self):
        with self.assertRaises(ValueError):
            mobile_scanner_core.parse_scan_payload({"part_id": "PART-A", "variant": "LH", "quantity": 1.5})

        with self.assertRaises(ValueError):
            mobile_scanner_core.parse_scan_payload("PART-A|LH|1.5")

    def test_parse_rejects_zero_and_negative_quantity(self):
        with self.assertRaises(ValueError):
            mobile_scanner_core.parse_scan_payload("PART-A|LH|0")

        with self.assertRaises(ValueError):
            mobile_scanner_core.parse_scan_payload("PART-A|LH|-1")

    def test_parse_rejects_malformed_pipe(self):
        with self.assertRaises(ValueError):
            mobile_scanner_core.parse_scan_payload("PART-A|LH")

        with self.assertRaises(ValueError):
            mobile_scanner_core.parse_scan_payload("PART-A|LH|1|EXTRA")

        with self.assertRaises(ValueError):
            mobile_scanner_core.parse_scan_payload("JUST_A_STRING")

    def test_parse_rejects_malformed_json(self):
        with self.assertRaises(ValueError):
            mobile_scanner_core.parse_scan_payload("{not-valid-json}")

        with self.assertRaises(ValueError):
            mobile_scanner_core.parse_scan_payload('{"part_id": "PART-A"}')

    # ----------------------------------------------------
    # Database Initialization & Isolation Tests
    # ----------------------------------------------------

    def test_mobile_database_initialization_tables(self):
        mobile_scanner_core.initialize_mobile_database(self.test_db_path)

        with closing(database.get_connection(self.test_db_path)) as conn:
            tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }

        expected = {
            "packing_orders",
            "part_requirements",
            "verification_events",
            "scan_events",
            "scan_requests",
            "mobile_attempts",
            "supervisor_reset_events",
            "hardware_command_outbox",
            "hardware_retry_events",
        }
        self.assertEqual(tables, expected)

    def test_mobile_order_initialization_independent(self):
        order = mobile_scanner_core.initialize_mobile_order(self.test_db_path)
        self.assertEqual(order["packing_order_id"], "PO-001")
        self.assertEqual(len(order["part_requirements"]), 3)

        # Verify it was saved to the test db
        loaded = database.load_packing_order("PO-001", db_path=self.test_db_path)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["box_id"], "BOX-001")

    # ----------------------------------------------------
    # Persistent Nonce Idempotency Tests
    # ----------------------------------------------------

    def test_persistent_nonce_returns_cached_response(self):
        nonce = "test-nonce-1001"
        payload = "PART-A|LH|1"

        status1, resp1 = mobile_scanner_core.process_mobile_scan(
            db_path=self.test_db_path,
            scan_nonce=nonce,
            raw_payload=payload,
        )
        self.assertEqual(status1, 200)
        self.assertFalse(resp1["idempotent_replay"])

        # Check part A scanned quantity in test db
        order_after_first = database.load_packing_order("PO-001", db_path=self.test_db_path)
        part_a = next(p for p in order_after_first["part_requirements"] if p["part_id"] == "PART-A")
        self.assertEqual(part_a["scanned_quantity"], 1)

        # Check scan_events count
        with closing(database.get_connection(self.test_db_path)) as conn:
            count = conn.execute("SELECT COUNT(*) as c FROM scan_events").fetchone()["c"]
            self.assertEqual(count, 1)

        # Repeat with identical nonce (e.g. retried HTTP request)
        status2, resp2 = mobile_scanner_core.process_mobile_scan(
            db_path=self.test_db_path,
            scan_nonce=nonce,
            raw_payload=payload,
        )
        self.assertEqual(status2, 200)
        self.assertTrue(resp2["idempotent_replay"])
        self.assertEqual(resp2["verification_result"], resp1["verification_result"])

        # Verify quantities and scan_events did NOT increase
        order_after_second = database.load_packing_order("PO-001", db_path=self.test_db_path)
        part_a_again = next(p for p in order_after_second["part_requirements"] if p["part_id"] == "PART-A")
        self.assertEqual(part_a_again["scanned_quantity"], 1)

        with closing(database.get_connection(self.test_db_path)) as conn:
            count_again = conn.execute("SELECT COUNT(*) as c FROM scan_events").fetchone()["c"]
            self.assertEqual(count_again, 1)

    def test_legitimate_repeated_scans_with_distinct_nonces(self):
        # Scan 1
        status1, resp1 = mobile_scanner_core.process_mobile_scan(
            db_path=self.test_db_path,
            scan_nonce="nonce-step-1",
            raw_payload="PART-A|LH|1",
        )
        self.assertEqual(status1, 200)
        self.assertFalse(resp1["idempotent_replay"])

        # Scan 2 (legitimate second item of PART-A)
        status2, resp2 = mobile_scanner_core.process_mobile_scan(
            db_path=self.test_db_path,
            scan_nonce="nonce-step-2",
            raw_payload="PART-A|LH|1",
        )
        self.assertEqual(status2, 200)
        self.assertFalse(resp2["idempotent_replay"])

        # Total scanned for PART-A must be 2
        order = database.load_packing_order("PO-001", db_path=self.test_db_path)
        part_a = next(p for p in order["part_requirements"] if p["part_id"] == "PART-A")
        self.assertEqual(part_a["scanned_quantity"], 2)
        self.assertEqual(part_a["packing_status"], "Complete")

        # Two distinct scan events recorded
        with closing(database.get_connection(self.test_db_path)) as conn:
            count = conn.execute("SELECT COUNT(*) as c FROM scan_events").fetchone()["c"]
            self.assertEqual(count, 2)

    def test_concurrent_requests_with_same_nonce(self):
        nonce = "concurrent-race-nonce"
        payload = "PART-B|RH|1"

        def do_scan():
            return mobile_scanner_core.process_mobile_scan(
                db_path=self.test_db_path,
                scan_nonce=nonce,
                raw_payload=payload,
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(do_scan) for _ in range(8)]
            results = [f.result() for f in futures]

        # All 8 requests must succeed with HTTP 200
        for status, resp in results:
            self.assertEqual(status, 200)

        # Exactly 1 scan event must have been recorded in the database
        with closing(database.get_connection(self.test_db_path)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) as c FROM scan_events WHERE part_id = 'PART-B'"
            ).fetchone()["c"]
            self.assertEqual(count, 1)

            req_count = conn.execute(
                "SELECT COUNT(*) as c FROM scan_requests WHERE scan_nonce = ?", (nonce,)
            ).fetchone()["c"]
            self.assertEqual(req_count, 1)

    # ----------------------------------------------------
    # Atomic Rollback on Failure & Reopen Persistence Tests
    # ----------------------------------------------------

    def test_simulated_persistence_failure_rolls_back_cleanly(self):
        nonce = "failing-nonce-99"
        payload = "PART-A|LH|1"

        # Initialize order cleanly first
        mobile_scanner_core.initialize_mobile_order(self.test_db_path)

        # Simulate a database failure during atomic transaction (e.g. disk full / constraint error)
        original_persist = mobile_scanner_core._persist_scan_transaction

        def faulty_persist(*args, **kwargs):
            # Call original to write first statement, then raise error to simulate mid-transaction failure
            database._upsert_packing_order(kwargs["connection"], kwargs["order"])
            raise sqlite3.OperationalError("Simulated mid-transaction disk failure")

        with patch("mobile_scanner_core._persist_scan_transaction", side_effect=faulty_persist):
            with self.assertRaises(sqlite3.OperationalError):
                mobile_scanner_core.process_mobile_scan(
                    db_path=self.test_db_path,
                    scan_nonce=nonce,
                    raw_payload=payload,
                )

        # Verify clean rollback:
        # 1. Order quantity in DB remains 0
        order_reloaded = database.load_packing_order("PO-001", db_path=self.test_db_path)
        part_a = next(p for p in order_reloaded["part_requirements"] if p["part_id"] == "PART-A")
        self.assertEqual(part_a["scanned_quantity"], 0)

        # 2. No scan events remain
        with closing(database.get_connection(self.test_db_path)) as conn:
            scan_events_count = conn.execute("SELECT COUNT(*) as c FROM scan_events").fetchone()["c"]
            self.assertEqual(scan_events_count, 0)

            # 3. No verification events remain
            verification_events_count = conn.execute("SELECT COUNT(*) as c FROM verification_events").fetchone()["c"]
            self.assertEqual(verification_events_count, 0)

            # 4. No scan request / nonce records remain
            nonces_count = conn.execute("SELECT COUNT(*) as c FROM scan_requests WHERE scan_nonce = ?", (nonce,)).fetchone()["c"]
            self.assertEqual(nonces_count, 0)

        # 5. Retrying the same nonce now succeeds exactly once
        status, resp = mobile_scanner_core.process_mobile_scan(
            db_path=self.test_db_path,
            scan_nonce=nonce,
            raw_payload=payload,
        )
        self.assertEqual(status, 200)
        self.assertFalse(resp["idempotent_replay"])

        # Check that quantity is now exactly 1
        order_reloaded_retry = database.load_packing_order("PO-001", db_path=self.test_db_path)
        part_a_retry = next(p for p in order_reloaded_retry["part_requirements"] if p["part_id"] == "PART-A")
        self.assertEqual(part_a_retry["scanned_quantity"], 1)

    def test_restart_persistence_across_connection_reopen(self):
        nonce = "reopen-restart-nonce"
        payload = "PART-C|Standard|2"

        # 1. Process initial scan
        status1, resp1 = mobile_scanner_core.process_mobile_scan(
            db_path=self.test_db_path,
            scan_nonce=nonce,
            raw_payload=payload,
        )
        self.assertEqual(status1, 200)
        self.assertFalse(resp1["idempotent_replay"])

        # 2. Simulate complete restart by querying via a fresh, separate database connection
        with closing(sqlite3.connect(str(self.test_db_path))) as fresh_conn:
            fresh_conn.row_factory = sqlite3.Row
            row = fresh_conn.execute(
                "SELECT response_status, response_payload FROM scan_requests WHERE scan_nonce = ?",
                (nonce,),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["response_status"], 200)

        # 3. Submit the same nonce again after reopen
        status2, resp2 = mobile_scanner_core.process_mobile_scan(
            db_path=self.test_db_path,
            scan_nonce=nonce,
            raw_payload=payload,
        )
        self.assertEqual(status2, 200)
        self.assertTrue(resp2["idempotent_replay"])

        # 4. Verify that scanned quantity did not increment a second time
        order = database.load_packing_order("PO-001", db_path=self.test_db_path)
        part_c = next(p for p in order["part_requirements"] if p["part_id"] == "PART-C")
        self.assertEqual(part_c["scanned_quantity"], 2)

        # 5. Verify scan_events count remains exactly 1
        with closing(database.get_connection(self.test_db_path)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) as c FROM scan_events WHERE part_id = 'PART-C'"
            ).fetchone()["c"]
            self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
