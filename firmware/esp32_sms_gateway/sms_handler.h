/*
 * SMS Handler — Header
 *
 * Manages SMS reading from the SIM module and deduplication
 * using ESP32 Non-Volatile Storage (NVS). Maintains a circular
 * buffer of the last N SMS message IDs to prevent duplicate
 * forwarding after power cycles.
 */

#ifndef SMS_HANDLER_H
#define SMS_HANDLER_H

#include "config.h"
#include <Arduino.h>
#include <Preferences.h>

// ─── SMS Message Structure ───────────────────────────────
struct SmsMessage {
  String id;        // Unique SMS identifier (hash of sender+timestamp+content)
  String sender;    // Phone number of sender
  String body;      // Message content (OTP text)
  String timestamp; // ISO-8601 from SIM module
  bool isValid;     // Parsing success flag
};

class SmsHandler {
public:
  SmsHandler();

  /**
   * Initialize NVS and load previously stored SMS IDs.
   * Must be called in setup() before any SMS operations.
   */
  bool begin();

  /**
   * Check if an SMS ID has already been processed.
   * @param smsId  Unique identifier for the SMS
   * @return true if this SMS was already forwarded
   */
  bool isDuplicate(const String &smsId);

  /**
   * Store an SMS ID in the NVS-backed circular buffer.
   * Automatically evicts the oldest entry when buffer is full.
   * @param smsId  Unique identifier to persist
   */
  void persistSmsId(const String &smsId);

  /**
   * Read the next unread SMS from the SIM module.
   * @param serial  HardwareSerial connected to SIM module
   * @return Parsed SmsMessage (check .isValid)
   */
  SmsMessage readNextSms(HardwareSerial &serial);

  /**
   * Delete an SMS from the SIM module's memory.
   * @param serial  HardwareSerial connected to SIM module
   * @param index   SMS storage index on SIM
   */
  void deleteSmsFromSim(HardwareSerial &serial, int index);

  /**
   * Generate a unique ID for an SMS based on content hash.
   */
  String generateSmsId(const String &sender, const String &timestamp,
                       const String &body);

  /**
   * Get count of stored SMS IDs (for diagnostics).
   */
  int getStoredIdCount() const;

private:
  Preferences _prefs;
  String _smsIds[MAX_STORED_SMS_IDS];
  int _ringIndex;
  int _storedCount;

  void _loadFromNvs();
  void _saveToNvs();
  String _sendATCommand(HardwareSerial &serial, const String &cmd,
                        unsigned long timeout = 2000);
  SmsMessage _parseRawSms(const String &raw);
};

#endif // SMS_HANDLER_H
