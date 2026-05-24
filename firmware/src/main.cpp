/*
 * KioskDongle — user-link → kiosk USB HID bridge (dual-path)
 *
 * Target: M5Stack AtomS3 Lite (ESP32-S3FN8)
 *
 * Two USB HID interfaces are exposed as a composite device:
 *   1. Keyboard (standard, report ID 1) — types ASCII frames into a focused
 *      textarea on the kiosk page. Works on any browser, locked-down or not.
 *      Slow (~500 B/s) but the only path that survives without WebHID.
 *   2. Vendor HID (fast path, report ID 6, usage page 0xFF00) — bidirectional
 *      raw binary reports with 63-byte report bodies. Requires a kiosk browser with
 *      WebHID support that has been granted access to the dongle.
 *      Tens of KiB/s, full duplex, limited mostly by full-speed HID cadence.
 *
 * Pinout
 *   GPIO 35  SK6812 RGB LED
 *   GPIO 41  button (active-low, internal pull-up)
 *   GPIO  5  Serial1 TX  (exposed pad G5)
 *   GPIO  6  Serial1 RX  (exposed pad G6)
 *   USB-C    native ESP32-S3 USB OTG (composite HID: keyboard + vendor)
 *
 * User-side link modes:
 *   1. BLE GATT — current desktop-client path.
 *   2. Wi-Fi SoftAP + TCP server (configurable via WebHID config report).
 *      This bypasses the desktop BLE client and feeds the fast WebHID
 *      transport directly from a single TCP socket.
 *
 * BLE GATT service (UUIDs must match client/tx/ble.py):
 *   Service     4b696f73-6b55-0001-0000-000000000000
 *   TX char     4b696f73-6b55-0002-0000-000000000000  WRITE_NR  (standard: typed)
 *   CFG char    4b696f73-6b55-0003-0000-000000000000  WRITE     (key delay)
 *   WHID-RX     4b696f73-6b55-0004-0000-000000000000  NOTIFY    (vendor output -> laptop)
 *   WHID-TX     4b696f73-6b55-0005-0000-000000000000  WRITE_NR  (laptop -> vendor input)
 */

#include <Arduino.h>
#include <NimBLEDevice.h>
#include <Preferences.h>
#include <WiFi.h>
#include <USB.h>
#include <USBHID.h>
#include <USBHIDKeyboard.h>
#include <Adafruit_NeoPixel.h>
#include <atomic>

// ── pins ───────────────────────────────────────────────────────────────────
static constexpr uint8_t  PIN_LED       = 35;
static constexpr uint8_t  PIN_BTN       = 41;
static constexpr uint8_t  SERIAL1_TX_PIN =  5;
static constexpr uint8_t  SERIAL1_RX_PIN =  6;

// ── user-link mode selection ────────────────────────────────────────────────
enum PeerMode : uint8_t { PEER_MODE_BLE, PEER_MODE_WIFI_AP };
static constexpr PeerMode DEFAULT_PEER_MODE = PEER_MODE_BLE;

static constexpr uint16_t WIFI_TCP_PORT         = 4243;
static constexpr size_t   WIFI_SSID_MAX_LEN     = 32;
static constexpr size_t   WIFI_PASS_MAX_LEN     = 63;
static constexpr char     DEFAULT_WIFI_AP_SSID[] = "KioskDongle";
static constexpr char     DEFAULT_WIFI_AP_PASS[] = "retiqrfast";
static constexpr uint8_t  WIFI_AP_CHANNEL       = 6;
static constexpr uint8_t  WIFI_AP_MAX_CLIENTS   = 1;
// Read Wi-Fi TCP in a whole-number multiple of HID payloads so the fast path
// starts from cleaner chunk boundaries before the packer merges short tails.
static constexpr uint16_t WIFI_READ_BUF_SIZE    = 496;  // 8 * 62-byte payloads
static constexpr uint32_t WIFI_PACK_FLUSH_MS    = 1;
static const IPAddress    WIFI_AP_IP(10, 77, 0, 1);
static const IPAddress    WIFI_AP_GW(10, 77, 0, 1);
static const IPAddress    WIFI_AP_MASK(255, 255, 255, 0);
static char               wifiApSsid[WIFI_SSID_MAX_LEN + 1] = {0};
static char               wifiApPass[WIFI_PASS_MAX_LEN + 1] = {0};

// ── BLE identity ───────────────────────────────────────────────────────────
static const char* SVC_UUID      = "4b696f73-6b55-0001-0000-000000000000";
static const char* TX_UUID       = "4b696f73-6b55-0002-0000-000000000000";
static const char* CFG_UUID      = "4b696f73-6b55-0003-0000-000000000000";
static const char* WHID_RX_UUID  = "4b696f73-6b55-0004-0000-000000000000";
static const char* WHID_TX_UUID  = "4b696f73-6b55-0005-0000-000000000000";
static const char* DEVICE_NAME   = "KioskDongle";
static constexpr uint8_t HID_REPORT_ID_CONFIG = HID_REPORT_ID_VENDOR + 1;
static constexpr uint8_t CONFIG_REPORT_VERSION = 1;
static constexpr size_t  CONFIG_REPORT_SIZE = 63;

enum ConfigCommand : uint8_t {
    CONFIG_CMD_NONE = 0,
    CONFIG_CMD_GET_STATUS = 1,
    CONFIG_CMD_STAGE_MODE_SSID = 2,
    CONFIG_CMD_STAGE_PASSWORD = 3,
    CONFIG_CMD_APPLY = 4,
    CONFIG_CMD_RESET_DEFAULTS = 5,
};

enum ConfigStatusCode : uint8_t {
    CONFIG_STATUS_OK = 0,
    CONFIG_STATUS_BAD_COMMAND = 1,
    CONFIG_STATUS_BAD_MODE = 2,
    CONFIG_STATUS_BAD_SSID = 3,
    CONFIG_STATUS_BAD_PASSWORD = 4,
};

static constexpr uint8_t CONFIG_FLAG_PEER_CONNECTED = 0x01;
static constexpr uint8_t CONFIG_FLAG_BLE_CONNECTED  = 0x02;
static constexpr uint8_t CONFIG_FLAG_WIFI_ACTIVE    = 0x04;

static constexpr char PREFS_NAMESPACE[] = "retiqr";
static constexpr char PREF_KEY_PEER_MODE[] = "peer_mode";
static constexpr char PREF_KEY_WIFI_SSID[] = "wifi_ssid";
static constexpr char PREF_KEY_WIFI_PASS[] = "wifi_pass";

