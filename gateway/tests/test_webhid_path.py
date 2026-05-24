"""
Playwright tests for the WebHID fast-path code in static/index.html.

Mocks ``navigator.hid`` so the page logic can be exercised end-to-end
without a real USB device or browser support. Verifies:

  * Feature detection — Fast button visibility tracks navigator.hid presence.
  * Mode switch — clicking Fast attaches the transport, hides the QR canvas,
    and surfaces the fast-path overlay.
  * RX (Reticulum → kiosk) — bytes arriving on the WebSocket are tunneled
    raw via sendReport, chunked into 63-byte length-prefixed HID reports,
    and the QR canvas does NOT render.
  * TX (kiosk → Reticulum) — input reports dispatched by the mock device
    are unpacked and forwarded onto the WebSocket as raw bytes.

These tests are entirely host-side and require no firmware or hardware.
"""
import asyncio

import pytest
from playwright.async_api import async_playwright

from gateway import build_app
from tests.conftest import echo_server, start_app, STATIC


# ─── mock navigator.hid setup ────────────────────────────────────────────────
#
# Installed via page.add_init_script before any page script runs, so the
# page's `'hid' in navigator` check sees the mock and the WebHID code path
# initialises against it.

_HID_MOCK_JS = r"""
(() => {
  // A minimal mock that mimics enough of the WebHID API surface for the
  // page's WebHidTransport class.

  const sentReports = [];      // reportId 6 only
  const sentConfigReports = [];
  const enc = new TextEncoder();
  const configState = {
    configuredMode: 0,
    activeMode: 0,
    statusCode: 0,
    ssid: 'KioskDongle',
    pass: 'retiqrfast',
    peerConnected: true,
  };

  function buildConfigReport() {
    const report = new Uint8Array(63);
    const ssidBytes = enc.encode(configState.ssid);
    report[0] = 1;
    report[1] = configState.configuredMode;
    report[2] = configState.activeMode;
    report[3] = configState.statusCode;
    report[4] = ssidBytes.length;
    report.set(ssidBytes, 5);
    report[61] = configState.peerConnected ? 1 : 0;
    return report;
  }

  function dispatchConfigReport() {
    const evt = new Event('inputreport');
    evt.data = new DataView(buildConfigReport().buffer);
    evt.reportId = 7;
    mockDevice.dispatchEvent(evt);
  }

  class MockHIDDevice extends EventTarget {
    constructor() {
      super();
      this.opened       = false;
      this.vendorId     = 0x303A;
      this.productId    = 0xBEEF;
      this.productName  = 'MockKioskDongle';
      this.collections  = [{ usagePage: 0xFF00, usage: 0x01 }];
    }
    async open()  { this.opened = true; }
    async close() { this.opened = false; }
    async sendReport(reportId, data) {
      // The page passes a Uint8Array; clone so the test can inspect later.
      const u8 = data instanceof Uint8Array
        ? new Uint8Array(data)
        : new Uint8Array(data.buffer || data);
      if (reportId === 6) {
        sentReports.push({ reportId, data: Array.from(u8) });
        return;
      }
      if (reportId !== 7) throw new Error('unexpected report id');
      sentConfigReports.push({ reportId, data: Array.from(u8) });
      switch (u8[0]) {
        case 1:
          dispatchConfigReport();
          break;
        case 2: {
          const mode = u8[1];
          const ssidLen = u8[2];
          configState.configuredMode = mode;
          configState.ssid = new TextDecoder().decode(u8.slice(3, 3 + ssidLen));
          configState.statusCode = 0;
          break;
        }
        case 3: {
          const passLen = u8[1];
          configState.pass = new TextDecoder().decode(u8.slice(2, 2 + passLen));
          configState.statusCode = 0;
          break;
        }
        case 4:
          configState.activeMode = configState.configuredMode;
          configState.statusCode = 0;
          break;
        case 5:
          configState.configuredMode = 0;
          configState.activeMode = 0;
          configState.ssid = 'KioskDongle';
          configState.pass = 'retiqrfast';
          configState.statusCode = 0;
          break;
        default:
          configState.statusCode = 1;
      }
    }
  }

  const mockDevice = new MockHIDDevice();

  const mockHid = {
    async requestDevice(_opts) { return [mockDevice]; },
    async getDevices()         { return []; },  // simulate "no prior grant"
    addEventListener()         {},
    removeEventListener()      {},
  };

  Object.defineProperty(navigator, 'hid', { value: mockHid, configurable: true });

  // Expose handles the test can use to drive the mock.
  window.__mockHidTest = {
    sentReports,
    sentConfigReports,
    /** Dispatch a fake input report to the page. */
    pushInputReport(payloadArray) {
      const reportSize = 63;
      const buf = new Uint8Array(reportSize);
      buf[0] = payloadArray.length;
      for (let i = 0; i < payloadArray.length; i++) buf[1 + i] = payloadArray[i];
      const evt = new Event('inputreport');
      evt.data = new DataView(buf.buffer);
      evt.reportId = 6;
      mockDevice.dispatchEvent(evt);
    },
    /** Number of sendReport calls so far. */
    sentCount() { return sentReports.length; },
    /** Concatenated payload bytes (length-prefix stripped) from sent reports. */
    sentPayload() {
      const all = [];
      for (const r of sentReports) {
        const n = r.data[0];
        for (let i = 0; i < n; i++) all.push(r.data[1 + i]);
      }
      return all;
    },
    sentConfigCount() { return sentConfigReports.length; },
  };
})();
"""


