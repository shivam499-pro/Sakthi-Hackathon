# Smart Export Packing Verification System

> **Sakthi Hackathon 2.0 — Problem Statement 16 (PS-016)**
> An automated, multi-tiered verification prototype for automotive export packaging, combining domain Bill of Materials (BOM) validation, request-level idempotency, quarantine attempt isolation, durable asynchronous hardware signaling, and an auditable event log.

---

## 1. Problem Statement and Industrial Context

In automotive component manufacturing and international export packaging, container packing errors present significant operational and financial challenges:
- **Variant Mismatches:** Accidentally packing Left-Hand (`LH`) components instead of Right-Hand (`RH`) components into export crates causes assembly line stoppages at destination plants.
- **Quantity Discrepancies:** Under-packing leads to incomplete assemblies abroad; over-packing results in physical damage, inventory inaccuracies, and transport weight violations.
- **Human Error under Paper-Based Processes:** Traditional manual checklists and passive scanning workflows fail to prevent operators from accidentally scanning duplicate items, ignoring error indicators, or packing unverified parts.

**Sakthi Hackathon Problem Statement 16** calls for an automated Poka-Yoke (error-proofing) verification system to intercept packing errors at the packing station before containers are sealed and shipped.

---

## 2. Proposed Solution and System Objectives

This repository implements a working software prototype designed to enforce strict packaging compliance through layered validation:
1. **Deterministic BOM Verification:** Validate incoming component IDs, variants, and quantities against order requirements.
2. **Immediate Error Interception (NG Lock):** Freeze the packing order upon any mismatch (wrong part, wrong variant, excess quantity) to prevent subsequent scans from proceeding.
3. **Application-Level Attempt Isolation:** Lock rejected attempts (`CLOSED_NG`) and require authorized supervisor intervention to initialize a clean attempt (`PO-001-ATTEMPT-2`), retaining historical scan events for root-cause analysis through non-destructive application logic.
4. **Persistent Request Idempotency:** Deduplicate barcode submissions using client-generated request nonces stored in SQLite. This prevents duplicate increments from network replays, connection retries, or repeated button clicks using the same nonce. *(Note: It does not detect two separate physical scans of the same item if submitted with different nonces; item-level duplicate detection requires unique item serialization).*
5. **Durable Microcontroller Signaling:** Decouple domain decisions from physical serial communication using an outbox pattern (`hardware_command_outbox`) and background worker (`ArduinoOutboxWorker`). Outbox records persist across application restarts, and stranded processing commands are recovered for redelivery attempts.
6. **Auditable Event Logging:** Capture records of individual scans, verification state transitions, supervisor resets, and hardware retry attempts through application-level event logging and non-destructive attempt history retention.

---

## 3. Key Implemented Features

### Domain Logic & Verification Engine
- **Part & Variant Enforcement:** Strictly matches `part_id` and `variant` (e.g. `PART-A` with `LH`, `PART-B` with `RH`, `PART-C` with `Standard`).
- **Dynamic Fulfillment Tracking:** Computes real-time progress (`Pending`, `In Progress`, `Complete`, or `NG`) per component line.
- **Three-State Decision Machine:**
  - `HOLD`: Incomplete order; awaiting required components without defects.
  - `NG`: Defect detected (unrecognized part, incorrect variant, or over-packing).
  - `PASS`: 100% BOM fulfillment with zero errors.

### Web & Mobile Scanner Interface
- **Mobile-First Responsive Dashboard:** Optimized for handheld devices, tablets, and desktop workstations.
- **Camera Viewfinder & Barcode Scanner:** Integrated with the `html5-qrcode` library (loaded via CDN from `unpkg.com`) for camera-based scanning in secure browser contexts. *(Note: Integrated in frontend code, but live physical-phone camera scanning in warehouse environments has not been verified with captured evidence; manual/wedge entry was used for Phase 3.1 test evidence).*
- **Manual / Wedge Input Pane:** Fallback barcode entry form supporting pipe-delimited format (`PART_ID|VARIANT|QUANTITY`) and manual selection dropdowns.
- **Staged Confirmation Gate:** Decoded barcodes are held in a staging card for operator review before network submission, minimizing accidental trigger errors.

