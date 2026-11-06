
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from contextlib import closing
from typing import Any, Optional, Union

# Store the database alongside this Python file.
DB_PATH = Path(__file__).with_name("packing_verification.db")


def get_connection(
    db_path: Union[str, Path] = DB_PATH
) -> sqlite3.Connection:
    """Create and return a SQLite database connection."""

    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")

    return connection


def initialize_database(
    db_path: Union[str, Path] = DB_PATH
) -> None:
    """Create all database tables if they do not already exist."""

    with closing(get_connection(db_path)) as connection, connection:

        connection.execute("""
            CREATE TABLE IF NOT EXISTS packing_orders (
                packing_order_id TEXT PRIMARY KEY,
                box_id TEXT NOT NULL,
                order_id TEXT NOT NULL,
                product_description TEXT NOT NULL,
                verification_result TEXT
                    CHECK (
                        verification_result IS NULL
                        OR verification_result IN ('PASS', 'NG', 'HOLD')
                    ),
                verification_timestamp TEXT,
                arduino_communication_status TEXT
                    CHECK (
                        arduino_communication_status IS NULL
                        OR arduino_communication_status IN (
                            'CONFIRMED', 'UNCONFIRMED'
                        )
                    )
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS part_requirements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                packing_order_id TEXT NOT NULL,
                part_id TEXT NOT NULL,
                variant TEXT NOT NULL,
                required_quantity INTEGER NOT NULL
                    CHECK (required_quantity > 0),
                scanned_quantity INTEGER NOT NULL DEFAULT 0
                    CHECK (scanned_quantity >= 0),
                packing_status TEXT NOT NULL DEFAULT 'Pending'
                    CHECK (
                        packing_status IN (
                            'Pending', 'In Progress',
                            'Complete', 'NG'
                        )
                    ),
                unit_of_measurement TEXT NOT NULL,

                UNIQUE (packing_order_id, part_id),

                FOREIGN KEY (packing_order_id)
                    REFERENCES packing_orders(packing_order_id)
                    ON DELETE CASCADE
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS verification_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                packing_order_id TEXT NOT NULL,
                verification_result TEXT NOT NULL
                    CHECK (
                        verification_result IN ('PASS', 'NG', 'HOLD')
                    ),
                event_timestamp TEXT NOT NULL,
                event_description TEXT,

                FOREIGN KEY (packing_order_id)
                    REFERENCES packing_orders(packing_order_id)
                    ON DELETE CASCADE
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS scan_events (
                scan_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                packing_order_id TEXT NOT NULL,
                part_id TEXT,
                supplied_variant TEXT,
                quantity INTEGER NOT NULL,
                scan_result TEXT NOT NULL
                    CHECK (scan_result IN ('PASS', 'NG', 'HOLD')),
                scan_timestamp TEXT NOT NULL,

                FOREIGN KEY (packing_order_id)
                    REFERENCES packing_orders(packing_order_id)
                    ON DELETE CASCADE
            )
        """)

        connection.commit()

    print("Database initialized successfully.")
    print("Database location:", db_path)


def _upsert_packing_order(
    connection: sqlite3.Connection,
    order: dict[str, Any],
    arduino_communication_status: Optional[str] = None,
) -> None:
    connection.execute("""
        INSERT INTO packing_orders (
            packing_order_id,
            box_id,
            order_id,
            product_description,
            verification_result,
            verification_timestamp,
            arduino_communication_status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(packing_order_id) DO UPDATE SET
            box_id = excluded.box_id,
            order_id = excluded.order_id,
            product_description = excluded.product_description,
            verification_result = excluded.verification_result,
            verification_timestamp = excluded.verification_timestamp,
            arduino_communication_status = COALESCE(
                excluded.arduino_communication_status,
                packing_orders.arduino_communication_status
            )
    """, (
        order["packing_order_id"],
        order["box_id"],
        order["order_id"],
        order["product_description"],
        order.get("verification_result"),
        order.get("verification_timestamp"),
        arduino_communication_status,
    ))

    for part in order["part_requirements"]:
        connection.execute("""
            INSERT INTO part_requirements (
                packing_order_id,
                part_id,
                variant,
                required_quantity,
                scanned_quantity,
                packing_status,
                unit_of_measurement
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(packing_order_id, part_id) DO UPDATE SET
                variant = excluded.variant,
                required_quantity = excluded.required_quantity,
                scanned_quantity = excluded.scanned_quantity,
                packing_status = excluded.packing_status,
                unit_of_measurement = excluded.unit_of_measurement
        """, (
            order["packing_order_id"],
            part["part_id"],
            part["variant"],
            part["required_quantity"],
            part["scanned_quantity"],
            part["packing_status"],
            part["unit_of_measurement"],
        ))


