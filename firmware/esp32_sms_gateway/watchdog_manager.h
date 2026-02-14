/*
 * Watchdog Manager — Header
 *
 * Configures the ESP32 Task Watchdog Timer (TWDT) to automatically
 * reset the device if the main loop stalls. Essential for reliability
 * in unattended edge deployments.
 */

#ifndef WATCHDOG_MANAGER_H
#define WATCHDOG_MANAGER_H

#include "config.h"
#include <Arduino.h>
#include <esp_task_wdt.h>

class WatchdogManager {
public:
  WatchdogManager();

  /**
   * Initialize the Task Watchdog Timer.
   * Sets the timeout from config and subscribes the current task.
   * @return true if initialization succeeded
   */
  bool begin();

  /**
   * Feed the watchdog — must be called regularly in the main loop.
   * Failure to call this within WDT_TIMEOUT_SEC triggers a device reset.
   */
  void feed();

  /**
   * Check if the last reset was caused by the watchdog.
   * Useful for logging/telemetry after boot.
   * @return true if the last reset was a watchdog reset
   */
  bool wasWatchdogReset() const;

  /**
   * Get number of watchdog resets since NVS was last cleared.
   */
  int getResetCount() const;

  /**
   * Temporarily disable the watchdog (e.g., during OTA updates).
   * ⚠️ Must re-enable promptly.
   */
  void disable();

  /**
   * Re-enable the watchdog after a temporary disable.
   */
  void enable();

private:
  bool _enabled;
  int _resetCount;

  void _incrementResetCounter();
  void _loadResetCount();
};

#endif // WATCHDOG_MANAGER_H
