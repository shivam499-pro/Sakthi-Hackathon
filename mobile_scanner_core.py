"""
Core logic for the Smart Export Packing Verification Mobile Scanner.
Provides payload validation, persistent idempotency, isolated database management,
atomic scan processing, and supervisor-authorized attempt lifecycle management
without touching main.py or production databases.
"""

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Callable, Optional, Union
from contextlib import closing

import database
import main

# Target isolated test database path
DEFAULT_MOBILE_DB_PATH = Path(__file__).with_name("packing_verification_mobile_test.db")

# Pre-shared supervisor authorization PIN for prototype/demonstration
DEFAULT_SUPERVISOR_PIN = os.environ.get("SUPERVISOR_PIN", "SPV-9042")

# Reentrant lock: process_mobile_scan and authorize_supervisor_reset hold this lock
# while calling initialize_mobile_order, which also acquires it.  RLock allows
# the same thread to re-enter without deadlocking.
_scan_lock = threading.RLock()


def parse_scan_payload(raw_payload: Union[str, dict[str, Any]]) -> tuple[str, str, int]:
    """
    Parse and strictly validate a scan payload.
    Supports:
      1. Project-defined pipe format: "PART_ID|VARIANT|QUANTITY"
      2. Structured JSON: {"part_id": "PART_ID", "variant": "VARIANT", "quantity": 1}
    Rejects booleans, floats, decimals, missing fields, zero, or negative quantities.
    Returns: (part_id, variant, quantity)
    """
    if isinstance(raw_payload, dict):
        part_id = raw_payload.get("part_id")
        variant = raw_payload.get("variant")
        quantity = raw_payload.get("quantity")
    elif isinstance(raw_payload, str):
        trimmed = raw_payload.strip()
        if trimmed.startswith("{") and trimmed.endswith("}"):
            try:
                data = json.loads(trimmed)
            except Exception as err:
                raise ValueError(f"Malformed JSON payload: {err}") from err
            if not isinstance(data, dict):
                raise ValueError("JSON payload must be a JSON object")
            part_id = data.get("part_id")
            variant = data.get("variant")
            quantity = data.get("quantity")
        else:
            parts = [p.strip() for p in trimmed.split("|")]
            if len(parts) != 3:
                raise ValueError(
                    "Invalid payload format. Expected project format: 'PART_ID|VARIANT|QUANTITY'"
                )
            part_id, variant, qty_str = parts
            if not qty_str.isdigit():
                raise ValueError("Quantity must be a positive integer")
            quantity = int(qty_str)
    else:
        raise ValueError("Payload must be a string or JSON object")

    # Strict type checks
    if not isinstance(part_id, str) or not part_id.strip():
        raise ValueError("Part ID must be a non-empty string")
    part_id = part_id.strip()

    if not isinstance(variant, str) or not variant.strip():
        raise ValueError("Variant must be a non-empty string")
    variant = variant.strip()

    # Reject boolean (since in Python isinstance(True, int) is True)
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise ValueError("Quantity must be a genuine integer (not boolean or float)")

    if quantity <= 0:
        raise ValueError("Quantity must be greater than zero")

    return part_id, variant, quantity


