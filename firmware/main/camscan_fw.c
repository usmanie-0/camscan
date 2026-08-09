// camscan_fw.c  --  Phase 4, Step 3: concurrency hardening (double-buffered table)
//
// Same D-line output as Step 2 -- this step only makes the shared table safe when
// the callback and the status loop race under load.
//
// The hazard (single-core C5): the promiscuous callback runs in the Wi-Fi task
// (prio 23) and can preempt the status loop (prio 1) at any instruction -- e.g.
// mid-reset. The loop can NEVER preempt the callback. So a busy channel can catch
// the loop mid-memset while the callback writes the same entry -> torn row.
//
// The fix: two tables. The callback always writes *g_active (no lock, hot path stays
// light). Once a second the loop swaps g_active to the other buffer under a tiny
// critical section (one pointer store), then drains + clears the now-inactive buffer
// with no lock held. After the swap the callback only ever touches the new buffer, so
// the drained one is exclusively the loop's. The critical section makes the swap
// atomic vs any reader (and keeps this correct on a hypothetical dual-core port).
//
// D line contract (unchanged from Step 2):
//   D,<mac>,<band>,<ch>,<rssi_avg>,<frames>,<bytes_up>,<bytes_down>,<h0>,<h1>,<h2>,<h3>,<retries>
//
// Attribution (unchanged): uplink(ToDS) station=Addr2 count bytes_up + rssi;
//   downlink(FromDS) station=Addr1 count bytes_down; skip group/self; ignore WDS/IBSS.
// NOT yet: retry dedup (Step 4).

#include <stdio.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_timer.h"
#include "esp_system.h"
#include "esp_mac.h"
#include "esp_log.h"
#include "nvs_flash.h"

// ---- Config ---------------------------------------------------------------

// For the Step 3 LOAD TEST set this to your home AP's 2.4 GHz channel, then
// stream video on a device on that network to push hundreds of frames/sec.
#define SNIFF_CHANNEL   1

#define TABLE_SIZE      64     // fixed, static; free_heap must stay flat
#define PRINT_RAW       0      // OFF for the load test (no printf in the hot path)

static const char *TAG = "camscan";

// ---- Per-MAC table (double-buffered) --------------------------------------

typedef struct {
    bool     in_use;
    uint8_t  mac[6];
    uint32_t frames;
    uint32_t bytes_up;      // ToDS   (station -> AP)
    uint32_t bytes_down;    // FromDS (AP -> station)
    uint32_t h[4];          // <100 / 100-500 / 500-1000 / >1000
    uint32_t retries;       // retry-bit frames (tallied; Step 4 excludes)
    int32_t  rssi_sum;      // sum of UPLINK rssi
    uint32_t rssi_n;        // count of uplink frames feeding rssi_sum
} dev_entry_t;

// Two buffers; the callback writes *g_active, the loop drains the other.
static dev_entry_t         g_buf[2][TABLE_SIZE];
static dev_entry_t * volatile g_active = g_buf[0];
static portMUX_TYPE        g_swap_mux = portMUX_INITIALIZER_UNLOCKED;

static volatile uint32_t g_frames_dropped = 0;   // table-full drops (cumulative)
static volatile uint32_t g_data_frames    = 0;   // total data frames (for RATE)
static uint8_t           g_base_mac[6];

// Find/insert `mac` in buffer `t`. NULL if full.
static dev_entry_t *table_get(dev_entry_t *t, const uint8_t *mac)
{
    int free_slot = -1;
    for (int i = 0; i < TABLE_SIZE; i++) {
        if (t[i].in_use) {
            if (memcmp(t[i].mac, mac, 6) == 0) {
                return &t[i];
            }
        } else if (free_slot < 0) {
            free_slot = i;
        }
    }
    if (free_slot < 0) {
        return NULL;
    }
    dev_entry_t *e = &t[free_slot];
    e->in_use = true;
    memcpy(e->mac, mac, 6);
    return e;
}

// ---- Promiscuous RX callback (Wi-Fi task, prio 23 -- lock-free hot path) ---