// ── vendor-HID descriptor (kiosk page side) ────────────────────────────────
// Usage page 0xFF00 — passes the kiosk USB-class filter as a generic HID
// device, but is NOT keyboard/mouse/gamepad/sec-key so it's accessible to
// WebHID (none of the browser's protected-usage blocks apply).
//
// Report ID 6 carries the fast-path Input/Output stream with 63 data bytes at
// the descriptor level. TinyUSB prepends the Report ID onto the wire, so this
// stays within the framework's 64-byte endpoint buffer.
//
// Report ID 7 is a low-rate config/control report. It uses the same WebHID
// sendReport/inputreport path as the fast stream, but on its own report ID so
// settings traffic never collides with the raw data channel.
static const uint8_t hidVendorDesc[] = {
    0x06, 0x00, 0xFF,          // Usage Page (Vendor-Defined 0xFF00)
    0x09, 0x01,                // Usage (0x01 — vendor application)
    0xA1, 0x01,                // Collection (Application)
    0x85, HID_REPORT_ID_VENDOR,//   Report ID (6)

    // Input report body — 63 bytes, dongle -> kiosk
    0x09, 0x02,                //   Usage (0x02 — input)
    0x15, 0x00,                //   Logical Minimum (0)
    0x26, 0xFF, 0x00,          //   Logical Maximum (255)
    0x75, 0x08,                //   Report Size (8)
    0x95, 0x3F,                //   Report Count (63) — TinyUSB EP buffer
                               //   is 64 bytes including Report ID, so we
                               //   can only carry 63 data bytes per report
    0x81, 0x02,                //   Input (Data, Var, Abs)

    // Output report body — 63 bytes, kiosk -> dongle
    0x09, 0x03,                //   Usage (0x03 — output)
    0x91, 0x02,                //   Output (Data, Var, Abs)

    // Config report body — 63 bytes, bidirectional control/status
    0x85, HID_REPORT_ID_CONFIG,//   Report ID (7)
    0x09, 0x04,                //   Usage (0x04 — config input)
    0x15, 0x00,                //   Logical Minimum (0)
    0x26, 0xFF, 0x00,          //   Logical Maximum (255)
    0x75, 0x08,                //   Report Size (8)
    0x95, 0x3F,                //   Report Count (63)
    0x81, 0x02,                //   Input (Data, Var, Abs)
    0x09, 0x05,                //   Usage (0x05 — config output)
    0x91, 0x02,                //   Output (Data, Var, Abs)

    0xC0                       // End Collection
};

static constexpr size_t VENDOR_REPORT_SIZE = 63;

// ── frame queue ────────────────────────────────────────────────────────────
// Max Reticulum packet ~500 bytes → HID frame = 1 + 500*2 + 2 + 1 = 1004 bytes.
static constexpr uint16_t FRAME_MAX   = 1024;
static constexpr uint8_t  QUEUE_DEPTH =    4;
// Fast-path vendor-HID queues are sized larger than the standard keyboard
// frame queue: each entry is one 63-byte report and bursts from the kiosk
// (4-KB WS message = 67 reports) overrun a tiny queue before the BLE
// notify or USB IN side can drain it. 128 entries × ~70 bytes ≈ 9 KB total.
static constexpr uint16_t WHID_QUEUE_DEPTH = 128;
// When the queue fills, xQueueSend blocks for this long instead of dropping
// immediately. In a USB-task context the OUT endpoint NAKs new transfers
// for the duration, which gives the kernel/host time to back off; in the
// BLE-task context it just lets the loop() drain more before we drop.
static constexpr uint32_t WHID_QUEUE_BLOCK_MS = 20;
// BLE connection parameter request. Interval units are 1.25 ms, so this asks
// for a 7.5–15 ms range and leaves the central some room to choose.
static constexpr uint16_t BLE_CONN_ITVL_MIN_UNITS = 6;
static constexpr uint16_t BLE_CONN_ITVL_MAX_UNITS = 12;
static constexpr uint16_t BLE_CONN_LATENCY        = 0;
static constexpr uint16_t BLE_CONN_TIMEOUT        = 200;  // 2 s, in 10 ms units

struct Frame {
    uint8_t  data[FRAME_MAX];
    uint16_t len;
};

static QueueHandle_t     frameQueue;
static volatile uint16_t keyDelayMs  = 5;
static volatile bool     bleConnected = false;
static volatile bool     peerConnected = false;
static PeerMode          configuredPeerMode = DEFAULT_PEER_MODE;
static PeerMode          peerMode = PEER_MODE_BLE;

struct PendingPeerConfig {
    ConfigCommand command;
    PeerMode      mode;
    char          ssid[WIFI_SSID_MAX_LEN + 1];
    char          pass[WIFI_PASS_MAX_LEN + 1];
};

static PendingPeerConfig pendingPeerConfig = {
    CONFIG_CMD_NONE,
    DEFAULT_PEER_MODE,
    {0},
    {0},
};
static std::atomic<bool>    pendingPeerConfigDirty{false};
static std::atomic<uint8_t> lastConfigStatus{CONFIG_STATUS_OK};
static std::atomic<bool>    configStatusRequested{false};

// Reassembly buffer — touched only from the NimBLE task.
static uint8_t  reassemBuf[FRAME_MAX];
static uint16_t reassemLen = 0;

// ── vendor-HID queues ──────────────────────────────────────────────────────
// Two fixed-size 63-byte report-body queues, one per direction. Both are populated
// in callback contexts (BLE for whidTx, USB for whidRx) and drained from
// loop() so the heavy work (USB SendReport, BLE notify) never runs in a
// callback where it might deadlock the radio stack.

struct VendorReport {
    uint8_t data[VENDOR_REPORT_SIZE];
    uint8_t len;   // 0..VENDOR_REPORT_SIZE; 0 reports are heartbeats, dropped
};

static QueueHandle_t whidTxQueue;     // BLE -> USB Input Report  (laptop -> kiosk)
static QueueHandle_t whidRxQueue;     // USB Output Report -> BLE (kiosk -> laptop)

// Partial 63-byte report body being reassembled from BLE writes. The laptop
// chunks each report body into ATT writes; the dongle accumulates until a
// full report body is in hand, then queues it for USB transmission.
static uint8_t  whidTxAccum[VENDOR_REPORT_SIZE];
static size_t   whidTxAccumLen = 0;

