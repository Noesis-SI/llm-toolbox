#pragma once

#include <Arduino.h>

struct OtaAppConfig {
  const char *wifiSsid;
  const char *wifiPassword;
  const char *hostname;
  const char *otaPassword;
};

// Defined in the project's generated src/device_config.cpp.
extern const OtaAppConfig otaAppConfig;

// Implemented by the application. OtaApp defines setup() and loop() itself and calls these.
// appSetup() runs once after WiFi has connected (or after a 15 s connection timeout).
// appLoop() runs repeatedly. Both may block; OTA updates are served from a separate task.
void appSetup();
void appLoop();

// Firmware that has never joined WiFi rolls back by itself: if a newly flashed image cannot connect
// within 60 s of booting, the device restarts into the firmware it was running before the update.

// True when the application crashed on several consecutive boots and is therefore not being run.
// The device then only serves OTA updates and logs, until new firmware is flashed or power is cycled.
bool otaAppInSafeMode();

// Use like Serial: Log.println(...), Log.printf(...). Output goes to Serial and to a buffer that
// `tb esp-logs` reads over WiFi. The buffer survives crashes and reboots, but not power loss.
// Safe to call from any task; not from interrupt handlers.
class OtaAppLog : public Print {
 public:
  using Print::write;
  size_t write(uint8_t byte) override;
  size_t write(const uint8_t *buffer, size_t size) override;
};

extern OtaAppLog Log;
