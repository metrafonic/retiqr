/*
 * KioskDongle — BLE → USB HID keyboard bridge
 *
 * Target: M5Stack AtomS3 Lite (ESP32-S3FN8)
 *
 * Pinout
 *   GPIO 35  SK6812 RGB LED
 *   GPIO 41  button (active-low, internal pull-up)
 *   GPIO  5  Serial1 TX  (exposed pad G5)
 *   GPIO  6  Serial1 RX  (exposed pad G6)
 *   USB-C    native ESP32-S3 USB OTG (HID keyboard, kiosk side)
 *
 * BLE GATT service (UUIDs must match tx/ble.py):
 *   Service   4b696f73-6b55-0001-0000-000000000000
 *   TX char   4b696f73-6b55-0002-0000-000000000000  WRITE_NR  (20-byte chunks)
 *   CFG char  4b696f73-6b55-0003-0000-000000000000  WRITE     (2-byte big-endian delay ms)
 */

#include <Arduino.h>
#include <NimBLEDevice.h>
#include <USB.h>
#include <USBHIDKeyboard.h>
#include <Adafruit_NeoPixel.h>

// ── pins ───────────────────────────────────────────────────────────────────
static constexpr uint8_t  PIN_LED       = 35;
static constexpr uint8_t  PIN_BTN       = 41;
static constexpr uint8_t  SERIAL1_TX_PIN =  5;
static constexpr uint8_t  SERIAL1_RX_PIN =  6;

// ── BLE identity ───────────────────────────────────────────────────────────
static const char* SVC_UUID  = "4b696f73-6b55-0001-0000-000000000000";
static const char* TX_UUID   = "4b696f73-6b55-0002-0000-000000000000";
static const char* CFG_UUID  = "4b696f73-6b55-0003-0000-000000000000";
static const char* DEVICE_NAME = "KioskDongle";

// ── frame queue ────────────────────────────────────────────────────────────
// Max Reticulum packet ~500 bytes → HID frame = 1 + 500*2 + 2 + 1 = 1004 bytes.
static constexpr uint16_t FRAME_MAX   = 1024;
static constexpr uint8_t  QUEUE_DEPTH =    4;

struct Frame {
    uint8_t  data[FRAME_MAX];
    uint16_t len;
};

static QueueHandle_t     frameQueue;
static volatile uint16_t keyDelayMs  = 5;
static volatile bool     bleConnected = false;

// Reassembly buffer — touched only from the NimBLE task.
static uint8_t  reassemBuf[FRAME_MAX];
static uint16_t reassemLen = 0;

// ── peripherals ────────────────────────────────────────────────────────────
static USBHIDKeyboard    Keyboard;
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

// ── BLE server callbacks ───────────────────────────────────────────────────
class ServerCBs : public NimBLEServerCallbacks {
    void onConnect(NimBLEServer*) override {
        bleConnected = true;
        reassemLen   = 0;     // discard any leftover partial frame
        ledState     = LED_IDLE;
        Serial1.println("[BLE] connected");
    }
    void onDisconnect(NimBLEServer*) override {
        bleConnected = false;
        reassemLen   = 0;     // do not carry partial frame into the next session
        ledState     = LED_SCANNING;
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

// ── button: long-press (3 s) clears BLE bonds and reboots ─────────────────
static void handleButton(uint32_t now) {
    static uint32_t pressedAt = 0;
    static bool     held      = false;
    static bool     fired     = false;

    bool down = (digitalRead(PIN_BTN) == LOW);
    if (down && !held) { held = true; pressedAt = now; fired = false; }
    if (!down)         { held = false; fired = false; }
    if (held && !fired && (now - pressedAt >= 3000)) {
        fired = true;
        Serial1.println("[BTN] clearing bonds, rebooting");
        NimBLEDevice::deleteAllBonds();
        delay(100);
        ESP.restart();
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

    frameQueue = xQueueCreate(QUEUE_DEPTH, sizeof(Frame));
    configASSERT(frameQueue != nullptr);

    // USB HID — register keyboard before starting USB stack
    Keyboard.begin();
    USB.begin();

    // BLE
    NimBLEDevice::init(DEVICE_NAME);

    NimBLEServer* server = NimBLEDevice::createServer();
    server->setCallbacks(new ServerCBs(), /*deleteCallbacks=*/false);

    NimBLEService* svc = server->createService(SVC_UUID);

    auto* txChar = svc->createCharacteristic(TX_UUID, NIMBLE_PROPERTY::WRITE_NR);
    txChar->setCallbacks(new TxCBs());

    auto* cfgChar = svc->createCharacteristic(CFG_UUID, NIMBLE_PROPERTY::WRITE);
    cfgChar->setCallbacks(new CfgCBs());

    svc->start();

    NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
    adv->addServiceUUID(SVC_UUID);
    adv->setScanResponse(true);    // scan response carries the device name
    NimBLEDevice::startAdvertising();

    Serial1.println("[BOOT] advertising as KioskDongle");
}

// ── loop ───────────────────────────────────────────────────────────────────
void loop() {
    uint32_t now = millis();
    handleButton(now);

    Frame frame;
    if (xQueueReceive(frameQueue, &frame, pdMS_TO_TICKS(10)) == pdTRUE) {
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

        ledState = bleConnected ? LED_IDLE : LED_SCANNING;
    }

    ledUpdate(millis());
}
