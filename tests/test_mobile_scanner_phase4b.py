"""
Phase 4B Tests — Flask API Integration and Hardware Status Synchronization.

Covers:
  - Worker lifecycle (start in create_app, skip in TESTING mode, stop on teardown)
  - GET /api/order hardware status fields (arduino_communication_status,
    hardware_retry_count, hardware_retry_max, can_hardware_retry)
  - Hardware status values: null before terminal, PENDING after enqueue,
    CONFIRMED after mocked ACK, UNCONFIRMED after failure
  - POST /api/hardware/retry: authorization, reason, idempotency, limit
  - Retry audit trail in supervisor_reset_events
  - Retry does NOT modify verification_result or scanned quantities
  - Retry re-queues command to PENDING so worker re-sends
  - POST /api/supervisor/reset still works (Phase 3.1 regression)
  - Protected database hashes unchanged
"""

import hashlib
import json
from contextlib import closing
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import database
import mobile_scanner
import mobile_scanner_core
from mobile_scanner_core import (
    ArduinoOutboxWorker,
    process_single_outbox_command,
    stop_global_worker,
    HARDWARE_RETRY_MAX,
)


def compute_file_hash(path: Path) -> str:
    if not path.exists():
        return ""
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest().upper()


class TestPhase4BFlaskHardwareIntegration(unittest.TestCase):
    """Flask API tests for hardware status and retry endpoints."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_db_path = Path(self.temp_dir.name) / "test_phase4b.db"

        # Stop any lingering worker from other tests
        stop_global_worker()

        # Create Flask test client with TESTING=True (skips real worker start)
        self.app = mobile_scanner.create_app(db_path=self.test_db_path, testing=True)
        self.client = self.app.test_client()

        # Protected database baselines
        self.prod_db_path = Path("packing_verification.db")
        self.backup_db_path = Path("packing_verification_backup.db")
        self.hw_test_db_path = Path("packing_verification_hardware_test.db")
        self.ng_test_db_path = Path("packing_verification_ng_test.db")
        self.baseline_hashes = {
            p: compute_file_hash(p)
            for p in [
                self.prod_db_path,
                self.backup_db_path,
                self.hw_test_db_path,
                self.ng_test_db_path,
            ]
        }

    def tearDown(self):
        stop_global_worker()
        self.temp_dir.cleanup()
        for path, baseline in self.baseline_hashes.items():
            if baseline:
                self.assertEqual(
                    compute_file_hash(path),
                    baseline,
                    f"CRITICAL: Protected database {path} was modified!",
                )

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    def _get_outbox_rows(self):
        with closing(database.get_connection(self.test_db_path)) as conn:
            return conn.execute(
                "SELECT * FROM hardware_command_outbox ORDER BY outbox_id ASC"
            ).fetchall()

    def _post_scan(self, nonce, payload):
        return self.client.post(
            "/api/scan",
            data=json.dumps({"scan_nonce": nonce, "payload": payload}),
            content_type="application/json",
        )

    # ==================================================================
    # 1. Worker lifecycle
    # ==================================================================

    def test_01_testing_flag_suppresses_worker_start(self):
        """TESTING=True must not start the ArduinoOutboxWorker."""
        # setUp already stopped any prior worker and created app with TESTING=True.
        # Confirm no auto-start happened.
        worker = ArduinoOutboxWorker.get_active_worker()
        self.assertIsNone(worker, "Worker must NOT start when TESTING=True")

    def test_02_worker_starts_when_testing_false(self):
        """create_app with testing=False starts the background worker."""
        # Ensure clean slate
        stop_global_worker()
        mock_send = MagicMock(return_value=True)
        with tempfile.TemporaryDirectory() as td:
            db2 = Path(td) / "worker_start_test.db"
            with patch("mobile_scanner_core.main.send_arduino_result", mock_send):
                try:
                    app = mobile_scanner.create_app(db_path=db2, testing=False)
                    active = ArduinoOutboxWorker.get_active_worker()
                    self.assertIsNotNone(active)
                    self.assertTrue(active.is_alive())
                finally:
                    stop_global_worker()
        worker_after = ArduinoOutboxWorker.get_active_worker()
        self.assertIsNone(worker_after)

    # ==================================================================
    # 2. GET /api/order — hardware status fields
    # ==================================================================

    def test_03_order_hardware_fields_present_before_terminal(self):
        """Before any scan, hardware status fields are present and null/False."""
        resp = self.client.get("/api/order")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()

        self.assertIn("arduino_communication_status", data)
        self.assertIn("hardware_retry_count", data)
        self.assertIn("hardware_retry_max", data)
        self.assertIn("can_hardware_retry", data)

        self.assertIsNone(data["arduino_communication_status"])
        self.assertEqual(data["hardware_retry_count"], 0)
        self.assertEqual(data["hardware_retry_max"], HARDWARE_RETRY_MAX)
        self.assertFalse(data["can_hardware_retry"])

    def test_04_order_hardware_status_pending_after_ng_scan(self):
        """After an NG scan, hardware status must be PENDING (outbox enqueued)."""
        self._post_scan("hw-ng-p4b", "PART-A|RH|1")  # wrong variant → NG

        resp = self.client.get("/api/order")
        data = resp.get_json()

        self.assertEqual(data["verification_result"], "NG")
        self.assertEqual(data["arduino_communication_status"], "PENDING")
        self.assertFalse(data["can_hardware_retry"])  # not UNCONFIRMED yet

    def test_05_order_hardware_status_pending_after_pass_scan(self):
        """After a full PASS, hardware status must be PENDING."""
        for i, (p, v) in enumerate([
            ("PART-A", "LH"), ("PART-A", "LH"),
            ("PART-B", "RH"), ("PART-B", "RH"),
            ("PART-C", "Standard"), ("PART-C", "Standard"),
            ("PART-C", "Standard"), ("PART-C", "Standard"),
        ]):
            self._post_scan(f"hw-pass-{i}", f"{p}|{v}|1")

        resp = self.client.get("/api/order")
        data = resp.get_json()
        self.assertEqual(data["verification_result"], "PASS")
        self.assertEqual(data["arduino_communication_status"], "PENDING")

    def test_06_order_hardware_status_confirmed_after_mocked_ack(self):
        """After mocked ACK, /api/order must report CONFIRMED."""
        self._post_scan("hw-conf-ng", "PART-A|RH|1")

        mock_send = MagicMock(return_value=True)
        process_single_outbox_command(self.test_db_path, send_fn=mock_send)

        resp = self.client.get("/api/order")
        data = resp.get_json()
        self.assertEqual(data["arduino_communication_status"], "CONFIRMED")
        self.assertFalse(data["can_hardware_retry"])
        self.assertEqual(data["hardware_retry_count"], 0)

    def test_07_order_hardware_status_unconfirmed_after_failed_send(self):
        """After failed send, /api/order reports UNCONFIRMED and can_hardware_retry=True."""
        self._post_scan("hw-unconf-ng", "PART-A|RH|1")

        mock_send = MagicMock(return_value=False)
        process_single_outbox_command(self.test_db_path, send_fn=mock_send)

        resp = self.client.get("/api/order")
        data = resp.get_json()
        self.assertEqual(data["arduino_communication_status"], "UNCONFIRMED")
        self.assertTrue(data["can_hardware_retry"])
        self.assertEqual(data["hardware_retry_count"], 0)

    def test_08_verification_result_unchanged_after_unconfirmed(self):
        """UNCONFIRMED hardware status must never demote the packing verification result."""
        self._post_scan("hw-prot-ng", "PART-A|RH|1")

        mock_send = MagicMock(side_effect=Exception("COM5 disconnected"))
        process_single_outbox_command(self.test_db_path, send_fn=mock_send)

        resp = self.client.get("/api/order")
        data = resp.get_json()
        self.assertEqual(data["verification_result"], "NG")
        self.assertEqual(data["arduino_communication_status"], "UNCONFIRMED")

    # ==================================================================
    # 3. POST /api/hardware/retry
    # ==================================================================

    def test_09_hardware_retry_requires_valid_supervisor_pin(self):
        """Retry with wrong PIN returns 401."""
        self._post_scan("retry-auth-ng", "PART-A|RH|1")
        process_single_outbox_command(self.test_db_path, send_fn=MagicMock(return_value=False))

        resp = self.client.post(
            "/api/hardware/retry",
            data=json.dumps({
                "supervisor_pin": "WRONG-PIN",
                "attempt_order_id": "PO-001",
                "reason": "Cable replaced",
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 401)
        self.assertIn("error", resp.get_json())

    def test_10_hardware_retry_requires_reason(self):
        """Retry with empty reason returns 400."""
        self._post_scan("retry-reason-ng", "PART-A|RH|1")
        process_single_outbox_command(self.test_db_path, send_fn=MagicMock(return_value=False))

        resp = self.client.post(
            "/api/hardware/retry",
            data=json.dumps({
                "supervisor_pin": "SPV-9042",
                "attempt_order_id": "PO-001",
                "reason": "",
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_11_hardware_retry_requeues_unconfirmed_to_pending(self):
        """Successful retry resets outbox to PENDING and increments retry_count."""
        self._post_scan("retry-requeue-ng", "PART-A|RH|1")
        process_single_outbox_command(self.test_db_path, send_fn=MagicMock(return_value=False))

        # Verify UNCONFIRMED state
        rows = self._get_outbox_rows()
        self.assertEqual(rows[0]["status"], "UNCONFIRMED")
        self.assertEqual(rows[0]["retry_count"], 0)

        # Authorized retry
        resp = self.client.post(
            "/api/hardware/retry",
            data=json.dumps({
                "supervisor_pin": "SPV-9042",
                "attempt_order_id": "PO-001",
                "reason": "COM5 cable replaced",
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["previous_status"], "UNCONFIRMED")
        self.assertEqual(body["retry_count"], 1)
        self.assertEqual(body["retry_max"], HARDWARE_RETRY_MAX)

        # Outbox now PENDING with retry_count=1
        rows = self._get_outbox_rows()
        self.assertEqual(rows[0]["status"], "PENDING")
        self.assertEqual(rows[0]["retry_count"], 1)

    def test_12_hardware_retry_does_not_modify_verification_result(self):
        """Retry must never alter the authoritative verification result or quantities."""
        self._post_scan("retry-prot-ng", "PART-A|RH|1")
        process_single_outbox_command(self.test_db_path, send_fn=MagicMock(return_value=False))

        # Snapshot the order state before retry
        order_before = database.load_packing_order("PO-001", db_path=self.test_db_path)
        vr_before = order_before["verification_result"]
        parts_before = {p["part_id"]: p["scanned_quantity"] for p in order_before["part_requirements"]}

        self.client.post(
            "/api/hardware/retry",
            data=json.dumps({
                "supervisor_pin": "SPV-9042",
                "attempt_order_id": "PO-001",
                "reason": "Cable replaced",
            }),
            content_type="application/json",
        )

        # After retry, verification_result and scanned quantities must be unchanged
        order_after = database.load_packing_order("PO-001", db_path=self.test_db_path)
        self.assertEqual(order_after["verification_result"], vr_before)
        for p in order_after["part_requirements"]:
            self.assertEqual(p["scanned_quantity"], parts_before[p["part_id"]],
                             f"scanned_quantity for {p['part_id']} changed after hardware retry")

    def test_13_hardware_retry_rejected_when_not_unconfirmed(self):
        """Retry on PENDING or CONFIRMED command returns 409."""
        # Command is PENDING (not yet dispatched)
        self._post_scan("retry-conflict-ng", "PART-A|RH|1")
        rows = self._get_outbox_rows()
        self.assertEqual(rows[0]["status"], "PENDING")

        resp = self.client.post(
            "/api/hardware/retry",
            data=json.dumps({
                "supervisor_pin": "SPV-9042",
                "attempt_order_id": "PO-001",
                "reason": "Pre-dispatch retry attempt",
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 409)

    def test_14_hardware_retry_rejected_when_no_outbox_command(self):
        """Retry on order with no outbox entry returns 404."""
        # Initialize order but don't scan to terminal
        mobile_scanner_core.initialize_mobile_order(self.test_db_path)

        resp = self.client.post(
            "/api/hardware/retry",
            data=json.dumps({
                "supervisor_pin": "SPV-9042",
                "attempt_order_id": "PO-001",
                "reason": "Test missing outbox",
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 404)

    def test_15_hardware_retry_limit_enforced(self):
        """After HARDWARE_RETRY_MAX retries, further retries return 429."""
        self._post_scan("retry-limit-ng", "PART-A|RH|1")

        # Exhaust retry limit
        for i in range(HARDWARE_RETRY_MAX):
            process_single_outbox_command(self.test_db_path, send_fn=MagicMock(return_value=False))
            resp = self.client.post(
                "/api/hardware/retry",
                data=json.dumps({
                    "supervisor_pin": "SPV-9042",
                    "attempt_order_id": "PO-001",
                    "reason": f"Retry attempt {i + 1}",
                }),
                content_type="application/json",
            )
            if resp.status_code == 429:
                break  # limit hit before exhausting loop

        # One more retry after limit
        process_single_outbox_command(self.test_db_path, send_fn=MagicMock(return_value=False))
        resp = self.client.post(
            "/api/hardware/retry",
            data=json.dumps({
                "supervisor_pin": "SPV-9042",
                "attempt_order_id": "PO-001",
                "reason": "Over limit",
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 429)

    def test_16_hardware_retry_audit_recorded_in_dedicated_table(self):
        """Retry must write to hardware_retry_events and NOT supervisor_reset_events."""
        self._post_scan("retry-audit-ng", "PART-A|RH|1")
        process_single_outbox_command(self.test_db_path, send_fn=MagicMock(return_value=False))

        self.client.post(
            "/api/hardware/retry",
            data=json.dumps({
                "supervisor_pin": "SPV-9042",
                "attempt_order_id": "PO-001",
                "reason": "COM5 cable replaced",
            }),
            content_type="application/json",
        )

        with closing(database.get_connection(self.test_db_path)) as conn:
            # 1. Dedicated audit table must contain the record
            retry_rows = conn.execute(
                "SELECT * FROM hardware_retry_events ORDER BY retry_id DESC"
            ).fetchall()
            self.assertEqual(len(retry_rows), 1)
            audit = retry_rows[0]
            self.assertEqual(audit["attempt_order_id"], "PO-001")
            self.assertEqual(audit["command"], "NG")
            self.assertEqual(audit["supervisor_id"], "SUPERVISOR-01")
            self.assertEqual(audit["reason"], "COM5 cable replaced")
            self.assertEqual(audit["previous_status"], "UNCONFIRMED")
            self.assertEqual(audit["new_status"], "PENDING")
            self.assertEqual(audit["retry_number"], 1)
            self.assertIsNotNone(audit["timestamp"])

            # 2. supervisor_reset_events must remain completely empty (zero cross-contamination)
            reset_rows = conn.execute("SELECT * FROM supervisor_reset_events").fetchall()
            self.assertEqual(
                len(reset_rows), 0,
                "Hardware retry must NOT be recorded in supervisor_reset_events"
            )

    def test_17_hardware_retry_missing_body_returns_400(self):
        """Retry with non-JSON body returns 400."""
        resp = self.client.post(
            "/api/hardware/retry",
            data="not-json",
            content_type="text/plain",
        )
        self.assertEqual(resp.status_code, 400)

    def test_18_hardware_retry_after_confirmed_returns_409(self):
        """Retry on a CONFIRMED command returns 409."""
        self._post_scan("retry-confirmed", "PART-A|RH|1")
        process_single_outbox_command(self.test_db_path, send_fn=MagicMock(return_value=True))

        resp = self.client.post(
            "/api/hardware/retry",
            data=json.dumps({
                "supervisor_pin": "SPV-9042",
                "attempt_order_id": "PO-001",
                "reason": "Already confirmed, testing guard",
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 409)

    # ==================================================================
    # 4. Phase 3.1 regression — supervisor reset still works
    # ==================================================================

    def test_19_supervisor_reset_still_works_after_phase4b(self):
        """POST /api/supervisor/reset must still function correctly."""
        self._post_scan("reset-p4b-ng", "PART-A|RH|1")

        resp = self.client.post(
            "/api/supervisor/reset",
            data=json.dumps({
                "supervisor_pin": "SPV-9042",
                "reason": "Phase 4B regression test reset",
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["new_attempt_order_id"], "PO-001-ATTEMPT-2")

    # ==================================================================
    # 5. GET /api/order — can_hardware_retry reflects retry exhaustion
    # ==================================================================

    def test_20_can_hardware_retry_false_after_limit_reached(self):
        """can_hardware_retry in /api/order must be False after limit is exhausted."""
        self._post_scan("limit-order-ng", "PART-A|RH|1")

        # Drive retry count to max
        for i in range(HARDWARE_RETRY_MAX):
            process_single_outbox_command(self.test_db_path, send_fn=MagicMock(return_value=False))
            self.client.post(
                "/api/hardware/retry",
                data=json.dumps({
                    "supervisor_pin": "SPV-9042",
                    "attempt_order_id": "PO-001",
                    "reason": f"limit test {i}",
                }),
                content_type="application/json",
            )

        # Dispatch one last PENDING command to UNCONFIRMED again
        process_single_outbox_command(self.test_db_path, send_fn=MagicMock(return_value=False))

        resp = self.client.get("/api/order")
        data = resp.get_json()
        self.assertFalse(data["can_hardware_retry"])
        self.assertEqual(data["arduino_communication_status"], "UNCONFIRMED")

    # ==================================================================
    # 6. HTML serves hardware section markup
    # ==================================================================

    def test_21_frontend_contains_hardware_status_section(self):
        """The frontend HTML must contain the hardware status section elements."""
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("hardwareSection", html)
        self.assertIn("hwStatusBadge", html)
        self.assertIn("hwRetryForm", html)
        self.assertIn("Arduino Signal Status", html)
        # Must include the CONFIRMED semantics disclaimer
        self.assertIn("does not confirm physical", html)


class TestPhase4BWorkerIntegrationWithMock(unittest.TestCase):
    """
    Integration tests that start an actual worker thread with a mocked send_fn.
    Uses temporary databases; never touches COM5.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "worker_integration.db"
        mobile_scanner_core.initialize_mobile_database(self.db_path)
        stop_global_worker()

    def tearDown(self):
        stop_global_worker()
        self.temp_dir.cleanup()

    def test_22_worker_dispatches_ng_command_and_marks_confirmed(self):
        """Running worker with mocked ACK should mark outbox CONFIRMED end-to-end."""
        mock_send = MagicMock(return_value=True)
        mobile_scanner_core.start_global_worker(
            db_path=self.db_path, poll_interval=0.05, send_fn=mock_send
        )

        mobile_scanner_core.process_mobile_scan(
            self.db_path, "worker-int-ng", "PART-A|RH|1"
        )

        # Allow worker to consume
        for _ in range(40):
            with closing(database.get_connection(self.db_path)) as conn:
                row = conn.execute(
                    "SELECT status FROM hardware_command_outbox WHERE attempt_order_id = 'PO-001'"
                ).fetchone()
                if row and row["status"] == "CONFIRMED":
                    break
            time.sleep(0.05)

        with closing(database.get_connection(self.db_path)) as conn:
            row = conn.execute(
                "SELECT status FROM hardware_command_outbox"
            ).fetchone()
        self.assertEqual(row["status"], "CONFIRMED")
        mock_send.assert_called_with("NG")

    def test_23_worker_marks_unconfirmed_on_failed_send(self):
        """Running worker with failed mock should mark outbox UNCONFIRMED."""
        mock_send = MagicMock(return_value=False)
        mobile_scanner_core.start_global_worker(
            db_path=self.db_path, poll_interval=0.05, send_fn=mock_send
        )

        mobile_scanner_core.process_mobile_scan(
            self.db_path, "worker-int-fail", "PART-A|RH|1"
        )

        for _ in range(40):
            with closing(database.get_connection(self.db_path)) as conn:
                row = conn.execute(
                    "SELECT status FROM hardware_command_outbox"
                ).fetchone()
                if row and row["status"] == "UNCONFIRMED":
                    break
            time.sleep(0.05)

        with closing(database.get_connection(self.db_path)) as conn:
            row = conn.execute("SELECT status FROM hardware_command_outbox").fetchone()
        self.assertEqual(row["status"], "UNCONFIRMED")

    def test_24_retry_wakes_worker_and_command_is_reprocessed(self):
        """Supervisor retry sets PENDING; worker re-claims and dispatches."""
        # First attempt: fail
        fail_send = MagicMock(return_value=False)
        mobile_scanner_core.start_global_worker(
            db_path=self.db_path, poll_interval=0.05, send_fn=fail_send
        )

        mobile_scanner_core.process_mobile_scan(
            self.db_path, "worker-retry-ng", "PART-A|RH|1"
        )

        # Wait for UNCONFIRMED
        for _ in range(40):
            with closing(database.get_connection(self.db_path)) as conn:
                row = conn.execute("SELECT status FROM hardware_command_outbox").fetchone()
                if row and row["status"] == "UNCONFIRMED":
                    break
            time.sleep(0.05)

        # Stop worker, swap to success mock, restart
        stop_global_worker()

        success_send = MagicMock(return_value=True)
        mobile_scanner_core.start_global_worker(
            db_path=self.db_path, poll_interval=0.05, send_fn=success_send
        )

        # Supervisor retry → resets to PENDING
        code, result = mobile_scanner_core.supervisor_hardware_retry(
            db_path=self.db_path,
            supervisor_pin="SPV-9042",
            attempt_order_id="PO-001",
            reason="Cable reconnected — retry test",
        )
        self.assertEqual(code, 200)

        # Worker should now pick it up and mark CONFIRMED
        for _ in range(40):
            with closing(database.get_connection(self.db_path)) as conn:
                row = conn.execute("SELECT status FROM hardware_command_outbox").fetchone()
                if row and row["status"] == "CONFIRMED":
                    break
            time.sleep(0.05)

        with closing(database.get_connection(self.db_path)) as conn:
            row = conn.execute("SELECT status FROM hardware_command_outbox").fetchone()
        self.assertEqual(row["status"], "CONFIRMED")

    def test_25_worker_survives_multiple_http_requests(self):
        """Worker must NOT be stopped by per-request appcontext teardown."""
        stop_global_worker()
        mock_send = MagicMock(return_value=True)

        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "lifecycle_test.db"
            mobile_scanner_core.initialize_mobile_database(db_path)

            with patch("mobile_scanner_core.main.send_arduino_result", mock_send):
                app = mobile_scanner.create_app(db_path=db_path, testing=False)
                client = app.test_client()

                worker = ArduinoOutboxWorker.get_active_worker()
                self.assertIsNotNone(worker)
                self.assertTrue(worker.is_alive())

                # Request 1: GET / (triggers request context push & teardown)
                r1 = client.get("/")
                self.assertEqual(r1.status_code, 200)
                self.assertTrue(worker.is_alive(), "Worker was killed after GET /")
                self.assertIs(ArduinoOutboxWorker.get_active_worker(), worker)

                # Request 2: GET /api/order
                r2 = client.get("/api/order")
                self.assertEqual(r2.status_code, 200)
                self.assertTrue(worker.is_alive(), "Worker was killed after GET /api/order")
                self.assertIs(ArduinoOutboxWorker.get_active_worker(), worker)

                # Request 3: GET /api/history
                r3 = client.get("/api/history")
                self.assertEqual(r3.status_code, 200)
                self.assertTrue(worker.is_alive(), "Worker was killed after GET /api/history")
                self.assertIs(ArduinoOutboxWorker.get_active_worker(), worker)

                # Clean up at process boundary
                stop_global_worker()
                self.assertFalse(worker.is_alive())
                self.assertIsNone(ArduinoOutboxWorker.get_active_worker())


if __name__ == "__main__":
    unittest.main()
