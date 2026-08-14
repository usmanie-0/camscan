// ble_scan_test.c  --  Phase 4.5, Step 4: emit the real L line
//
// Standalone BLE scan. Parses AD structures (name + manufacturer company
// ID) and emits the Phase 4.5 protocol deliverable, one line per advert:
//
//   L,<bdaddr>,<rssi>,<name>,<mfr_id>
//
// name: parsed local name with commas sanitized to spaces; EMPTY if absent
//       (empty field = "no name seen", honest CSV convention).
// mfr_id: 4-hex-digit company ID, or EMPTY if no manufacturer data.
//
// Vendor/camera matching is intentionally NOT done here -- it lives on the
// Python side (Phase 5), matching mfr_id and name against a table next to
// data/camera_vendors.txt. Firmware stays dumb and portable.
//
#include <stdio.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "nvs_flash.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "host/ble_hs.h"
#include "host/util/util.h"

static const char *TAG = "ble_scan";

#define AD_TYPE_NAME_SHORT  0x08
#define AD_TYPE_NAME_FULL   0x09
#define AD_TYPE_MFR_DATA    0xFF

static void start_scan(void);

static void parse_ad(const uint8_t *data, uint8_t len,
                     char *name_out, size_t name_sz,
                     uint16_t *mfr_id, bool *have_mfr)
{
    name_out[0] = '\0';
    *have_mfr = false;
    *mfr_id = 0;

    size_t i = 0;
    while (i < len) {
        uint8_t field_len = data[i];
        if (field_len == 0) {
            break;
        }
        if (i + 1 + field_len > len) {
            break;
        }
        uint8_t type = data[i + 1];
        const uint8_t *val = &data[i + 2];
        uint8_t val_len = field_len - 1;

        if (type == AD_TYPE_NAME_FULL || type == AD_TYPE_NAME_SHORT) {
            size_t copy = val_len;
            if (copy > name_sz - 1) {
                copy = name_sz - 1;
            }
            memcpy(name_out, val, copy);
            name_out[copy] = '\0';
            // Sanitize: a comma in the name would break the CSV contract.
            // Also strip any control chars / stray newlines defensively.
            for (size_t k = 0; k < copy; k++) {
                if (name_out[k] == ',' ) name_out[k] = ' ';
                else if (name_out[k] < 0x20 || name_out[k] == 0x7F) name_out[k] = ' ';
            }
        } else if (type == AD_TYPE_MFR_DATA) {
            if (val_len >= 2) {
                *mfr_id = (uint16_t)val[0] | ((uint16_t)val[1] << 8);
                *have_mfr = true;
            }
        }

        i += field_len + 1;
    }
}

static int gap_event(struct ble_gap_event *event, void *arg)
{
    if (event->type == BLE_GAP_EVENT_DISC) {
        char addr[18];
        const uint8_t *a = event->disc.addr.val;
        snprintf(addr, sizeof(addr), "%02X:%02X:%02X:%02X:%02X:%02X",
                 a[5], a[4], a[3], a[2], a[1], a[0]);

        char name[32];
        uint16_t mfr_id;
        bool have_mfr;
        parse_ad(event->disc.data, event->disc.length_data,
                 name, sizeof(name), &mfr_id, &have_mfr);

        char mfr_str[8];
        if (have_mfr) {
            snprintf(mfr_str, sizeof(mfr_str), "%04X", mfr_id);
        } else {
            mfr_str[0] = '\0';   // empty field = no manufacturer data
        }

        // L,<bdaddr>,<rssi>,<name>,<mfr_id>
        // name and mfr_id are empty strings when absent.
        printf("L,%s,%d,%s,%s\n",
               addr, event->disc.rssi, name, mfr_str);
        return 0;
    }
    return 0;
}

static void start_scan(void)
{
    struct ble_gap_disc_params p = {0};
    p.passive          = 1;
    p.filter_duplicates = 0;
    p.itvl             = 0;
    p.window           = 0;
    p.filter_policy    = 0;
    p.limited          = 0;

    int rc = ble_gap_disc(BLE_OWN_ADDR_PUBLIC, BLE_HS_FOREVER, &p,
                          gap_event, NULL);
    if (rc != 0) {
        ESP_LOGE(TAG, "ble_gap_disc failed; rc=%d", rc);
    } else {
        ESP_LOGI(TAG, "passive scan started");
    }
}

static void on_sync(void)
{
    int rc = ble_hs_util_ensure_addr(0);
    if (rc != 0) {
        ESP_LOGE(TAG, "ensure_addr failed; rc=%d", rc);
        return;
    }
    ESP_LOGI(TAG, "host synced, starting scan");
    start_scan();
}

static void host_task(void *param)
{
    ESP_LOGI(TAG, "nimble host task started");
    nimble_port_run();
    nimble_port_freertos_deinit();
}

void app_main(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    ret = nimble_port_init();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "nimble_port_init failed; ret=%d", ret);
        return;
    }

    ble_hs_cfg.sync_cb = on_sync;
    nimble_port_freertos_init(host_task);
    ESP_LOGI(TAG, "app_main done; host task running");
}
