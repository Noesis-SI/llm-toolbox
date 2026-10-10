#include "OtaApp.h"

#include <ArduinoOTA.h>
#include <Preferences.h>
#include <WiFi.h>
#include <esp_ota_ops.h>
#include <esp_system.h>

namespace {

const uint32_t RETAINED_STATE_MAGIC = 0x07AA9902;
const uint32_t SAFE_MODE_CRASH_THRESHOLD = 3;
const uint32_t STABLE_UPTIME_MS = 30000;
const uint32_t WIFI_CONNECT_TIMEOUT_MS = 15000;
const uint32_t ROLLBACK_WIFI_TIMEOUT_MS = 60000;
const size_t IMAGE_ID_BYTES = 8;
const char *PREFERENCES_NAMESPACE = "otaapp";
const char *CONFIRMED_IMAGE_KEY = "confirmed";
const uint32_t BACKGROUND_TASK_STACK_BYTES = 8192;
const UBaseType_t BACKGROUND_TASK_PRIORITY = 2;
const uint16_t LOG_SERVER_PORT = 3233;
// Must stay a power of two so buffer positions remain continuous when logBytesWritten overflows.
const size_t LOG_BUFFER_BYTES = 4096;

// RTC memory survives panics and software resets but not power loss, so it needs a validity marker.
RTC_NOINIT_ATTR uint32_t retainedStateMagic;
RTC_NOINIT_ATTR uint32_t consecutiveCrashes;
RTC_NOINIT_ATTR uint32_t logBytesWritten;
RTC_NOINIT_ATTR char logBuffer[LOG_BUFFER_BYTES];

SemaphoreHandle_t logMutex = nullptr;
WiFiServer logServer(LOG_SERVER_PORT);
esp_reset_reason_t resetReason;
bool safeMode = false;
volatile bool updateInProgress = false;
uint8_t runningImageId[IMAGE_ID_BYTES];
// True while this firmware image has never joined WiFi. Such an image is abandoned for the previous one.
bool awaitingFirstWifiConnection = false;

const char *resetReasonName() {
  switch (resetReason) {
    case ESP_RST_POWERON: return "power_on";
    case ESP_RST_EXT: return "reset_pin";
    case ESP_RST_SW: return "software_restart";
    case ESP_RST_PANIC: return "crash";
    case ESP_RST_INT_WDT: return "interrupt_watchdog";
    case ESP_RST_TASK_WDT: return "task_watchdog";
    case ESP_RST_WDT: return "watchdog";
    case ESP_RST_DEEPSLEEP: return "deep_sleep_wake";
    case ESP_RST_BROWNOUT: return "brownout";
    default: return "unknown";
  }
}

bool lastResetWasCrash() {
  switch (resetReason) {
    case ESP_RST_PANIC:
    case ESP_RST_INT_WDT:
    case ESP_RST_TASK_WDT:
    case ESP_RST_WDT:
      return true;
    default:
      return false;
  }
}

void recordBoot() {
  resetReason = esp_reset_reason();
  if (retainedStateMagic != RETAINED_STATE_MAGIC) {
    retainedStateMagic = RETAINED_STATE_MAGIC;
    consecutiveCrashes = 0;
    logBytesWritten = 0;
  } else if (lastResetWasCrash()) {
    consecutiveCrashes++;
  } else {
    consecutiveCrashes = 0;
  }
  safeMode = consecutiveCrashes >= SAFE_MODE_CRASH_THRESHOLD;
}

void loadImageConfirmation() {
  esp_app_desc_t description;
  if (esp_ota_get_partition_description(esp_ota_get_running_partition(), &description) != ESP_OK) {
    return;
  }
  memcpy(runningImageId, description.app_elf_sha256, IMAGE_ID_BYTES);

  uint8_t confirmedImageId[IMAGE_ID_BYTES] = {};
  Preferences preferences;
  preferences.begin(PREFERENCES_NAMESPACE, true);
  size_t storedBytes = preferences.getBytes(CONFIRMED_IMAGE_KEY, confirmedImageId, IMAGE_ID_BYTES);
  preferences.end();
  awaitingFirstWifiConnection =
    storedBytes != IMAGE_ID_BYTES || memcmp(confirmedImageId, runningImageId, IMAGE_ID_BYTES) != 0;
}

void confirmImage() {
  Preferences preferences;
  preferences.begin(PREFERENCES_NAMESPACE, false);
  preferences.putBytes(CONFIRMED_IMAGE_KEY, runningImageId, IMAGE_ID_BYTES);
  preferences.end();
  awaitingFirstWifiConnection = false;
  Log.println("OtaApp: this firmware joined WiFi and is now confirmed");
}

void rollBackToPreviousFirmware() {
  awaitingFirstWifiConnection = false;
  const esp_partition_t *previous = esp_ota_get_next_update_partition(nullptr);
  esp_app_desc_t description;
  if (previous == nullptr || esp_ota_get_partition_description(previous, &description) != ESP_OK) {
    Log.println("OtaApp: WiFi not joined within 60 s and no previous firmware exists to roll back to");
    return;
  }
  if (esp_ota_set_boot_partition(previous) != ESP_OK) {
    Log.println("OtaApp: WiFi not joined within 60 s, but switching to the previous firmware failed");
    return;
  }
  Log.println("OtaApp: ROLLBACK - this firmware never joined WiFi within 60 s, restarting into previous firmware");
  delay(200);
  ESP.restart();
}

// Wire format read by `tb esp-logs`: one JSON status line, then the raw retained log bytes, then close.
void serveLogClient() {
  WiFiClient client = logServer.available();
  if (!client) {
    return;
  }
  char *snapshot = static_cast<char *>(malloc(LOG_BUFFER_BYTES));
  if (snapshot == nullptr) {
    client.stop();
    return;
  }

  xSemaphoreTake(logMutex, portMAX_DELAY);
  uint32_t logEnd = logBytesWritten;
  size_t length = logEnd < LOG_BUFFER_BYTES ? logEnd : LOG_BUFFER_BYTES;
  for (size_t i = 0; i < length; i++) {
    snapshot[i] = logBuffer[(logEnd - length + i) % LOG_BUFFER_BYTES];
  }
  xSemaphoreGive(logMutex);

  client.printf(
    "{\"hostname\":\"%s\",\"uptime_ms\":%lu,\"reset_reason\":\"%s\",\"safe_mode\":%s,"
    "\"consecutive_crashes\":%lu,\"free_heap_bytes\":%lu,\"log_end\":%lu}\n",
    otaAppConfig.hostname, static_cast<unsigned long>(millis()), resetReasonName(), safeMode ? "true" : "false",
    static_cast<unsigned long>(consecutiveCrashes), static_cast<unsigned long>(ESP.getFreeHeap()),
    static_cast<unsigned long>(logEnd)
  );
  client.write(snapshot, length);
  client.stop();
  free(snapshot);
}

void backgroundTask(void *) {
  bool servicesStarted = false;
  for (;;) {
    if (WiFi.status() == WL_CONNECTED) {
      if (!servicesStarted) {
        ArduinoOTA.begin();
        logServer.begin();
        servicesStarted = true;
        Log.printf("OtaApp: WiFi connected, ip=%s\n", WiFi.localIP().toString().c_str());
      }
      if (awaitingFirstWifiConnection) {
        confirmImage();
      }
      ArduinoOTA.handle();
      serveLogClient();
    } else if (awaitingFirstWifiConnection && millis() > ROLLBACK_WIFI_TIMEOUT_MS) {
      rollBackToPreviousFirmware();
    }
    if (!safeMode && consecutiveCrashes > 0 && millis() > STABLE_UPTIME_MS) {
      consecutiveCrashes = 0;
    }
    delay(20);
  }
}

}  // namespace

