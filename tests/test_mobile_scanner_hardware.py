"""
Unit and integration tests for Phase 4A: Durable SQLite Outbox & Arduino Background Worker.
Verifies asynchronous, idempotent hardware dispatch, crash recovery, and isolation
without requiring physical COM5 serial hardware.
"""

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

import database
import main
import mobile_scanner_core
from mobile_scanner_core import (
    ArduinoOutboxWorker,
    claim_next_outbox_command,
    process_claimed_command,
    process_single_outbox_command,
    recover_stranded_outbox_commands,
    start_global_worker,
    stop_global_worker,
)


class TestMobileScannerHardware(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_hardware.db"
        mobile_scanner_core.initialize_mobile_database(self.db_path)
        # Ensure any active background worker from previous runs is stopped
        stop_global_worker()

    def tearDown(self):
        stop_global_worker()
        self.temp_dir.cleanup()

    def _get_outbox_rows(self):
        with closing(database.get_connection(self.db_path)) as conn:
            return conn.execute("""
                SELECT outbox_id, attempt_order_id, command, status, retry_count,
                       error_message, created_at, claimed_at, processed_at
                FROM hardware_command_outbox
                ORDER BY outbox_id ASC
            """).fetchall()

    def test_01_terminal_pass_creates_exactly_one_pending_command(self):
        """1. Terminal PASS creates exactly one PENDING outbox command."""
        # Complete all 8 items for PO-001 to reach PASS
        scans = [
            ("PART-A", "LH", 1, "N-P1"),
            ("PART-A", "LH", 1, "N-P2"),
            ("PART-B", "RH", 1, "N-P3"),
            ("PART-B", "RH", 1, "N-P4"),
            ("PART-C", "Standard", 1, "N-P5"),
            ("PART-C", "Standard", 1, "N-P6"),
            ("PART-C", "Standard", 1, "N-P7"),
            ("PART-C", "Standard", 1, "N-P8"),
        ]
        for part_id, variant, qty, nonce in scans:
            status, res = mobile_scanner_core.process_mobile_scan(
                self.db_path,
                scan_nonce=nonce,
                raw_payload={"part_id": part_id, "variant": variant, "quantity": qty},
            )
            self.assertEqual(status, 200)

        self.assertEqual(res["verification_result"], "PASS")

        # Verify exactly one PENDING outbox command exists
        rows = self._get_outbox_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["attempt_order_id"], "PO-001")
        self.assertEqual(row["command"], "PASS")
        self.assertEqual(row["status"], "PENDING")
        self.assertEqual(row["retry_count"], 0)
        self.assertIsNone(row["claimed_at"])
        self.assertIsNone(row["processed_at"])

    def test_02_terminal_ng_creates_exactly_one_pending_command(self):
        """2. Terminal NG creates exactly one PENDING outbox command."""
        status, res = mobile_scanner_core.process_mobile_scan(
            self.db_path,
            scan_nonce="N-NG1",
            raw_payload={"part_id": "PART-A", "variant": "RH", "quantity": 1},  # wrong variant
        )
        self.assertEqual(status, 200)
        self.assertEqual(res["verification_result"], "NG")

        rows = self._get_outbox_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["attempt_order_id"], "PO-001")
        self.assertEqual(row["command"], "NG")
        self.assertEqual(row["status"], "PENDING")
        self.assertEqual(row["retry_count"], 0)

    def test_03_hold_does_not_create_hardware_command(self):
        """3. Intermediate HOLD does not create a hardware command."""
        status, res = mobile_scanner_core.process_mobile_scan(
            self.db_path,
            scan_nonce="N-HOLD1",
            raw_payload={"part_id": "PART-A", "variant": "LH", "quantity": 1},  # 1 of 2
        )
        self.assertEqual(status, 200)
        self.assertEqual(res["verification_result"], "HOLD")

        rows = self._get_outbox_rows()
        self.assertEqual(len(rows), 0)

    def test_04_additional_scans_on_already_ng_attempt_do_not_create_duplicates(self):
        """4. Additional scans on an already-NG attempt do not create duplicate commands."""
        # Initial scan causes NG
        mobile_scanner_core.process_mobile_scan(
            self.db_path,
            scan_nonce="N-NG-INITIAL",
            raw_payload={"part_id": "PART-A", "variant": "RH", "quantity": 1},
        )
        self.assertEqual(len(self._get_outbox_rows()), 1)

        # Subsequent scan with fresh nonce on rejected order
        status, res = mobile_scanner_core.process_mobile_scan(
            self.db_path,
            scan_nonce="N-NG-REPEAT",
            raw_payload={"part_id": "PART-B", "variant": "RH", "quantity": 1},
        )
        self.assertEqual(status, 200)
        self.assertEqual(res["verification_result"], "NG")

        # Verify no duplicate command was added
        rows = self._get_outbox_rows()
        self.assertEqual(len(rows), 1)

    def test_05_duplicate_nonce_replay_does_not_create_duplicates(self):
        """5. Duplicate nonce replay does not create duplicate commands."""
        mobile_scanner_core.process_mobile_scan(
            self.db_path,
            scan_nonce="N-REPLAY",
            raw_payload={"part_id": "PART-A", "variant": "RH", "quantity": 1},
        )
        self.assertEqual(len(self._get_outbox_rows()), 1)

        # Exact same nonce resubmitted
        status, res = mobile_scanner_core.process_mobile_scan(
            self.db_path,
            scan_nonce="N-REPLAY",
            raw_payload={"part_id": "PART-A", "variant": "RH", "quantity": 1},
        )
        self.assertEqual(status, 200)
        self.assertTrue(res["idempotent_replay"])
        self.assertEqual(len(self._get_outbox_rows()), 1)

    def test_06_successful_mocked_ack_produces_confirmed(self):
        """6. Successful mocked ACK produces CONFIRMED in outbox and packing_orders."""
        mobile_scanner_core.process_mobile_scan(
            self.db_path,
            scan_nonce="N-PASS-CONF",
            raw_payload="PART-A|RH|1",  # triggers NG
        )

        # Mock send_arduino_result returning True (acknowledged)
        mock_send = MagicMock(return_value=True)
        res = process_single_outbox_command(self.db_path, send_fn=mock_send)
        self.assertTrue(res)
        mock_send.assert_called_once_with("NG")

        rows = self._get_outbox_rows()
        self.assertEqual(rows[0]["status"], "CONFIRMED")
        self.assertIsNotNone(rows[0]["processed_at"])
        self.assertIsNone(rows[0]["error_message"])

        # Check packing_orders status
        with closing(database.get_connection(self.db_path)) as conn:
            order_row = conn.execute(
                "SELECT verification_result, arduino_communication_status FROM packing_orders WHERE packing_order_id = 'PO-001'"
            ).fetchone()
            self.assertEqual(order_row["arduino_communication_status"], "CONFIRMED")
            self.assertEqual(order_row["verification_result"], "NG")

    def test_07_failed_serial_communication_produces_unconfirmed(self):
        """7. Failed serial communication produces UNCONFIRMED in outbox and packing_orders."""
        mobile_scanner_core.process_mobile_scan(
            self.db_path,
            scan_nonce="N-FAIL-UNCONF",
            raw_payload="PART-A|RH|1",
        )

        # Mock send_arduino_result returning False (timeout/bad ACK)
        mock_send = MagicMock(return_value=False)
        res = process_single_outbox_command(self.db_path, send_fn=mock_send)
        self.assertFalse(res)

        rows = self._get_outbox_rows()
        self.assertEqual(rows[0]["status"], "UNCONFIRMED")
        self.assertIn("unconfirmed", rows[0]["error_message"].lower())

        with closing(database.get_connection(self.db_path)) as conn:
            order_row = conn.execute(
                "SELECT arduino_communication_status FROM packing_orders WHERE packing_order_id = 'PO-001'"
            ).fetchone()
            self.assertEqual(order_row["arduino_communication_status"], "UNCONFIRMED")

    def test_08_hardware_failure_does_not_change_authoritative_result(self):
        """8. Hardware failure does not change authoritative PASS or NG result."""
        # 1. Complete an order to PASS
        for i in range(2):
            mobile_scanner_core.process_mobile_scan(self.db_path, f"PA-{i}", "PART-A|LH|1")
        for i in range(2):
            mobile_scanner_core.process_mobile_scan(self.db_path, f"PB-{i}", "PART-B|RH|1")
        for i in range(4):
            mobile_scanner_core.process_mobile_scan(self.db_path, f"PC-{i}", "PART-C|Standard|1")

        # Mock hardware communication raising exception
        mock_send = MagicMock(side_effect=Exception("COM5 disconnected"))
        res = process_single_outbox_command(self.db_path, send_fn=mock_send)
        self.assertFalse(res)

        # Authoritative result must REMAIN strictly PASS (never demoted to HOLD)
        with closing(database.get_connection(self.db_path)) as conn:
            order_row = conn.execute(
                "SELECT verification_result, arduino_communication_status FROM packing_orders WHERE packing_order_id = 'PO-001'"
            ).fetchone()
            self.assertEqual(order_row["verification_result"], "PASS")
            self.assertEqual(order_row["arduino_communication_status"], "UNCONFIRMED")

    def test_09_stranded_processing_command_recovered_after_restart(self):
        """9. A stranded PROCESSING command is recovered to PENDING after restart."""
        # Ensure order exists in packing_orders for foreign key constraint
        mobile_scanner_core.initialize_mobile_order(self.db_path)

        # Manually seed a command stranded in PROCESSING state
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with closing(database.get_connection(self.db_path)) as conn:
            with conn:
                conn.execute("""
                    INSERT INTO hardware_command_outbox (
                        attempt_order_id, command, status, retry_count, created_at, claimed_at
                    ) VALUES ('PO-001', 'PASS', 'PROCESSING', 0, ?, ?)
                """, (now_iso, now_iso))

        # Run recovery
        recovered_count = recover_stranded_outbox_commands(self.db_path)
        self.assertEqual(recovered_count, 1)

        rows = self._get_outbox_rows()
        self.assertEqual(rows[0]["status"], "PENDING")
        self.assertEqual(rows[0]["retry_count"], 1)
        self.assertIsNone(rows[0]["claimed_at"])
        self.assertIn("recovered", rows[0]["error_message"].lower())

    def test_10_outbox_transaction_rollback_leaves_no_command_behind(self):
        """10. Outbox transaction rollback leaves no command behind."""
        order = mobile_scanner_core.initialize_mobile_order(self.db_path)
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

        with self.assertRaises(sqlite3.IntegrityError):
            with closing(database.get_connection(self.db_path)) as conn:
                with conn:
                    # Enqueue an outbox command
                    conn.execute("""
                        INSERT INTO hardware_command_outbox (
                            attempt_order_id, command, status, retry_count, created_at
                        ) VALUES (?, 'PASS', 'PENDING', 0, ?)
                    """, (order["packing_order_id"], timestamp))

                    # Trigger deliberate integrity error on the same transaction
                    conn.execute("""
                        INSERT INTO scan_requests (
                            request_id, scan_nonce, packing_order_id, response_status, response_payload, created_at
                        ) VALUES (1, 'NONCE-DUPLICATE', 'PO-001', 200, '{}', ?)
                    """, (timestamp,))
                    conn.execute("""
                        INSERT INTO scan_requests (
                            request_id, scan_nonce, packing_order_id, response_status, response_payload, created_at
                        ) VALUES (1, 'NONCE-DUPLICATE', 'PO-001', 200, '{}', ?)
                    """, (timestamp,))

        # Verify outbox table has no records due to rollback
        rows = self._get_outbox_rows()
        self.assertEqual(len(rows), 0)

    def test_11_commands_are_processed_in_fifo_order(self):
        """11. Commands are claimed and processed in strict FIFO order."""
        t1 = "2026-11-08T12:00:00+00:00"
        t2 = "2026-11-08T12:01:00+00:00"
        with closing(database.get_connection(self.db_path)) as conn:
            with conn:
                # Insert order rows for foreign key constraints
                conn.execute("""
                    INSERT OR IGNORE INTO packing_orders (packing_order_id, box_id, order_id, product_description)
                    VALUES ('PO-001', 'B1', 'O1', 'P1'), ('PO-002', 'B2', 'O2', 'P2')
                """)
                conn.execute("""
                    INSERT INTO hardware_command_outbox (attempt_order_id, command, status, created_at)
                    VALUES ('PO-001', 'NG', 'PENDING', ?)
                """, (t1,))
                conn.execute("""
                    INSERT INTO hardware_command_outbox (attempt_order_id, command, status, created_at)
                    VALUES ('PO-002', 'PASS', 'PENDING', ?)
                """, (t2,))

        # Claim first command
        first = claim_next_outbox_command(self.db_path)
        self.assertIsNotNone(first)
        self.assertEqual(first["attempt_order_id"], "PO-001")
        self.assertEqual(first["command"], "NG")

        # Claim second command
        second = claim_next_outbox_command(self.db_path)
        self.assertIsNotNone(second)
        self.assertEqual(second["attempt_order_id"], "PO-002")
        self.assertEqual(second["command"], "PASS")

        # No more commands
        third = claim_next_outbox_command(self.db_path)
        self.assertIsNone(third)

    def test_12_different_packing_attempts_remain_isolated(self):
        """12. Different packing attempts remain isolated in hardware dispatch."""
        # Attempt 1 triggers NG
        mobile_scanner_core.process_mobile_scan(
            self.db_path,
            scan_nonce="ATTEMPT1-NG",
            raw_payload="PART-A|RH|1",
        )
        rows = self._get_outbox_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["attempt_order_id"], "PO-001")
        self.assertEqual(rows[0]["command"], "NG")

        # Supervisor reset creates Attempt 2 (PO-001-ATTEMPT-2)
        status, reset_res = mobile_scanner_core.authorize_supervisor_reset(
            self.db_path,
            supervisor_pin="SPV-9042",
            reason="Quarantined variant mismatch from attempt 1",
        )
        self.assertEqual(status, 200)
        self.assertEqual(reset_res["new_attempt_order_id"], "PO-001-ATTEMPT-2")

        # Attempt 2 is completed with clean PASS scans
        scans = [
            ("PART-A", "LH", 1, "A2-P1"),
            ("PART-A", "LH", 1, "A2-P2"),
            ("PART-B", "RH", 1, "A2-P3"),
            ("PART-B", "RH", 1, "A2-P4"),
            ("PART-C", "Standard", 1, "A2-P5"),
            ("PART-C", "Standard", 1, "A2-P6"),
            ("PART-C", "Standard", 1, "A2-P7"),
            ("PART-C", "Standard", 1, "A2-P8"),
        ]
        for p, v, q, n in scans:
            mobile_scanner_core.process_mobile_scan(
                self.db_path,
                scan_nonce=n,
                raw_payload={"part_id": p, "variant": v, "quantity": q},
            )

        rows = self._get_outbox_rows()
        self.assertEqual(len(rows), 2)
        # Attempt 1 command
        self.assertEqual(rows[0]["attempt_order_id"], "PO-001")
        self.assertEqual(rows[0]["command"], "NG")
        # Attempt 2 command
        self.assertEqual(rows[1]["attempt_order_id"], "PO-001-ATTEMPT-2")
        self.assertEqual(rows[1]["command"], "PASS")

    def test_13_worker_lifecycle_and_singleton_protection(self):
        """13. Background worker runs asynchronously and enforces singleton thread protection."""
        mock_send = MagicMock(return_value=True)

        worker = start_global_worker(self.db_path, poll_interval=0.05, send_fn=mock_send)
        self.assertTrue(worker.is_alive())

        # Attempting to start a second worker raises RuntimeError
        worker_duplicate = ArduinoOutboxWorker(self.db_path, send_fn=mock_send)
        with self.assertRaises(RuntimeError):
            worker_duplicate.start()

        # Enqueue a terminal NG scan while worker is active
        mobile_scanner_core.process_mobile_scan(
            self.db_path,
            scan_nonce="N-WORKER-ASYNC",
            raw_payload="PART-A|RH|1",
        )

        # Allow worker thread brief moment to consume the command
        for _ in range(30):
            rows = self._get_outbox_rows()
            if rows and rows[0]["status"] == "CONFIRMED":
                break
            time.sleep(0.05)

        rows = self._get_outbox_rows()
        self.assertEqual(rows[0]["status"], "CONFIRMED")
        mock_send.assert_called_with("NG")

        # Stop worker cleanly
        stop_global_worker()
        self.assertFalse(worker.is_alive())
        self.assertIsNone(ArduinoOutboxWorker.get_active_worker())


if __name__ == "__main__":
    unittest.main()
