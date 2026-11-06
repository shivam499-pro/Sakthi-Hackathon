from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

import database
import serial
import time

ARDUINO_PORT = "COM5"
BAUD_RATE = 9600
ARDUINO_COMMANDS = {"PASS": b"PASS\n", "NG": b"NG\n", "HOLD": b"HOLD\n"}


# =================================
# PACKING ORDER DATA
# =================================

packing_order = {
    "packing_order_id": "PO-001",
    "box_id": "BOX-001",
    "order_id": "ORD-2026-001",
    "product_description": "Automotive Component Export Box",
    "verification_result": None,
    "verification_timestamp": None,
    "part_requirements": [
        {
            "part_id": "PART-A",
            "variant": "LH",
            "required_quantity": 2,
            "scanned_quantity": 0,
            "packing_status": "Pending",
            "unit_of_measurement": "PCS"
        },
        {
            "part_id": "PART-B",
            "variant": "RH",
            "required_quantity": 2,
            "scanned_quantity": 0,
            "packing_status": "Pending",
            "unit_of_measurement": "PCS"
        },
        {
            "part_id": "PART-C",
            "variant": "Standard",
            "required_quantity": 4,
            "scanned_quantity": 0,
            "packing_status": "Pending",
            "unit_of_measurement": "PCS"
        }
    ]
}


def validate_part_id(order: dict[str, Any], part_id: Any) -> bool:
    """Return whether part_id is listed in this packing order."""
    requirements = order.get("part_requirements")
    return (
        isinstance(requirements, list)
        and bool(requirements)
        and isinstance(part_id, str)
        and any(
            isinstance(part, dict) and part.get("part_id") == part_id
            for part in requirements
        )
    )


def validate_variant(
    order: dict[str, Any], part_id: Any, variant: Any
) -> bool:
    """Return whether the supplied variant matches the listed part."""
    requirements = order.get("part_requirements")
    if not isinstance(requirements, list) or not isinstance(variant, str):
        return False

    return any(
        isinstance(part, dict)
        and part.get("part_id") == part_id
        and part.get("variant") == variant
        for part in requirements
    )


def _set_verification_result(order: dict[str, Any], result: str) -> str:
    order["verification_result"] = result
    order["verification_timestamp"] = datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )
    return result


def _valid_order_structure(order: dict[str, Any]) -> bool:
    requirements = order.get("part_requirements")
    if not isinstance(requirements, list) or not requirements:
        return False

    seen_part_ids = set()
    for part in requirements:
        if not isinstance(part, dict):
            return False
        part_id = part.get("part_id")
        variant = part.get("variant")
        required = part.get("required_quantity")
        scanned = part.get("scanned_quantity")
        if (
            not isinstance(part_id, str)
            or not part_id
            or part_id in seen_part_ids
            or not isinstance(variant, str)
            or not variant
            or isinstance(required, bool)
            or not isinstance(required, int)
            or required <= 0
            or isinstance(scanned, bool)
            or not isinstance(scanned, int)
            or scanned < 0
        ):
            return False
        seen_part_ids.add(part_id)
    return True


def update_part_status(part: dict[str, Any]) -> str:
    """Update and return a part's status based on its scanned quantity."""
    scanned = part["scanned_quantity"]
    required = part["required_quantity"]
    if scanned > required:
        status = "NG"
    elif scanned == required:
        status = "Complete"
    elif scanned == 0:
        status = "Pending"
    else:
        status = "In Progress"
    part["packing_status"] = status
    return status


def track_quantity(part: dict[str, Any], quantity: int = 1) -> int:
    """Add a positive integer scan quantity and update the part status."""
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise ValueError("quantity must be a positive integer")
    part["scanned_quantity"] += quantity
    update_part_status(part)
    return part["scanned_quantity"]


def verify_packing_order(order: dict[str, Any]) -> str:
    """Compute HOLD, NG, or PASS from the current in-memory order data."""
    if order.get("verification_result") == "NG":
        return _set_verification_result(order, "NG")

    if not _valid_order_structure(order):
        return _set_verification_result(order, "HOLD")

    statuses = [update_part_status(part) for part in order["part_requirements"]]
    if "NG" in statuses:
        result = "NG"
    elif all(status == "Complete" for status in statuses):
        result = "PASS"
    else:
        result = "HOLD"
    return _set_verification_result(order, result)


def record_scan(
    order: dict[str, Any],
    part_id: Any,
    variant: Any,
    quantity: int = 1,
) -> str:
    """Validate a scan and update quantities; unique IDs are needed to identify duplicate items."""
    if order.get("verification_result") == "NG":
        return _set_verification_result(order, "NG")
    if not _valid_order_structure(order):
        return _set_verification_result(order, "HOLD")
    if (
        not isinstance(part_id, str)
        or not part_id
        or not isinstance(variant, str)
        or not variant
    ):
        return _set_verification_result(order, "HOLD")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        return _set_verification_result(order, "HOLD")
    if not validate_part_id(order, part_id):
        return _set_verification_result(order, "NG")
    if not validate_variant(order, part_id, variant):
        part = next(
            part for part in order["part_requirements"]
            if part["part_id"] == part_id
        )
        part["packing_status"] = "NG"
        return _set_verification_result(order, "NG")

    part = next(
        part for part in order["part_requirements"]
        if part["part_id"] == part_id
    )
    track_quantity(part, quantity)
    return verify_packing_order(order)


