/*
 * Watchdog Manager — Implementation
 *
 * Uses ESP-IDF Task Watchdog Timer (TWDT) to ensure the main loop
 * continues running. If the loop stalls (hardware fault, infinite loop,
 * deadlock), the device automatically resets.
 *
 * Reset count is persisted in NVS for telemetry — the backend can
 * detect frequent watchdog resets as a hardware degradation signal.
 */

#include "watchdog_manager.h"
#include <Preferences.h>
#include <esp_system.h>

#define NVS_WDT_NAMESPACE "wdt_stats"
#define NVS_WDT_KEY_COUNT "rst_count"

// ─── Constructor ─────────────────────────────────────────

WatchdogManager::WatchdogManager() : _enabled(false), _resetCount(0) {}

// ─── Initialization ──────────────────────────────────────

bool WatchdogManager::begin() {
  _loadResetCount();

  // Check if this boot was caused by a watchdog reset
  if (wasWatchdogReset()) {
    _incrementResetCounter();
    Serial.printf("[WDT] ⚠️ Watchdog reset detected! Total resets: %d\n",
                  _resetCount);
  }

  // Configure the Task Watchdog Timer
  // esp_task_wdt_config_t requires ESP-IDF >= 5.x
  esp_task_wdt_config_t wdt_config = {
      .timeout_ms = WDT_TIMEOUT_SEC * 1000,
      .idle_core_mask = 0,  // Don't watch idle tasks
      .trigger_panic = true // Reset on timeout
  };

  esp_err_t err = esp_task_wdt_reconfigure(&wdt_config);
  if (err != ESP_OK) {
    // Fallback: try init instead of reconfigure
    err = esp_task_wdt_init(&wdt_config);
  }

  if (err != ESP_OK) {
    Serial.printf("[WDT] ERROR: Failed to configure TWDT (err: %d)\n", err);
    return false;
  }

  // Subscribe the current (main) task to the watchdog
  err = esp_task_wdt_add(NULL);
  if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
    Serial.printf("[WDT] ERROR: Failed to subscribe task (err: %d)\n", err);
    return false;
  }

  _enabled = true;
  Serial.printf("[WDT] Watchdog initialized. Timeout: %ds, Reset count: %d\n",
                WDT_TIMEOUT_SEC, _resetCount);
  return true;
}

// ─── Feed ────────────────────────────────────────────────

void WatchdogManager::feed() {
  if (_enabled) {
    esp_task_wdt_reset();
  }
}

// ─── Reset Detection ─────────────────────────────────────

bool WatchdogManager::wasWatchdogReset() const {
  esp_reset_reason_t reason = esp_reset_reason();
  return (reason == ESP_RST_TASK_WDT || reason == ESP_RST_WDT ||
          reason == ESP_RST_INT_WDT);
}

int WatchdogManager::getResetCount() const { return _resetCount; }

// ─── Enable / Disable ────────────────────────────────────

void WatchdogManager::disable() {
  if (_enabled) {
    esp_task_wdt_delete(NULL);
    _enabled = false;
    Serial.println("[WDT] Watchdog DISABLED (temporary)");
  }
}

void WatchdogManager::enable() {
  if (!_enabled) {
    esp_task_wdt_add(NULL);
    _enabled = true;
    Serial.println("[WDT] Watchdog RE-ENABLED");
  }
}

// ─── NVS Persistence (Private) ───────────────────────────

void WatchdogManager::_loadResetCount() {
  Preferences prefs;
  prefs.begin(NVS_WDT_NAMESPACE, true); // read-only
  _resetCount = prefs.getInt(NVS_WDT_KEY_COUNT, 0);
  prefs.end();
}

void WatchdogManager::_incrementResetCounter() {
  _resetCount++;
  Preferences prefs;
  prefs.begin(NVS_WDT_NAMESPACE, false);
  prefs.putInt(NVS_WDT_KEY_COUNT, _resetCount);
  prefs.end();
}