// Diagnostic counters for the kiosk -> laptop fast path. These stay read-only
// and out of band so we can measure where loss occurs without perturbing the
// report stream itself.
static std::atomic<uint32_t> whidRxEnqueueOk{0};
static std::atomic<uint32_t> whidRxEnqueueDrop{0};
static std::atomic<uint32_t> whidRxDequeued{0};
static std::atomic<uint32_t> whidRxNotifyAttempt{0};
static std::atomic<uint32_t> whidRxNotifyOk{0};
static std::atomic<uint32_t> whidRxNotifyErr{0};
static std::atomic<uint32_t> whidRxQueueHighWater{0};
static WiFiServer wifiServer(WIFI_TCP_PORT, WIFI_AP_MAX_CLIENTS);
static WiFiClient wifiClient;
static std::atomic<uint32_t> wifiTcpRxBytes{0};
static std::atomic<uint32_t> wifiTcpReadCalls{0};
static std::atomic<uint32_t> wifiRxPendingHighWater{0};
static std::atomic<uint32_t> whidTxEnqueueOk{0};
static std::atomic<uint32_t> whidTxEnqueueErr{0};
static std::atomic<uint32_t> whidTxDequeued{0};
static std::atomic<uint32_t> whidTxSendOk{0};
static std::atomic<uint32_t> whidTxSendErr{0};
static std::atomic<uint32_t> whidTxQueueHighWater{0};
static std::atomic<uint32_t> whidTxPayloadBytesIn{0};
static std::atomic<uint32_t> whidTxPayloadBytesOut{0};
static std::atomic<uint64_t> whidTxSendMicros{0};
static uint8_t wifiRxPending[WIFI_READ_BUF_SIZE];
static size_t  wifiRxPendingLen = 0;
static size_t  wifiRxPendingOff = 0;
static uint8_t wifiUsbPack[VENDOR_REPORT_SIZE - 1];
static size_t  wifiUsbPackLen = 0;
static uint32_t wifiUsbPackStartedAt = 0;
static uint8_t wifiTxPending[VENDOR_REPORT_SIZE];
static size_t  wifiTxPendingLen = 0;
static size_t  wifiTxPendingOff = 0;

static const char* peerModeName(PeerMode mode);
static void setPeerConnected(bool connected);
static bool isValidPeerMode(uint8_t mode);
static void copyBoundedString(char* dst, size_t dstSize, const char* src, const char* fallback = "");
static void loadPersistedPeerConfig();
static bool storePersistedPeerConfig(PeerMode mode, const char* ssid, const char* pass);
static bool resetPersistedPeerConfig();
static bool buildConfigReport(uint8_t* buffer, size_t len);
static bool handleConfigOutputReport(const uint8_t* buffer, uint16_t len);

static bool isValidPeerMode(uint8_t mode) {
    return mode == PEER_MODE_BLE || mode == PEER_MODE_WIFI_AP;
}

static void copyBoundedString(char* dst, size_t dstSize, const char* src, const char* fallback) {
    if (dstSize == 0)
        return;
    const char* use = (src && src[0]) ? src : fallback;
    if (!use)
        use = "";
    strncpy(dst, use, dstSize - 1);
    dst[dstSize - 1] = '\0';
}

static void loadPersistedPeerConfig() {
    configuredPeerMode = DEFAULT_PEER_MODE;
    copyBoundedString(wifiApSsid, sizeof(wifiApSsid), DEFAULT_WIFI_AP_SSID, DEFAULT_WIFI_AP_SSID);
    copyBoundedString(wifiApPass, sizeof(wifiApPass), DEFAULT_WIFI_AP_PASS, DEFAULT_WIFI_AP_PASS);

    Preferences prefs;
    if (!prefs.begin(PREFS_NAMESPACE, true))
        return;

    uint8_t storedMode = prefs.getUChar(PREF_KEY_PEER_MODE, static_cast<uint8_t>(DEFAULT_PEER_MODE));
    if (isValidPeerMode(storedMode))
        configuredPeerMode = static_cast<PeerMode>(storedMode);

    copyBoundedString(
        wifiApSsid,
        sizeof(wifiApSsid),
        prefs.getString(PREF_KEY_WIFI_SSID, DEFAULT_WIFI_AP_SSID).c_str(),
        DEFAULT_WIFI_AP_SSID);
    copyBoundedString(
        wifiApPass,
        sizeof(wifiApPass),
        prefs.getString(PREF_KEY_WIFI_PASS, DEFAULT_WIFI_AP_PASS).c_str(),
        DEFAULT_WIFI_AP_PASS);
    prefs.end();

    pendingPeerConfig.command = CONFIG_CMD_NONE;
    pendingPeerConfig.mode = configuredPeerMode;
    copyBoundedString(pendingPeerConfig.ssid, sizeof(pendingPeerConfig.ssid), wifiApSsid, DEFAULT_WIFI_AP_SSID);
    copyBoundedString(pendingPeerConfig.pass, sizeof(pendingPeerConfig.pass), wifiApPass, DEFAULT_WIFI_AP_PASS);
}

static bool storePersistedPeerConfig(PeerMode mode, const char* ssid, const char* pass) {
    Preferences prefs;
    if (!prefs.begin(PREFS_NAMESPACE, false))
        return false;
    bool ok =
        prefs.putUChar(PREF_KEY_PEER_MODE, static_cast<uint8_t>(mode)) > 0 &&
        prefs.putString(PREF_KEY_WIFI_SSID, ssid) > 0 &&
        prefs.putString(PREF_KEY_WIFI_PASS, pass) > 0;
    prefs.end();
    return ok;
}

static bool resetPersistedPeerConfig() {
    Preferences prefs;
    if (!prefs.begin(PREFS_NAMESPACE, false))
        return false;
    bool ok =
        prefs.remove(PREF_KEY_PEER_MODE) &&
        prefs.remove(PREF_KEY_WIFI_SSID) &&
        prefs.remove(PREF_KEY_WIFI_PASS);
    prefs.end();
    return ok;
}

static bool buildConfigReport(uint8_t* buffer, size_t len) {
    constexpr size_t SSID_OFFSET = 5;
    if (buffer == nullptr || len < CONFIG_REPORT_SIZE)
        return false;

    memset(buffer, 0, CONFIG_REPORT_SIZE);
    const size_t ssidLen = strnlen(wifiApSsid, WIFI_SSID_MAX_LEN);

    buffer[0] = CONFIG_REPORT_VERSION;
    buffer[1] = static_cast<uint8_t>(configuredPeerMode);
    buffer[2] = static_cast<uint8_t>(peerMode);
    buffer[3] = lastConfigStatus.load(std::memory_order_relaxed);
    buffer[4] = static_cast<uint8_t>(ssidLen);
    memcpy(buffer + SSID_OFFSET, wifiApSsid, ssidLen);
    buffer[61] =
        (peerConnected ? CONFIG_FLAG_PEER_CONNECTED : 0) |
        (bleConnected ? CONFIG_FLAG_BLE_CONNECTED : 0) |
        (peerMode == PEER_MODE_WIFI_AP ? CONFIG_FLAG_WIFI_ACTIVE : 0);
    return true;
}

