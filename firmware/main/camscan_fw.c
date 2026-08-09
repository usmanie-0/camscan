// camscan_fw.c  --  Phase 4, Step 4: retry dedup
//
// Builds on Step 3 (double-buffered table). Adds per-MAC retransmission
// suppression so retried frames don't get double-counted.
//
// Dedup rule: each entry remembers its last 12-bit sequence number. A frame
// with the retry bit set AND seq == entry->last_seq is a retransmission of a
// frame we already counted -> excluded from frames/bytes/histogram/rssi, and
// only tallied in `retries`. (retries in the D line now means "duplicates
// excluded".) A retry with a NEW seq is a retransmit whose original we never
// saw, so it's counted once.
//
// Scope: single last_seq per station, reset each 1-second window. Catches the
// common case (a retransmit immediately follows its original). Out-of-order or
// window-straddling retries are not deduped -- acceptable and rare.
//
// New per-second diagnostic: DEDUP,<n> = duplicates excluded that second
// (printed from the loop, NOT the callback -- hot path stays printf-free).
//
// D line contract (unchanged):
//   D,<mac>,<band>,<ch>,<rssi_avg>,<frames>,<bytes_up>,<bytes_down>,<h0..h3>,<retries>

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

#define SNIFF_CHANNEL   1      // user's 2.4 GHz AP is on ch 1
#define TABLE_SIZE      64
#define PRINT_RAW       0

static const char *TAG = "camscan";

// ---- Per-MAC table (double-buffered) --------------------------------------

typedef struct {
    bool     in_use;
    uint8_t  mac[6];
    uint32_t frames;
    uint32_t bytes_up;
    uint32_t bytes_down;
    uint32_t h[4];
    uint32_t retries;       // duplicates EXCLUDED this window
    int32_t  rssi_sum;
    uint32_t rssi_n;
    uint16_t last_seq;      // most-recent 12-bit seq, for dedup
    bool     seq_valid;     // false until the first counted frame this window
} dev_entry_t;

static dev_entry_t         g_buf[2][TABLE_SIZE];
static dev_entry_t * volatile g_active = g_buf[0];
static portMUX_TYPE        g_swap_mux = portMUX_INITIALIZER_UNLOCKED;

static volatile uint32_t g_frames_dropped = 0;   // table-full drops (cumulative)
static volatile uint32_t g_data_frames    = 0;   // total data frames (for RATE)
static volatile uint32_t g_dups           = 0;   // duplicates excluded (cumulative, for DEDUP)
static uint8_t           g_base_mac[6];

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

// ---- Promiscuous RX callback ----------------------------------------------

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
    // Sequence Control at fixed offset 22: high 12 bits = sequence number.
    uint16_t seq_ctrl = (uint16_t)p[22] | ((uint16_t)p[23] << 8);
    uint16_t seq = (seq_ctrl >> 4) & 0x0FFF;
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

    dev_entry_t *t = g_active;
    dev_entry_t *e = table_get(t, sta);
    if (e == NULL) {
        g_frames_dropped++;
        return;
    }

    // Retry dedup: retry-flagged frame repeating this station's last seq is a
    // retransmission of a frame already counted -> exclude, tally only.
    if (retry && e->seq_valid && seq == e->last_seq) {
        e->retries++;
        g_dups++;
        return;
    }

#if PRINT_RAW
    {
        static int64_t last_print_us = 0;
        int64_t now_us = esp_timer_get_time();
        if (now_us - last_print_us >= 40000) {
            last_print_us = now_us;
            printf("RAW,%02x:%02x:%02x:%02x:%02x:%02x,%s,%u,%u,%d,%u\n",
                   sta[0], sta[1], sta[2], sta[3], sta[4], sta[5],
                   is_up ? "UP" : "DN", (unsigned)len, (unsigned)seq,
                   (int)rssi, (unsigned)retry);
            fflush(stdout);
        }
    }
#endif

    // Count this frame as real (unique) traffic.
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
    e->last_seq = seq;
    e->seq_valid = true;
}

// ---- Wi-Fi bring-up -------------------------------------------------------

static void wifi_sniffer_start(void)
{
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_NULL));
    ESP_ERROR_CHECK(esp_wifi_start());

    ESP_ERROR_CHECK(esp_wifi_set_band_mode(WIFI_BAND_MODE_2G_ONLY));

    wifi_promiscuous_filter_t filt = { .filter_mask = WIFI_PROMIS_FILTER_MASK_DATA };
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous_filter(&filt));

    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous_rx_cb(promisc_cb));

    ESP_ERROR_CHECK(esp_wifi_set_channel(SNIFF_CHANNEL, WIFI_SECOND_CHAN_NONE));

    ESP_LOGI(TAG, "sniffer up: 2.4 GHz ch %d, DATA-only, double-buffered, dedup, table=%d",
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
    uint32_t last_dups   = 0;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(1000));

        taskENTER_CRITICAL(&g_swap_mux);
        dev_entry_t *snap = g_active;
        g_active = (snap == g_buf[0]) ? g_buf[1] : g_buf[0];
        taskEXIT_CRITICAL(&g_swap_mux);

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

        memset(snap, 0, sizeof(g_buf[0]));

        uint32_t uptime_ms = (uint32_t)(esp_timer_get_time() / 1000);
        uint32_t free_heap = esp_get_free_heap_size();
        printf("S,%u,%u,%u\n",
               (unsigned)uptime_ms, (unsigned)free_heap, (unsigned)g_frames_dropped);

        uint32_t now_frames = g_data_frames;
        printf("RATE,%u\n", (unsigned)(now_frames - last_frames));
        last_frames = now_frames;

        uint32_t now_dups = g_dups;
        printf("DEDUP,%u\n", (unsigned)(now_dups - last_dups));
        last_dups = now_dups;

        fflush(stdout);
    }
}