# ─── additional init scripts ─────────────────────────────────────────────────

# Installs navigator.hid but makes requestDevice() reject (permission denied).
_HID_PERMISSION_DENIED_JS = r"""
(() => {
  Object.defineProperty(navigator, 'hid', {
    value: {
      async requestDevice(_opts) { throw new DOMException('Permission denied', 'SecurityError'); },
      async getDevices()         { return []; },
      addEventListener()         {},
      removeEventListener()      {},
    },
    configurable: true,
  });
})();
"""

# Simulates a browser with no WebHID support.
# The page gates the Fast button on `'hid' in navigator && window.isSecureContext`.
# In Chromium, navigator.hid is non-deletable, so we can't remove it from the
# prototype chain; instead we flip isSecureContext to false, which is the other
# half of the gate and achieves the same result.
_HID_UNAVAILABLE_JS = r"""
(() => {
  Object.defineProperty(window, 'isSecureContext', { value: false, configurable: true });
})();
"""


# ─── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
async def gateway_and_echo(event_loop):
    """Spin up a gateway pointed at an echo server. Yields (gateway_url, echo)."""
    echo, tcp_port = await echo_server()
    runner, gw_port = await start_app(build_app("127.0.0.1", tcp_port, STATIC))
    yield f"http://127.0.0.1:{gw_port}", echo
    await runner.cleanup()
    echo.close()
    await echo.wait_closed()


@pytest.fixture
async def browser():
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        yield b
        await b.close()


@pytest.fixture
async def page_with_mock(gateway_and_echo, browser):
    """A page with the mock navigator.hid installed, navigated to the gateway."""
    gw_url, _echo = gateway_and_echo
    ctx = await browser.new_context(viewport={"width": 1280, "height": 800})
    await ctx.add_init_script(_HID_MOCK_JS)
    p = await ctx.new_page()
    await p.goto(gw_url)
    await p.wait_for_timeout(800)   # let WS handshake complete
    yield p
    await p.close()
    await ctx.close()


# ─── tests ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fast_button_visible_when_hid_present(page_with_mock):
    """The Fast pill is only displayed when navigator.hid is present."""
    visible = await page_with_mock.evaluate(
        "document.getElementById('fast-btn').classList.contains('available')"
    )
    assert visible is True


