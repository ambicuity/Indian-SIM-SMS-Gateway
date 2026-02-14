/*
 * WiFi Manager — Header
 *
 * Manages WiFi connectivity with exponential backoff reconnection.
 * Implements jitter to avoid thundering herd effects when multiple
 * devices reconnect simultaneously (e.g., after a router reboot).
 */

#ifndef WIFI_MANAGER_H
#define WIFI_MANAGER_H

#include "config.h"
#include <Arduino.h>
#include <WiFi.h>

// Connection state enum for external monitoring
enum class WifiState {
  DISCONNECTED,
  CONNECTING,
  CONNECTED,
  RECONNECTING,
  FAILED
};

// Callback type for state change notifications
typedef void (*WifiStateCallback)(WifiState newState, int attempts);

class WifiManager {
public:
  WifiManager();

  /**
   * Initialize WiFi in station mode and attempt first connection.
   * Blocks until connected or maxAttempts reached on first boot.
   * @param maxInitialAttempts  Max attempts for initial connection (0 =
   * infinite)
   * @return true if connected
   */
  bool begin(int maxInitialAttempts = 10);

  /**
   * Non-blocking connection maintenance — call in loop().
   * Handles reconnection with exponential backoff + jitter.
   */
  void loop();

  /**
   * Check current connection status.
   */
  bool isConnected() const;

  /**
   * Get current WiFi state.
   */
  WifiState getState() const;

  /**
   * Get RSSI (signal strength in dBm).
   */
  int getRssi() const;

  /**
   * Get current backoff delay (for telemetry).
   */
  unsigned long getCurrentBackoffMs() const;

  /**
   * Get total reconnection attempts since boot.
   */
  int getTotalReconnectAttempts() const;

  /**
   * Register a callback for state changes.
   */
  void onStateChange(WifiStateCallback callback);

private:
  WifiState _state;
  WifiStateCallback _callback;
  unsigned long _backoffMs;
  unsigned long _lastAttemptMs;
  int _reconnectAttempts;
  int _totalReconnects;

  void _setState(WifiState newState);
  void _attemptReconnect();
  unsigned long _calculateBackoff() const;
  unsigned long _addJitter(unsigned long baseMs) const;
};

#endif // WIFI_MANAGER_H
