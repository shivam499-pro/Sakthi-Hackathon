import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from database import get_connection, initialize_database


class TestDatabase(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_packing.db"
        initialize_database(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def insert_order(self, connection):
        connection.execute("""
            INSERT INTO packing_orders (
                packing_order_id,
                box_id,
                order_id,
                product_description
            )
            VALUES (?, ?, ?, ?)
        """, (
            "PO-TEST-001",
            "BOX-TEST-001",
            "ORD-TEST-001",
            "Test Product"
        ))

    def test_all_four_tables_exist(self):
        with closing(get_connection(self.db_path)) as connection, connection:
            tables = connection.execute("""
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                AND name NOT LIKE 'sqlite_%'
            """).fetchall()

        table_names = {row["name"] for row in tables}

        expected_tables = {
            "packing_orders",
            "part_requirements",
            "verification_events",
            "scan_events"
        }

        self.assertEqual(table_names, expected_tables)

    def test_foreign_key_rejects_nonexistent_order(self):
        with closing(get_connection(self.db_path)) as connection, connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("""
                    INSERT INTO part_requirements (
                        packing_order_id,
                        part_id,
                        variant,
                        required_quantity,
                        unit_of_measurement
                    )
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    "NONEXISTENT-ORDER",
                    "PART-A",
                    "LH",
                    2,
                    "PCS"
                ))

    def test_duplicate_part_id_in_same_order_is_rejected(self):
        with closing(get_connection(self.db_path)) as connection, connection:
            self.insert_order(connection)

            part_data = (
                "PO-TEST-001",
                "PART-A",
                "LH",
                2,
                "PCS"
            )

            connection.execute("""
                INSERT INTO part_requirements (
                    packing_order_id,
                    part_id,
                    variant,
                    required_quantity,
                    unit_of_measurement
                )
                VALUES (?, ?, ?, ?, ?)
            """, part_data)

            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("""
                    INSERT INTO part_requirements (
                        packing_order_id,
                        part_id,
                        variant,
                        required_quantity,
                        unit_of_measurement
                    )
                    VALUES (?, ?, ?, ?, ?)
                """, part_data)

    def test_invalid_verification_result_is_rejected(self):
        with closing(get_connection(self.db_path)) as connection, connection:
            self.insert_order(connection)

            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("""
                    UPDATE packing_orders
                    SET verification_result = ?
                    WHERE packing_order_id = ?
                """, ("INVALID", "PO-TEST-001"))

    def test_connection_closes_correctly(self):
        connection = get_connection(self.db_path)
        connection.close()

        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()