def initialize_mobile_database(db_path: Union[str, Path] = DEFAULT_MOBILE_DB_PATH) -> None:
    """
    Initialize the isolated mobile database with standard tables, scan_requests table,
    and attempt/supervisor-reset tracking tables.
    """
    database.initialize_database(db_path=db_path)

    with closing(database.get_connection(db_path)) as connection, connection:
        # Idempotency table
        connection.execute("""
            CREATE TABLE IF NOT EXISTS scan_requests (
                request_id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_nonce TEXT NOT NULL UNIQUE,
                packing_order_id TEXT NOT NULL,
                part_id TEXT,
                variant TEXT,
                quantity INTEGER,
                response_status INTEGER NOT NULL,
                response_payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        # Packing attempts lifecycle table
        connection.execute("""
            CREATE TABLE IF NOT EXISTS mobile_attempts (
                attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                base_order_id TEXT NOT NULL,
                attempt_order_id TEXT NOT NULL UNIQUE,
                attempt_number INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'CLOSED_NG', 'COMPLETED')),
                created_at TEXT NOT NULL,
                closed_at TEXT
            )
        """)

        # Supervisor reset audit log table
        connection.execute("""
            CREATE TABLE IF NOT EXISTS supervisor_reset_events (
                reset_id INTEGER PRIMARY KEY AUTOINCREMENT,
                base_order_id TEXT NOT NULL,
                previous_attempt_order_id TEXT NOT NULL,
                new_attempt_order_id TEXT NOT NULL,
                supervisor_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                timestamp TEXT NOT NULL
            )
        """)

        # Hardware command durable outbox table
        connection.execute("""
            CREATE TABLE IF NOT EXISTS hardware_command_outbox (
                outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_order_id TEXT NOT NULL,
                command TEXT NOT NULL CHECK (command IN ('PASS', 'NG', 'HOLD')),
                status TEXT NOT NULL CHECK (status IN ('PENDING', 'PROCESSING', 'CONFIRMED', 'UNCONFIRMED')),
                retry_count INTEGER NOT NULL DEFAULT 0,
                error_message TEXT,
                created_at TEXT NOT NULL,
                claimed_at TEXT,
                processed_at TEXT,
                UNIQUE (attempt_order_id, command),
                FOREIGN KEY (attempt_order_id)
                    REFERENCES packing_orders(packing_order_id)
                    ON DELETE CASCADE
            )
        """)

        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_outbox_pending
            ON hardware_command_outbox (status, outbox_id ASC)
        """)

        # Dedicated hardware retry audit log table (Phase 4B)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS hardware_retry_events (
                retry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_order_id TEXT NOT NULL,
                outbox_id INTEGER NOT NULL,
                command TEXT NOT NULL CHECK (command IN ('PASS', 'NG', 'HOLD')),
                supervisor_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                previous_status TEXT NOT NULL,
                new_status TEXT NOT NULL,
                retry_number INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                FOREIGN KEY (outbox_id)
                    REFERENCES hardware_command_outbox(outbox_id)
                    ON DELETE CASCADE
            )
        """)
        connection.commit()


def get_current_mobile_attempt(
    db_path: Union[str, Path] = DEFAULT_MOBILE_DB_PATH,
    base_order_id: str = "PO-001",
) -> Optional[dict[str, Any]]:
    """Return the active or latest attempt record for the given base order."""
    with closing(database.get_connection(db_path)) as connection:
        # Check for currently ACTIVE attempt
        active = connection.execute("""
            SELECT attempt_id, base_order_id, attempt_order_id, attempt_number, status, created_at, closed_at
            FROM mobile_attempts
            WHERE base_order_id = ? AND status = 'ACTIVE'
            ORDER BY attempt_id DESC
            LIMIT 1
        """, (base_order_id,)).fetchone()

        if active is not None:
            return dict(active)

        # If no active attempt, fetch the most recent closed/rejected attempt
        latest = connection.execute("""
            SELECT attempt_id, base_order_id, attempt_order_id, attempt_number, status, created_at, closed_at
            FROM mobile_attempts
            WHERE base_order_id = ?
            ORDER BY attempt_id DESC
            LIMIT 1
        """, (base_order_id,)).fetchone()

        if latest is not None:
            return dict(latest)

    return None


def initialize_mobile_order(
    db_path: Union[str, Path] = DEFAULT_MOBILE_DB_PATH,
    template_order: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    Ensure the active mobile order exists in the isolated mobile test database.
    Manages attempt 1 creation if table is unseeded, and returns the current active/latest order.
    """
    initialize_mobile_database(db_path=db_path)

    if template_order is None:
        template_order = main.packing_order

    base_order_id = template_order["packing_order_id"]

    with _scan_lock:
        attempt = get_current_mobile_attempt(db_path, base_order_id=base_order_id)
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

        if attempt is None:
            # Seed attempt 1: for backwards compatibility, attempt 1 uses the base ID (e.g. PO-001)
            attempt_order_id = base_order_id

            # Check if order row already exists in packing_orders
            existing = database.load_packing_order(attempt_order_id, db_path=db_path)
            if existing is None:
                new_order = deepcopy(template_order)
                new_order["packing_order_id"] = attempt_order_id
                database.save_packing_order(new_order, db_path=db_path)
                order_status = "ACTIVE"
            else:
                # If existing is already NG, record it as CLOSED_NG
                order_status = "CLOSED_NG" if existing.get("verification_result") == "NG" else "ACTIVE"

            with closing(database.get_connection(db_path)) as connection, connection:
                connection.execute("""
                    INSERT INTO mobile_attempts (base_order_id, attempt_order_id, attempt_number, status, created_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (base_order_id, attempt_order_id, 1, order_status, now_iso))
                connection.commit()

            target_order_id = attempt_order_id
        else:
            target_order_id = attempt["attempt_order_id"]

        order = database.load_packing_order(target_order_id, db_path=db_path)
        if order is None:
            # Fallback if attempt record exists but packing order row was missing
            new_order = deepcopy(template_order)
            new_order["packing_order_id"] = target_order_id
            database.save_packing_order(new_order, db_path=db_path)
            return new_order

        return order


def authorize_supervisor_reset(
    db_path: Union[str, Path],
    supervisor_pin: str,
    reason: str,
    supervisor_id: str = "SUPERVISOR-01",
    base_order_id: str = "PO-001",
    template_order: Optional[dict[str, Any]] = None,
) -> tuple[int, dict[str, Any]]:
    """
    Supervisor-authorized workflow to close an NG packing attempt and launch a clean attempt:
    1. Validate supervisor authorization credentials (pre-shared PIN).
    2. Require a non-empty root-cause / reason.
    3. Verify that the previous attempt is currently in NG status (cannot reset non-NG orders).
    4. In ONE single atomic transaction:
       - Close previous attempt as 'CLOSED_NG'.
       - Create new attempt 'PO-001-ATTEMPT-N' in mobile_attempts.
       - Create clean new packing order and part requirements in packing_orders.
       - Record supervisor_reset_events audit trail.
    All historical scan events and rejected orders remain 100% preserved.
    """
    if not isinstance(supervisor_pin, str) or supervisor_pin != DEFAULT_SUPERVISOR_PIN:
        return 401, {"error": "Unauthorized: Invalid supervisor authorization PIN."}

    if not isinstance(reason, str) or not reason.strip():
        return 400, {"error": "Bad Request: A clear root-cause reason is required for supervisor reset."}

    reason = reason.strip()

    with _scan_lock:
        initialize_mobile_database(db_path=db_path)

        current_attempt = get_current_mobile_attempt(db_path, base_order_id=base_order_id)
        if current_attempt is None:
            initialize_mobile_order(db_path=db_path, template_order=template_order)
            current_attempt = get_current_mobile_attempt(db_path, base_order_id=base_order_id)

        prev_attempt_order_id = current_attempt["attempt_order_id"]
        prev_order = database.load_packing_order(prev_attempt_order_id, db_path=db_path)

        # Constraint: Only NG attempts may be reset by supervisor
        if not prev_order or prev_order.get("verification_result") != "NG":
            return 400, {"error": "Invalid action: Supervisor reset is only permitted on rejected (NG) orders."}

        next_attempt_number = current_attempt["attempt_number"] + 1
        new_attempt_order_id = f"{base_order_id}-ATTEMPT-{next_attempt_number}"
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

        # Build fresh order template for new attempt
        new_order = deepcopy(template_order or main.packing_order)
        new_order["packing_order_id"] = new_attempt_order_id
        new_order["box_id"] = prev_order.get("box_id", "BOX-001")
        new_order["verification_result"] = None
        new_order["verification_timestamp"] = None
        for part in new_order["part_requirements"]:
            part["scanned_quantity"] = 0
            part["packing_status"] = "Pending"

        # Execute atomic reset transaction
        with closing(database.get_connection(db_path)) as connection:
            with connection:
                # 1. Close previous attempt
                connection.execute("""
                    UPDATE mobile_attempts
                    SET status = 'CLOSED_NG', closed_at = ?
                    WHERE attempt_id = ?
                """, (now_iso, current_attempt["attempt_id"]))

                # 2. Insert new attempt record
                connection.execute("""
                    INSERT INTO mobile_attempts (base_order_id, attempt_order_id, attempt_number, status, created_at)
                    VALUES (?, ?, ?, 'ACTIVE', ?)
                """, (base_order_id, new_attempt_order_id, next_attempt_number, now_iso))

                # 3. Create fresh order and part requirements
                database._upsert_packing_order(connection, new_order)

                # 4. Record supervisor audit event
                connection.execute("""
                    INSERT INTO supervisor_reset_events (
                        base_order_id, previous_attempt_order_id, new_attempt_order_id,
                        supervisor_id, reason, timestamp
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    base_order_id,
                    prev_attempt_order_id,
                    new_attempt_order_id,
                    supervisor_id,
                    reason,
                    now_iso,
                ))

                # 5. Record verification event
                connection.execute("""
                    INSERT INTO verification_events (
                        packing_order_id, verification_result, event_timestamp, event_description
                    )
                    VALUES (?, 'HOLD', ?, ?)
                """, (
                    new_attempt_order_id,
                    now_iso,
                    f"Fresh packing attempt initiated by supervisor {supervisor_id}: {reason}",
                ))

        response_payload = {
            "message": "Supervisor reset authorized. Fresh packing attempt initialized.",
            "base_order_id": base_order_id,
            "previous_attempt_order_id": prev_attempt_order_id,
            "new_attempt_order_id": new_attempt_order_id,
            "attempt_number": next_attempt_number,
            "supervisor_id": supervisor_id,
            "timestamp": now_iso,
            "order": new_order,
        }
        return 200, response_payload