### Persistence & Concurrency
- **SQLite Relational Storage:** Structured schema enforcing foreign keys, check constraints, and unique part requirements per order.
- **Persistent Nonce Idempotency:** `scan_requests` table caches response payloads by `scan_nonce`, ensuring replay of the same request nonce does not mutate state. Does not prevent multiple scans of the same physical item when distinct nonces are generated.
- **Non-Destructive Quarantine Lifecycle:** Supervisor-authorized resets close the failed attempt and initialize a clean attempt; historical scan records remain intact through application-level attempt isolation.

### Durable Hardware Outbox & Worker
- **Transactional Outbox:** Terminal decisions (`PASS`, `NG`) atomically enqueue into `hardware_command_outbox` within the scan database transaction. `HOLD` decisions are never enqueued.
- **Autonomous Background Worker:** `ArduinoOutboxWorker` daemon thread claims pending outbox commands in strict FIFO order (by ascending `outbox_id`), serializing communication across USB UART.
- **Startup Crash Recovery & Redelivery:** Automatically transitions stranded `PROCESSING` commands back to `PENDING` upon application restart, re-attempting dispatch. (Provides at-least-once dispatch attempts; does not guarantee that physical execution occurs exactly once).
- **Authoritative Result Invariance:** If serial communication times out or disconnects, the hardware status is recorded as `UNCONFIRMED`, but the authoritative packaging result (`PASS` or `NG`) is never modified or demoted.
- **Supervisor Hardware Retry:** Bounded manual retry mechanism (up to 3 retries) with dedicated audit tracking in `hardware_retry_events`.

---

## 4. System Architecture

The following diagram illustrates the data flow, database boundaries, and serial interfaces implemented across the system:

```mermaid
flowchart TD
    subgraph ClientLayer ["Client Layer (Mobile Browser / Workstation)"]
        Scanner["Camera Barcode Scanner (html5-qrcode via CDN)"]
        WedgeInput["Manual / Wedge Barcode Input"]
        StagedGate["Staged Confirmation Gate (Review & Client Nonce)"]
        Scanner --> StagedGate
        WedgeInput --> StagedGate
    end

    subgraph APILayer ["Flask Application Layer (mobile_scanner.py)"]
        ScanEndpoint["POST /api/scan"]
        OrderEndpoint["GET /api/order"]
        HistoryEndpoint["GET /api/history"]
        ResetEndpoint["POST /api/supervisor/reset"]
        RetryEndpoint["POST /api/hardware/retry"]
        StagedGate -->|HTTP JSON Payload + Nonce| ScanEndpoint
    end

    subgraph CoreEngine ["Domain & Core Engine (main.py & mobile_scanner_core.py)"]
        IdempotencyCheck["Idempotency Filter (scan_requests)"]
        DomainVerifier["Domain Verification Engine (verify_packing_order)"]
        AttemptManager["Attempt Lifecycle & Supervisor Authorization"]
        ScanEndpoint --> IdempotencyCheck
        IdempotencyCheck -->|Fresh Nonce| DomainVerifier
        IdempotencyCheck -->|Duplicate Nonce| CachedResponse["Replay Cached JSON Response"]
        ResetEndpoint --> AttemptManager
    end

    subgraph DatabaseLayer ["SQLite Persistence Layer (packing_verification.db)"]
        DBTx["Atomic SQLite Transaction Boundary"]
        TableOrders[("packing_orders")]
        TableParts[("part_requirements")]
        TableEvents[("scan_events")]
        TableOutbox[("hardware_command_outbox")]
        TableResetAudit[("supervisor_reset_events")]
        TableRetryAudit[("hardware_retry_events")]

        DomainVerifier --> DBTx
        DBTx --> TableOrders
        DBTx --> TableParts
        DBTx --> TableEvents
        DBTx -->|On Terminal Result (PASS/NG)| TableOutbox
        AttemptManager --> TableResetAudit
        RetryEndpoint --> TableRetryAudit
    end

    subgraph WorkerLayer ["Asynchronous Hardware Subsystem"]
        OutboxWorker["ArduinoOutboxWorker (Background Daemon Thread)"]
        TableOutbox -.->|FIFO Atomic Claim (outbox_id ASC)| OutboxWorker
        SerialTransceiver["PySerial UART Transceiver (main.send_arduino_result)"]
        OutboxWorker --> SerialTransceiver
    end

    subgraph HardwareLayer ["Physical Hardware Interface"]
        ArduinoUno["Arduino Uno R3 (COM5 @ 9600 Baud UART)"]
        SerialTransceiver <-->|"Command: PASS / NG / HOLD\nACK: PASS: Packing Approved\n     HOLD: Packing Not Approved"| ArduinoUno
    end

    OutboxWorker -->|Update Status: CONFIRMED / UNCONFIRMED| TableOutbox
    OutboxWorker -->|Update Communication Status| TableOrders
    OrderEndpoint --> TableOrders
    HistoryEndpoint --> TableEvents
```