static bool handleConfigOutputReport(const uint8_t* buffer, uint16_t len) {
    if (buffer == nullptr || len == 0) {
        lastConfigStatus.store(CONFIG_STATUS_BAD_COMMAND, std::memory_order_relaxed);
        return false;
    }

    const uint8_t command = buffer[0];
    switch (command) {
        case CONFIG_CMD_GET_STATUS:
            configStatusRequested.store(true, std::memory_order_release);
            lastConfigStatus.store(CONFIG_STATUS_OK, std::memory_order_relaxed);
            return true;

        case CONFIG_CMD_STAGE_MODE_SSID: {
            if (len < 4) {
                lastConfigStatus.store(CONFIG_STATUS_BAD_COMMAND, std::memory_order_relaxed);
                return false;
            }
            const uint8_t nextMode = buffer[1];
            const uint8_t ssidLen = buffer[2];
            if (!isValidPeerMode(nextMode)) {
                lastConfigStatus.store(CONFIG_STATUS_BAD_MODE, std::memory_order_relaxed);
                return false;
            }
            if (ssidLen == 0 || ssidLen > WIFI_SSID_MAX_LEN || 3u + ssidLen > len) {
                lastConfigStatus.store(CONFIG_STATUS_BAD_SSID, std::memory_order_relaxed);
                return false;
            }
            pendingPeerConfig.command = CONFIG_CMD_APPLY;
            pendingPeerConfig.mode = static_cast<PeerMode>(nextMode);
            memcpy(pendingPeerConfig.ssid, buffer + 3, ssidLen);
            pendingPeerConfig.ssid[ssidLen] = '\0';
            lastConfigStatus.store(CONFIG_STATUS_OK, std::memory_order_relaxed);
            return true;
        }

        case CONFIG_CMD_STAGE_PASSWORD: {
            if (len < 3) {
                lastConfigStatus.store(CONFIG_STATUS_BAD_COMMAND, std::memory_order_relaxed);
                return false;
            }
            const uint8_t passLen = buffer[1];
            if (passLen < 8 || passLen > WIFI_PASS_MAX_LEN || 2u + passLen > len) {
                lastConfigStatus.store(CONFIG_STATUS_BAD_PASSWORD, std::memory_order_relaxed);
                return false;
            }
            memcpy(pendingPeerConfig.pass, buffer + 2, passLen);
            pendingPeerConfig.pass[passLen] = '\0';
            lastConfigStatus.store(CONFIG_STATUS_OK, std::memory_order_relaxed);
            return true;
        }

        case CONFIG_CMD_APPLY:
            pendingPeerConfig.command = CONFIG_CMD_APPLY;
            pendingPeerConfigDirty.store(true, std::memory_order_release);
            lastConfigStatus.store(CONFIG_STATUS_OK, std::memory_order_relaxed);
            return true;

        case CONFIG_CMD_RESET_DEFAULTS:
            pendingPeerConfig.command = CONFIG_CMD_RESET_DEFAULTS;
            pendingPeerConfig.mode = DEFAULT_PEER_MODE;
            copyBoundedString(pendingPeerConfig.ssid, sizeof(pendingPeerConfig.ssid), DEFAULT_WIFI_AP_SSID, DEFAULT_WIFI_AP_SSID);
            copyBoundedString(pendingPeerConfig.pass, sizeof(pendingPeerConfig.pass), DEFAULT_WIFI_AP_PASS, DEFAULT_WIFI_AP_PASS);
            pendingPeerConfigDirty.store(true, std::memory_order_release);
            lastConfigStatus.store(CONFIG_STATUS_OK, std::memory_order_relaxed);
            return true;

        default:
            lastConfigStatus.store(CONFIG_STATUS_BAD_COMMAND, std::memory_order_relaxed);
            return false;
    }
}

static void noteHighWater(std::atomic<uint32_t>& highWater, uint32_t want) {
    uint32_t cur = highWater.load(std::memory_order_relaxed);
    while (want > cur &&
           !highWater.compare_exchange_weak(
               cur, want, std::memory_order_relaxed, std::memory_order_relaxed)) {
    }
}

static void noteWhidTxQueueDepth(UBaseType_t depth) {
    noteHighWater(whidTxQueueHighWater, static_cast<uint32_t>(depth));
}

static void noteWifiRxPendingDepth(size_t depth) {
    noteHighWater(wifiRxPendingHighWater, static_cast<uint32_t>(depth));
}

static bool hasActiveWifiClient() {
    return wifiClient.fd() >= 0;
}

static bool hasWifiRxPending() {
    return wifiRxPendingOff < wifiRxPendingLen;
}

static bool hasWifiUsbPackPending() {
    return wifiUsbPackLen > 0;
}

static void resetWifiTxPending() {
    wifiTxPendingLen = 0;
    wifiTxPendingOff = 0;
}

static void resetWifiUsbPack() {
    wifiUsbPackLen = 0;
    wifiUsbPackStartedAt = 0;
}

static void resetWifiRxPending() {
    wifiRxPendingLen = 0;
    wifiRxPendingOff = 0;
}

static uint32_t dropWhidRxQueue() {
    uint32_t dropped = 0;
    VendorReport r;
    while (xQueueReceive(whidRxQueue, &r, 0) == pdTRUE)
        dropped++;
    return dropped;
}

static bool wifiUplinkIdle() {
    return !hasWifiRxPending() &&
           !hasWifiUsbPackPending() &&
           uxQueueMessagesWaiting(whidTxQueue) == 0;
}

static void closeWifiClient(const char* reason) {
    uint32_t dropped = dropWhidRxQueue();
    if (wifiTxPendingLen > wifiTxPendingOff)
        dropped++;

    if (dropped > 0)
        whidRxNotifyErr.fetch_add(dropped, std::memory_order_relaxed);

    resetWifiRxPending();
    resetWifiUsbPack();
    resetWifiTxPending();

    if (hasActiveWifiClient()) {
        Serial1.printf(
            "[WIFI] client closed (%s), dropped %u queued reports\n",
            reason,
            (unsigned)dropped);
        wifiClient.stop();
        wifiClient = WiFiClient();
    }
    setPeerConnected(false);
}

static void adoptWifiClient(WiFiClient incoming) {
    incoming.setNoDelay(true);
    wifiClient = incoming;
    resetWifiRxPending();
    resetWifiUsbPack();
    resetWifiTxPending();
    uint32_t dropped = dropWhidRxQueue();  // stale downlink belongs to the prior TCP session
    if (dropped > 0)
        whidRxNotifyErr.fetch_add(dropped, std::memory_order_relaxed);
    setPeerConnected(true);
    Serial1.printf(
        "[WIFI] client connected from %s:%u (fd=%d, dropped stale=%u)\n",
        wifiClient.remoteIP().toString().c_str(),
        wifiClient.remotePort(),
        wifiClient.fd(),
        (unsigned)dropped);
}

// Vendor HID interface. Constructed once, registered with the global
// USBHID device list before USB.begin().
class VendorHid : public USBHIDDevice {
public:
    VendorHid() {
        USBHID::addDevice(this, sizeof(hidVendorDesc));
    }

    uint16_t _onGetDescriptor(uint8_t* dst) override {
        memcpy(dst, hidVendorDesc, sizeof(hidVendorDesc));
        return sizeof(hidVendorDesc);
    }

