/**
 * Smart Export Packing Verification - Mobile Scanner Frontend
 * Handles camera/QR scanning, manual barcode input, operator confirmation gate,
 * persistent nonce generation, and synchronization with Flask backend.
 */

(function () {
  'use strict';

  // State Management
  const state = {
    order: null,
    html5QrCode: null,
    isScanning: false,
    stagedScan: null,       // { raw, partId, variant, quantity, nonce }
    currentRetryNonce: null // Nonce reused during network retries
  };

  // DOM Elements
  const elements = {
    connectionBadge: document.getElementById('connectionBadge'),
    connectionLabel: document.getElementById('connectionLabel'),
    securityBanner: document.getElementById('securityBanner'),
    notificationBanner: document.getElementById('notificationBanner'),
    notificationMessage: document.getElementById('notificationMessage'),
    notificationClose: document.getElementById('notificationClose'),

    // Order Context
    ctxPackingOrderId: document.getElementById('ctxPackingOrderId'),
    ctxBoxId: document.getElementById('ctxBoxId'),
    ctxOrderId: document.getElementById('ctxOrderId'),
    ctxProductDesc: document.getElementById('ctxProductDesc'),

    // Decision Card
    decisionCard: document.getElementById('decisionCard'),
    decisionBadge: document.getElementById('decisionBadge'),
    decisionStateLabel: document.getElementById('decisionStateLabel'),
    decisionGuidance: document.getElementById('decisionGuidance'),
    decisionTimestamp: document.getElementById('decisionTimestamp'),

    // BOM Progress
    progressNumbers: document.getElementById('progressNumbers'),
    progressBarFill: document.getElementById('progressBarFill'),
    bomTableBody: document.getElementById('bomTableBody'),
    refreshOrderBtn: document.getElementById('refreshOrderBtn'),

    // Scanner Tabs & Panes
    tabCameraBtn: document.getElementById('tabCameraBtn'),
    tabManualBtn: document.getElementById('tabManualBtn'),
    paneCamera: document.getElementById('paneCamera'),
    paneManual: document.getElementById('paneManual'),

    // Camera Controls
    startCameraBtn: document.getElementById('startCameraBtn'),
    stopCameraBtn: document.getElementById('stopCameraBtn'),
    viewfinderWrapper: document.getElementById('viewfinderWrapper'),
    viewfinderStatus: document.getElementById('viewfinderStatus'),
    cameraErrorBox: document.getElementById('cameraErrorBox'),
    cameraErrorMsg: document.getElementById('cameraErrorMsg'),

    // Manual Form
    manualScanForm: document.getElementById('manualScanForm'),
    manualPayloadInput: document.getElementById('manualPayloadInput'),
    manualPartSelect: document.getElementById('manualPartSelect'),
    manualVariantSelect: document.getElementById('manualVariantSelect'),
    manualQtyInput: document.getElementById('manualQtyInput'),

    // Staged Confirmation Card
    stagedScanCard: document.getElementById('stagedScanCard'),
    stagedRaw: document.getElementById('stagedRaw'),
    stagedPart: document.getElementById('stagedPart'),
    stagedVariant: document.getElementById('stagedVariant'),
    stagedQty: document.getElementById('stagedQty'),
    confirmScanBtn: document.getElementById('confirmScanBtn'),
    cancelScanBtn: document.getElementById('cancelScanBtn'),

    // History
    historyCount: document.getElementById('historyCount'),
    historyList: document.getElementById('historyList'),

    // Supervisor Reset Section
    supervisorSection: document.getElementById('supervisorSection'),
    spvAttemptId: document.getElementById('spvAttemptId'),
    supervisorResetForm: document.getElementById('supervisorResetForm'),
    spvPinInput: document.getElementById('spvPinInput'),
    spvReasonInput: document.getElementById('spvReasonInput'),
    spvSubmitBtn: document.getElementById('spvSubmitBtn'),

    // Attempt context
    ctxAttemptNum: document.getElementById('ctxAttemptNum'),

    // Hardware Status (Phase 4B)
    hardwareSection: document.getElementById('hardwareSection'),
    hwStatusBadge: document.getElementById('hwStatusBadge'),
    hwStatusDesc: document.getElementById('hwStatusDesc'),
    hwRetryBlock: document.getElementById('hwRetryBlock'),
    hwRetryCount: document.getElementById('hwRetryCount'),
    hwRetryForm: document.getElementById('hwRetryForm'),
    hwRetryPinInput: document.getElementById('hwRetryPinInput'),
    hwRetryReasonInput: document.getElementById('hwRetryReasonInput'),
    hwRetrySubmitBtn: document.getElementById('hwRetrySubmitBtn')
  };

  /**
   * Generate a cryptographically strong UUID or fallback nonce.
   */
  function generateNonce() {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
      return crypto.randomUUID();
    }
    return 'nonce-' + Date.now() + '-' + Math.random().toString(36).substring(2, 10);
  }

  /**
   * Check and display security notice if on insecure HTTP on mobile.
   */
  function checkSecurityContext() {
    const isLocalhost = window.location.hostname === 'localhost' ||
                        window.location.hostname === '127.0.0.1';
    if (!window.isSecureContext && !isLocalhost) {
      if (elements.securityBanner) {
        elements.securityBanner.classList.remove('hidden');
      }
    }
  }

  /**
   * Display toast notifications.
   */
  function showNotification(message, type) {
    if (!elements.notificationBanner || !elements.notificationMessage) return;
    elements.notificationMessage.textContent = message;
    elements.notificationBanner.className = 'notification-banner ' + (type || 'warning');
    elements.notificationBanner.classList.remove('hidden');

    if (type === 'success') {
      setTimeout(() => {
        elements.notificationBanner.classList.add('hidden');
      }, 5000);
    }
  }

  if (elements.notificationClose) {
    elements.notificationClose.addEventListener('click', () => {
      elements.notificationBanner.classList.add('hidden');
    });
  }

  /**
   * Parse a raw barcode string into { partId, variant, quantity } or return null if unparseable.
   */
  function parseBarcodePayload(raw) {
    if (!raw || typeof raw !== 'string') return null;
    const trimmed = raw.trim();

    // Check project pipe format: PART_ID|VARIANT|QUANTITY
    if (trimmed.includes('|')) {
      const parts = trimmed.split('|').map(s => s.trim());
      if (parts.length === 3) {
        const qty = parseInt(parts[2], 10);
        if (!isNaN(qty) && qty > 0) {
          return { partId: parts[0], variant: parts[1], quantity: qty };
        }
      }
    }

    // Check JSON format
    if (trimmed.startsWith('{') && trimmed.endsWith('}')) {
      try {
        const parsed = JSON.parse(trimmed);
        if (parsed.part_id && parsed.variant && parsed.quantity) {
          const qty = parseInt(parsed.quantity, 10);
          if (!isNaN(qty) && qty > 0) {
            return { partId: parsed.part_id, variant: parsed.variant, quantity: qty };
          }
        }
      } catch (e) {
        // Not valid JSON
      }
    }

    return null;
  }

  /**
   * Fetch and render current packing order.
   */
  async function fetchOrder() {
    try {
      const res = await fetch('/api/order');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      state.order = data;
      renderOrder(data);
    } catch (err) {
      console.error('Failed to fetch order:', err);
      showNotification('Unable to fetch order status. Server connection failed.', 'error');
    }
  }

  /**
   * Fetch and render scan history.
   */
  async function fetchHistory() {
    try {
      const res = await fetch('/api/history');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      renderHistory(data);
    } catch (err) {
      console.error('Failed to fetch history:', err);
    }
  }

  /**
   * Render order context, decision card, and BOM table.
   */
  function renderOrder(order) {
    if (!order) return;

    if (elements.ctxPackingOrderId) elements.ctxPackingOrderId.textContent = order.packing_order_id || '--';
    if (elements.ctxBoxId) elements.ctxBoxId.textContent = order.box_id || '--';
    if (elements.ctxOrderId) elements.ctxOrderId.textContent = order.order_id || '--';
    if (elements.ctxProductDesc) elements.ctxProductDesc.textContent = order.product_description || '--';
    if (elements.ctxAttemptNum) elements.ctxAttemptNum.textContent = 'Attempt #' + (order.attempt_number || 1);

    // Decision status
    const result = (order.verification_result || 'HOLD').toUpperCase();
    if (elements.decisionBadge) elements.decisionBadge.textContent = result;

    if (elements.decisionCard) {
      elements.decisionCard.className = 'decision-card ' +
        (result === 'PASS' ? 'status-pass' : (result === 'NG' ? 'status-ng' : 'status-hold'));
    }

    if (elements.decisionStateLabel && elements.decisionGuidance) {
      if (result === 'PASS') {
        elements.decisionStateLabel.textContent = 'Packing Approved';
        elements.decisionGuidance.textContent = 'All packing requirements have been satisfied. Ready for export sealing.';
      } else if (result === 'NG') {
        elements.decisionStateLabel.textContent = 'Packing Rejected (NG)';
        elements.decisionGuidance.textContent = 'Mismatched variant or excess count detected. Supervisor review required.';
      } else {
        elements.decisionStateLabel.textContent = 'Packing On Hold';
        elements.decisionGuidance.textContent = 'Incomplete packing requirements. Scan remaining components below.';
      }
    }

    if (elements.decisionTimestamp) {
      elements.decisionTimestamp.textContent = order.verification_timestamp
        ? 'Decision timestamp: ' + order.verification_timestamp
        : 'Status: Pending initial scan';
    }

    // Show or hide supervisor reset section based on can_reset flag
    if (elements.supervisorSection) {
      if (order.can_reset === true) {
        elements.supervisorSection.classList.remove('hidden');
        if (elements.spvAttemptId) {
          elements.spvAttemptId.textContent = order.packing_order_id || '--';
        }
      } else {
        elements.supervisorSection.classList.add('hidden');
      }
    }

    // Phase 4B: Render hardware signal status
    renderHardwareStatus(order);

    // BOM Requirements & Progress calculation
    const parts = order.part_requirements || [];
    let totalReq = 0;
    let totalScanned = 0;

    let rowsHtml = '';
    parts.forEach(part => {
      totalReq += (part.required_quantity || 0);
      totalScanned += (part.scanned_quantity || 0);

      const status = part.packing_status || 'Pending';
      let statusClass = 'status-pending';
      if (status === 'Complete') statusClass = 'status-complete';
      else if (status === 'In Progress') statusClass = 'status-in-progress';
      else if (status === 'NG') statusClass = 'status-ng';

      rowsHtml += `
        <tr>
          <td class="bold mono">${escapeHtml(part.part_id)}</td>
          <td>${escapeHtml(part.variant)}</td>
          <td class="num-col">${part.required_quantity}</td>
          <td class="num-col">${part.scanned_quantity}</td>
          <td class="num-col">${part.remaining_quantity !== undefined ? part.remaining_quantity : Math.max(0, part.required_quantity - part.scanned_quantity)}</td>
          <td><span class="part-status-badge ${statusClass}">${escapeHtml(status)}</span></td>
        </tr>
      `;
    });

    if (elements.bomTableBody) {
      elements.bomTableBody.innerHTML = rowsHtml || '<tr><td colspan="6">No parts found.</td></tr>';
    }

    if (elements.progressNumbers && elements.progressBarFill) {
      elements.progressNumbers.textContent = `${totalScanned} / ${totalReq} PCS`;
      const pct = totalReq > 0 ? Math.min(100, Math.round((totalScanned / totalReq) * 100)) : 0;
      elements.progressBarFill.style.width = pct + '%';
    }
  }

  /**
   * Render Arduino hardware signal status badge and optional retry form (Phase 4B).
   *
   * arduino_communication_status values from /api/order:
   *   null       — no terminal command dispatched yet
   *   'PENDING'  — command queued in outbox, worker has not sent it yet
   *   'PROCESSING' — worker is currently transmitting
   *   'CONFIRMED'  — expected serial ACK received (communication only, NOT physical actuation)
   *   'UNCONFIRMED' — timeout, disconnect, or unexpected response
   */
  function renderHardwareStatus(order) {
    const hwSection = document.getElementById('hardwareSection');
    const hwBadge = document.getElementById('hwStatusBadge');
    const hwDesc = document.getElementById('hwStatusDesc');
    const hwRetryBlock = document.getElementById('hwRetryBlock');
    const hwRetryCount = document.getElementById('hwRetryCount');

    if (!hwSection) return;

    const verResult = (order.verification_result || '').toUpperCase();
    const hwStatus = (order.arduino_communication_status || '').toUpperCase();
    const retryCount = order.hardware_retry_count || 0;
    const retryMax = order.hardware_retry_max || 3;
    const canRetry = order.can_hardware_retry === true;

    // Show hardware section only once a terminal result exists
    if (verResult !== 'PASS' && verResult !== 'NG') {
      hwSection.classList.add('hidden');
      return;
    }
    hwSection.classList.remove('hidden');

    // Badge text and style class
    const badgeClassMap = {
      'PENDING':     'hw-pending',
      'PROCESSING':  'hw-processing',
      'CONFIRMED':   'hw-confirmed',
      'UNCONFIRMED': 'hw-unconfirmed'
    };
    const badgeClass = badgeClassMap[hwStatus] || '';
    if (hwBadge) {
      hwBadge.textContent = hwStatus || '--';
      hwBadge.className = 'hardware-status-badge' + (badgeClass ? ' ' + badgeClass : '');
    }

    // Status description
    const descMap = {
      'PENDING':     'Command queued — waiting for the Arduino worker to dispatch.',
      'PROCESSING':  'Transmitting command to Arduino over serial UART…',
      'CONFIRMED':   'Serial acknowledgement received. Command delivered to Arduino. ' +
                     'Physical LED/gate actuation depends on Arduino firmware execution.',
      'UNCONFIRMED': 'Serial acknowledgement not received — timeout, cable disconnect, or unexpected response. ' +
                     'Physical hardware state is unknown.'
    };
    if (hwDesc) {
      hwDesc.textContent = descMap[hwStatus] || 'Hardware command status unknown.';
    }

    // Show retry block only when UNCONFIRMED and retries remain
    if (hwRetryBlock) {
      if (canRetry) {
        hwRetryBlock.classList.remove('hidden');
        if (hwRetryCount) {
          hwRetryCount.textContent = `Attempt ${retryCount} of ${retryMax}`;
        }
      } else {
        hwRetryBlock.classList.add('hidden');
        // Limit reached: surface a final message in the desc
        if (hwStatus === 'UNCONFIRMED' && retryCount >= retryMax) {
          if (hwDesc) {
            hwDesc.textContent += ` Retry limit reached (${retryCount}/${retryMax}). Manual investigation required.`;
          }
        }
      }
    }
  }

  /**
   * Render history list.
   */
  function renderHistory(historyData) {
    const events = historyData.scan_history || [];
    if (elements.historyCount) {
      elements.historyCount.textContent = `${events.length} event${events.length === 1 ? '' : 's'}`;
    }

    if (!elements.historyList) return;

    if (events.length === 0) {
      elements.historyList.innerHTML = '<div class="empty-history">No scan events recorded yet.</div>';
      return;
    }

    let html = '';
    events.forEach(ev => {
      const res = ev.scan_result || 'HOLD';
      let badgeClass = 'status-hold';
      if (res === 'PASS') badgeClass = 'status-complete';
      else if (res === 'NG') badgeClass = 'status-ng';

      html += `
        <div class="history-item">
          <div class="history-item-details">
            <span class="history-item-title mono">${escapeHtml(ev.part_id || 'UNKNOWN')} (${escapeHtml(ev.supplied_variant || '--')}) &times; ${ev.quantity}</span>
            <span class="history-item-meta mono">${escapeHtml(ev.scan_timestamp || '')}</span>
          </div>
          <span class="part-status-badge ${badgeClass}">${escapeHtml(res)}</span>
        </div>
      `;
    });
    elements.historyList.innerHTML = html;
  }

  /**
   * Stage a decoded scan for operator confirmation (Staged Scan Gate).
   */
  function stageScan(rawPayload) {
    const parsed = parseBarcodePayload(rawPayload);

    state.stagedScan = {
      raw: rawPayload,
      partId: parsed ? parsed.partId : rawPayload,
      variant: parsed ? parsed.variant : 'Unknown',
      quantity: parsed ? parsed.quantity : 1,
      nonce: generateNonce() // Generate unique nonce for this fresh staged scan
    };

    if (elements.stagedRaw) elements.stagedRaw.textContent = state.stagedScan.raw;
    if (elements.stagedPart) elements.stagedPart.textContent = state.stagedScan.partId;
    if (elements.stagedVariant) elements.stagedVariant.textContent = state.stagedScan.variant;
    if (elements.stagedQty) elements.stagedQty.textContent = state.stagedScan.quantity;

    if (elements.stagedScanCard) {
      elements.stagedScanCard.classList.remove('hidden');
    }

    // Pause/freeze camera scanner while operator confirmation is pending
    if (state.html5QrCode && state.isScanning) {
      try {
        state.html5QrCode.pause(true);
      } catch (e) {
        console.warn('Could not pause camera scanner:', e);
      }
    }

    if (elements.viewfinderStatus) {
      elements.viewfinderStatus.textContent = 'Scan decoded. Confirm below to proceed.';
    }
  }

  /**
   * Dismiss staged scan and resume scanning.
   */
  function dismissStagedScan() {
    state.stagedScan = null;
    state.currentRetryNonce = null;

    if (elements.stagedScanCard) {
      elements.stagedScanCard.classList.add('hidden');
    }

    // Resume camera scanner if active
    if (state.html5QrCode && state.isScanning) {
      try {
        state.html5QrCode.resume();
        if (elements.viewfinderStatus) {
          elements.viewfinderStatus.textContent = 'Position barcode inside viewfinder...';
        }
      } catch (e) {
        console.warn('Could not resume scanner:', e);
      }
    }
  }

  /**
   * Submit confirmed scan to POST /api/scan.
   */
  async function submitConfirmedScan() {
    if (!state.stagedScan) return;

    const staged = state.stagedScan;
    // Use staged nonce, or reuse retry nonce if repeating after uncertain network drop
    const nonce = state.currentRetryNonce || staged.nonce;
    state.currentRetryNonce = nonce;

    if (elements.confirmScanBtn) {
      elements.confirmScanBtn.disabled = true;
      elements.confirmScanBtn.textContent = 'Submitting...';
    }

    try {
      const payloadBody = {
        scan_nonce: nonce,
        payload: staged.raw
      };

      const res = await fetch('/api/scan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payloadBody)
      });

      const data = await res.json();

      if (res.ok) {
        // Success: Clear staged scan and retry nonce
        state.currentRetryNonce = null;
        dismissStagedScan();

        // Refresh UI with verified server state
        await fetchOrder();
        await fetchHistory();

        // Display outcome based on backend verification_result
        const verResult = data.verification_result;
        if (verResult === 'PASS') {
          showNotification('All parts complete! Verification status: PASS.', 'success');
        } else if (verResult === 'NG') {
          showNotification('Scan resulted in NG rejection. Supervisor review required.', 'error');
        } else {
          showNotification('Scan recorded successfully. Verification status: HOLD.', 'success');
        }
      } else {
        // Client error (400) or server error (500)
        const errMsg = data.error || 'Server rejected scan with status ' + res.status;
        showNotification('Error: ' + errMsg, 'error');
      }
    } catch (err) {
      console.error('Scan submission network error:', err);
      showNotification('Network connection error. You may tap Confirm again to retry with the same nonce.', 'warning');
    } finally {
      if (elements.confirmScanBtn) {
        elements.confirmScanBtn.disabled = false;
        elements.confirmScanBtn.textContent = 'Confirm & Log Scan';
      }
    }
  }

  /**
   * Initialize and start camera scanner.
   */
  async function startCameraScanner() {
    if (elements.cameraErrorBox) elements.cameraErrorBox.classList.add('hidden');

    if (typeof Html5Qrcode === 'undefined') {
      showCameraError('Scanner library (Html5Qrcode) not loaded. Please use manual entry or check network.');
      return;
    }

    try {
      if (!state.html5QrCode) {
        state.html5QrCode = new Html5Qrcode('reader');
      }

      if (elements.viewfinderWrapper) elements.viewfinderWrapper.classList.remove('hidden');
      if (elements.startCameraBtn) elements.startCameraBtn.classList.add('hidden');
      if (elements.stopCameraBtn) elements.stopCameraBtn.classList.remove('hidden');

      const config = {
        fps: 10,
        qrbox: { width: 220, height: 160 },
        aspectRatio: 1.333
      };

      await state.html5QrCode.start(
        { facingMode: 'environment' }, // Prefer rear-facing camera on mobile
        config,
        (decodedText) => {
          // Success callback: A barcode was decoded
          if (!state.stagedScan) {
            stageScan(decodedText);
          }
        },
        () => {
          // Frame failure callback (scanning in progress, no barcode found)
        }
      );

      state.isScanning = true;
      if (elements.viewfinderStatus) {
        elements.viewfinderStatus.textContent = 'Position barcode inside viewfinder...';
      }
    } catch (err) {
      console.error('Camera start failed:', err);
      let message = 'Unable to access camera.';
      if (err.name === 'NotAllowedError' || err.name === 'PermissionDeniedError') {
        message = 'Camera permission denied. Allow camera access in your browser settings.';
      } else if (err.name === 'NotFoundError' || err.name === 'DevicesNotFoundError') {
        message = 'No camera device found on this device.';
      } else if (!window.isSecureContext) {
        message = 'Camera access blocked: Mobile browsers require HTTPS or localhost.';
      }
      showCameraError(message);
      stopCameraScanner();
    }
  }

  /**
   * Stop camera scanner.
   */
  async function stopCameraScanner() {
    if (state.html5QrCode && state.isScanning) {
      try {
        await state.html5QrCode.stop();
      } catch (e) {
        console.warn('Error stopping scanner:', e);
      }
      state.isScanning = false;
    }

    if (elements.viewfinderWrapper) elements.viewfinderWrapper.classList.add('hidden');
    if (elements.startCameraBtn) elements.startCameraBtn.classList.remove('hidden');
    if (elements.stopCameraBtn) elements.stopCameraBtn.classList.add('hidden');
  }

  function showCameraError(msg) {
    if (elements.cameraErrorBox && elements.cameraErrorMsg) {
      elements.cameraErrorMsg.textContent = msg;
      elements.cameraErrorBox.classList.remove('hidden');
    }
  }

  /**
   * Setup UI Event Listeners.
   */
  function setupEventListeners() {
    // Mode tabs
    if (elements.tabCameraBtn && elements.tabManualBtn) {
      elements.tabCameraBtn.addEventListener('click', () => {
        elements.tabCameraBtn.classList.add('active');
        elements.tabManualBtn.classList.remove('active');
        elements.paneCamera.classList.add('active');
        elements.paneManual.classList.remove('active');
      });

      elements.tabManualBtn.addEventListener('click', () => {
        elements.tabManualBtn.classList.add('active');
        elements.tabCameraBtn.classList.remove('active');
        elements.paneManual.classList.add('active');
        elements.paneCamera.classList.remove('active');
        stopCameraScanner();
      });
    }

    // Camera action buttons
    if (elements.startCameraBtn) {
      elements.startCameraBtn.addEventListener('click', startCameraScanner);
    }
    if (elements.stopCameraBtn) {
      elements.stopCameraBtn.addEventListener('click', stopCameraScanner);
    }

    // Staged Confirmation buttons
    if (elements.confirmScanBtn) {
      elements.confirmScanBtn.addEventListener('click', submitConfirmedScan);
    }
    if (elements.cancelScanBtn) {
      elements.cancelScanBtn.addEventListener('click', dismissStagedScan);
    }

    // Refresh button
    if (elements.refreshOrderBtn) {
      elements.refreshOrderBtn.addEventListener('click', () => {
        fetchOrder();
        fetchHistory();
      });
    }

    // Manual Quick Part Select sync
    if (elements.manualPartSelect && elements.manualVariantSelect && elements.manualQtyInput) {
      const syncManualPayload = () => {
        const part = elements.manualPartSelect.value;
        const variant = elements.manualVariantSelect.value;
        const qty = elements.manualQtyInput.value || 1;
        if (part && variant && qty && elements.manualPayloadInput) {
          elements.manualPayloadInput.value = `${part}|${variant}|${qty}`;
        }
      };
      elements.manualPartSelect.addEventListener('change', syncManualPayload);
      elements.manualVariantSelect.addEventListener('change', syncManualPayload);
      elements.manualQtyInput.addEventListener('input', syncManualPayload);
    }

    // Manual Form Submission
    if (elements.manualScanForm) {
      elements.manualScanForm.addEventListener('submit', (e) => {
        e.preventDefault();
        const raw = (elements.manualPayloadInput ? elements.manualPayloadInput.value : '').trim();
        if (!raw) {
          showNotification('Please enter a barcode string or pick part/variant.', 'warning');
          return;
        }
        // Stage manual scan for confirmation just like camera scan
        stageScan(raw);
      });
    }

    // Hardware Retry Form Submission (Phase 4B)
    const hwRetryForm = document.getElementById('hwRetryForm');
    if (hwRetryForm) {
      hwRetryForm.addEventListener('submit', async (e) => {
        e.preventDefault();

        const pin = document.getElementById('hwRetryPinInput') ? document.getElementById('hwRetryPinInput').value : '';
        const reason = document.getElementById('hwRetryReasonInput') ? document.getElementById('hwRetryReasonInput').value.trim() : '';
        const submitBtn = document.getElementById('hwRetrySubmitBtn');

        if (!pin) {
          showNotification('Supervisor PIN is required for hardware retry.', 'error');
          return;
        }
        if (!reason) {
          showNotification('A retry reason is required for hardware retry.', 'error');
          return;
        }

        if (submitBtn) {
          submitBtn.disabled = true;
          submitBtn.textContent = 'Authorizing\u2026';
        }

        try {
          const orderId = state.order ? state.order.packing_order_id : null;
          if (!orderId) {
            showNotification('Cannot determine current order ID. Refresh and try again.', 'error');
            return;
          }

          const res = await fetch('/api/hardware/retry', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              supervisor_pin: pin,
              attempt_order_id: orderId,
              reason: reason
            })
          });

          const data = await res.json();

          if (res.ok) {
            if (document.getElementById('hwRetryPinInput')) document.getElementById('hwRetryPinInput').value = '';
            if (document.getElementById('hwRetryReasonInput')) document.getElementById('hwRetryReasonInput').value = '';
            showNotification(
              'Hardware retry authorized. Command re-queued (attempt ' +
              data.retry_count + ' of ' + data.retry_max + ').',
              'success'
            );
            setTimeout(() => { fetchOrder(); }, 1200);
          } else if (res.status === 401) {
            showNotification('Unauthorized: Invalid supervisor PIN.', 'error');
          } else if (res.status === 409) {
            showNotification('Retry not allowed: ' + (data.error || 'Command not in UNCONFIRMED status.'), 'warning');
            fetchOrder();
          } else if (res.status === 429) {
            showNotification('Retry limit reached: ' + (data.error || 'Maximum retries exceeded.'), 'error');
            fetchOrder();
          } else {
            showNotification('Error: ' + (data.error || 'Hardware retry failed.'), 'error');
          }
        } catch (err) {
          console.error('Hardware retry network error:', err);
          showNotification('Network error during hardware retry. Please retry.', 'warning');
        } finally {
          if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.textContent = 'Authorize Hardware Retry';
          }
        }
      });
    }

    // Supervisor Reset Form Submission
    if (elements.supervisorResetForm) {
      elements.supervisorResetForm.addEventListener('submit', async (e) => {
        e.preventDefault();

        const pin = elements.spvPinInput ? elements.spvPinInput.value : '';
        const reason = elements.spvReasonInput ? elements.spvReasonInput.value.trim() : '';

        if (!pin) {
          showNotification('Supervisor PIN is required.', 'error');
          return;
        }
        if (!reason) {
          showNotification('Root-cause reason is required for supervisor reset.', 'error');
          return;
        }

        if (elements.spvSubmitBtn) {
          elements.spvSubmitBtn.disabled = true;
          elements.spvSubmitBtn.textContent = 'Authorizing...';
        }

        try {
          const res = await fetch('/api/supervisor/reset', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ supervisor_pin: pin, reason: reason })
          });

          const data = await res.json();

          if (res.ok) {
            // Clear form and hide supervisor section
            if (elements.spvPinInput) elements.spvPinInput.value = '';
            if (elements.spvReasonInput) elements.spvReasonInput.value = '';
            if (elements.supervisorSection) elements.supervisorSection.classList.add('hidden');

            // Refresh UI with new attempt state
            await fetchOrder();
            await fetchHistory();

            const newAttemptId = data.new_attempt_order_id || '';
            showNotification(
              'Supervisor reset authorized. New packing attempt started: ' + newAttemptId,
              'success'
            );
          } else if (res.status === 401) {
            showNotification('Unauthorized: Invalid supervisor PIN. Access denied.', 'error');
          } else {
            const errMsg = data.error || 'Supervisor reset failed with status ' + res.status;
            showNotification('Error: ' + errMsg, 'error');
          }
        } catch (err) {
          console.error('Supervisor reset network error:', err);
          showNotification('Network error during supervisor reset. Please retry.', 'warning');
        } finally {
          if (elements.spvSubmitBtn) {
            elements.spvSubmitBtn.disabled = false;
            elements.spvSubmitBtn.textContent = 'Authorize & Start Fresh Attempt';
          }
        }
      });
    }
  }

  function escapeHtml(str) {
    if (str === null || str === undefined) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  // Initialization
  document.addEventListener('DOMContentLoaded', () => {
    checkSecurityContext();
    setupEventListeners();
    fetchOrder();
    fetchHistory();
  });
})();