---

## 5. End-to-End Workflow

### Normal Operational Flow
1. **Order Initialization:** Operator navigates to `http://localhost:5000/`. The client fetches order context, part requirements, and current fulfillment counts via `GET /api/order`.
2. **Barcode Scan & Stage:** The operator scans a component barcode using the camera or submits a pipe string (e.g. `PART-A|LH|1`). The decoded data is staged for review.
3. **Confirmation & Dispatch:** Operator taps **Confirm & Log Scan**. The frontend generates a unique UUIDv4 nonce and dispatches a JSON payload to `POST /api/scan`.
4. **Domain Evaluation:** Backend verifies the nonce in `scan_requests`. If fresh, domain logic updates counts and calculates the order state (`HOLD`, `NG`, or `PASS`).
5. **Atomic Persistence:** In a single transaction, the order state, part status, scan event, and (if terminal) outbox command are committed.
6. **Asynchronous Hardware Dispatch Attempt:** If `PASS` or `NG`, the background worker is signaled, claims the outbox command (`ORDER BY outbox_id ASC LIMIT 1`), and attempts to transmit `PASS\n` or `NG\n` over serial (`COM5`).
7. **Acknowledgement Evaluation:** If the expected acknowledgement string is received from Arduino within 2.0 seconds, worker updates outbox status to `CONFIRMED`. If a timeout, disconnect, or unexpected string occurs, status is updated to `UNCONFIRMED`.

### Error & Quarantine Recovery Flow
1. **Defect Detected:** An invalid variant or over-pack is submitted (e.g. `PART-A|RH|1`).
2. **NG Lock:** Domain logic immediately sets order status to `NG`. An `NG` command is queued for the Arduino. The attempt transitions to a locked state.
3. **Operator Lockout:** Subsequent scans are rejected with an error; fulfillment counts are frozen.
4. **Supervisor Authorization:** An authorized supervisor enters their PIN and a root-cause explanation in the quarantine section (`POST /api/supervisor/reset`).
5. **Attempt Rotation:** Attempt #1 is recorded as `CLOSED_NG`. A fresh attempt (`PO-001-ATTEMPT-2`) is initialized with zeroed counts, allowing packing to restart cleanly.
6. **Audit Trail:** Attempt #1 scan history remains preserved in `scan_events` and `supervisor_reset_events`.

---

## 6. Technology Stack

### Backend & Core
- **Language:** Python 3.11 (tested and verified runtime environment)
- **Web Framework:** Flask 3.1.3
- **Serial Communication:** PySerial 3.5
- **WSGI / Reloader Support:** Werkzeug 3.1.9, Jinja2 3.1.6, MarkupSafe 3.0.4, Click 8.5.0, Blinker 1.9.0, ItsDangerous 2.2.0
- **Database:** SQLite 3 (Standard Library `sqlite3`)
- **Concurrency:** Standard Library `threading` (Daemon Threads, `threading.RLock`, `threading.Event`)

### Frontend
- **Markup & Layout:** HTML5, CSS3 (Mobile-first responsive flex/grid)
- **Scripting:** Vanilla JavaScript (ES6+, IIFE modular state encapsulation)
- **Barcode & QR Decoding:** `html5-qrcode` (v2.3.8, loaded via external CDN from `unpkg.com`)

### Hardware & Microcontroller
- **Target Board:** Arduino Uno R3 (ATmega328P)
- **Interface:** USB-to-UART Serial (8N1, 9600 Baud, 2.0s read timeout)
- **Protocol:** ASCII text command/response strings terminated by newline (`\n`)

