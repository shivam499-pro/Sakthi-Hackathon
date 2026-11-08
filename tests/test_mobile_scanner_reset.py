"""
Phase 3.1 Tests — Supervisor-Authorized Reset for the Mobile Packing Scanner.

Tests:
- NG order locks further operator scans
- Unauthorized reset attempt (wrong PIN) is rejected with HTTP 401
- Authorized reset creates a new attempt, returning the new attempt ID
- Previous NG attempt history (scan events, verification events) is preserved
- New attempt starts with all quantities at zero / status Pending
- New scans after reset affect only the new attempt
- Duplicate reset on a non-NG (ACTIVE) order is rejected with HTTP 400
- Atomic rollback: simulated failure mid-reset leaves no partial state
- Nonce idempotency from old attempt is unaffected by reset

All tests use temporaryDirectory-isolated databases.
No production or project databases are touched.
"""

import hashlib
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import database
import main
import mobile_scanner_core


PROTECTED_DBS = [
    Path("packing_verification.db"),
    Path("packing_verification_backup.db"),
    Path("packing_verification_hardware_test.db"),
    Path("packing_verification_ng_test.db"),
]

VALID_PIN = mobile_scanner_core.DEFAULT_SUPERVISOR_PIN
WRONG_PIN = "WRONG-PIN-000"


def _hash(path: Path) -> str:
    if not path.exists():
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest().upper()


def _make_ng_order(db_path: Path) -> str:
    """
    Helper: initialise mobile DB, submit one scan that causes NG (wrong variant),
    and return the packing_order_id of the locked attempt.
    """
    mobile_scanner_core.initialize_mobile_database(db_path)
    mobile_scanner_core.initialize_mobile_order(db_path)
    # PART-A requires LH — scanning RH triggers NG
    status, result = mobile_scanner_core.process_mobile_scan(
        db_path=db_path,
        scan_nonce="ng-setup-nonce-001",
        raw_payload="PART-A|RH|1",
    )
    assert result["verification_result"] == "NG", (
        f"Expected NG from setup scan, got {result['verification_result']}"
    )
    return result["packing_order_id"]