def _persist_scan_transaction(
    connection: sqlite3.Connection,
    order: dict[str, Any],
    part_id: str,
    variant: str,
    quantity: int,
    scan_result: str,
    scan_nonce: str,
    response_payload: dict[str, Any],
    timestamp: str,
) -> None:
    """
    Execute all write operations for a scan in one single SQLite transaction:
    1. Update packing order & part requirements.
    2. Insert scan event.
    3. Insert verification event.
    4. Insert scan nonce and cached response in scan_requests.
    """
    database._upsert_packing_order(connection, order)

    connection.execute("""
        INSERT INTO scan_events (
            packing_order_id, part_id, supplied_variant, quantity,
            scan_result, scan_timestamp
        )
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        order["packing_order_id"],
        part_id,
        variant,
        quantity,
        scan_result,
        timestamp,
    ))

    description = (
        f"Mobile scan attempt for {part_id} ({variant}), quantity {quantity}"
    )
    connection.execute("""
        INSERT INTO verification_events (
            packing_order_id, verification_result, event_timestamp,
            event_description
        )
        VALUES (?, ?, ?, ?)
    """, (
        order["packing_order_id"],
        scan_result,
        timestamp,
        description,
    ))

    connection.execute("""
        INSERT INTO scan_requests (
            scan_nonce,
            packing_order_id,
            part_id,
            variant,
            quantity,
            response_status,
            response_payload,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        scan_nonce,
        order["packing_order_id"],
        part_id,
        variant,
        quantity,
        200,
        json.dumps(response_payload),
        timestamp,
    ))

    # 5. Enqueue hardware command outbox entry if terminal (PASS or NG)
    # and no command was previously enqueued for this attempt.
    if scan_result in ("PASS", "NG"):
        existing_outbox = connection.execute("""
            SELECT 1 FROM hardware_command_outbox
            WHERE attempt_order_id = ?
        """, (order["packing_order_id"],)).fetchone()

        if existing_outbox is None:
            connection.execute("""
                INSERT INTO hardware_command_outbox (
                    attempt_order_id,
                    command,
                    status,
                    retry_count,
                    error_message,
                    created_at
                )
                VALUES (?, ?, 'PENDING', 0, NULL, ?)
            """, (
                order["packing_order_id"],
                scan_result,
                timestamp,
            ))


