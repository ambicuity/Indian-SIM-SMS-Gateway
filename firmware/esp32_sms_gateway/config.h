/*
 * Indian SIM SMS Gateway — ESP32 Configuration
 * 
 * All compile-time constants for the gateway firmware.
 * Copy this file as config_local.h and fill in your values.
 */

#ifndef CONFIG_H
#define CONFIG_H

// ─── WiFi Configuration ───────────────────────────────────
#define WIFI_SSID           "YOUR_WIFI_SSID"
#define WIFI_PASSWORD       "YOUR_WIFI_PASSWORD"
#define WIFI_MAX_BACKOFF_MS 60000   // 60 second cap
#define WIFI_INITIAL_DELAY  1000    // 1 second initial retry
#define WIFI_BACKOFF_MULT   2       // Doubling factor
#define WIFI_JITTER_MAX_MS  500     // Random jitter up to 500ms

// ─── MQTT Configuration ───────────────────────────────────
#define MQTT_BROKER_HOST    "your-mqtt-broker.com"
#define MQTT_BROKER_PORT    8883    // TLS port
#define MQTT_CLIENT_ID      "esp32-sms-gw-01"
#define MQTT_USERNAME       "gateway"
#define MQTT_PASSWORD       "your_mqtt_password"
#define MQTT_TOPIC_SMS      "gateway/sms/inbound"
#define MQTT_TOPIC_TELEM    "gateway/telemetry"
#define MQTT_QOS            1       // At-least-once delivery

// ─── SIM Module (SIM800L / SIM7600) ──────────────────────
#define SIM_RX_PIN          16
#define SIM_TX_PIN          17
#define SIM_BAUD_RATE       115200
#define SIM_POWER_PIN       4

// ─── NVS Deduplication ───────────────────────────────────
#define NVS_NAMESPACE       "sms_dedup"
#define NVS_KEY_IDS         "last_ids"
#define NVS_KEY_INDEX       "ring_idx"
#define MAX_STORED_SMS_IDS  5       // Circular buffer size

// ─── Watchdog Configuration ──────────────────────────────
#define WDT_TIMEOUT_SEC     30      // Reset if main loop stalls > 30s

// ─── Battery Monitoring ──────────────────────────────────
#define BATTERY_ADC_PIN     34
#define BATTERY_LOW_MV      3300    // ~20% for single 18650
#define BATTERY_DIVIDER_R1  100000  // 100kΩ
#define BATTERY_DIVIDER_R2  100000  // 100kΩ

// ─── Telemetry ───────────────────────────────────────────
#define TELEMETRY_INTERVAL  30000   // Publish every 30 seconds (ms)

// ─── LED Status ──────────────────────────────────────────
#define LED_PIN             2       // Built-in LED
#define LED_BLINK_FAST      100     // ms (error state)
#define LED_BLINK_SLOW      1000    // ms (normal operation)

#endif // CONFIG_H