---

## 7. Repository Structure

```text
C:\Hackathons\Sakthi-hackathon\
├── main.py                             # Core domain verification engine, CLI interface, and direct serial transceiver
├── database.py                         # SQLite database initialization, schema DDL, and CRUD persistence functions
├── mobile_scanner.py                   # Flask API application factory, routing endpoints, and worker lifecycle
├── mobile_scanner_core.py              # Payload parsing, idempotency, outbox worker, supervisor reset & retry logic
├── check_database.py                   # Utility script to inspect packing_orders and part_requirements
├── check_database_history.py           # Utility script to inspect scan_events and verification_events
├── requirements-before-mobile.txt      # Historical initial dependency list (pyserial==3.5)
├── static/
│   ├── css/
│   │   └── mobile_scanner.css          # Mobile-first industrial UI stylesheets
│   └── js/
│       └── mobile_scanner.js           # Frontend logic, staged scanning gate, and API polling
├── templates/
│   └── mobile_scanner.html             # Responsive dashboard layout and supervisor modals
├── tests/
│   ├── test_database.py                # Database schema, table constraints, and foreign key tests (5 tests)
│   ├── test_database_persistence.py    # Transaction rollback and persistence integration tests (9 tests)
│   ├── test_packing_verification.py    # Domain verification logic and serial transceiver tests (15 tests)
│   ├── test_mobile_scanner_api.py      # Payload parsing, strict types, and nonce idempotency tests (16 tests)
│   ├── test_mobile_scanner_flask.py    # Flask HTTP endpoints, status codes, and JSON responses (12 tests)
│   ├── test_mobile_scanner_reset.py    # Supervisor quarantine, attempt isolation, and audit tests (12 tests)
│   ├── test_mobile_scanner_hardware.py # Durable outbox, FIFO queue, and crash recovery tests (13 tests)
│   └── test_mobile_scanner_phase4b.py  # Worker lifecycle, hardware status sync, and retry tests (25 tests)
└── evidence/
    ├── phase-3.1/
    │   ├── PHASE_3_1_DATABASE_AUDIT.md # Read-only database audit verifying Attempt #1 vs #2 isolation
    │   ├── Screenshot 2026-11-06 182723.png # Verified mobile UI screenshot: Final PASS dashboard
    │   └── Screenshot 2026-11-06 182737.png # Verified mobile UI screenshot: Preserved historical scan stream
    └── phase-5-hardware/
        ├── 02_pass_terminal.png        # CLI execution: PASS command and Arduino acknowledgement on COM5
        ├── 03_ng_terminal.png          # CLI execution: NG command and Arduino acknowledgement on COM5
        ├── 04_hold_database.png        # SQLite verification: HOLD status records
        ├── 05_pass_database.png        # SQLite verification: PASS status records with CONFIRMED serial status
        └── 06_ng_database.png          # SQLite verification: NG status records with CONFIRMED serial status
```

---

## 8. Setup and Installation

### Prerequisites
- Windows 10/11
- Python 3.11 installed and available on PATH
- Optional: Arduino Uno R3 connected via USB (enumerating as `COM5`)

### Environment Setup
1. Clone or open the project directory in PowerShell:
   ```powershell
   cd C:\Hackathons\Sakthi-hackathon
   ```
2. Create and activate a Python virtual environment:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```
3. Install required runtime dependencies:
   ```powershell
   pip install Flask==3.1.3 pyserial==3.5
   ```

---

## 9. Running the Applications

### Option A: Mobile Web Scanner (Recommended)
Launch the Flask web server with background outbox processing:
```powershell
.\.venv\Scripts\python.exe mobile_scanner.py
```
- Access the web dashboard locally at: `http://localhost:5000/`
- When accessed over local Wi-Fi from an Android mobile device, camera permissions require HTTPS or port forwarding via ADB:
  ```powershell
  adb reverse tcp:5000 tcp:5000
  ```
  *(Then open `http://localhost:5000` inside Chrome on the mobile device).*