def process_mobile_scan(
    db_path: Union[str, Path],
    scan_nonce: str,
    raw_payload: Union[str, dict[str, Any]],
    template_order: Optional[dict[str, Any]] = None,
) -> tuple[int, dict[str, Any]]:
    """
    Atomically process a mobile scan with persistent idempotency against the active attempt.
    If the active attempt is locked in NG, rejects further operator scans with NG.
    """
    if not isinstance(scan_nonce, str) or not scan_nonce.strip():
        return 400, {"error": "Missing or invalid scan_nonce"}

    scan_nonce = scan_nonce.strip()

    with _scan_lock:
        initialize_mobile_database(db_path=db_path)

        # Check persistent idempotency table
        with closing(database.get_connection(db_path)) as connection:
            existing_row = connection.execute("""
                SELECT response_status, response_payload
                FROM scan_requests
                WHERE scan_nonce = ?
            """, (scan_nonce,)).fetchone()

            if existing_row is not None:
                cached_status = existing_row["response_status"]
                cached_payload = json.loads(existing_row["response_payload"])
                cached_payload["idempotent_replay"] = True
                return cached_status, cached_payload

        # Parse & validate payload
        try:
            part_id, variant, quantity = parse_scan_payload(raw_payload)
        except ValueError as err:
            err_payload = {"error": str(err), "scan_nonce": scan_nonce}
            return 400, err_payload

        # Load fresh order from isolated mobile DB (active attempt)
        order = initialize_mobile_order(db_path=db_path, template_order=template_order)

        # Check if attempt is closed/rejected
        if order.get("verification_result") == "NG":
            scan_result = "NG"
        else:
            # Execute domain verification in-memory
            scan_result = main.record_scan(order, part_id, variant, quantity)

        response_data = {
            "packing_order_id": order["packing_order_id"],
            "verification_result": scan_result,
            "verification_timestamp": order.get("verification_timestamp"),
            "part_requirements": order["part_requirements"],
            "scan_nonce": scan_nonce,
            "idempotent_replay": False,
        }

        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

        # Execute all database writes in ONE single atomic SQLite transaction
        with closing(database.get_connection(db_path)) as connection:
            with connection:
                _persist_scan_transaction(
                    connection=connection,
                    order=order,
                    part_id=part_id,
                    variant=variant,
                    quantity=quantity,
                    scan_result=scan_result,
                    scan_nonce=scan_nonce,
                    response_payload=response_data,
                    timestamp=timestamp,
                )

        # Notify active background worker if running
        active_worker = ArduinoOutboxWorker.get_active_worker()
        if active_worker is not None and active_worker.is_alive():
            active_worker.wake()

        return 200, response_data


