#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "esp_log.h"

static const char *TAG = "camscan_fw";

// Emit one S (status) line per the serial contract:
//   S,<uptime_ms>,<free_heap>,<frames_dropped>
static void emit_status(uint32_t frames_dropped)
{
    uint32_t uptime_ms = (uint32_t)(esp_timer_get_time() / 1000);
    uint32_t free_heap = (uint32_t)esp_get_free_heap_size();
    printf("S,%lu,%lu,%lu\n",
           (unsigned long)uptime_ms,
           (unsigned long)free_heap,
           (unsigned long)frames_dropped);
    fflush(stdout);
}

void app_main(void)
{
    ESP_LOGI(TAG, "camscan firmware boot - phase 3 serial protocol");

    while (1) {
        vTaskDelay(pdMS_TO_TICKS(1000));
        emit_status(0);   // frames_dropped hardcoded 0 until Phase 4
    }
}