@pytest.mark.asyncio
async def test_clicking_fast_attaches_transport_and_hides_qr(page_with_mock):
    await page_with_mock.click("#fast-btn")
    await page_with_mock.wait_for_timeout(150)

    # Page state — transport set, fast button shows active style, QR hidden.
    state = await page_with_mock.evaluate("""({
        hasTransport:    !!transport,
        btnActive:       document.getElementById('fast-btn').classList.contains('active'),
        overlayActive:   document.getElementById('webhid-area').classList.contains('active'),
        canvasDisplay:   document.getElementById('qr-canvas').style.display,
        deviceLabel:     document.getElementById('webhid-device').textContent,
    })""")
    assert state["hasTransport"]    is True
    assert state["btnActive"]       is True
    assert state["overlayActive"]   is True
    assert state["canvasDisplay"]   == "none"
    assert state["deviceLabel"]     == "MockKioskDongle"


@pytest.mark.asyncio
async def test_fast_config_panel_uses_config_reports(page_with_mock):
    await page_with_mock.click("#fast-btn")
    await page_with_mock.wait_for_timeout(250)

    state = await page_with_mock.evaluate("""({
        peerMode: document.getElementById('peer-mode-select').value,
        ssid: document.getElementById('wifi-ssid-input').value,
        status: document.getElementById('webhid-config-status').textContent,
    })""")
    assert state["peerMode"] == "ble"
    assert state["ssid"] == "KioskDongle"
    assert "Configured ble" in state["status"]

    await page_with_mock.select_option("#peer-mode-select", "wifi-ap")
    await page_with_mock.fill("#wifi-ssid-input", "dongletest")
    await page_with_mock.fill("#wifi-pass-input", "newpassword")
    await page_with_mock.click("#peer-apply-btn")
    await page_with_mock.wait_for_timeout(150)

    config_reports = await page_with_mock.evaluate(
        "() => window.__mockHidTest.sentConfigReports"
    )
    assert len(config_reports) >= 4
    get_status, stage_mode, stage_pass, apply = config_reports[:4]
    assert get_status["data"][0] == 1
    assert stage_mode["data"][0] == 2
    assert stage_mode["data"][1] == 1
    assert stage_mode["data"][2] == len("dongletest")
    assert stage_pass["data"][0] == 3
    assert stage_pass["data"][1] == len("newpassword")
    assert apply["data"][0] == 4


@pytest.mark.asyncio
async def test_input_report_forwards_to_websocket(gateway_and_echo, page_with_mock):
    """An input report from the mock dongle ends up echoed back from Reticulum."""
    # Activate fast path.
    await page_with_mock.click("#fast-btn")
    await page_with_mock.wait_for_timeout(150)

    # In standard mode the page would HDLC-decode WS bytes → render QR.
    # In fast mode it forwards them raw, so we send already-HDLC bytes
    # via the mock input report and expect them to come back to the page
    # untouched (since the gateway just echoes them).
    payload = [0x7E, 0x10, 0x20, 0x30, 0x7E]   # one HDLC frame around \x10\x20\x30
    await page_with_mock.evaluate(
        "(arr) => window.__mockHidTest.pushInputReport(arr)", payload
    )
    # After a short wait, the page should have sent these to the WebSocket,
    # the gateway forwarded them to the echo server, the echo bounced them
    # back, and the page sent a sendReport of the same bytes back to the
    # mock device.
    await page_with_mock.wait_for_timeout(400)

    sent_payload = await page_with_mock.evaluate(
        "() => window.__mockHidTest.sentPayload()"
    )
    assert sent_payload == payload