class TestSupervisorReset(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "mobile_test.db"
        self.baseline_hashes = {p: _hash(p) for p in PROTECTED_DBS}

    def tearDown(self):
        self.temp_dir.cleanup()
        for path, baseline in self.baseline_hashes.items():
            if baseline:
                current = _hash(path)
                self.assertEqual(
                    current, baseline,
                    f"CRITICAL: Protected database {path} was modified during test!"
                )

    # ------------------------------------------------------------------
    # 1. NG order locks further operator scans
    # ------------------------------------------------------------------
    def test_ng_attempt_remains_locked_after_scan(self):
        """A second scan on an NG-locked order must return NG without updating quantities."""
        _make_ng_order(self.db_path)

        # Load the NG order state
        order_before = mobile_scanner_core.initialize_mobile_order(self.db_path)
        self.assertEqual(order_before["verification_result"], "NG")
        part_a_before = next(
            (p for p in order_before["part_requirements"] if p["part_id"] == "PART-A"), None
        )
        scanned_before = part_a_before["scanned_quantity"] if part_a_before else 0

        # Attempt another scan on the locked order
        status, result = mobile_scanner_core.process_mobile_scan(
            db_path=self.db_path,
            scan_nonce="locked-scan-nonce-002",
            raw_payload="PART-A|LH|1",
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["verification_result"], "NG",
                         "NG-locked order must reject further operator scans")

        # Quantities must remain unchanged (NG short-circuit does not update counts)
        order_after = mobile_scanner_core.initialize_mobile_order(self.db_path)
        part_a_after = next(
            (p for p in order_after["part_requirements"] if p["part_id"] == "PART-A"), None
        )
        scanned_after = part_a_after["scanned_quantity"] if part_a_after else 0
        self.assertEqual(scanned_after, scanned_before,
                         "Scanned quantity must not change on NG-locked attempt")

    # ------------------------------------------------------------------
    # 2. Wrong PIN is rejected
    # ------------------------------------------------------------------
    def test_unauthorized_reset_rejected(self):
        """Wrong supervisor PIN must return 401 and not create a new attempt."""
        _make_ng_order(self.db_path)

        attempt_before = mobile_scanner_core.get_current_mobile_attempt(self.db_path)
        self.assertIsNotNone(attempt_before)

        status, result = mobile_scanner_core.authorize_supervisor_reset(
            db_path=self.db_path,
            supervisor_pin=WRONG_PIN,
            reason="Unauthorized test reset",
        )
        self.assertEqual(status, 401)
        self.assertIn("error", result)
        self.assertIn("Unauthorized", result["error"])

        # No new attempt must have been created
        attempt_after = mobile_scanner_core.get_current_mobile_attempt(self.db_path)
        self.assertEqual(
            attempt_before["attempt_id"], attempt_after["attempt_id"],
            "Attempt ID must not change after rejected reset"
        )

    # ------------------------------------------------------------------
    # 3. Empty reason is rejected
    # ------------------------------------------------------------------
    def test_reset_without_reason_rejected(self):
        """Empty or whitespace-only reason must return 400."""
        _make_ng_order(self.db_path)

        status, result = mobile_scanner_core.authorize_supervisor_reset(
            db_path=self.db_path,
            supervisor_pin=VALID_PIN,
            reason="   ",
        )
        self.assertEqual(status, 400)
        self.assertIn("error", result)

    # ------------------------------------------------------------------
    # 4. Authorized reset creates new attempt
    # ------------------------------------------------------------------
    def test_authorized_reset_creates_new_attempt(self):
        """Correct PIN + reason must return 200 and a new attempt ID."""
        _make_ng_order(self.db_path)

        status, result = mobile_scanner_core.authorize_supervisor_reset(
            db_path=self.db_path,
            supervisor_pin=VALID_PIN,
            reason="Defective PART-A — quarantined for re-pack",
        )
        self.assertEqual(status, 200)
        self.assertIn("new_attempt_order_id", result)
        self.assertIn("attempt_number", result)
        self.assertEqual(result["attempt_number"], 2)
        self.assertIn("ATTEMPT-2", result["new_attempt_order_id"])

        # New attempt must be ACTIVE
        new_attempt = mobile_scanner_core.get_current_mobile_attempt(self.db_path)
        self.assertIsNotNone(new_attempt)
        self.assertEqual(new_attempt["status"], "ACTIVE")
        self.assertEqual(new_attempt["attempt_number"], 2)

    # ------------------------------------------------------------------
    # 5. Historical scan events are preserved after reset
    # ------------------------------------------------------------------
    def test_previous_ng_attempt_history_preserved(self):
        """Old scan events from the NG attempt must still exist after reset."""
        old_order_id = _make_ng_order(self.db_path)

        mobile_scanner_core.authorize_supervisor_reset(
            db_path=self.db_path,
            supervisor_pin=VALID_PIN,
            reason="Re-pack: quarantined part",
        )

        with closing(database.get_connection(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT * FROM scan_events WHERE packing_order_id = ?",
                (old_order_id,),
            ).fetchall()

        self.assertGreater(len(rows), 0,
                           "Scan events from rejected attempt must be preserved after reset")

    # ------------------------------------------------------------------
    # 6. New attempt starts with zero scanned quantities
    # ------------------------------------------------------------------
    def test_new_attempt_starts_with_zero_quantities(self):
        """After reset, the new packing order must have all scanned_quantity = 0."""
        _make_ng_order(self.db_path)

        status, result = mobile_scanner_core.authorize_supervisor_reset(
            db_path=self.db_path,
            supervisor_pin=VALID_PIN,
            reason="Starting fresh packing attempt",
        )
        self.assertEqual(status, 200)
        new_order_id = result["new_attempt_order_id"]

        new_order = database.load_packing_order(new_order_id, db_path=self.db_path)
        self.assertIsNotNone(new_order)
        for part in new_order["part_requirements"]:
            self.assertEqual(part["scanned_quantity"], 0,
                             f"Part {part['part_id']} must start at 0 after reset")
            self.assertEqual(part["packing_status"], "Pending",
                             f"Part {part['part_id']} must be Pending after reset")
        self.assertIsNone(new_order.get("verification_result"))

    # ------------------------------------------------------------------
    # 7. New scans affect only the new attempt
    # ------------------------------------------------------------------
    def test_new_scans_affect_only_new_attempt(self):
        """Scan after reset must update new attempt, not the old rejected attempt."""
        old_order_id = _make_ng_order(self.db_path)

        status, result = mobile_scanner_core.authorize_supervisor_reset(
            db_path=self.db_path,
            supervisor_pin=VALID_PIN,
            reason="Restart packing after defect quarantine",
        )
        self.assertEqual(status, 200)
        new_order_id = result["new_attempt_order_id"]

        # Submit a valid scan on the new attempt
        scan_status, scan_result = mobile_scanner_core.process_mobile_scan(
            db_path=self.db_path,
            scan_nonce="new-attempt-scan-001",
            raw_payload="PART-A|LH|1",
        )
        self.assertEqual(scan_status, 200)
        # New attempt's order ID must be used for the scan
        self.assertEqual(scan_result["packing_order_id"], new_order_id)

        # Old attempt's order must remain unchanged (locked NG — not updated)
        old_order = database.load_packing_order(old_order_id, db_path=self.db_path)
        self.assertEqual(old_order["verification_result"], "NG")

        # New attempt's order must reflect the scan
        new_order = database.load_packing_order(new_order_id, db_path=self.db_path)
        part_a = next(
            (p for p in new_order["part_requirements"] if p["part_id"] == "PART-A"), None
        )
        self.assertIsNotNone(part_a)
        self.assertGreater(part_a["scanned_quantity"], 0,
                           "New attempt PART-A must have scan registered")

    # ------------------------------------------------------------------
    # 8. Reset on a non-NG (ACTIVE) order is rejected
    # ------------------------------------------------------------------
    def test_duplicate_reset_rejected_on_active_order(self):
        """Supervisor reset on a non-NG order must return 400."""
        mobile_scanner_core.initialize_mobile_database(self.db_path)
        mobile_scanner_core.initialize_mobile_order(self.db_path)

        # Order is ACTIVE/HOLD — submit a valid scan (not causing NG)
        mobile_scanner_core.process_mobile_scan(
            db_path=self.db_path,
            scan_nonce="active-scan-nonce-001",
            raw_payload="PART-A|LH|1",
        )
        # Order still in HOLD/partial state — not NG — reset must be refused
        status, result = mobile_scanner_core.authorize_supervisor_reset(
            db_path=self.db_path,
            supervisor_pin=VALID_PIN,
            reason="Attempting reset on non-NG order",
        )
        self.assertEqual(status, 400)
        self.assertIn("error", result)
        self.assertIn("NG", result["error"])

    # ------------------------------------------------------------------
    # 9. Atomic rollback: failure mid-reset leaves no partial state
    # ------------------------------------------------------------------
    def test_reset_rollback_on_failure(self):
        """
        If an exception is raised during the reset transaction, the database
        must roll back completely — no new attempt record, no new packing order.
        """
        _make_ng_order(self.db_path)

        attempt_before = mobile_scanner_core.get_current_mobile_attempt(self.db_path)
        self.assertIsNotNone(attempt_before)
        attempt_id_before = attempt_before["attempt_id"]

        # Patch database._upsert_packing_order to fail inside the transaction
        with patch.object(database, "_upsert_packing_order",
                          side_effect=sqlite3.OperationalError("simulated_failure")):
            with self.assertRaises(sqlite3.OperationalError):
                mobile_scanner_core.authorize_supervisor_reset(
                    db_path=self.db_path,
                    supervisor_pin=VALID_PIN,
                    reason="Rollback test: simulated mid-transaction failure",
                )

        # After rollback: attempt must still be the original NG attempt
        attempt_after = mobile_scanner_core.get_current_mobile_attempt(self.db_path)
        self.assertIsNotNone(attempt_after)
        self.assertEqual(attempt_after["attempt_id"], attempt_id_before,
                         "Attempt ID must not change after rolled-back reset")
        # The attempt row was not committed as CLOSED_NG (transaction rolled back),
        # so it must still be in its pre-reset state (ACTIVE with an NG packing order).
        self.assertEqual(attempt_after["status"], "ACTIVE",
                         "Attempt status must remain ACTIVE after rollback (commit never happened)")

        # No supervisor_reset_events must have been persisted
        with closing(database.get_connection(self.db_path)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM supervisor_reset_events"
            ).fetchone()[0]
        self.assertEqual(count, 0,
                         "supervisor_reset_events must be empty after rollback")

    # ------------------------------------------------------------------
    # 10. Nonce idempotency from old attempt is unaffected by reset
    # ------------------------------------------------------------------
    def test_existing_nonce_idempotency_unaffected_by_reset(self):
        """
        A nonce from the old NG attempt must still return the cached response
        after a supervisor reset (the scan_requests table is shared and append-only).
        """
        old_nonce = "idempotency-old-nonce-001"
        mobile_scanner_core.initialize_mobile_database(self.db_path)
        mobile_scanner_core.initialize_mobile_order(self.db_path)

        # Submit a scan that sets NG
        mobile_scanner_core.process_mobile_scan(
            db_path=self.db_path,
            scan_nonce=old_nonce,
            raw_payload="PART-A|RH|1",
        )

        # Perform supervisor reset
        status, _ = mobile_scanner_core.authorize_supervisor_reset(
            db_path=self.db_path,
            supervisor_pin=VALID_PIN,
            reason="Idempotency test reset",
        )
        self.assertEqual(status, 200)

        # Re-submit old nonce — must return the cached (NG) response unchanged
        replay_status, replay_result = mobile_scanner_core.process_mobile_scan(
            db_path=self.db_path,
            scan_nonce=old_nonce,
            raw_payload="PART-A|RH|1",
        )
        self.assertEqual(replay_status, 200)
        self.assertTrue(replay_result.get("idempotent_replay"),
                        "Old nonce must be replayed from cache after reset")

    # ------------------------------------------------------------------
    # 11. Second reset increments attempt number to 3
    # ------------------------------------------------------------------
    def test_multiple_resets_increment_attempt_numbers(self):
        """Two consecutive resets must produce attempt 2 then attempt 3."""
        _make_ng_order(self.db_path)

        # First reset -> attempt 2
        s1, r1 = mobile_scanner_core.authorize_supervisor_reset(
            db_path=self.db_path,
            supervisor_pin=VALID_PIN,
            reason="First reset — defect cleared",
        )
        self.assertEqual(s1, 200)
        self.assertEqual(r1["attempt_number"], 2)
        new_id_2 = r1["new_attempt_order_id"]

        # Cause NG on attempt 2
        mobile_scanner_core.process_mobile_scan(
            db_path=self.db_path,
            scan_nonce="ng-attempt2-nonce",
            raw_payload="PART-A|RH|1",
        )

        # Second reset -> attempt 3
        s2, r2 = mobile_scanner_core.authorize_supervisor_reset(
            db_path=self.db_path,
            supervisor_pin=VALID_PIN,
            reason="Second reset — another defect",
        )
        self.assertEqual(s2, 200)
        self.assertEqual(r2["attempt_number"], 3)
        self.assertIn("ATTEMPT-3", r2["new_attempt_order_id"])

        # Both old attempts must still exist in DB
        with closing(database.get_connection(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT attempt_number, status FROM mobile_attempts ORDER BY attempt_number"
            ).fetchall()
        attempt_nums = [r["attempt_number"] for r in rows]
        self.assertIn(1, attempt_nums)
        self.assertIn(2, attempt_nums)
        self.assertIn(3, attempt_nums)

    # ------------------------------------------------------------------
    # 12. supervisor_reset_events audit log is written
    # ------------------------------------------------------------------
    def test_supervisor_audit_log_written(self):
        """A successful reset must write exactly one record to supervisor_reset_events."""
        _make_ng_order(self.db_path)
        reason_text = "Audit log test — defect quarantined"

        mobile_scanner_core.authorize_supervisor_reset(
            db_path=self.db_path,
            supervisor_pin=VALID_PIN,
            reason=reason_text,
        )

        with closing(database.get_connection(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT * FROM supervisor_reset_events"
            ).fetchall()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], reason_text)
        self.assertEqual(rows[0]["supervisor_id"], "SUPERVISOR-01")
        self.assertIsNotNone(rows[0]["timestamp"])


if __name__ == "__main__":
    unittest.main()