def send_arduino_result(result: str) -> bool:
    """Send only a computed verification decision to the Arduino."""
    
    command = ARDUINO_COMMANDS.get(result)
    
    if command is None:
        raise ValueError(f"Unsupported verification result: {result}")

    arduino = None
    
    try:
        arduino = serial.Serial(ARDUINO_PORT, BAUD_RATE, timeout=2)
        time.sleep(2)

        print("Arduino Integration: Connected")
        while arduino.in_waiting:
            message = arduino.readline().decode(
                "utf-8", errors="replace"
            ).strip()

            if message:
                print("Arduino:", message)

        print(f"\nSending command: {result}")
        
        arduino.write(command)
        arduino.flush()

        response = arduino.readline().decode(
            "utf-8", errors="replace"
        ).strip()

        print("Arduino Response:", response)

        if not response:
            print("ERROR: No response received from Arduino.")
            return False

        if result == "PASS":
            acknowledged = response == "PASS: Packing Approved"
        elif result in ("NG", "HOLD"):
            acknowledged = response == "HOLD: Packing Not Approved"
        else:
            acknowledged = False

        if not acknowledged:
            print("ERROR: Unexpected Arduino response.")
            return False

        return True
              
    except serial.SerialException as error:
        print("Arduino Integration: Connection Failed")
        print("Error:", error)
        return False

    finally:
        if arduino is not None and arduino.is_open:
            arduino.close()
            print("\nArduino connection closed.")


def display_packing_order(order: dict[str, Any]) -> None:
    print("\n========== PACKING ORDER ==========")
    print("Packing Order ID:", order["packing_order_id"])
    print("Box ID:", order["box_id"])
    print("Order ID:", order["order_id"])
    print("Product:", order["product_description"])
    print("\nRequired Parts:")
    for part in order["part_requirements"]:
        print(
            f"Part ID: {part['part_id']} | "
            f"Variant: {part['variant']} | "
            f"Required Quantity: {part['required_quantity']} | "
            f"Scanned Quantity: {part['scanned_quantity']} "
            f"{part['unit_of_measurement']} | "
            f"Packing Status: {part['packing_status']}"
        )
    print("===================================")

def show_recovery_guidance(order: dict[str, Any]) -> None:
    """Explain why packing is on HOLD or NG and what the operator should do."""

    result = order.get("verification_result")

    print("\n========== OPERATOR GUIDANCE ==========")

    if result == "HOLD":
        print("STATUS: PACKING ON HOLD")
        print("Action: Review incomplete or uncertain packing requirements.")

        for part in order["part_requirements"]:
            if part["packing_status"] != "Complete":
                missing = max(
                    0,
                    part["required_quantity"] - part["scanned_quantity"]
                )

                print(
                    f"- {part['part_id']} | "
                    f"Variant: {part['variant']} | "
                    f"Scanned: {part['scanned_quantity']} | "
                    f"Required: {part['required_quantity']} | "
                    f"Missing: {missing}"
                )

    elif result == "NG":
        print("STATUS: PACKING REJECTED")
        print("Action: Stop packing and request supervisor review.")
        print("A confirmed NG cannot be cleared by the operator.")

        for part in order["part_requirements"]:
            if part["packing_status"] == "NG":
                print(
                    f"- Problem part: {part['part_id']} | "
                    f"Variant: {part['variant']} | "
                    f"Scanned: {part['scanned_quantity']} | "
                    f"Required: {part['required_quantity']}"
                )

    elif result == "PASS":
        print("STATUS: PACKING APPROVED")
        print("All packing requirements have been satisfied.")

    print("=======================================")


def operator_recovery_interface(
    order: dict[str, Any],
    db_path: Optional[Union[str, Path]] = None,
) -> str:
    """Allow an operator to correct incomplete packing through new scans."""

    while True:
        result = verify_packing_order(order)

        show_recovery_guidance(order)

        if result in ("PASS", "NG"):
            return result

        print("\nRECOVERY OPTIONS")
        print("1. Enter another part scan")
        print("2. Stop and keep packing on HOLD")

        choice = input("Select an option (1/2): ").strip()

        if choice == "2":
            print("Packing remains on HOLD.")
            return "HOLD"

        if choice != "1":
            print("Invalid option. Please select 1 or 2.")
            continue

        part_id = input("Enter Part ID: ").strip()
        variant = input("Enter Variant (LH/RH/Standard): ").strip()

        try:
            quantity = int(input("Enter scanned quantity: ").strip())
        except ValueError:
            print("Invalid quantity. Packing remains on HOLD.")
            continue

        result = record_scan(order, part_id, variant, quantity)

        if db_path is not None:
            database.record_scan_event(
                order,
                part_id,
                variant,
                quantity,
                result,
                db_path,
            )

        print("\nUpdated verification result:", result)

        if result == "NG":
            print("Packing rejected. Supervisor review required.")
            return "NG"
def main(db_path: Union[str, Path] = database.DB_PATH) -> None:
    print("\n================================")
    print(" SMART PACKING VERIFICATION ")
    print("================================")
    print("Project: PS-016")
    print("System Status: Initializing...")

    database.initialize_database(db_path)
    order = database.load_packing_order(packing_order["packing_order_id"], db_path)
    if order is None:
        order = deepcopy(packing_order)
        database.save_packing_order(order, db_path)

    display_packing_order(order)

    result = operator_recovery_interface(order, db_path)

    print("\nFinal Verification Result:", result)
    print("Verification Timestamp:", order["verification_timestamp"])

    communication_success = send_arduino_result(result)

    if not communication_success:
        _set_verification_result(order, "HOLD")

        print("\nSYSTEM STATUS: HOLD")
        print("Arduino communication could not be confirmed.")
        print("Physical indicator state is unconfirmed.")

    else:
        print("\nSYSTEM STATUS:", result)
        print("Arduino communication confirmed.")

    database.record_verification_event(
        order,
        "Final verification after Arduino communication",
        "CONFIRMED" if communication_success else "UNCONFIRMED",
        db_path,
    )

    print("================================")


if __name__ == "__main__":
    main()