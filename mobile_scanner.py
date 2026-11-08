"""
Flask API backend for the Smart Export Packing Verification Mobile Scanner.
Provides REST endpoints for order status, scan processing, scan history,
hardware status, and supervisor-authorized hardware retry using isolated
mobile database storage without modifying existing workflows.
"""

import atexit
from contextlib import closing
import os
from pathlib import Path
from typing import Any, Optional, Union

from flask import Flask, jsonify, render_template, request

import database
import mobile_scanner_core

DEFAULT_MOBILE_DB = mobile_scanner_core.DEFAULT_MOBILE_DB_PATH

_worker_shutdown_registered = False


def _register_worker_shutdown():
    """Register worker shutdown hook with atexit once per process."""
    global _worker_shutdown_registered
    if not _worker_shutdown_registered:
        atexit.register(mobile_scanner_core.stop_global_worker)
        _worker_shutdown_registered = True


def create_app(
    db_path: Optional[Union[str, Path]] = None,
    testing: bool = False,
) -> Flask:
    """Application factory for the mobile scanner Flask API."""
    app = Flask(__name__)
    resolved_db_path = Path(db_path) if db_path else DEFAULT_MOBILE_DB
    app.config["MOBILE_DB_PATH"] = resolved_db_path
    if testing:
        app.config["TESTING"] = True

    # Ensure the mobile database tables exist upon app creation
    mobile_scanner_core.initialize_mobile_database(resolved_db_path)

    # --- Phase 4B: Start the durable Arduino outbox background worker ---
    # The worker runs continuously in the background for the process lifetime.
    # It performs crash recovery (PROCESSING -> PENDING) on startup and drains
    # the outbox in strict FIFO order, waking immediately on terminal transitions.
    #
    # Lifecycle controls:
    # 1. Automated testing (testing=True) suppresses worker startup so tests
    #    can control serial dispatch deterministically via mock interfaces.
    # 2. Repeated create_app() calls safely reuse an existing active worker
    #    for the same database, while rejecting conflicting database paths.
    # 3. Flask development reloader parent process is detected to avoid duplicates.
    # 4. Worker cleanup is registered via atexit for process-level termination,
    #    never tied to individual HTTP request context teardown.
    if not app.config.get("TESTING", False):
        is_reloader_parent = (
            os.environ.get("WERKZEUG_RUN_MAIN") is None
            and os.environ.get("WERKZEUG_SERVER_FD") is not None
        )
        if not is_reloader_parent:
            active_worker = mobile_scanner_core.ArduinoOutboxWorker.get_active_worker()
            if active_worker is not None and active_worker.is_alive():
                if str(getattr(active_worker, "db_path", "")) != str(resolved_db_path):
                    # Different database requested: cleanly stop stale worker
                    # before starting a worker for the new database path.
                    mobile_scanner_core.stop_global_worker(timeout=2.0)
                    mobile_scanner_core.start_global_worker(db_path=resolved_db_path)
                    _register_worker_shutdown()
            else:
                mobile_scanner_core.start_global_worker(db_path=resolved_db_path)
                _register_worker_shutdown()

    @app.route("/", methods=["GET"])
    def index():
        """Serve the industrial mobile scanner frontend interface."""
        return render_template("mobile_scanner.html")

    @app.route("/api/order", methods=["GET"])
    def get_order():
        """Return the current mobile packing order and part status."""
        try:
            current_db = app.config["MOBILE_DB_PATH"]
            order = mobile_scanner_core.initialize_mobile_order(current_db)

            enriched_parts = []
            for part in order.get("part_requirements", []):
                required = part.get("required_quantity", 0)
                scanned = part.get("scanned_quantity", 0)
                part_copy = dict(part)
                part_copy["remaining_quantity"] = max(0, required - scanned)
                enriched_parts.append(part_copy)

            attempt = mobile_scanner_core.get_current_mobile_attempt(current_db)
            attempt_number = attempt["attempt_number"] if attempt else 1
            attempt_status = attempt["status"] if attempt else (
                "CLOSED_NG" if order.get("verification_result") == "NG" else "ACTIVE"
            )

            # --- Phase 4B: Include hardware communication status ---
            # NOTE: database.load_packing_order() does NOT SELECT arduino_communication_status
            # (it was not added to load_packing_order's SELECT list in database.py).
            # Read it via a direct query so we don't modify database.py.
            packing_order_id = order.get("packing_order_id", "PO-001")
            with closing(database.get_connection(current_db)) as _hw_conn:
                _hw_row = _hw_conn.execute(
                    "SELECT arduino_communication_status FROM packing_orders WHERE packing_order_id = ?",
                    (packing_order_id,)
                ).fetchone()
            hw_status = _hw_row["arduino_communication_status"] if _hw_row else None
            # hw_status: CONFIRMED | UNCONFIRMED | None
            # CONFIRMED = expected ACK string received from Arduino over UART.
            # UNCONFIRMED = timeout, disconnect, or unexpected response.
            # None = no terminal command dispatched yet.

            # Outbox record provides PENDING/PROCESSING while worker is in flight
            outbox = mobile_scanner_core.get_hardware_outbox_status(
                current_db, attempt_order_id=packing_order_id
            )
            outbox_status = outbox["status"] if outbox else None
            outbox_retry_count = outbox["retry_count"] if outbox else 0

            # Effective hardware status surfaced to the UI:
            #   PENDING / PROCESSING → command in flight (not yet settled)
            #   CONFIRMED / UNCONFIRMED → settled result from packing_orders column
            #   None → no terminal command dispatched yet
            if outbox_status in ("PENDING", "PROCESSING"):
                effective_hw_status = outbox_status
            else:
                effective_hw_status = hw_status  # CONFIRMED | UNCONFIRMED | None

            response_data = {
                "packing_order_id": order.get("packing_order_id"),
                "box_id": order.get("box_id"),
                "order_id": order.get("order_id"),
                "product_description": order.get("product_description"),
                "verification_result": order.get("verification_result"),
                "verification_timestamp": order.get("verification_timestamp"),
                "part_requirements": enriched_parts,
                "attempt_number": attempt_number,
                "attempt_status": attempt_status,
                "can_reset": order.get("verification_result") == "NG",
                # Hardware status fields
                "arduino_communication_status": effective_hw_status,
                "hardware_retry_count": outbox_retry_count,
                "hardware_retry_max": mobile_scanner_core.HARDWARE_RETRY_MAX,
                "can_hardware_retry": (
                    effective_hw_status == "UNCONFIRMED"
                    and outbox_retry_count < mobile_scanner_core.HARDWARE_RETRY_MAX
                ),
            }
            return jsonify(response_data), 200
        except Exception:
            return jsonify({"error": "Failed to retrieve packing order."}), 500

    @app.route("/api/scan", methods=["POST"])
    def post_scan():
        """Accept a scan payload and unique nonce, returning verified order state."""
        try:
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                return (
                    jsonify({"error": "Invalid request body. JSON object expected."}),
                    400,
                )

            scan_nonce = data.get("scan_nonce")
            if not scan_nonce or not isinstance(scan_nonce, str) or not scan_nonce.strip():
                return jsonify({"error": "Missing or invalid scan_nonce."}), 400

            if "payload" in data:
                raw_payload = data["payload"]
            elif "part_id" in data:
                raw_payload = {
                    "part_id": data.get("part_id"),
                    "variant": data.get("variant"),
                    "quantity": data.get("quantity"),
                }
            else:
                return (
                    jsonify(
                        {
                            "error": "Missing scan payload (expected 'payload' or 'part_id')."
                        }
                    ),
                    400,
                )

            current_db = app.config["MOBILE_DB_PATH"]
            status_code, result = mobile_scanner_core.process_mobile_scan(
                db_path=current_db,
                scan_nonce=scan_nonce.strip(),
                raw_payload=raw_payload,
            )

            if isinstance(result, dict) and "part_requirements" in result:
                for part in result["part_requirements"]:
                    required = part.get("required_quantity", 0)
                    scanned = part.get("scanned_quantity", 0)
                    part["remaining_quantity"] = max(0, required - scanned)

            return jsonify(result), status_code
        except Exception:
            return (
                jsonify(
                    {"error": "An internal error occurred while processing scan."}
                ),
                500,
            )

    @app.route("/api/history", methods=["GET"])
    def get_history():
        """Return chronological scan history from SQLite scan_events."""
        try:
            current_db = app.config["MOBILE_DB_PATH"]
            with closing(database.get_connection(current_db)) as connection:
                rows = connection.execute("""
                    SELECT scan_event_id, packing_order_id, part_id, supplied_variant,
                           quantity, scan_result, scan_timestamp
                    FROM scan_events
                    ORDER BY scan_event_id DESC
                """).fetchall()

                history = [
                    {
                        "scan_event_id": row["scan_event_id"],
                        "packing_order_id": row["packing_order_id"],
                        "part_id": row["part_id"],
                        "supplied_variant": row["supplied_variant"],
                        "quantity": row["quantity"],
                        "scan_result": row["scan_result"],
                        "scan_timestamp": row["scan_timestamp"],
                    }
                    for row in rows
                ]
            return jsonify({"scan_history": history, "total_events": len(history)}), 200
        except Exception:
            return jsonify({"error": "Failed to retrieve scan history."}), 500

    @app.route("/api/supervisor/reset", methods=["POST"])
    def post_supervisor_reset():
        """Supervisor authorization endpoint to reset an NG order and start a clean attempt."""
        try:
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                return jsonify({"error": "Invalid request body. JSON object expected."}), 400

            supervisor_pin = data.get("supervisor_pin")
            reason = data.get("reason")
            current_db = app.config["MOBILE_DB_PATH"]

            status_code, result = mobile_scanner_core.authorize_supervisor_reset(
                db_path=current_db,
                supervisor_pin=supervisor_pin,
                reason=reason,
            )

            if isinstance(result, dict) and "order" in result:
                for part in result["order"].get("part_requirements", []):
                    req = part.get("required_quantity", 0)
                    scanned = part.get("scanned_quantity", 0)
                    part["remaining_quantity"] = max(0, req - scanned)

            return jsonify(result), status_code
        except Exception:
            return jsonify({"error": "An internal error occurred during supervisor reset."}), 500

    @app.route("/api/hardware/retry", methods=["POST"])
    def post_hardware_retry():
        """
        Supervisor-authorized manual retry for a failed (UNCONFIRMED) hardware command.

        Required JSON fields:
          - supervisor_pin: pre-shared supervisor PIN (same as /api/supervisor/reset)
          - attempt_order_id: the packing order ID whose outbox command to retry
          - reason: human-readable retry reason (audit log required)

        Rules enforced:
          - Only UNCONFIRMED outbox commands may be retried.
          - Maximum HARDWARE_RETRY_MAX (3) retries per command.
          - Does NOT modify verification_result, scanned quantities, or attempt history.
          - Resets outbox row to PENDING so the background worker dispatches it.
          - Audit entry recorded in supervisor_reset_events.
          - Wakes the background worker if running.

        CONFIRMED means only that the expected serial acknowledgement string was
        received over UART — it does NOT confirm physical LED or gate operation.
        At-least-once hardware delivery applies: if the Arduino executed the
        previous command before the ACK was lost, it may actuate again on retry.
        """
        try:
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                return jsonify({"error": "Invalid request body. JSON object expected."}), 400

            supervisor_pin = data.get("supervisor_pin")
            attempt_order_id = data.get("attempt_order_id")
            reason = data.get("reason")
            current_db = app.config["MOBILE_DB_PATH"]

            status_code, result = mobile_scanner_core.supervisor_hardware_retry(
                db_path=current_db,
                supervisor_pin=supervisor_pin,
                attempt_order_id=attempt_order_id,
                reason=reason,
            )
            return jsonify(result), status_code
        except Exception:
            return jsonify({"error": "An internal error occurred during hardware retry."}), 500

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=5000, debug=False)