# ==============================================================================
# PHASE 4A: DURABLE ARDUINO OUTBOX WORKER & CRASH RECOVERY
# ==============================================================================

def recover_stranded_outbox_commands(
    db_path: Union[str, Path] = DEFAULT_MOBILE_DB_PATH
) -> int:
    """
    Recover commands left stranded in 'PROCESSING' status across application restarts.
    Transitions them back to 'PENDING', clears claimed_at, and increments retry_count.

    PHYSICAL EXECUTION NOTICE (At-Least-Once Delivery):
    If the application process crashed after the microcontroller received the UART bytes
    and actuated hardware (e.g. lit LED or toggled relay), but before the acknowledgement
    was committed to SQLite, recovering and re-transmitting this command causes physical
    re-execution on the hardware. True exactly-once physical execution cannot be promised
    without microcontroller-side deduplication or transaction identifiers.

    Returns the number of recovered commands.
    """
    with closing(database.get_connection(db_path)) as connection:
        with connection:
            cursor = connection.execute("""
                UPDATE hardware_command_outbox
                SET status = 'PENDING',
                    retry_count = retry_count + 1,
                    claimed_at = NULL,
                    error_message = 'Stranded PROCESSING command recovered on restart'
                WHERE status = 'PROCESSING'
            """)
            return cursor.rowcount