@pytest.mark.asyncio
async def test_outbound_chunking_uses_63_byte_payloads(page_with_mock):
    """sendBytes splits the stream into 63-byte reports with length byte then ≤62 payload."""
    await page_with_mock.click("#fast-btn")
    await page_with_mock.wait_for_timeout(150)

    # Push a 150-byte HDLC-ish payload at the page via the mock input port.
    # The gateway will echo it back, so we'll see the page's sendReport calls.
    payload = list(range(150))
    await page_with_mock.evaluate(
        "(arr) => window.__mockHidTest.pushInputReport(arr.slice(0, 62))", payload
    )
    await page_with_mock.evaluate(
        "(arr) => window.__mockHidTest.pushInputReport(arr.slice(62, 124))", payload
    )
    await page_with_mock.evaluate(
        "(arr) => window.__mockHidTest.pushInputReport(arr.slice(124))", payload
    )
    await page_with_mock.wait_for_timeout(400)

    reports = await page_with_mock.evaluate(
        "() => window.__mockHidTest.sentReports"
    )
    assert len(reports) >= 3
    # Each report payload length must be encoded in its first byte and the
    # whole descriptor-level report must be 63 bytes long.
    for r in reports:
        assert len(r["data"]) == 63
        n = r["data"][0]
        assert 0 <= n <= 62


# ─── fallback / unavailability tests ─────────────────────────────────────────

@pytest.fixture
async def page_no_hid(gateway_and_echo, browser):
    """A page where navigator.hid is absent (WebHID not supported)."""
    gw_url, _echo = gateway_and_echo
    ctx = await browser.new_context(viewport={"width": 1280, "height": 800})
    await ctx.add_init_script(_HID_UNAVAILABLE_JS)
    p = await ctx.new_page()
    await p.goto(gw_url)
    await p.wait_for_timeout(800)
    yield p
    await p.close()
    await ctx.close()


@pytest.fixture
async def page_hid_denied(gateway_and_echo, browser):
    """A page where navigator.hid exists but requestDevice() rejects."""
    gw_url, _echo = gateway_and_echo
    ctx = await browser.new_context(viewport={"width": 1280, "height": 800})
    await ctx.add_init_script(_HID_PERMISSION_DENIED_JS)
    p = await ctx.new_page()
    await p.goto(gw_url)
    await p.wait_for_timeout(800)
    yield p
    await p.close()
    await ctx.close()


@pytest.mark.asyncio
async def test_fast_button_hidden_when_hid_absent(page_no_hid):
    """Fast pill must not be shown when the browser has no WebHID support."""
    visible = await page_no_hid.evaluate(
        "document.getElementById('fast-btn').classList.contains('available')"
    )
    assert visible is False


@pytest.mark.asyncio
async def test_standard_mode_active_when_hid_absent(page_no_hid):
    """QR canvas must be visible and the page stays in standard mode."""
    state = await page_no_hid.evaluate("""({
        canvasDisplay: getComputedStyle(document.getElementById('qr-canvas')).display,
        transport:     typeof transport !== 'undefined' ? transport : null,
    })""")
    assert state["canvasDisplay"] != "none"
    assert state["transport"] is None


@pytest.mark.asyncio
async def test_permission_denied_leaves_standard_mode_intact(page_hid_denied):
    """Clicking Fast when requestDevice() rejects must not break standard mode."""
    await page_hid_denied.click("#fast-btn")
    await page_hid_denied.wait_for_timeout(300)

    state = await page_hid_denied.evaluate("""({
        transport:   typeof transport !== 'undefined' ? transport : null,
        btnActive:   document.getElementById('fast-btn').classList.contains('active'),
        qrVisible:   document.getElementById('qr-canvas').style.display !== 'none',
    })""")
    # Transport must not have been set — no device was granted.
    assert state["transport"] is None
    assert state["btnActive"] is False
    # Standard QR path must still be running.
    assert state["qrVisible"] is True


@pytest.mark.asyncio
async def test_splice_stats_message_received(page_with_mock):
    """Gateway sends splice_stats JSON text frames; page must not crash on them."""
    errors: list[str] = []
    page_with_mock.on("pageerror", lambda e: errors.append(str(e)))

    # Wait long enough for at least one stats tick (_STATS_INTERVAL_SECS = 1 s).
    await page_with_mock.wait_for_timeout(1400)

    assert errors == [], f"page errors after splice_stats: {errors}"