    uint16_t _onGetFeature(uint8_t, uint8_t*, uint16_t) override { return 0; }

    void _onSetFeature(uint8_t, const uint8_t*, uint16_t) override {}

    // Output Report received from the kiosk page. Runs in TinyUSB task
    // context — keep it short.
    void _onOutput(uint8_t report_id, const uint8_t* buffer, uint16_t len) override {
        if (report_id == HID_REPORT_ID_CONFIG) {
            handleConfigOutputReport(buffer, len);
            return;
        }
        if (report_id != HID_REPORT_ID_VENDOR) return;
        if (len < 1) return;

        uint8_t payloadLen = buffer[0];
        if (payloadLen == 0) return;                     // heartbeat, no data
        if (payloadLen > VENDOR_REPORT_SIZE - 1)
            payloadLen = VENDOR_REPORT_SIZE - 1;
        if ((size_t)payloadLen + 1 > len)
            payloadLen = len - 1;

        VendorReport r;
        r.len = payloadLen;
        memcpy(r.data, buffer + 1, payloadLen);
        // Block briefly when the queue is full — the OUT endpoint will
        // NAK the next transfer, which propagates back-pressure to the
        // kiosk page. Drops only if 20 ms isn't enough.
        if (xQueueSend(whidRxQueue, &r, pdMS_TO_TICKS(WHID_QUEUE_BLOCK_MS)) == pdTRUE) {
            whidRxEnqueueOk.fetch_add(1, std::memory_order_relaxed);
            noteHighWater(whidRxQueueHighWater, static_cast<uint32_t>(uxQueueMessagesWaiting(whidRxQueue)));
        } else {
            whidRxEnqueueDrop.fetch_add(1, std::memory_order_relaxed);
        }
    }
};

static USBHIDKeyboard    Keyboard;     // registers report ID 1 in its ctor
static VendorHid         vendorHid;    // registers report ID 6 in its ctor
static USBHID            hid;          // shared handle for SendReport on the vendor RID
static Adafruit_NeoPixel led(1, PIN_LED, NEO_GRB + NEO_KHZ800);

// ── LED ────────────────────────────────────────────────────────────────────
enum LedState : uint8_t { LED_SCANNING, LED_IDLE, LED_TYPING };
static volatile LedState  ledState   = LED_SCANNING;

static void ledSet(uint8_t r, uint8_t g, uint8_t b) {
    led.setPixelColor(0, led.Color(r, g, b));
    led.show();
}

static void ledUpdate(uint32_t now) {
    static LedState  prev    = (LedState)0xFF;
    static uint32_t  pulseMs = 0;
    static bool      pulseOn = false;

    if (ledState == LED_SCANNING) {
        if (now - pulseMs >= 500) {
            pulseOn = !pulseOn;
            ledSet(pulseOn ? 60 : 0, pulseOn ? 24 : 0, 0);   // dim yellow pulse
            pulseMs = now;
        }
    } else if (ledState != prev) {
        if (ledState == LED_IDLE)
            ledSet(0, 0, 40);    // dim blue
        else
            ledSet(0, 60, 0);    // green
    }
    prev = ledState;
}

static const char* peerModeName(PeerMode mode) {
    return mode == PEER_MODE_WIFI_AP ? "wifi-ap" : "ble";
}

static void setPeerConnected(bool connected) {
    peerConnected = connected;
    if (ledState != LED_TYPING)
        ledState = connected ? LED_IDLE : LED_SCANNING;
}

static bool enqueueWhidTxReport(const VendorReport& r, uint32_t blockMs, uint8_t payloadLen) {
    if (xQueueSend(whidTxQueue, &r, pdMS_TO_TICKS(blockMs)) == pdTRUE) {
        whidTxEnqueueOk.fetch_add(1, std::memory_order_relaxed);
        whidTxPayloadBytesIn.fetch_add(payloadLen, std::memory_order_relaxed);
        noteWhidTxQueueDepth(uxQueueMessagesWaiting(whidTxQueue));
        return true;
    }
    whidTxEnqueueErr.fetch_add(1, std::memory_order_relaxed);
    return false;
}

static bool enqueueWhidTxPayload(const uint8_t* payload, size_t len, uint32_t blockMs = WHID_QUEUE_BLOCK_MS) {
    if (len == 0 || len > VENDOR_REPORT_SIZE - 1)
        return false;

    VendorReport r = {};
    r.len = VENDOR_REPORT_SIZE;
    r.data[0] = static_cast<uint8_t>(len);
    memcpy(r.data + 1, payload, len);
    return enqueueWhidTxReport(r, blockMs, static_cast<uint8_t>(len));
}

static bool flushWifiUsbPack(uint32_t blockMs = WHID_QUEUE_BLOCK_MS) {
    if (!hasWifiUsbPackPending())
        return true;
    if (!enqueueWhidTxPayload(wifiUsbPack, wifiUsbPackLen, blockMs))
        return false;
    resetWifiUsbPack();
    return true;
}

static size_t queueTcpBytesForUsb(const uint8_t* data, size_t len, uint32_t blockMs = WHID_QUEUE_BLOCK_MS) {
    constexpr size_t PAYLOAD_SIZE = VENDOR_REPORT_SIZE - 1;
    size_t off = 0;

    if (wifiUsbPackLen == PAYLOAD_SIZE && !flushWifiUsbPack(blockMs))
        return 0;

    while (off < len) {
        if (!hasWifiUsbPackPending() && (len - off) >= PAYLOAD_SIZE) {
            size_t whole = len - off;
            whole -= whole % PAYLOAD_SIZE;
            while (whole > 0) {
                if (!enqueueWhidTxPayload(data + off, PAYLOAD_SIZE, blockMs)) {
                    Serial1.printf(
                        "[WIFI] TX queue full, buffered %u bytes (remaining %u)\n",
                        (unsigned)off,
                        (unsigned)(len - off));
                    return off;
                }
                off += PAYLOAD_SIZE;
                whole -= PAYLOAD_SIZE;
            }
            continue;
        }

        if (!hasWifiUsbPackPending())
            wifiUsbPackStartedAt = millis();

        size_t chunk = PAYLOAD_SIZE - wifiUsbPackLen;
        if (chunk > len - off)
            chunk = len - off;
        memcpy(wifiUsbPack + wifiUsbPackLen, data + off, chunk);
        wifiUsbPackLen += chunk;
        off += chunk;

        if (wifiUsbPackLen == PAYLOAD_SIZE && !flushWifiUsbPack(blockMs)) {
            Serial1.printf(
                "[WIFI] TX queue full, buffered %u bytes (remaining %u)\n",
                (unsigned)off,
                (unsigned)(len - off));
            break;
        }
    }
    return off;
}