def claim_next_outbox_command(
    db_path: Union[str, Path] = DEFAULT_MOBILE_DB_PATH
) -> Optional[dict[str, Any]]:
    """
    Atomically claim the oldest PENDING hardware command in strict FIFO order.
    Transitions its status to 'PROCESSING' and records claimed_at timestamp.
    Returns a dictionary of the claimed record, or None if no PENDING commands exist.

    The SQLite transaction commits immediately, ensuring no database lock is held
    while waiting for serial communication.
    """
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(database.get_connection(db_path)) as connection:
        with connection:
            row = connection.execute("""
                SELECT outbox_id, attempt_order_id, command, retry_count
                FROM hardware_command_outbox
                WHERE status = 'PENDING'
                ORDER BY outbox_id ASC
                LIMIT 1
            """).fetchone()

            if row is None:
                return None

            outbox_id = row["outbox_id"]
            connection.execute("""
                UPDATE hardware_command_outbox
                SET status = 'PROCESSING',
                    claimed_at = ?
                WHERE outbox_id = ? AND status = 'PENDING'
            """, (now_iso, outbox_id))

            return {
                "outbox_id": outbox_id,
                "attempt_order_id": row["attempt_order_id"],
                "command": row["command"],
                "retry_count": row["retry_count"],
            }


def process_claimed_command(
    claimed: dict[str, Any],
    db_path: Union[str, Path] = DEFAULT_MOBILE_DB_PATH,
    send_fn: Optional[Callable[[str], bool]] = None,
) -> bool:
    """
    Transmit the claimed command to hardware via send_fn (defaults to main.send_arduino_result),
    and record the result in hardware_command_outbox and packing_orders.

    AUTHORITATIVE RESULT INVARIANCE:
    If serial communication fails, packing_orders.arduino_communication_status is updated
    to 'UNCONFIRMED', but the authoritative packing outcome in packing_orders.verification_result
    is NEVER modified or demoted to HOLD.

    Returns True if communication was CONFIRMED, False if UNCONFIRMED.
    """
    if send_fn is None:
        send_fn = main.send_arduino_result

    outbox_id = claimed["outbox_id"]
    attempt_order_id = claimed["attempt_order_id"]
    command = claimed["command"]

    success = False
    error_message: Optional[str] = None
    try:
        success = bool(send_fn(command))
        if not success:
            error_message = "Hardware communication unconfirmed: timeout, disconnect, or unexpected ACK"
    except Exception as ex:
        success = False
        error_message = f"Hardware communication failed with exception: {ex}"

    hw_status = "CONFIRMED" if success else "UNCONFIRMED"
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with closing(database.get_connection(db_path)) as connection:
        with connection:
            connection.execute("""
                UPDATE hardware_command_outbox
                SET status = ?,
                    processed_at = ?,
                    error_message = ?
                WHERE outbox_id = ?
            """, (hw_status, now_iso, error_message, outbox_id))

            # Update packing_orders communication status only.
            # Allowed values by CHECK constraint: 'CONFIRMED', 'UNCONFIRMED', or NULL.
            # The domain verification result (PASS/NG) is strictly preserved.
            connection.execute("""
                UPDATE packing_orders
                SET arduino_communication_status = ?
                WHERE packing_order_id = ?
            """, (hw_status, attempt_order_id))

    return success


def process_single_outbox_command(
    db_path: Union[str, Path] = DEFAULT_MOBILE_DB_PATH,
    send_fn: Optional[Callable[[str], bool]] = None,
) -> Optional[bool]:
    """
    Claim and process exactly one PENDING outbox command in FIFO sequence.
    Returns True (CONFIRMED), False (UNCONFIRMED), or None if no command was pending.
    """
    claimed = claim_next_outbox_command(db_path=db_path)
    if claimed is None:
        return None
    return process_claimed_command(claimed, db_path=db_path, send_fn=send_fn)


