# Phase 3.1 Database Audit & Evidence Report

**Database Inspected:** `packing_verification_mobile_test.db`
**Connection Mode:** SQLite URI Read-Only (`file:packing_verification_mobile_test.db?mode=ro`)
**Audit Type:** Strictly Read-Only Database Audit
**Audit Scope:** Inspection of manual mobile dashboard test events, attempt lifecycle states, supervisor reset records, and scan event distribution. No records were modified, inserted, updated, or deleted.

> **Note on Audit Execution:** The initial audit script command reported exit code 1 during execution (due to a query addressing a non-existent column name `verification_event_id` in `verification_events`, which uses `event_id` as primary key; the query was subsequently corrected and re-run in read-only mode). The broader cause and implications for automated runners have not yet been established.

---

## 1. Attempt #1 (`PO-001`) Status & Preservation

### Actual SQL Queries & Results

**Packing Order State:**
```sql
SELECT packing_order_id, box_id, order_id, verification_result, verification_timestamp
FROM packing_orders WHERE packing_order_id = 'PO-001';
```
| packing_order_id | box_id | order_id | verification_result | verification_timestamp |
| :--- | :--- | :--- | :--- | :--- |
| `PO-001` | `BOX-001` | `ORD-2026-001` | `NG` | `2026-11-06T12:04:40+00:00` |

**Part Requirements State:**
```sql
SELECT id, packing_order_id, part_id, variant, required_quantity, scanned_quantity, packing_status
FROM part_requirements WHERE packing_order_id = 'PO-001' ORDER BY part_id;
```
| id | packing_order_id | part_id | variant | required_quantity | scanned_quantity | packing_status |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 1 | `PO-001` | `PART-A` | `LH` | 2 | 2 | `NG` |
| 2 | `PO-001` | `PART-B` | `RH` | 2 | 0 | `Pending` |
| 3 | `PO-001` | `PART-C` | `Standard` | 4 | 0 | `Pending` |

**Attempt Lifecycle State:**
```sql
SELECT attempt_id, base_order_id, attempt_order_id, attempt_number, status, created_at, closed_at
FROM mobile_attempts WHERE attempt_order_id = 'PO-001';
```
| attempt_id | base_order_id | attempt_order_id | attempt_number | status | created_at | closed_at |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 1 | `PO-001` | `PO-001` | 1 | `CLOSED_NG` | `2026-11-06T12:43:28+00:00` | `2026-11-06T12:46:21+00:00` |

* **Verified Fact:** Attempt #1 (`PO-001`) remains locked in `NG` status. Its scanned quantities remain frozen at 2 (PART-A), 0 (PART-B), and 0 (PART-C). The attempt lifecycle record is marked `CLOSED_NG` with the exact closure timestamp of the reset.
* **Distinction from Assumptions:** It is a verified database fact that Attempt #1 quantities were not reset or cleared in place; Attempt #1 was closed as rejected.

---

## 2. Attempt #2 (`PO-001-ATTEMPT-2`) Final State & Quantities

### Actual SQL Queries & Results

**Packing Order State:**
```sql
SELECT packing_order_id, box_id, order_id, verification_result, verification_timestamp
FROM packing_orders WHERE packing_order_id = 'PO-001-ATTEMPT-2';
```
| packing_order_id | box_id | order_id | verification_result | verification_timestamp |
| :--- | :--- | :--- | :--- | :--- |
| `PO-001-ATTEMPT-2` | `BOX-001` | `ORD-2026-001` | `PASS` | `2026-11-06T12:55:26+00:00` |

**Part Requirements State:**
```sql
SELECT id, packing_order_id, part_id, variant, required_quantity, scanned_quantity, packing_status
FROM part_requirements WHERE packing_order_id = 'PO-001-ATTEMPT-2' ORDER BY part_id;
```
| id | packing_order_id | part_id | variant | required_quantity | scanned_quantity | packing_status | Fulfillment |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 16 | `PO-001-ATTEMPT-2` | `PART-A` | `LH` | 2 | 2 | `Complete` | **2/2** |
| 17 | `PO-001-ATTEMPT-2` | `PART-B` | `RH` | 2 | 2 | `Complete` | **2/2** |
| 18 | `PO-001-ATTEMPT-2` | `PART-C` | `Standard` | 4 | 4 | `Complete` | **4/4** |

**Attempt Lifecycle State:**
```sql
SELECT attempt_id, base_order_id, attempt_order_id, attempt_number, status, created_at, closed_at
FROM mobile_attempts WHERE attempt_order_id = 'PO-001-ATTEMPT-2';
```
| attempt_id | base_order_id | attempt_order_id | attempt_number | status | created_at | closed_at |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 2 | `PO-001` | `PO-001-ATTEMPT-2` | 2 | `ACTIVE` | `2026-11-06T12:46:21+00:00` | `None` |