static bool drainWifiRxPendingToUsb() {
    while (hasWifiRxPending()) {
        size_t queued = queueTcpBytesForUsb(
            wifiRxPending + wifiRxPendingOff,
            wifiRxPendingLen - wifiRxPendingOff,
            0);
        if (queued == 0)
            return false;
        wifiRxPendingOff += queued;
    }
    resetWifiRxPending();
    return true;
}

static bool flushWifiUsbPackIfReady(uint32_t now, bool force = false) {
    if (!hasWifiUsbPackPending())
        return true;
    if (!force) {
        uint32_t age = now - wifiUsbPackStartedAt;
        if (age < WIFI_PACK_FLUSH_MS)
            return true;
    }
    return flushWifiUsbPack(0);
}

// ── BLE server callbacks ───────────────────────────────────────────────────
class ServerCBs : public NimBLEServerCallbacks {
    void onConnect(NimBLEServer*) override {
        bleConnected = true;
        setPeerConnected(true);
        reassemLen   = 0;     // discard any leftover partial frame
        whidTxAccumLen = 0;   // ditto for WHID-TX accumulator
        Serial1.println("[BLE] connected");
    }
    // Fires after the no-arg onConnect. Use it to request tighter connection
    // params now that we have a conn_handle. Interval units are 1.25 ms
    // (6 = 7.5 ms, 12 = 15 ms); supervision timeout is in 10 ms units.
    // setDataLen requests LE Data Length Extension up to a 251-byte PDU.
    void onConnect(NimBLEServer* server, ble_gap_conn_desc* desc) override {
        server->updateConnParams(
            desc->conn_handle,
            BLE_CONN_ITVL_MIN_UNITS,
            BLE_CONN_ITVL_MAX_UNITS,
            BLE_CONN_LATENCY,
            BLE_CONN_TIMEOUT
        );
        server->setDataLen(desc->conn_handle, 251);
        Serial1.printf(
            "[BLE] connected interval=%u (%.2f ms) latency=%u timeout=%u; requested %.2f-%.2f ms + DLE 251 (handle=%d)\n",
            desc->conn_itvl,
            desc->conn_itvl * 1.25f,
            desc->conn_latency,
            desc->supervision_timeout,
            BLE_CONN_ITVL_MIN_UNITS * 1.25f,
            BLE_CONN_ITVL_MAX_UNITS * 1.25f,
            desc->conn_handle
        );
    }
    void onMTUChange(uint16_t mtu, ble_gap_conn_desc* desc) override {
        Serial1.printf(
            "[BLE] MTU=%u interval=%u (%.2f ms) latency=%u timeout=%u\n",
            mtu,
            desc->conn_itvl,
            desc->conn_itvl * 1.25f,
            desc->conn_latency,
            desc->supervision_timeout
        );
    }
    void onDisconnect(NimBLEServer*) override {
        bleConnected = false;
        setPeerConnected(false);
        reassemLen   = 0;
        whidTxAccumLen = 0;
        Serial1.println("[BLE] disconnected — restarting advertising");
        NimBLEDevice::startAdvertising();
    }
};

// ── TX characteristic: reassemble 20-byte BLE chunks into HID frames ───────
class TxCBs : public NimBLECharacteristicCallbacks {
    void onWrite(NimBLECharacteristic* c) override {
        auto           val = c->getValue();   // own the copy; temporaries die at semicolon
        const uint8_t* src = (const uint8_t*)val.data();
        size_t         n   = val.size();

        for (size_t i = 0; i < n; i++) {
            uint8_t b = src[i];

            // '>' marks frame start — reset buffer so a reconnect mid-frame
            // never contaminates the new session.
            if (b == '>') reassemLen = 0;

            if (reassemLen < FRAME_MAX)
                reassemBuf[reassemLen++] = b;

            if (b == '<') {
                // Frame complete — hand off to loop(); drop if queue full.
                Frame frame;
                frame.len = reassemLen;
                memcpy(frame.data, reassemBuf, reassemLen);
                xQueueSend(frameQueue, &frame, 0);
                reassemLen = 0;
            }
        }
    }
};

// ── CFG characteristic: 2-byte big-endian inter-key delay (ms) ─────────────
class CfgCBs : public NimBLECharacteristicCallbacks {
    void onWrite(NimBLECharacteristic* c) override {
        auto           val  = c->getValue();
        const uint8_t* data = (const uint8_t*)val.data();
        if (val.size() >= 2) {
            keyDelayMs = ((uint16_t)data[0] << 8) | data[1];
            Serial1.printf("[CFG] key_delay=%u ms\n", (unsigned)keyDelayMs);
        }
    }
};

// ── WHID-TX characteristic: reassemble BLE chunks into 63-byte HID report bodies ─
// The laptop client in --mode webhid pre-chunks the outbound HDLC byte stream
// into fixed-size report bodies (length byte + payload, see shared/framing.py
// hid_report_pack) and writes them as a stream of 20-byte ATT chunks.
// The dongle accumulates exactly VENDOR_REPORT_SIZE bytes per report and
// queues them for USB SendReport.
class WhidTxCBs : public NimBLECharacteristicCallbacks {
    void onWrite(NimBLECharacteristic* c) override {
        auto           val = c->getValue();
        const uint8_t* src = (const uint8_t*)val.data();
        size_t         n   = val.size();

        for (size_t i = 0; i < n; i++) {
            whidTxAccum[whidTxAccumLen++] = src[i];
            if (whidTxAccumLen == VENDOR_REPORT_SIZE) {
                VendorReport r = {};
                r.len = VENDOR_REPORT_SIZE;
                memcpy(r.data, whidTxAccum, VENDOR_REPORT_SIZE);
                // Brief block — gives loop() time to drain to USB IN before
                // we resort to dropping. WRITE_NR on the central side means
                // no real BLE-layer back-pressure, but this keeps the BLE
                // task from oversaturating the queue during a typical burst.
                enqueueWhidTxReport(r, WHID_QUEUE_BLOCK_MS, r.data[0]);
                whidTxAccumLen = 0;
            }
        }
    }
};

// Pointer to the NOTIFY characteristic — captured in setup(), used by loop()
// to push vendor-HID Output Report payloads back to the laptop.
static NimBLECharacteristic* rxChar = nullptr;

class WhidRxCBs : public NimBLECharacteristicCallbacks {
    void onStatus(NimBLECharacteristic*, Status s, int) override {
        if (s == Status::SUCCESS_NOTIFY) {
            whidRxNotifyOk.fetch_add(1, std::memory_order_relaxed);
        } else if (s == Status::ERROR_GATT) {
            whidRxNotifyErr.fetch_add(1, std::memory_order_relaxed);
        }
    }
};

