#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"

static const char *TAG = "camscan_fw";

void app_main(void)
{
    ESP_LOGI(TAG, "camscan firmware boot - phase 3 skeleton");

    uint32_t tick = 0;
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(1000));
        ESP_LOGI(TAG, "alive tick %lu", (unsigned long)tick++);
    }
}