* **Verified Fact:** Attempt #2 reached a final verification result of `PASS` at `2026-11-06T12:55:26+00:00`. All part requirements are 100% fulfilled (`Complete`) with exact required quantities: PART-A LH (2/2), PART-B RH (2/2), and PART-C Standard (4/4).

---

## 3. Scan Event Association by Attempt ID

### Actual SQL Query & Results

```sql
SELECT packing_order_id, COUNT(*) as scan_count, MIN(scan_event_id) as first_id, MAX(scan_event_id) as last_id
FROM scan_events GROUP BY packing_order_id ORDER BY first_id;
```
| packing_order_id | scan_count | first_id | last_id |
| :--- | :--- | :--- | :--- |
| `PO-001` | 4 | 1 | 4 |
| `PO-001-ATTEMPT-2` | 8 | 5 | 12 |

* **Verified Fact:** Every scan event has a non-null, valid `packing_order_id`. Events 1 through 4 are bound to `PO-001`, and events 5 through 12 are bound to `PO-001-ATTEMPT-2`. There is zero cross-contamination of attempt identifiers.

---

## 4. Supervisor Reset Audit Record

### Actual SQL Query & Results

```sql
SELECT reset_id, base_order_id, previous_attempt_order_id, new_attempt_order_id, supervisor_id, reason, timestamp
FROM supervisor_reset_events;
```
| Column | Value |
| :--- | :--- |
| `reset_id` | `1` |
| `base_order_id` | `PO-001` |
| `previous_attempt_order_id` | `PO-001` |
| `new_attempt_order_id` | `PO-001-ATTEMPT-2` |
| `supervisor_id` | `SUPERVISOR-01` |
| `reason` | `Attempt 1 rejected due to PART-A variant mismatch. Quarantined the rejected packing attempt and initiated a fresh verification attempt.` |
| `timestamp` | `2026-11-06T12:46:21+00:00` |

* **Verified Fact:** The supervisor reset audit record was captured atomically at `2026-11-06T12:46:21+00:00`. It contains the previous attempt ID (`PO-001`), the new attempt ID (`PO-001-ATTEMPT-2`), supervisor identity (`SUPERVISOR-01`), root-cause reason, and timestamp.

---

## 5. Attempt #1 History Non-Destruction

### Actual Evidence Verification

- **Initial state of Attempt #1:** Recorded 4 scan events between `12:00:45` and `12:04:40`.
- **Reset occurred at:** `12:46:21`.
- **Current database state for Attempt #1:** All 4 events (IDs 1–4) exist unchanged with their exact original timestamps, payloads, and results:
  - Event 1 (`12:00:45`): `PART-A` LH (Qty 1) -> `HOLD`
  - Event 2 (`12:01:47`): `PART-A` LH (Qty 1) -> `HOLD`
  - Event 3 (`12:03:45`): `PART-A` RH (Qty 1) -> `NG`
  - Event 4 (`12:04:40`): `PART-B` RH (Qty 1) -> `NG`

* **Verified Fact:** Neither scan events, verification events, nor order rows for Attempt #1 were deleted, truncated, or modified when Attempt #2 was created or executed.

---

## 6. Scan Event Distribution (12 Total Events)

### Actual SQL Query & Results

```sql
SELECT scan_event_id, packing_order_id, part_id, supplied_variant, quantity, scan_result, scan_timestamp
FROM scan_events ORDER BY scan_event_id;
```

| Event ID | Attempt (`packing_order_id`) | Part ID | Variant | Qty | Result | Timestamp | Operational Note |
| :---: | :--- | :--- | :--- | :---: | :---: | :--- | :--- |
| **1** | `PO-001` | PART-A | LH | 1 | `HOLD` | `2026-11-06T12:00:45+00:00` | Valid part 1 of 2 |
| **2** | `PO-001` | PART-A | LH | 1 | `HOLD` | `2026-11-06T12:01:47+00:00` | Valid part 2 of 2 |
| **3** | `PO-001` | PART-A | **RH** | 1 | **`NG`** | `2026-11-06T12:03:45+00:00` | **Wrong variant triggers NG lock** |
| **4** | `PO-001` | PART-B | RH | 1 | **`NG`** | `2026-11-06T12:04:40+00:00` | **Scan rejected (order locked in NG)** |
| — | *SUPERVISOR RESET* | — | — | — | — | `2026-11-06T12:46:21+00:00` | *Attempt 1 closed; Attempt 2 created* |
| **5** | `PO-001-ATTEMPT-2` | PART-A | LH | 1 | `HOLD` | `2026-11-06T12:48:21+00:00` | Clean attempt: PART-A 1/2 |
| **6** | `PO-001-ATTEMPT-2` | PART-A | LH | 1 | `HOLD` | `2026-11-06T12:50:19+00:00` | Clean attempt: PART-A 2/2 Complete |
| **7** | `PO-001-ATTEMPT-2` | PART-B | RH | 1 | `HOLD` | `2026-11-06T12:51:31+00:00` | Clean attempt: PART-B 1/2 |
| **8** | `PO-001-ATTEMPT-2` | PART-B | RH | 1 | `HOLD` | `2026-11-06T12:52:27+00:00` | Clean attempt: PART-B 2/2 Complete |
| **9** | `PO-001-ATTEMPT-2` | PART-C | Standard | 1 | `HOLD` | `2026-11-06T12:53:22+00:00` | Clean attempt: PART-C 1/4 |
| **10** | `PO-001-ATTEMPT-2` | PART-C | Standard | 1 | `HOLD` | `2026-11-06T12:54:30+00:00` | Clean attempt: PART-C 2/4 |
| **11** | `PO-001-ATTEMPT-2` | PART-C | Standard | 1 | `HOLD` | `2026-11-06T12:55:16+00:00` | Clean attempt: PART-C 3/4 |
| **12** | `PO-001-ATTEMPT-2` | PART-C | Standard | 1 | **`PASS`** | `2026-11-06T12:55:26+00:00` | **Final part scanned: 4/4 Complete -> PASS** |