static void processPendingPeerConfig() {
    if (!pendingPeerConfigDirty.exchange(false, std::memory_order_acq_rel))
        return;

    if (pendingPeerConfig.command == CONFIG_CMD_RESET_DEFAULTS) {
        resetPersistedPeerConfig();
        if (peerMode == PEER_MODE_BLE)
            NimBLEDevice::deleteAllBonds();
        Serial1.println("[CFG] reset peer-link config to defaults, rebooting");
        delay(100);
        ESP.restart();
    }

    if (!storePersistedPeerConfig(pendingPeerConfig.mode, pendingPeerConfig.ssid, pendingPeerConfig.pass)) {
        lastConfigStatus.store(CONFIG_STATUS_BAD_COMMAND, std::memory_order_relaxed);
        return;
    }

    Serial1.printf(
        "[CFG] saved peer mode=%s wifi_ssid=%s, rebooting\n",
        peerModeName(pendingPeerConfig.mode),
        pendingPeerConfig.ssid);
    delay(100);
    ESP.restart();
}

// ── button: long-press (3 s) resets config defaults and reboots ───────────
static void handleButton(uint32_t now) {
    static uint32_t pressedAt = 0;
    static bool     held      = false;
    static bool     fired     = false;

    bool down = (digitalRead(PIN_BTN) == LOW);
    if (down && !held) { held = true; pressedAt = now; fired = false; }
    if (!down)         { held = false; fired = false; }
    if (held && !fired && (now - pressedAt >= 3000)) {
        fired = true;
        resetPersistedPeerConfig();
        if (peerMode == PEER_MODE_BLE)
            NimBLEDevice::deleteAllBonds();
        Serial1.println("[BTN] reset defaults + clear bonds, rebooting");
        delay(100);
        ESP.restart();
    }
}

static void setupBlePeer() {
    // BLE — bump preferred ATT MTU from the 23-byte default to fit a full
    // 63-byte vendor HID report in a single notification/write (MTU - 3 ATT
    // header = 244 payload bytes at MTU 247). setMTU calls into the NimBLE
    // host stack, which must be initialized first.
    NimBLEDevice::init(DEVICE_NAME);
    NimBLEDevice::setMTU(247);

    NimBLEServer* server = NimBLEDevice::createServer();
    server->setCallbacks(new ServerCBs(), /*deleteCallbacks=*/false);

    NimBLEService* svc = server->createService(SVC_UUID);

    auto* txChar = svc->createCharacteristic(TX_UUID, NIMBLE_PROPERTY::WRITE_NR);
    txChar->setCallbacks(new TxCBs());

    auto* cfgChar = svc->createCharacteristic(CFG_UUID, NIMBLE_PROPERTY::WRITE);
    cfgChar->setCallbacks(new CfgCBs());

    // Fast-path: vendor-HID input direction (laptop -> kiosk)
    auto* whidTxChar = svc->createCharacteristic(WHID_TX_UUID, NIMBLE_PROPERTY::WRITE_NR);
    whidTxChar->setCallbacks(new WhidTxCBs());

    // Fast-path: vendor-HID output direction (kiosk -> laptop), NOTIFY
    rxChar = svc->createCharacteristic(WHID_RX_UUID, NIMBLE_PROPERTY::NOTIFY);
    rxChar->setCallbacks(new WhidRxCBs());

    svc->start();

    NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
    adv->addServiceUUID(SVC_UUID);
    adv->setScanResponse(true);    // scan response carries the device name
    NimBLEDevice::startAdvertising();

    Serial1.println("[BOOT] advertising as KioskDongle");
}

static void setupWifiPeer() {
    WiFi.mode(WIFI_MODE_AP);
    if (!WiFi.softAPConfig(WIFI_AP_IP, WIFI_AP_GW, WIFI_AP_MASK)) {
        Serial1.println("[WIFI] softAPConfig failed");
        return;
    }
    if (!WiFi.softAP(
            wifiApSsid,
            wifiApPass,
            WIFI_AP_CHANNEL,
            0,
            WIFI_AP_MAX_CLIENTS)) {
        Serial1.println("[WIFI] softAP start failed");
        return;
    }

    wifiServer.begin();
    wifiServer.setNoDelay(true);
    Serial1.printf(
        "[WIFI] softAP ssid=%s ip=%s port=%u\n",
        wifiApSsid,
        WiFi.softAPIP().toString().c_str(),
        WIFI_TCP_PORT);
}

static void pumpWifiPeer() {
    if (hasActiveWifiClient() &&
        !wifiClient.connected() &&
        wifiClient.available() == 0 &&
        wifiTxPendingLen == wifiTxPendingOff &&
        wifiUplinkIdle()) {
        closeWifiClient("peer disconnect");
    }

    if (wifiServer.hasClient()) {
        if (!hasActiveWifiClient() && !wifiUplinkIdle()) {
            // Prior session bytes are still draining into USB; defer the next
            // client until that work is complete so sessions do not interleave.
        } else {
            WiFiClient incoming = wifiServer.available();
            if (incoming) {
                if (hasActiveWifiClient()) {
                    Serial1.printf(
                        "[WIFI] rejecting extra client from %s:%u\n",
                        incoming.remoteIP().toString().c_str(),
                        incoming.remotePort());
                    incoming.stop();
                } else {
                    adoptWifiClient(incoming);
                }
            }
        }
    }

    if (!hasActiveWifiClient())
        return;

    if (hasWifiRxPending() && !drainWifiRxPendingToUsb())
        return;

    while (!hasWifiRxPending() && wifiClient.available() > 0) {
        int n = wifiClient.read(wifiRxPending, sizeof(wifiRxPending));
        if (n <= 0)
            break;
        wifiTcpReadCalls.fetch_add(1, std::memory_order_relaxed);
        wifiTcpRxBytes.fetch_add(static_cast<uint32_t>(n), std::memory_order_relaxed);
        wifiRxPendingLen = static_cast<size_t>(n);
        wifiRxPendingOff = 0;
        noteWifiRxPendingDepth(wifiRxPendingLen);
        if (!drainWifiRxPendingToUsb())
            break;
    }

    uint32_t now = millis();
    bool peerDisconnected = !wifiClient.connected() && wifiClient.available() == 0;
    if (!hasWifiRxPending() && !flushWifiUsbPackIfReady(now, peerDisconnected))
        return;

    if (hasActiveWifiClient() &&
        !wifiClient.connected() &&
        wifiClient.available() == 0 &&
        wifiTxPendingLen == wifiTxPendingOff &&
        wifiUplinkIdle()) {
        closeWifiClient("peer disconnect");
    }
}

