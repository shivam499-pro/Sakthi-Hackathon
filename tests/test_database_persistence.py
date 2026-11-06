import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch

import database
import main
from main import operator_recovery_interface, packing_order


class TestDatabasePersistence(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "persistence_test.db"
        database.initialize_database(self.db_path)
        self.order = copy.deepcopy(packing_order)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_saves_and_loads_order_and_part_quantities(self):
        part = self.order["part_requirements"][0]
        part["scanned_quantity"] = 1
        part["packing_status"] = "In Progress"
        self.order["verification_result"] = "HOLD"
        self.order["verification_timestamp"] = "2026-11-04T10:00:00+00:00"

        database.save_packing_order(self.order, self.db_path)
        loaded = database.load_packing_order(
            self.order["packing_order_id"], self.db_path
        )

        self.assertEqual(loaded, self.order)

    def test_updating_order_does_not_duplicate_order_or_parts(self):
        database.save_packing_order(self.order, self.db_path)
        self.order["part_requirements"][0]["scanned_quantity"] = 1
        self.order["part_requirements"][0]["packing_status"] = "In Progress"
        database.save_packing_order(self.order, self.db_path)

        with closing(database.get_connection(self.db_path)) as connection:
            order_count = connection.execute(
                "SELECT COUNT(*) FROM packing_orders"
            ).fetchone()[0]
            part_count = connection.execute(
                "SELECT COUNT(*) FROM part_requirements"
            ).fetchone()[0]

        self.assertEqual(order_count, 1)
        self.assertEqual(part_count, 3)
        self.assertEqual(
            database.load_packing_order(
                self.order["packing_order_id"], self.db_path
            )["part_requirements"][0]["scanned_quantity"],
            1,
        )

    def test_scan_event_and_current_order_state_are_persisted(self):
        part = self.order["part_requirements"][0]
        part["scanned_quantity"] = 1
        part["packing_status"] = "In Progress"
        self.order["verification_result"] = "HOLD"
        self.order["verification_timestamp"] = "2026-11-04T10:00:00+00:00"

        database.record_scan_event(
            self.order, "PART-A", "LH", 1, "HOLD", self.db_path
        )

        loaded = database.load_packing_order(
            self.order["packing_order_id"], self.db_path
        )
        with closing(database.get_connection(self.db_path)) as connection:
            scan = connection.execute(
                "SELECT part_id, supplied_variant, quantity, scan_result "
                "FROM scan_events"
            ).fetchone()
            verification_count = connection.execute(
                "SELECT COUNT(*) FROM verification_events"
            ).fetchone()[0]

        self.assertEqual(loaded["part_requirements"][0]["scanned_quantity"], 1)
        self.assertEqual(
            tuple(scan), ("PART-A", "LH", 1, "HOLD")
        )
        self.assertEqual(verification_count, 1)

    def test_wrong_part_scan_is_persisted_as_ng(self):
        self.order["verification_result"] = "NG"
        self.order["verification_timestamp"] = "2026-11-04T10:00:00+00:00"

        database.record_scan_event(
            self.order, "PART-UNKNOWN", "LH", 1, "NG", self.db_path
        )

        with closing(database.get_connection(self.db_path)) as connection:
            scan = connection.execute(
                "SELECT part_id, scan_result FROM scan_events"
            ).fetchone()
        self.assertEqual(tuple(scan), ("PART-UNKNOWN", "NG"))

    def test_final_verification_and_arduino_status_are_persisted(self):
        self.order["verification_result"] = "PASS"
        self.order["verification_timestamp"] = "2026-11-04T10:00:00+00:00"

        database.record_verification_event(
            self.order, "Final result", "CONFIRMED", self.db_path
        )

        with closing(database.get_connection(self.db_path)) as connection:
            order_row = connection.execute(
                "SELECT verification_result, arduino_communication_status "
                "FROM packing_orders WHERE packing_order_id = ?",
                (self.order["packing_order_id"],),
            ).fetchone()
            event = connection.execute(
                "SELECT verification_result, event_description "
                "FROM verification_events"
            ).fetchone()

        self.assertEqual(tuple(order_row), ("PASS", "CONFIRMED"))
        self.assertEqual(tuple(event), ("PASS", "Final result"))

    def test_failed_scan_write_rolls_back_order_and_event(self):
        self.order["verification_result"] = "PASS"
        with self.assertRaises(sqlite3.IntegrityError):
            database.record_scan_event(
                self.order, "PART-A", "LH", 1, "INVALID", self.db_path
            )

        with closing(database.get_connection(self.db_path)) as connection:
            order_count = connection.execute(
                "SELECT COUNT(*) FROM packing_orders"
            ).fetchone()[0]
            scan_count = connection.execute(
                "SELECT COUNT(*) FROM scan_events"
            ).fetchone()[0]
        self.assertEqual(order_count, 0)
        self.assertEqual(scan_count, 0)

    @patch("builtins.input", side_effect=["1", "PART-A", "LH", "1", "2"])
    def test_recovery_flow_persists_scan_and_hold(self, mock_input):
        database.save_packing_order(self.order, self.db_path)

        result = operator_recovery_interface(self.order, self.db_path)

        self.assertEqual(result, "HOLD")
        loaded = database.load_packing_order(
            self.order["packing_order_id"], self.db_path
        )
        self.assertEqual(loaded["part_requirements"][0]["scanned_quantity"], 1)
        with closing(database.get_connection(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM scan_events"
                ).fetchone()[0],
                1,
            )

    @patch("main.send_arduino_result", return_value=True)
    @patch("builtins.input", side_effect=["2"])
    def test_application_persists_final_result_and_arduino_status(
        self, mock_input, mock_arduino
    ):
        main.main(self.db_path)

        loaded = database.load_packing_order(
            self.order["packing_order_id"], self.db_path
        )
        self.assertEqual(loaded["verification_result"], "HOLD")
        with closing(database.get_connection(self.db_path)) as connection:
            order_row = connection.execute(
                "SELECT arduino_communication_status FROM packing_orders "
                "WHERE packing_order_id = ?",
                (self.order["packing_order_id"],),
            ).fetchone()
            event = connection.execute(
                "SELECT verification_result, event_description "
                "FROM verification_events ORDER BY event_id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(order_row["arduino_communication_status"], "CONFIRMED")
        self.assertEqual(
            tuple(event),
            ("HOLD", "Final verification after Arduino communication"),
        )

    @patch("main.send_arduino_result", return_value=False)
    @patch("builtins.input", side_effect=["2"])
    def test_application_persists_unconfirmed_arduino_status(
        self, mock_input, mock_arduino
    ):
        main.main(self.db_path)

        loaded = database.load_packing_order(
            self.order["packing_order_id"], self.db_path
        )
        self.assertEqual(loaded["verification_result"], "HOLD")
        with closing(database.get_connection(self.db_path)) as connection:
            row = connection.execute(
                "SELECT arduino_communication_status FROM packing_orders "
                "WHERE packing_order_id = ?",
                (self.order["packing_order_id"],),
            ).fetchone()
        self.assertEqual(row["arduino_communication_status"], "UNCONFIRMED")


if __name__ == "__main__":
    unittest.main()