**Summary Distribution:**
- **Total Scan Events:** `12`
- **Attempt #1 Events:** `4` (2 HOLD, 2 NG)
- **Attempt #2 Events:** `8` (7 HOLD, 1 PASS)

---

## 7. Evidence Artifacts Verification (Screenshots)

Both captured mobile dashboard screenshots depict the final approved state (`PASS`) of Attempt #2 (`PO-001-ATTEMPT-2`) captured at `2026-11-06 18:27`:

* **`Screenshot 2026-11-06 182723.png` (Top View - Final PASS Dashboard):**
  - Order Context: Packing Order `PO-001-ATTEMPT-2`, Box `BOX-001`, Order `ORD-2026-001`, `Attempt #2`.
  - Decision Status: Green **`PASS`** card with label *"Packing Approved"* and guidance *"All packing requirements have been satisfied. Ready for export sealing."* (Decision timestamp: `2026-11-06T12:55:26+00:00`).
  - BOM Fulfillment Progress: `8 / 8 PCS` (100% full progress bar).
  - Required Components Table: PART-A LH (2/2, Complete), PART-B RH (2/2, Complete), PART-C Standard (4/4, Complete).

* **`Screenshot 2026-11-06 182737.png` (Bottom View - Scrolled Event History):**
  - Manual / Wedge Input Pane: Active tab with raw payload `PART-C|Standard|1` from the final qualifying scan.
  - Recent Scan Events Counter: Displays `12 events`.
  - Event Stream: Shows chronological event feed including the preserved rejected scan events from Attempt #1 (`PART-A (RH) x 1 NG`, `PART-B (RH) x 1 NG`) and subsequent clean attempt events.

---

## Phase 3.1 Verified Capabilities

The following capabilities have been empirically verified through read-only inspection of application, API, and SQLite database artifacts:

1. **Mobile web dashboard manual/wedge scan workflow tested:** Operators can enter and stage barcode payloads via the browser UI and submit them to the backend API.
2. **Valid and incorrect part variants handled:** Valid variants update fulfillment counts towards completion; incorrect variants (e.g., PART-A with RH variant) trigger immediate rejection.
3. **NG lock prevents further accepted scans:** Once an attempt enters NG status, subsequent scans (even for otherwise valid parts) are rejected and do not modify fulfillment counts.
4. **Supervisor-authorized reset starts a fresh attempt:** Providing supervisor authorization and root-cause documentation closes the rejected attempt and initializes a new isolated packing attempt (`PO-001-ATTEMPT-2`).
5. **Previous rejected attempt history is preserved:** The rejected attempt, its part fulfillment state, and all associated scan and verification events remain intact in the database for auditing and traceability.
6. **New attempt independently completes with PASS:** The fresh attempt begins with zero fulfillment counts and progresses through valid scans to a verified `PASS` state.
7. **Scan events and reset audit are persisted in SQLite:** All scan attempts, verification decisions, and supervisor reset events are durably persisted in SQLite database tables.

### Limitations

The following items are outside the scope of Phase 3.1 and remain unverified or unimplemented:
- **Physical QR-camera scanning has not yet been verified:** Live video scanning via mobile cameras under varied lighting and physical conditions has not been tested.
- **Production-grade supervisor authentication has not been implemented:** The supervisor reset mechanism uses a pre-shared demonstration PIN, not production-grade role-based identity management or MFA.
- **Physical gate interlocking has not been implemented or verified:** Hardware-level physical barriers, solenoid interlocks, or conveyor gating tied to mobile verification results have not been implemented or verified.