def save_packing_order(
    order: dict[str, Any],
    db_path: Union[str, Path] = DB_PATH,
) -> None:
    """Insert or update an order and its parts without duplicating them."""
    with closing(get_connection(db_path)) as connection, connection:
        _upsert_packing_order(connection, order)


def load_packing_order(
    packing_order_id: str,
    db_path: Union[str, Path] = DB_PATH,
) -> Optional[dict[str, Any]]:
    """Load a persisted order in the application's existing dictionary shape."""
    with closing(get_connection(db_path)) as connection:
        order_row = connection.execute("""
            SELECT packing_order_id, box_id, order_id, product_description,
                   verification_result, verification_timestamp
            FROM packing_orders
            WHERE packing_order_id = ?
        """, (packing_order_id,)).fetchone()
        if order_row is None:
            return None

        part_rows = connection.execute("""
            SELECT part_id, variant, required_quantity, scanned_quantity,
                   packing_status, unit_of_measurement
            FROM part_requirements
            WHERE packing_order_id = ?
            ORDER BY id
        """, (packing_order_id,)).fetchall()

    return {
        "packing_order_id": order_row["packing_order_id"],
        "box_id": order_row["box_id"],
        "order_id": order_row["order_id"],
        "product_description": order_row["product_description"],
        "verification_result": order_row["verification_result"],
        "verification_timestamp": order_row["verification_timestamp"],
        "part_requirements": [
            {
                "part_id": part["part_id"],
                "variant": part["variant"],
                "required_quantity": part["required_quantity"],
                "scanned_quantity": part["scanned_quantity"],
                "packing_status": part["packing_status"],
                "unit_of_measurement": part["unit_of_measurement"],
            }
            for part in part_rows
        ],
    }


def record_scan_event(
    order: dict[str, Any],
    part_id: Optional[str],
    supplied_variant: Optional[str],
    quantity: int,
    scan_result: str,
    db_path: Union[str, Path] = DB_PATH,
) -> None:
    """Atomically save current order state and a scan plus decision history."""
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    description = (
        f"Scan attempt for {part_id or 'unknown part'} "
        f"({supplied_variant or 'unknown variant'}), quantity {quantity}"
    )
    with closing(get_connection(db_path)) as connection, connection:
        _upsert_packing_order(connection, order)
        connection.execute("""
            INSERT INTO scan_events (
                packing_order_id, part_id, supplied_variant, quantity,
                scan_result, scan_timestamp
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            order["packing_order_id"],
            part_id,
            supplied_variant,
            quantity,
            scan_result,
            timestamp,
        ))
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


def record_verification_event(
    order: dict[str, Any],
    event_description: str,
    arduino_communication_status: str,
    db_path: Union[str, Path] = DB_PATH,
) -> None:
    """Save current order state and append a final verification event."""
    timestamp = order.get("verification_timestamp") or datetime.now(
        timezone.utc
    ).isoformat(timespec="seconds")
    with closing(get_connection(db_path)) as connection, connection:
        _upsert_packing_order(
            connection,
            order,
            arduino_communication_status=arduino_communication_status,
        )
        connection.execute("""
            INSERT INTO verification_events (
                packing_order_id, verification_result, event_timestamp,
                event_description
            )
            VALUES (?, ?, ?, ?)
        """, (
            order["packing_order_id"],
            order["verification_result"],
            timestamp,
            event_description,
        ))


if __name__ == "__main__":
    initialize_database()
