/*
 * SMS Handler — Implementation
 *
 * NVS-backed circular buffer for SMS deduplication.
 * Stores the last MAX_STORED_SMS_IDS message IDs in flash memory,
 * ensuring no duplicate forwarding even after unexpected power cycles.
 */

#include "sms_handler.h"
#include <mbedtls/md.h>

// ─── Constructor ─────────────────────────────────────────

SmsHandler::SmsHandler() : _ringIndex(0), _storedCount(0) {
  for (int i = 0; i < MAX_STORED_SMS_IDS; i++) {
    _smsIds[i] = "";
  }
}

// ─── Initialization ──────────────────────────────────────

bool SmsHandler::begin() {
  bool success = _prefs.begin(NVS_NAMESPACE, false);
  if (!success) {
    Serial.println("[SMS] ERROR: Failed to initialize NVS namespace");
    return false;
  }
  _loadFromNvs();
  Serial.printf("[SMS] NVS initialized. %d stored IDs loaded, ring index: %d\n",
                _storedCount, _ringIndex);
  return true;
}

// ─── Deduplication ───────────────────────────────────────

bool SmsHandler::isDuplicate(const String &smsId) {
  for (int i = 0; i < MAX_STORED_SMS_IDS; i++) {
    if (_smsIds[i] == smsId) {
      Serial.printf("[SMS] Duplicate detected: %s (slot %d)\n", smsId.c_str(),
                    i);
      return true;
    }
  }
  return false;
}

void SmsHandler::persistSmsId(const String &smsId) {
  _smsIds[_ringIndex] = smsId;
  _ringIndex = (_ringIndex + 1) % MAX_STORED_SMS_IDS;
  if (_storedCount < MAX_STORED_SMS_IDS) {
    _storedCount++;
  }
  _saveToNvs();
  Serial.printf("[SMS] Persisted ID: %s (slot %d, total: %d)\n", smsId.c_str(),
                (_ringIndex - 1 + MAX_STORED_SMS_IDS) % MAX_STORED_SMS_IDS,
                _storedCount);
}

// ─── SMS Reading ─────────────────────────────────────────

SmsMessage SmsHandler::readNextSms(HardwareSerial &serial) {
  SmsMessage msg;
  msg.isValid = false;

  // Set SMS to text mode
  _sendATCommand(serial, "AT+CMGF=1");

  // List unread messages
  String response = _sendATCommand(serial, "AT+CMGL=\"REC UNREAD\"", 5000);

  if (response.indexOf("+CMGL:") == -1) {
    return msg; // No unread messages
  }

  msg = _parseRawSms(response);

  if (msg.isValid) {
    msg.id = generateSmsId(msg.sender, msg.timestamp, msg.body);
  }

  return msg;
}

void SmsHandler::deleteSmsFromSim(HardwareSerial &serial, int index) {
  String cmd = "AT+CMGD=" + String(index);
  _sendATCommand(serial, cmd);
  Serial.printf("[SMS] Deleted SMS at index %d from SIM\n", index);
}

// ─── ID Generation ───────────────────────────────────────

String SmsHandler::generateSmsId(const String &sender, const String &timestamp,
                                 const String &body) {
  // SHA-256 hash of sender + timestamp + first 32 chars of body
  String input = sender + "|" + timestamp + "|" + body.substring(0, 32);

  byte hash[32];
  mbedtls_md_context_t ctx;
  mbedtls_md_init(&ctx);
  mbedtls_md_setup(&ctx, mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), 0);
  mbedtls_md_starts(&ctx);
  mbedtls_md_update(&ctx, (const unsigned char *)input.c_str(), input.length());
  mbedtls_md_finish(&ctx, hash);
  mbedtls_md_free(&ctx);

  // Convert first 8 bytes to hex string (16-char ID)
  String id = "";
  for (int i = 0; i < 8; i++) {
    char hex[3];
    sprintf(hex, "%02x", hash[i]);
    id += hex;
  }
  return id;
}

int SmsHandler::getStoredIdCount() const { return _storedCount; }

// ─── NVS Persistence (Private) ───────────────────────────

void SmsHandler::_loadFromNvs() {
  _ringIndex = _prefs.getInt(NVS_KEY_INDEX, 0);
  _storedCount = 0;

  for (int i = 0; i < MAX_STORED_SMS_IDS; i++) {
    String key = "id_" + String(i);
    _smsIds[i] = _prefs.getString(key.c_str(), "");
    if (_smsIds[i].length() > 0) {
      _storedCount++;
    }
  }
}

void SmsHandler::_saveToNvs() {
  _prefs.putInt(NVS_KEY_INDEX, _ringIndex);

  for (int i = 0; i < MAX_STORED_SMS_IDS; i++) {
    String key = "id_" + String(i);
    _prefs.putString(key.c_str(), _smsIds[i]);
  }
}

// ─── AT Command Helper (Private) ─────────────────────────

String SmsHandler::_sendATCommand(HardwareSerial &serial, const String &cmd,
                                  unsigned long timeout) {
  serial.println(cmd);

  unsigned long start = millis();
  String response = "";

  while (millis() - start < timeout) {
    while (serial.available()) {
      char c = serial.read();
      response += c;
    }
    if (response.indexOf("OK") != -1 || response.indexOf("ERROR") != -1) {
      break;
    }
    delay(10);
  }

  return response;
}

// ─── SMS Parsing (Private) ───────────────────────────────

SmsMessage SmsHandler::_parseRawSms(const String &raw) {
  SmsMessage msg;
  msg.isValid = false;

  /*
   * Expected format (text mode):
   * +CMGL: <index>,<stat>,<sender>,,<timestamp>\r\n
   * <message body>\r\n
   */

  int headerStart = raw.indexOf("+CMGL:");
  if (headerStart == -1)
    return msg;

  int headerEnd = raw.indexOf('\n', headerStart);
  if (headerEnd == -1)
    return msg;

  String header = raw.substring(headerStart, headerEnd);

  // Extract sender (third field in quotes)
  int firstQuote = header.indexOf('"');
  // Skip stat field
  int senderStart =
      header.indexOf('"', header.indexOf('"', firstQuote + 1) + 1);
  int senderEnd = header.indexOf('"', senderStart + 1);

  if (senderStart != -1 && senderEnd != -1) {
    msg.sender = header.substring(senderStart + 1, senderEnd);
  }

  // Extract timestamp (last quoted field)
  int tsStart = header.lastIndexOf('"');
  int tsEnd = header.lastIndexOf('"', tsStart - 1);
  // Actually re-parse: find the 4th pair of quotes
  int quotePos = -1;
  int quoteCount = 0;
  for (int i = 0; i < (int)header.length(); i++) {
    if (header[i] == '"') {
      quoteCount++;
      if (quoteCount == 7)
        tsEnd = i;
      if (quoteCount == 8) {
        tsStart = i;
        break;
      }
    }
  }
  if (tsEnd > 0 && tsStart > tsEnd) {
    msg.timestamp = header.substring(tsEnd + 1, tsStart);
  }

  // Extract body (lines after header until next +CMGL or OK)
  int bodyStart = headerEnd + 1;
  int bodyEnd = raw.indexOf("+CMGL:", bodyStart);
  if (bodyEnd == -1)
    bodyEnd = raw.indexOf("\r\nOK", bodyStart);
  if (bodyEnd == -1)
    bodyEnd = raw.length();

  msg.body = raw.substring(bodyStart, bodyEnd);
  msg.body.trim();

  if (msg.sender.length() > 0 && msg.body.length() > 0) {
    msg.isValid = true;
  }

  return msg;
}
