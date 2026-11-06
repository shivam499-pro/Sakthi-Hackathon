import copy
import unittest


from unittest.mock import patch
from main import operator_recovery_interface
from main import packing_order, record_scan, verify_packing_order
from unittest.mock import Mock, patch
import main

def make_order():
    return copy.deepcopy(packing_order)


class PackingVerificationTests(unittest.TestCase):
    def scan_correct_order(self, order):
        for part in order["part_requirements"]:
            record_scan(
                order,
                part["part_id"],
                part["variant"],
                part["required_quantity"],
            )

    def test_correct_packing_passes(self):
        order = make_order()
        self.scan_correct_order(order)
        self.assertEqual(order["verification_result"], "PASS")
        self.assertIsNotNone(order["verification_timestamp"])
        self.assertTrue(
            all(
                part["packing_status"] == "Complete"
                for part in order["part_requirements"]
            )
        )

    def test_missing_quantity_holds(self):
        order = make_order()
        part = order["part_requirements"][0]
        record_scan(order, part["part_id"], part["variant"], 1)
        self.assertEqual(verify_packing_order(order), "HOLD")
        self.assertEqual(part["packing_status"], "In Progress")

    def test_wrong_part_id_is_ng(self):
        order = make_order()
        self.assertEqual(record_scan(order, "PART-UNKNOWN", "LH"), "NG")

    def test_wrong_variant_is_ng(self):
        order = make_order()
        self.assertEqual(record_scan(order, "PART-A", "RH"), "NG")
        self.assertEqual(order["part_requirements"][0]["packing_status"], "NG")

    def test_repeated_scan_beyond_requirement_is_ng(self):
        order = make_order()
        part = order["part_requirements"][0]
        record_scan(order, part["part_id"], part["variant"], 2)
        self.assertEqual(
            record_scan(order, part["part_id"], part["variant"]), "NG"
        )
        self.assertEqual(part["scanned_quantity"], 3)

    def test_confirmed_ng_cannot_be_downgraded(self):
        order = make_order()
        self.assertEqual(record_scan(order, "PART-UNKNOWN", "LH"), "NG")
        self.assertEqual(record_scan(order, "", "LH"), "NG")

    def test_excess_quantity_is_ng(self):
        order = make_order()
        part = order["part_requirements"][0]
        self.assertEqual(
            record_scan(order, part["part_id"], part["variant"], 3), "NG"
        )
        self.assertEqual(part["packing_status"], "NG")

    def test_empty_order_holds(self):
        order = make_order()
        order["part_requirements"] = []
        self.assertEqual(verify_packing_order(order), "HOLD")
        self.assertIsNotNone(order["verification_timestamp"])

    def test_uncertain_scan_holds_without_changing_quantity(self):
        order = make_order()
        part = order["part_requirements"][0]
        self.assertEqual(record_scan(order, "", part["variant"]), "HOLD")
        self.assertEqual(part["scanned_quantity"], 0)

    @patch("main.time.sleep")
    @patch("main.serial.Serial")
    def test_arduino_connection_failure_returns_false(
        self, mock_serial, mock_sleep
    ):
        mock_serial.side_effect = main.serial.SerialException(
            "Arduino not connected"
        )

        result = main.send_arduino_result("PASS")

        self.assertFalse(result)

    @patch("main.time.sleep")
    @patch("main.serial.Serial")
    def test_missing_arduino_response_returns_false(
        self, mock_serial, mock_sleep
    ):
        arduino = Mock()
        arduino.in_waiting = 0
        arduino.readline.return_value = b""
        arduino.is_open = True

        mock_serial.return_value = arduino

        result = main.send_arduino_result("PASS")

        self.assertFalse(result)
        arduino.close.assert_called_once()

    @patch("main.time.sleep")
    @patch("main.serial.Serial")
    def test_unexpected_arduino_response_returns_false(
        self, mock_serial, mock_sleep
    ):
        arduino = Mock()
        arduino.in_waiting = 0
        arduino.readline.return_value = b"UNKNOWN RESPONSE\n"
        arduino.is_open = True

        mock_serial.return_value = arduino

        result = main.send_arduino_result("PASS")

        self.assertFalse(result)
        arduino.close.assert_called_once()

    @patch("builtins.input", side_effect=[
        "1", "PART-A", "LH", "2",
        "2"
    ])
    def test_recovery_scan_keeps_order_on_hold_if_parts_are_missing(
        self, mock_input
    ):
        order = make_order()

        result = operator_recovery_interface(order)

        self.assertEqual(result, "HOLD")
        self.assertEqual(order["part_requirements"][0]["packing_status"], "Complete")
        self.assertEqual(order["verification_result"], "HOLD")

    @patch("builtins.input", side_effect=[
        "1", "PART-A", "LH", "2",
        "1", "PART-B", "RH", "2",
        "1", "PART-C", "Standard", "4"
    ])
    def test_recovery_scan_completes_order(
        self, mock_input
    ):
        order = make_order()

        result = operator_recovery_interface(order)

        self.assertEqual(result, "PASS")
        self.assertEqual(order["verification_result"], "PASS")

    @patch("builtins.input", side_effect=[
        "1", "PART-A", "RH", "2"
    ])
    def test_wrong_variant_during_recovery_returns_ng(
        self, mock_input
    ):
        order = make_order()

        result = operator_recovery_interface(order)

        self.assertEqual(result, "NG")
        self.assertEqual(order["verification_result"], "NG")

if __name__ == "__main__":
    unittest.main()
