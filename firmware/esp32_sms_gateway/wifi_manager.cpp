/*
 * WiFi Manager — Implementation
 *
 * Self-healing WiFi connectivity with exponential backoff:
 *   Retry Delay = min(INITIAL_DELAY × 2^attempt + jitter, MAX_BACKOFF)
 *
 * Example progression:
 *   Attempt 1: ~1.0s + jitter(0-500ms) = ~1.3s
 *   Attempt 2: ~2.0s + jitter          = ~2.4s
 *   Attempt 3: ~4.0s + jitter          = ~4.2s
 *   Attempt 4: ~8.0s + jitter          = ~8.1s
 *   ...
 *   Attempt N: 60.0s (capped)          = ~60.3s
 */

#include "wifi_manager.h"

// ─── Constructor ─────────────────────────────────────────

WifiManager::WifiManager()
    : _state(WifiState::DISCONNECTED), _callback(nullptr),
      _backoffMs(WIFI_INITIAL_DELAY), _lastAttemptMs(0), _reconnectAttempts(0),
      _totalReconnects(0) {}

// ─── Initialization ──────────────────────────────────────

bool WifiManager::begin(int maxInitialAttempts) {
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(false); // We handle reconnection ourselves
  WiFi.persistent(false);       // Don't save to flash on every connect

  Serial.printf("[WiFi] Connecting to '%s'...\n", WIFI_SSID);
  _setState(WifiState::CONNECTING);

  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  int attempts = 0;
  while (!WiFi.isConnected()) {
    delay(500);
    Serial.print(".");
    attempts++;

    if (maxInitialAttempts > 0 && attempts >= maxInitialAttempts * 2) {
      Serial.println("\n[WiFi] Initial connection FAILED");
      _setState(WifiState::FAILED);
      return false;
    }
  }

  Serial.printf("\n[WiFi] Connected! IP: %s, RSSI: %d dBm\n",
                WiFi.localIP().toString().c_str(), WiFi.RSSI());
  _setState(WifiState::CONNECTED);
  _backoffMs = WIFI_INITIAL_DELAY; // Reset backoff on success
  return true;
}

// ─── Main Loop ───────────────────────────────────────────

void WifiManager::loop() {
  if (WiFi.isConnected()) {
    if (_state != WifiState::CONNECTED) {
      Serial.printf(
          "[WiFi] Reconnected! IP: %s, RSSI: %d dBm (after %d attempts)\n",
          WiFi.localIP().toString().c_str(), WiFi.RSSI(), _reconnectAttempts);
      _setState(WifiState::CONNECTED);
      _backoffMs = WIFI_INITIAL_DELAY;
      _reconnectAttempts = 0;
    }
    return;
  }

  // ─── Disconnected — handle exponential backoff ───
  if (_state == WifiState::CONNECTED) {
    Serial.println("[WiFi] ⚠️ Connection LOST. Starting reconnection...");
    _setState(WifiState::RECONNECTING);
    _backoffMs = WIFI_INITIAL_DELAY;
    _lastAttemptMs = millis();
  }

  unsigned long now = millis();
  unsigned long nextAttemptAt = _lastAttemptMs + _backoffMs;

  if (now >= nextAttemptAt) {
    _attemptReconnect();
  }
}

// ─── Getters ─────────────────────────────────────────────

bool WifiManager::isConnected() const { return WiFi.isConnected(); }

WifiState WifiManager::getState() const { return _state; }

int WifiManager::getRssi() const {
  return WiFi.isConnected() ? WiFi.RSSI() : -127;
}

unsigned long WifiManager::getCurrentBackoffMs() const { return _backoffMs; }

int WifiManager::getTotalReconnectAttempts() const { return _totalReconnects; }

void WifiManager::onStateChange(WifiStateCallback callback) {
  _callback = callback;
}

// ─── Private: Reconnection ──────────────────────────────

void WifiManager::_attemptReconnect() {
  _reconnectAttempts++;
  _totalReconnects++;

  Serial.printf("[WiFi] Reconnect attempt #%d (backoff: %lums)\n",
                _reconnectAttempts, _backoffMs);

  WiFi.disconnect();
  delay(100);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  // Wait briefly to check if it connected
  unsigned long connectStart = millis();
  while (!WiFi.isConnected() && millis() - connectStart < 5000) {
    delay(100);
  }

  _lastAttemptMs = millis();
  _backoffMs = _calculateBackoff();
}

// ─── Private: Backoff Calculation ────────────────────────

unsigned long WifiManager::_calculateBackoff() const {
  // Exponential: initial × 2^attempt
  unsigned long base = WIFI_INITIAL_DELAY;
  for (int i = 0; i < _reconnectAttempts && i < 20; i++) {
    base *= WIFI_BACKOFF_MULT;
    if (base >= WIFI_MAX_BACKOFF_MS) {
      base = WIFI_MAX_BACKOFF_MS;
      break;
    }
  }

  return _addJitter(min(base, (unsigned long)WIFI_MAX_BACKOFF_MS));
}

unsigned long WifiManager::_addJitter(unsigned long baseMs) const {
  // Add random jitter [0, WIFI_JITTER_MAX_MS) to prevent thundering herd
  return baseMs + (unsigned long)(random(0, WIFI_JITTER_MAX_MS));
}

// ─── Private: State Management ───────────────────────────

void WifiManager::_setState(WifiState newState) {
  if (_state != newState) {
    _state = newState;
    if (_callback) {
      _callback(newState, _reconnectAttempts);
    }
  }
}