// ── setup ──────────────────────────────────────────────────────────────────
void setup() {
    // Grove debug UART — baud, config, RX pin, TX pin
    Serial1.begin(115200, SERIAL_8N1, SERIAL1_RX_PIN, SERIAL1_TX_PIN);
    Serial1.println("[BOOT] KioskDongle starting");

    led.begin();
    led.setBrightness(80);
    ledSet(0, 0, 0);

    pinMode(PIN_BTN, INPUT_PULLUP);
    loadPersistedPeerConfig();
    peerMode = configuredPeerMode;
    Serial1.printf("[BOOT] peer mode=%s\n", peerModeName(peerMode));

    frameQueue   = xQueueCreate(QUEUE_DEPTH, sizeof(Frame));
    whidTxQueue  = xQueueCreate(WHID_QUEUE_DEPTH, sizeof(VendorReport));
    whidRxQueue  = xQueueCreate(WHID_QUEUE_DEPTH, sizeof(VendorReport));
    configASSERT(frameQueue  != nullptr);
    configASSERT(whidTxQueue != nullptr);
    configASSERT(whidRxQueue != nullptr);

    // USB HID — both Keyboard and VendorHid have already registered with the
    // global USBHID device list via their constructors. Starting one starts
    // the shared interface; both descriptors will be reported by GET_DESCRIPTOR.
    USB.productName(DEVICE_NAME);
    Keyboard.begin();
    USB.begin();

    if (peerMode == PEER_MODE_WIFI_AP)
        setupWifiPeer();
    else
        setupBlePeer();
}

// ── loop ───────────────────────────────────────────────────────────────────
void loop() {
    uint32_t now = millis();
    handleButton(now);
    processPendingPeerConfig();

    if (configStatusRequested.exchange(false, std::memory_order_acq_rel)) {
        uint8_t report[CONFIG_REPORT_SIZE];
        if (buildConfigReport(report, sizeof(report)))
            hid.SendReport(HID_REPORT_ID_CONFIG, report, sizeof(report), 50);
    }

    // ── Fast-path: drain BLE -> USB Input Reports (laptop -> kiosk) ────────
    // Bounded number of reports per loop tick so we never starve the standard
    // keyboard path or button handling. With WHID_QUEUE_DEPTH=32 we want to
    // be able to drain a full queue across a handful of loop ticks.
    int whidTxBudget = (peerMode == PEER_MODE_WIFI_AP) ? 64 : 16;
    for (int i = 0; i < whidTxBudget; i++) {
        VendorReport r;
        if (xQueueReceive(whidTxQueue, &r, 0) != pdTRUE) break;
        whidTxDequeued.fetch_add(1, std::memory_order_relaxed);
        // SendReport has its own 100ms internal timeout — typically returns
        // promptly when the host is polling, drops the report if the OUT
        // endpoint is stalled (e.g. nothing has claimed the interface).
        uint32_t t0 = micros();
        bool ok = hid.SendReport(HID_REPORT_ID_VENDOR, r.data, r.len, 50);
        uint32_t elapsed = micros() - t0;
        whidTxSendMicros.fetch_add(static_cast<uint64_t>(elapsed), std::memory_order_relaxed);
        if (ok) {
            whidTxSendOk.fetch_add(1, std::memory_order_relaxed);
            whidTxPayloadBytesOut.fetch_add(r.data[0], std::memory_order_relaxed);
        } else {
            whidTxSendErr.fetch_add(1, std::memory_order_relaxed);
        }
    }

    // ── Fast-path: drain USB Output Reports -> BLE NOTIFY (kiosk -> laptop) ─
    if (peerMode == PEER_MODE_BLE && rxChar && bleConnected) {
        for (int i = 0; i < 16; i++) {
            VendorReport r;
            if (xQueueReceive(whidRxQueue, &r, 0) != pdTRUE) break;
            whidRxDequeued.fetch_add(1, std::memory_order_relaxed);
            // With a negotiated MTU ≥ 66 the whole 63-byte report fits in a
            // single notification. NimBLE silently truncates to MTU-3 if the
            // negotiation failed, so the laptop reassembler must still be
            // able to handle short notifications — bridge.put_rx_raw is
            // length-agnostic, so this is fine.
            rxChar->setValue(r.data, r.len);
            whidRxNotifyAttempt.fetch_add(1, std::memory_order_relaxed);
            rxChar->notify();
        }
    } else if (peerMode == PEER_MODE_WIFI_AP && hasActiveWifiClient()) {
        for (int i = 0; i < 16; i++) {
            if (wifiTxPendingLen == wifiTxPendingOff) {
                VendorReport r;
                if (xQueueReceive(whidRxQueue, &r, 0) != pdTRUE) break;
                whidRxDequeued.fetch_add(1, std::memory_order_relaxed);
                memcpy(wifiTxPending, r.data, r.len);
                wifiTxPendingLen = r.len;
                wifiTxPendingOff = 0;
                whidRxNotifyAttempt.fetch_add(1, std::memory_order_relaxed);
            }

            size_t remaining = wifiTxPendingLen - wifiTxPendingOff;
            size_t wrote = wifiClient.write(wifiTxPending + wifiTxPendingOff, remaining);
            if (wrote > 0) {
                wifiTxPendingOff += wrote;
                if (wifiTxPendingOff == wifiTxPendingLen) {
                    resetWifiTxPending();
                    whidRxNotifyOk.fetch_add(1, std::memory_order_relaxed);
                }
                continue;
            }

            if (!wifiClient.connected() && wifiClient.available() == 0) {
                closeWifiClient("write failed");
                break;
            }
            break;
        }
    }

    if (peerMode == PEER_MODE_WIFI_AP)
        pumpWifiPeer();

    // ── Legacy: drain BLE -> typed HID frames (laptop -> kiosk via keyboard)
    Frame frame;
    bool fastPathBusy =
        uxQueueMessagesWaiting(whidTxQueue) > 0 ||
        uxQueueMessagesWaiting(whidRxQueue) > 0 ||
        wifiUsbPackLen > 0 ||
        wifiTxPendingLen != wifiTxPendingOff ||
        (peerMode == PEER_MODE_WIFI_AP && (!wifiUplinkIdle() || hasActiveWifiClient()));
    TickType_t frameWait = fastPathBusy ? 0 : pdMS_TO_TICKS(10);
    if (xQueueReceive(frameQueue, &frame, frameWait) == pdTRUE) {
        ledState = LED_TYPING;
        ledUpdate(millis());

        // Snapshot delay — CFG char write on the BLE task may update keyDelayMs
        // concurrently; reading once keeps the timing consistent within a frame.
        // Clamp to [1, 100]: 0 may not flush the HID report; >100 ms per key
        // would block loop() (and thus handleButton) for a noticeable duration.
        uint16_t dly = keyDelayMs;
        if (dly < 1)   dly = 1;
        if (dly > 100) dly = 100;

        for (uint16_t i = 0; i < frame.len; i++) {
            Keyboard.press((char)frame.data[i]);
            delay(dly);
            Keyboard.releaseAll();
            delay(dly);
            handleButton(millis());
        }

        ledState = peerConnected ? LED_IDLE : LED_SCANNING;
    }

    ledUpdate(millis());
}