OtaAppLog Log;

size_t OtaAppLog::write(uint8_t byte) {
  return write(&byte, 1);
}

size_t OtaAppLog::write(const uint8_t *buffer, size_t size) {
  Serial.write(buffer, size);
  if (logMutex != nullptr) {
    xSemaphoreTake(logMutex, portMAX_DELAY);
  }
  for (size_t i = 0; i < size; i++) {
    logBuffer[(logBytesWritten + i) % LOG_BUFFER_BYTES] = buffer[i];
  }
  logBytesWritten += size;
  if (logMutex != nullptr) {
    xSemaphoreGive(logMutex);
  }
  return size;
}

bool otaAppInSafeMode() {
  return safeMode;
}

void setup() {
  Serial.begin(115200);
  recordBoot();
  logMutex = xSemaphoreCreateMutex();
  Log.printf(
    "--- OtaApp boot: reset_reason=%s consecutive_crashes=%lu safe_mode=%s ---\n", resetReasonName(),
    static_cast<unsigned long>(consecutiveCrashes), safeMode ? "on (application not started)" : "off"
  );

  loadImageConfirmation();

  WiFi.mode(WIFI_STA);
  WiFi.setHostname(otaAppConfig.hostname);
  WiFi.begin(otaAppConfig.wifiSsid, otaAppConfig.wifiPassword);

  ArduinoOTA.setHostname(otaAppConfig.hostname);
  if (otaAppConfig.otaPassword[0] != '\0') {
    ArduinoOTA.setPassword(otaAppConfig.otaPassword);
  }
  ArduinoOTA.onStart([]() {
    updateInProgress = true;
    Log.println("OtaApp: OTA update started");
  });
  ArduinoOTA.onError([](ota_error_t error) {
    updateInProgress = false;
    Log.printf("OtaApp: OTA update failed, error=%u\n", static_cast<unsigned>(error));
  });
  xTaskCreate(backgroundTask, "ota_app", BACKGROUND_TASK_STACK_BYTES, nullptr, BACKGROUND_TASK_PRIORITY, nullptr);

  uint32_t connectStarted = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - connectStarted < WIFI_CONNECT_TIMEOUT_MS) {
    delay(100);
  }
  if (WiFi.status() != WL_CONNECTED) {
    Log.println("OtaApp: WiFi not connected after 15 s, still trying in the background");
  }

  if (!safeMode) {
    appSetup();
  }
}

void loop() {
  if (safeMode || updateInProgress) {
    delay(100);
    return;
  }
  appLoop();
}
