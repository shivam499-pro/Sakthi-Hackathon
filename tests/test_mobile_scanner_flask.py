"""
Flask API Unit Tests for Smart Export Packing Mobile Scanner Backend.
Tests:
- GET /api/order (returns initialized order with remaining_quantity)
- POST /api/scan (valid scan updates order, rejects malformed, supports pipe & JSON)
- Persistent nonce idempotency over API
- GET /api/history (returns chronological scan events)
- Persistence across independent API requests
- Full database isolation from production databases
"""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import mobile_scanner


def compute_file_hash(path: Path) -> str:
    if not path.exists():
        return ""
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest().upper()


class TestMobileScannerFlaskAPI(unittest.TestCase):

    def setUp(self):
        # Create an isolated temporary database for each test
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_db_path = Path(self.temp_dir.name) / "test_api_mobile.db"

        # Create Flask test client with injected isolated test db
        self.app = mobile_scanner.create_app(db_path=self.test_db_path)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

        # Baseline hashes of protected databases
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

        # Assert no test modified any existing database
        for path, baseline_hash in self.baseline_hashes.items():
            if baseline_hash:
                current_hash = compute_file_hash(path)
                self.assertEqual(
                    current_hash,
                    baseline_hash,
                    f"CRITICAL: Database {path} was modified during test!",
                )

    def test_get_root_serves_mobile_scanner_frontend(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.content_type)
        html = response.get_data(as_text=True)
        self.assertIn("Smart Packing Verification", html)
        self.assertIn("BOM Fulfillment Progress", html)
        self.assertIn("Camera Scanner", html)
        self.assertIn("Manual / Wedge Input", html)
        self.assertIn("Awaiting Confirmation", html)

    def test_static_assets_accessible(self):
        css_res = self.client.get("/static/css/mobile_scanner.css")
        self.assertEqual(css_res.status_code, 200)
        self.assertIn("text/css", css_res.content_type)
        css_res.close()

        js_res = self.client.get("/static/js/mobile_scanner.js")
        self.assertEqual(js_res.status_code, 200)
        js_res.close()

    def test_get_order_returns_initialized_mobile_order(self):
        response = self.client.get("/api/order")
        self.assertEqual(response.status_code, 200)

        data = response.get_json()
        self.assertEqual(data["packing_order_id"], "PO-001")
        self.assertEqual(data["box_id"], "BOX-001")
        self.assertEqual(data["order_id"], "ORD-2026-001")
        self.assertEqual(len(data["part_requirements"]), 3)

        # Check part remaining_quantity calculation
        part_a = next(p for p in data["part_requirements"] if p["part_id"] == "PART-A")
        self.assertEqual(part_a["required_quantity"], 2)
        self.assertEqual(part_a["scanned_quantity"], 0)
        self.assertEqual(part_a["remaining_quantity"], 2)
        self.assertEqual(part_a["packing_status"], "Pending")

    def test_post_valid_scan_pipe_format_updates_order(self):
        scan_payload = {
            "scan_nonce": "api-nonce-1",
            "payload": "PART-A|LH|1",
        }
        response = self.client.post(
            "/api/scan",
            data=json.dumps(scan_payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

        data = response.get_json()
        self.assertEqual(data["packing_order_id"], "PO-001")
        self.assertEqual(data["verification_result"], "HOLD")
        self.assertFalse(data["idempotent_replay"])

        part_a = next(p for p in data["part_requirements"] if p["part_id"] == "PART-A")
        self.assertEqual(part_a["scanned_quantity"], 1)
        self.assertEqual(part_a["remaining_quantity"], 1)
        self.assertEqual(part_a["packing_status"], "In Progress")

    def test_post_valid_scan_json_format_updates_order(self):
        scan_payload = {
            "scan_nonce": "api-nonce-2",
            "part_id": "PART-B",
            "variant": "RH",
            "quantity": 1,
        }
        response = self.client.post(
            "/api/scan",
            data=json.dumps(scan_payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

        data = response.get_json()
        part_b = next(p for p in data["part_requirements"] if p["part_id"] == "PART-B")
        self.assertEqual(part_b["scanned_quantity"], 1)
        self.assertEqual(part_b["remaining_quantity"], 1)

    def test_post_scan_rejects_missing_or_empty_nonce(self):
        response1 = self.client.post(
            "/api/scan",
            data=json.dumps({"payload": "PART-A|LH|1"}),
            content_type="application/json",
        )
        self.assertEqual(response1.status_code, 400)
        self.assertIn("error", response1.get_json())

        response2 = self.client.post(
            "/api/scan",
            data=json.dumps({"scan_nonce": "   ", "payload": "PART-A|LH|1"}),
            content_type="application/json",
        )
        self.assertEqual(response2.status_code, 400)

    def test_post_scan_rejects_malformed_payload(self):
        # Missing payload field
        response1 = self.client.post(
            "/api/scan",
            data=json.dumps({"scan_nonce": "nonce-bad-1"}),
            content_type="application/json",
        )
        self.assertEqual(response1.status_code, 400)

        # Boolean quantity
        response2 = self.client.post(
            "/api/scan",
            data=json.dumps({
                "scan_nonce": "nonce-bad-2",
                "part_id": "PART-A",
                "variant": "LH",
                "quantity": True,
            }),
            content_type="application/json",
        )
        self.assertEqual(response2.status_code, 400)

        # Decimal quantity
        response3 = self.client.post(
            "/api/scan",
            data=json.dumps({
                "scan_nonce": "nonce-bad-3",
                "payload": "PART-A|LH|1.5",
            }),
            content_type="application/json",
        )
        self.assertEqual(response3.status_code, 400)

        # Malformed pipe string
        response4 = self.client.post(
            "/api/scan",
            data=json.dumps({
                "scan_nonce": "nonce-bad-4",
                "payload": "INVALID_PIPE",
            }),
            content_type="application/json",
        )
        self.assertEqual(response4.status_code, 400)

    def test_repeating_same_nonce_returns_cached_response_without_duplicate_count(self):
        scan_payload = {
            "scan_nonce": "nonce-idempotency-check",
            "payload": "PART-A|LH|1",
        }

        # First request
        resp1 = self.client.post(
            "/api/scan",
            data=json.dumps(scan_payload),
            content_type="application/json",
        )
        self.assertEqual(resp1.status_code, 200)
        data1 = resp1.get_json()
        self.assertFalse(data1["idempotent_replay"])

        # Second request with identical nonce
        resp2 = self.client.post(
            "/api/scan",
            data=json.dumps(scan_payload),
            content_type="application/json",
        )
        self.assertEqual(resp2.status_code, 200)
        data2 = resp2.get_json()
        self.assertTrue(data2["idempotent_replay"])

        # Verify via GET /api/order that quantity was not incremented twice
        order_resp = self.client.get("/api/order")
        order_data = order_resp.get_json()
        part_a = next(p for p in order_data["part_requirements"] if p["part_id"] == "PART-A")
        self.assertEqual(part_a["scanned_quantity"], 1)
        self.assertEqual(part_a["remaining_quantity"], 1)

        # Verify via GET /api/history that only 1 event exists
        history_resp = self.client.get("/api/history")
        self.assertEqual(history_resp.get_json()["total_events"], 1)

    def test_get_history_returns_chronological_scans(self):
        # Scan 1
        self.client.post(
            "/api/scan",
            data=json.dumps({"scan_nonce": "hist-nonce-1", "payload": "PART-A|LH|1"}),
            content_type="application/json",
        )
        # Scan 2
        self.client.post(
            "/api/scan",
            data=json.dumps({"scan_nonce": "hist-nonce-2", "payload": "PART-B|RH|1"}),
            content_type="application/json",
        )

        response = self.client.get("/api/history")
        self.assertEqual(response.status_code, 200)

        data = response.get_json()
        self.assertEqual(data["total_events"], 2)

        history = data["scan_history"]
        # Descending chronological order: most recent scan first
        self.assertEqual(history[0]["part_id"], "PART-B")
        self.assertEqual(history[1]["part_id"], "PART-A")

    def test_persistence_across_separate_api_requests(self):
        # Scan 1: PART-C standard quantity 2
        self.client.post(
            "/api/scan",
            data=json.dumps({"scan_nonce": "persist-1", "payload": "PART-C|Standard|2"}),
            content_type="application/json",
        )

        # Separate independent request: fetch order
        resp = self.client.get("/api/order")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()

        part_c = next(p for p in data["part_requirements"] if p["part_id"] == "PART-C")
        self.assertEqual(part_c["scanned_quantity"], 2)
        self.assertEqual(part_c["remaining_quantity"], 2)
        self.assertEqual(part_c["packing_status"], "In Progress")

        # Scan 2: PART-C standard quantity 2 to complete
        self.client.post(
            "/api/scan",
            data=json.dumps({"scan_nonce": "persist-2", "payload": "PART-C|Standard|2"}),
            content_type="application/json",
        )

        resp2 = self.client.get("/api/order")
        data2 = resp2.get_json()
        part_c2 = next(p for p in data2["part_requirements"] if p["part_id"] == "PART-C")
        self.assertEqual(part_c2["scanned_quantity"], 4)
        self.assertEqual(part_c2["remaining_quantity"], 0)
        self.assertEqual(part_c2["packing_status"], "Complete")

    def test_get_history_empty_database_returns_valid_empty_json(self):
        # Call GET /api/history before submitting any scans
        response = self.client.get("/api/history")
        self.assertEqual(response.status_code, 200)

        data = response.get_json()
        self.assertEqual(data["total_events"], 0)
        self.assertEqual(data["scan_history"], [])

    def test_post_scan_domain_ng_on_mismatched_variant(self):
        # Submit syntactically valid payload with mismatched variant (PART-A requires LH, supplying RH)
        scan_payload = {
            "scan_nonce": "ng-test-nonce-1",
            "payload": "PART-A|RH|1",
        }
        response = self.client.post(
            "/api/scan",
            data=json.dumps(scan_payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

        data = response.get_json()
        self.assertEqual(data["verification_result"], "NG")
        self.assertNotEqual(data["verification_result"], "PASS")

        # Verify part A status is marked NG in response
        part_a = next(p for p in data["part_requirements"] if p["part_id"] == "PART-A")
        self.assertEqual(part_a["packing_status"], "NG")

        # Verify persisted order state reflects NG
        order_resp = self.client.get("/api/order")
        order_data = order_resp.get_json()
        self.assertEqual(order_data["verification_result"], "NG")
        persisted_part_a = next(p for p in order_data["part_requirements"] if p["part_id"] == "PART-A")
        self.assertEqual(persisted_part_a["packing_status"], "NG")

        # Verify scan history records the NG scan event
        hist_resp = self.client.get("/api/history")
        hist_data = hist_resp.get_json()
        self.assertEqual(hist_data["total_events"], 1)
        self.assertEqual(hist_data["scan_history"][0]["scan_result"], "NG")
        self.assertEqual(hist_data["scan_history"][0]["part_id"], "PART-A")
        self.assertEqual(hist_data["scan_history"][0]["supplied_variant"], "RH")


if __name__ == "__main__":
    unittest.main()