### Option B: Interactive CLI Interface
To run the interactive operator CLI interface on an isolated test database:
```powershell
.\.venv\Scripts\python.exe -c "import main; main.main('packing_verification_hardware_test.db')"
```

---

## 10. API Specification Overview

The Flask API exposes six routes/endpoints operating over JSON:

| Method | Endpoint | Request Body | Status Codes | Description |
| :--- | :--- | :--- | :--- | :--- |
| `GET` | `/` | *None* | `200` | Serves the mobile scanner web dashboard. |
| `GET` | `/api/order` | *None* | `200`, `500` | Returns active packing order, BOM fulfillment, remaining quantities, attempt number, and effective hardware status (`PENDING`, `PROCESSING`, `CONFIRMED`, `UNCONFIRMED`, or `null`). |
| `POST` | `/api/scan` | `{"scan_nonce": "...", "payload": "..."}` | `200`, `400`, `500` | Submits a barcode scan. Replays cached response if nonce was previously committed; otherwise updates order state. |
| `GET` | `/api/history` | *None* | `200`, `500` | Returns chronological log of all scan events from `scan_events`. |
| `POST` | `/api/supervisor/reset` | `{"supervisor_pin": "...", "reason": "..."}` | `200`, `400`, `401`, `409`, `500` | Authorizes closure of a rejected (`NG`) attempt and initializes a clean attempt. Requires supervisor PIN. |
| `POST` | `/api/hardware/retry` | `{"supervisor_pin": "...", "attempt_order_id": "...", "reason": "..."}` | `200`, `400`, `401`, `404`, `409`, `429`, `500` | Re-queues an `UNCONFIRMED` hardware command back to `PENDING`. Enforces a strict limit of 3 retries per command. |

---

## 11. Testing and Validation Inventory

The repository maintains an automated test suite inventory of **107 test methods across 8 test modules** located in `tests/`:

```text
tests/
├── test_database.py                 (5 tests)  - SQLite schema, unique constraints, and foreign key cascades
├── test_database_persistence.py     (9 tests)  - Atomic persistence, rollback on disk failure, event logging
├── test_packing_verification.py     (15 tests) - Domain verification logic, variant matching, serial mocks
├── test_mobile_scanner_api.py       (16 tests) - Payload parsing, strict types, persistent nonce idempotency
├── test_mobile_scanner_flask.py     (12 tests) - Flask route contracts, status codes, and error formatting
├── test_mobile_scanner_reset.py     (12 tests) - Quarantine lock, supervisor PIN auth, attempt isolation
├── test_mobile_scanner_hardware.py  (13 tests) - Durable outbox enqueueing, FIFO claiming, crash recovery
└── test_mobile_scanner_phase4b.py   (25 tests) - Worker lifecycle, hardware status sync, retry limits
```

*(Note: The above reflects the discovered test suite inventory in the codebase. Tests are not executed as part of documentation tasks).*

To run individual test suites safely without modifying production data:
```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_database -v
.\.venv\Scripts\python.exe -m unittest tests.test_mobile_scanner_phase4b -v
```

---

## 12. Screenshots and Implementation Evidence

Verified evidence files are maintained in the repository under `evidence/`:

### Mobile Scanner Interface (Phase 3.1)
- **Final Approved Order View:** Displays `Attempt #2` completed with `PASS` status and 8/8 PCS fulfilled.
  `evidence/phase-3.1/Screenshot 2026-11-06 182723.png`
- **Preserved Scan Stream & Audit:** Scrolled view showing preserved Attempt #1 rejected scans followed by Attempt #2 qualifying scans.
  `evidence/phase-3.1/Screenshot 2026-11-06 182737.png`
- **Database Audit Report:** Formal SQL transcript auditing attempt isolation and scan event distribution.
  `evidence/phase-3.1/PHASE_3_1_DATABASE_AUDIT.md`

### Hardware Communication Verification (Phase 5)
- **PASS Decision Dispatch:** PowerShell terminal showing CLI connection to Arduino Uno on `COM5`, transmitting `PASS`, and receiving `"PASS: Packing Approved"`.
  `evidence/phase-5-hardware/02_pass_terminal.png`
- **NG Decision Dispatch:** PowerShell terminal showing transmission of `NG` and receipt of `"HOLD: Packing Not Approved"`.
  `evidence/phase-5-hardware/03_ng_terminal.png`