static void promisc_cb(void *buf, wifi_promiscuous_pkt_type_t type)
{
    if (type != WIFI_PKT_DATA) {
        return;
    }
    const wifi_promiscuous_pkt_t *pkt = (const wifi_promiscuous_pkt_t *)buf;
    uint16_t len = pkt->rx_ctrl.sig_len;
    if (len < 24) {
        return;
    }
    g_data_frames++;

    const uint8_t *p = pkt->payload;
    uint8_t tods   = (p[1] >> 0) & 0x1;
    uint8_t fromds = (p[1] >> 1) & 0x1;
    uint8_t retry  = (p[1] >> 3) & 0x1;
    const uint8_t *ra = p + 4;
    const uint8_t *ta = p + 10;
    int8_t rssi = pkt->rx_ctrl.rssi;

    const uint8_t *sta;
    bool is_up;
    if (tods && !fromds) {
        sta = ta;  is_up = true;
    } else if (!tods && fromds) {
        sta = ra;  is_up = false;
    } else {
        return;
    }
    if (sta[0] & 0x01) {
        return;
    }
    if (memcmp(sta, g_base_mac, 6) == 0) {
        return;
    }

#if PRINT_RAW
    {
        static int64_t last_print_us = 0;
        int64_t now_us = esp_timer_get_time();
        if (now_us - last_print_us >= 40000) {
            last_print_us = now_us;
            printf("RAW,%02x:%02x:%02x:%02x:%02x:%02x,%s,%u,%d,%u\n",
                   sta[0], sta[1], sta[2], sta[3], sta[4], sta[5],
                   is_up ? "UP" : "DN", (unsigned)len, (int)rssi, (unsigned)retry);
            fflush(stdout);
        }
    }
#endif

    // Snapshot the active buffer once, then aggregate into it. No lock needed:
    // after a swap the callback sees the new buffer, and being higher priority it
    // always finishes this update before the loop can drain whatever it wrote.
    dev_entry_t *t = g_active;
    dev_entry_t *e = table_get(t, sta);
    if (e == NULL) {
        g_frames_dropped++;
        return;
    }
    e->frames++;
    if (is_up) {
        e->bytes_up += len;
        e->rssi_sum += rssi;
        e->rssi_n++;
    } else {
        e->bytes_down += len;
    }
    if      (len < 100)  e->h[0]++;
    else if (len < 500)  e->h[1]++;
    else if (len < 1000) e->h[2]++;
    else                 e->h[3]++;
    if (retry) {
        e->retries++;
    }
}

// ---- Wi-Fi bring-up -------------------------------------------------------

static void wifi_sniffer_start(void)
{
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_NULL));
    ESP_ERROR_CHECK(esp_wifi_start());

    // Default band is 5 GHz-only; select 2.4 GHz before tuning the channel.
    ESP_ERROR_CHECK(esp_wifi_set_band_mode(WIFI_BAND_MODE_2G_ONLY));

    wifi_promiscuous_filter_t filt = { .filter_mask = WIFI_PROMIS_FILTER_MASK_DATA };
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous_filter(&filt));

    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous_rx_cb(promisc_cb));

    ESP_ERROR_CHECK(esp_wifi_set_channel(SNIFF_CHANNEL, WIFI_SECOND_CHAN_NONE));

    ESP_LOGI(TAG, "sniffer up: 2.4 GHz ch %d, DATA-only, double-buffered table=%d",
             SNIFF_CHANNEL, TABLE_SIZE);
}

// ---- app_main -------------------------------------------------------------

void app_main(void)
{
    esp_err_t r = nvs_flash_init();
    if (r == ESP_ERR_NVS_NO_FREE_PAGES || r == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        r = nvs_flash_init();
    }
    ESP_ERROR_CHECK(r);

    ESP_ERROR_CHECK(esp_read_mac(g_base_mac, ESP_MAC_WIFI_STA));

    wifi_sniffer_start();

    uint32_t last_frames = 0;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(1000));

        // Swap active/drain buffers under a tiny critical section (one store).
        taskENTER_CRITICAL(&g_swap_mux);
        dev_entry_t *snap = g_active;
        g_active = (snap == g_buf[0]) ? g_buf[1] : g_buf[0];
        taskEXIT_CRITICAL(&g_swap_mux);

        // Drain the now-inactive buffer -- callback no longer touches it, no lock.
        for (int i = 0; i < TABLE_SIZE; i++) {
            if (!snap[i].in_use || snap[i].frames == 0) {
                continue;
            }
            dev_entry_t *e = &snap[i];
            int rssi_avg = (e->rssi_n > 0) ? (int)(e->rssi_sum / (int32_t)e->rssi_n) : 0;
            printf("D,%02x:%02x:%02x:%02x:%02x:%02x,2G,%u,%d,%u,%u,%u,%u,%u,%u,%u,%u\n",
                   e->mac[0], e->mac[1], e->mac[2], e->mac[3], e->mac[4], e->mac[5],
                   (unsigned)SNIFF_CHANNEL,
                   rssi_avg,
                   (unsigned)e->frames,
                   (unsigned)e->bytes_up, (unsigned)e->bytes_down,
                   (unsigned)e->h[0], (unsigned)e->h[1], (unsigned)e->h[2], (unsigned)e->h[3],
                   (unsigned)e->retries);
        }
        fflush(stdout);

        memset(snap, 0, sizeof(g_buf[0]));   // clear the drained buffer for reuse

        uint32_t uptime_ms = (uint32_t)(esp_timer_get_time() / 1000);
        uint32_t free_heap = esp_get_free_heap_size();
        printf("S,%u,%u,%u\n",
               (unsigned)uptime_ms, (unsigned)free_heap, (unsigned)g_frames_dropped);

        uint32_t now_frames = g_data_frames;
        printf("RATE,%u\n", (unsigned)(now_frames - last_frames));
        last_frames = now_frames;

        fflush(stdout);
    }
}