class ArduinoOutboxWorker:
    """
    Background worker daemon that processes hardware_command_outbox in FIFO order.
    Ensures a single active worker instance controls the COM port within the application process.
    """
    _singleton_lock = threading.Lock()
    _active_worker: Optional["ArduinoOutboxWorker"] = None

    def __init__(
        self,
        db_path: Union[str, Path] = DEFAULT_MOBILE_DB_PATH,
        poll_interval: float = 0.5,
        send_fn: Optional[Callable[[str], bool]] = None,
    ):
        self.db_path = db_path
        self.poll_interval = poll_interval
        self.send_fn = send_fn or main.send_arduino_result
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @classmethod
    def get_active_worker(cls) -> Optional["ArduinoOutboxWorker"]:
        with cls._singleton_lock:
            return cls._active_worker

    def start(self) -> None:
        """
        Start the background worker thread.
        Raises RuntimeError if an active worker is already running in this process.
        """
        with ArduinoOutboxWorker._singleton_lock:
            if ArduinoOutboxWorker._active_worker is not None and ArduinoOutboxWorker._active_worker.is_alive():
                raise RuntimeError("An active ArduinoOutboxWorker is already running in this process.")

            # Perform startup crash recovery before dispatching new commands
            recover_stranded_outbox_commands(self.db_path)

            self._stop_event.clear()
            self._wake_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="ArduinoOutboxWorkerThread",
                daemon=True,
            )
            self._thread.start()
            ArduinoOutboxWorker._active_worker = self

    def stop(self, timeout: Optional[float] = 5.0) -> None:
        """Stop the background worker cleanly and clear the active singleton reference."""
        self._stop_event.set()
        self._wake_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        with ArduinoOutboxWorker._singleton_lock:
            if ArduinoOutboxWorker._active_worker is self:
                ArduinoOutboxWorker._active_worker = None

    def wake(self) -> None:
        """Signal the worker to immediately process pending commands without waiting for poll interval."""
        self._wake_event.set()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                # Drain all available pending commands in FIFO order
                while not self._stop_event.is_set():
                    res = process_single_outbox_command(
                        db_path=self.db_path,
                        send_fn=self.send_fn,
                    )
                    if res is None:
                        break

                # Sleep until next poll interval or until woken up by wake()
                self._wake_event.wait(timeout=self.poll_interval)
                self._wake_event.clear()
            except Exception:
                # Brief sleep on unexpected error to prevent tight spin loops
                if not self._stop_event.is_set():
                    time.sleep(self.poll_interval)


def start_global_worker(
    db_path: Union[str, Path] = DEFAULT_MOBILE_DB_PATH,
    poll_interval: float = 0.5,
    send_fn: Optional[Callable[[str], bool]] = None,
) -> ArduinoOutboxWorker:
    """Start and return the singleton background worker for the given database."""
    worker = ArduinoOutboxWorker(db_path=db_path, poll_interval=poll_interval, send_fn=send_fn)
    worker.start()
    return worker


def stop_global_worker(timeout: Optional[float] = 5.0) -> None:
    """Stop the singleton background worker if one is currently running."""
    active = ArduinoOutboxWorker.get_active_worker()
    if active is not None:
        active.stop(timeout=timeout)


# ==============================================================================
# PHASE 4B: HARDWARE STATUS QUERY AND SUPERVISOR HARDWARE RETRY
# ==============================================================================

#: Maximum number of supervisor-initiated hardware retries allowed per outbox command.
HARDWARE_RETRY_MAX = 3


def get_hardware_outbox_status(
    db_path: Union[str, Path] = DEFAULT_MOBILE_DB_PATH,
    attempt_order_id: str = "PO-001",
) -> Optional[dict[str, Any]]:
    """
    Return the current hardware outbox record for the given attempt order, or None.
    Fields returned: outbox_id, attempt_order_id, command, status, retry_count,
    error_message, created_at, claimed_at, processed_at.
    """
    with closing(database.get_connection(db_path)) as connection:
        row = connection.execute("""
            SELECT outbox_id, attempt_order_id, command, status, retry_count,
                   error_message, created_at, claimed_at, processed_at
            FROM hardware_command_outbox
            WHERE attempt_order_id = ?
            ORDER BY outbox_id DESC
            LIMIT 1
        """, (attempt_order_id,)).fetchone()
        return dict(row) if row is not None else None