- **Database State Dumps:** Inspection of order records confirming `arduino_communication_status = 'CONFIRMED'`.
  `evidence/phase-5-hardware/04_hold_database.png`, `05_pass_database.png`, `06_ng_database.png`

---

## 13. Hardware Integration Status

### What is Implemented and Verified
- **Bidirectional Serial Protocol:** The host sends newline-delimited command strings (`PASS\n`, `NG\n`, `HOLD\n`) over USB serial (`COM5`, 9600 baud, 8N1).
- **Firmware Response Handling:** The Arduino Uno returns expected ASCII acknowledgement strings (`PASS: Packing Approved`, `HOLD: Packing Not Approved`).
- **Serial Status Semantics:** The status `CONFIRMED` in database records indicates that the expected serial acknowledgement string was received by the host over UART. It verifies successful communication with the board.
- **At-Least-Once Dispatch Attempts:** The durable SQLite outbox re-transmits stranded `PROCESSING` commands after application restart, ensuring hardware dispatch attempts are persisted across process failures.

### What is NOT Verified or Implemented (Hardware Boundaries)
- **No Physical Actuation Verification:** The current software does not establish physical LED illumination, buzzer sounding, or servo/solenoid actuation. `CONFIRMED` establishes UART serial acknowledgement only.
- **Microcontroller Firmware Source:** The Arduino firmware sketch (`.ino`) was flashed out-of-band and is not present in this repository. Firmware internal pin assignments, GPIO logic, and whether any firmware-level deduplication exists cannot be verified from available source files.
- **Physical Deduplication Limitation:** The microcontroller executes incoming serial strings directly. Sequence IDs or transaction nonces in software alone cannot guarantee exactly-once physical execution without microcontroller-side persistent deduplication logic.

---

## 14. Current Limitations and Security Considerations

1. **Demonstration Authentication:** The supervisor PIN is a pre-shared demonstration secret configured via the `SUPERVISOR_PIN` environment variable (falling back to a default prototype value). The system does not implement multi-user accounts, passwords, or role-based access control (RBAC).
2. **Serial Port Exclusivity:** Serial COM ports on Windows cannot be shared across multiple processes. Running the CLI (`main.py`) simultaneously with the Flask application (`mobile_scanner.py`) will result in port access collisions (`PermissionError: Access is denied`).
3. **Application-Level History Preservation:** Historical audit records are preserved through application-level attempt lifecycle isolation (creating new attempt identifiers). Immutability is not enforced by SQLite engine triggers or cryptographic signatures.
4. **Browser Camera Context & Operational Testing:** Browsers restrict camera access to secure origins (`HTTPS` or `localhost`). Direct access from mobile devices over plain HTTP requires reverse port forwarding via ADB or a reverse proxy. Live camera scanning on a physical phone in warehouse environments remains unverified in captured evidence.
5. **Request-Level Nonce Scoping:** Nonce-based idempotency prevents network-level duplicate submissions of the same request nonce. It does not prevent an operator from scanning the same physical component twice if assigned different nonces (item-level duplicate detection requires unique item serialization).

---

## 15. Future Development Scope

- **Physical Actuator Integration:** Connect physical indicator LEDs (Green/Red), an audible piezo alert buzzer, and a container lock servo to designated Arduino digital pins.
- **Microcontroller Idempotency Protocol:** Extend Arduino firmware with persistent sequence ID deduplication to enable exactly-once physical execution.
- **Enterprise Authentication & WMS Sync:** Replace demonstration PINs with enterprise SSO / LDAP authentication and integrate with warehouse management systems (SAP/Oracle WMS) via Webhooks.
- **Edge Deployment Appliance:** Package the Flask backend and worker daemon into an edge appliance container (Docker / Linux systemd service) with dynamic serial port auto-discovery.

---

## 16. Project Status & Acknowledgements

- **Event:** Sakthi Hackathon 2.0
- **Problem Statement:** PS-016 (Smart Export Packing Verification System)
- **Status:** Phase 1 through Phase 4B implementation completed, accompanied by a 107-test automated test suite inventory. Phase 5 physical actuator integration scoped for future hardware setup.