def supervisor_hardware_retry(
    db_path: Union[str, Path],
    supervisor_pin: str,
    attempt_order_id: str,
    reason: str,
    supervisor_id: str = "SUPERVISOR-01",
) -> tuple[int, dict[str, Any]]:
    """
    Supervisor-authorized manual retry of a failed hardware command.

    Authorization uses the same pre-shared PIN as authorize_supervisor_reset().
    Requires a non-empty retry reason.

    Rules enforced:
    - Command must exist in the outbox and be in UNCONFIRMED status.
    - retry_count must be below HARDWARE_RETRY_MAX (default 3).
    - Does NOT modify verification_result, scanned quantities, or attempt history.
    - Resets the outbox row to PENDING so the background worker will re-send it.
    - Records an audit entry in supervisor_reset_events with supervisor_id,
      reason, attempt_order_id, timestamp, and previous status.
    - Wakes the background worker if one is running.

    Returns (HTTP status code, response dict).

    SERIAL DELIVERY NOTE:
    Resetting to PENDING causes at-least-once hardware delivery.  If the
    Arduino already executed the previous command before the ACK was lost,
    the hardware may actuate again on retry.  CONFIRMED means only that the
    expected serial acknowledgement string was received — it does not confirm
    physical LED or gate operation.
    """
    if not isinstance(supervisor_pin, str) or supervisor_pin != DEFAULT_SUPERVISOR_PIN:
        return 401, {"error": "Unauthorized: Invalid supervisor authorization PIN."}

    if not isinstance(reason, str) or not reason.strip():
        return 400, {"error": "Bad Request: A retry reason is required."}

    reason = reason.strip()

    if not isinstance(attempt_order_id, str) or not attempt_order_id.strip():
        return 400, {"error": "Bad Request: attempt_order_id is required."}

    attempt_order_id = attempt_order_id.strip()

    with closing(database.get_connection(db_path)) as connection:
        outbox_row = connection.execute("""
            SELECT outbox_id, command, status, retry_count
            FROM hardware_command_outbox
            WHERE attempt_order_id = ?
            ORDER BY outbox_id DESC
            LIMIT 1
        """, (attempt_order_id,)).fetchone()

    if outbox_row is None:
        return 404, {
            "error": f"No hardware command found for attempt order '{attempt_order_id}'."
        }

    outbox_id = outbox_row["outbox_id"]
    current_status = outbox_row["status"]
    retry_count = outbox_row["retry_count"]
    command = outbox_row["command"]

    if current_status != "UNCONFIRMED":
        return 409, {
            "error": (
                f"Hardware retry is only permitted for UNCONFIRMED commands. "
                f"Current status: {current_status}."
            )
        }

    if retry_count >= HARDWARE_RETRY_MAX:
        return 429, {
            "error": (
                f"Hardware retry limit reached ({retry_count}/{HARDWARE_RETRY_MAX}). "
                f"Manual investigation required."
            )
        }

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with closing(database.get_connection(db_path)) as connection:
        with connection:
            # Reset outbox row to PENDING for worker pick-up
            connection.execute("""
                UPDATE hardware_command_outbox
                SET status = 'PENDING',
                    retry_count = retry_count + 1,
                    claimed_at = NULL,
                    error_message = NULL
                WHERE outbox_id = ?
            """, (outbox_id,))

            # Audit trail: record in dedicated hardware_retry_events table
            connection.execute("""
                INSERT INTO hardware_retry_events (
                    attempt_order_id,
                    outbox_id,
                    command,
                    supervisor_id,
                    reason,
                    previous_status,
                    new_status,
                    retry_number,
                    timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                attempt_order_id,
                outbox_id,
                command,
                supervisor_id,
                reason,
                current_status,
                "PENDING",
                retry_count + 1,
                now_iso,
            ))

    # Wake the background worker without holding the DB transaction open
    active_worker = ArduinoOutboxWorker.get_active_worker()
    if active_worker is not None and active_worker.is_alive():
        active_worker.wake()

    return 200, {
        "message": "Hardware retry authorized. Command re-queued for dispatch.",
        "attempt_order_id": attempt_order_id,
        "command": command,
        "previous_status": current_status,
        "retry_count": retry_count + 1,
        "retry_max": HARDWARE_RETRY_MAX,
        "supervisor_id": supervisor_id,
        "timestamp": now_iso,
    }
