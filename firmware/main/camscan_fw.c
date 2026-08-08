// camscan_fw.c  --  Phase 4, Step 2: per-MAC aggregation table -> D summary lines
//
// Builds on Step 1 (promiscuous DATA capture + 802.11 header parse). Now, instead
// of printing every frame, the callback folds each frame into a fixed-size per-MAC
// table, and a 1-second status loop emits one "D" summary line per active device
// and resets the window.
//
// D line contract (the Phase 4 deliverable):
//   D,<mac>,<band>,<ch>,<rssi_avg>,<frames>,<bytes_up>,<bytes_down>,<h0>,<h1>,<h2>,<h3>,<retries>
//   band  : "2G" | "5G"   (we're 2.4 GHz only for now)
//   rssi_avg : mean dBm of this station's OWN (uplink) frames; 0 = no uplink this window
//   up = ToDS (station->AP),  down = FromDS (AP->station)
//   h0..h3 : frame-size buckets  <100 / 100-500 / 500-1000 / >1000 bytes (len incl. FCS)
//   retries: frames seen with the retry bit set (TALLIED here; Step 4 will EXCLUDE them)
//
// Attribution (the core design decision -- see the chat notes):
//   uplink   (ToDS=1,FromDS=0): station = Addr2 (TA), count as bytes_up, feed rssi
//   downlink (ToDS=0,FromDS=1): station = Addr1 (RA), count as bytes_down, no rssi
//   group-addressed station (broadcast/multicast) or our own board MAC -> skipped
//   WDS(1,1)/IBSS(0,0) -> ignored (not infrastructure station<->AP)
//
// NOT yet: retry dedup (Step 4), concurrency locking (Step 3 -- table is shared
// between the Wi-Fi task and the status loop with no lock; benign on a quiet channel).
//
// Protocol/data lines use printf()+fflush(stdout), NOT ESP_LOGI.

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

// 2.4 GHz channel to listen on. Set to your home AP's actual channel to see
// your own devices strongly (Step 5's hop removes this manual choice).
#define SNIFF_CHANNEL   6

// Fixed-size device table. Static (.bss), so free_heap must stay flat.
// If frames_dropped starts climbing in a dense environment, bump this.
#define TABLE_SIZE      64

// Keep the per-frame RAW echo (rate-capped) so you can cross-check the D lines
// against the frames that produced them. Set to 0 to silence once satisfied.
#define PRINT_RAW       1

static const char *TAG = "camscan";

// ---- Per-MAC table --------------------------------------------------------

typedef struct {
    bool     in_use;
    uint8_t  mac[6];
    uint32_t frames;
    uint32_t bytes_up;      // ToDS   (station -> AP)
    uint32_t bytes_down;    // FromDS (AP -> station)
    uint32_t h[4];          // size buckets: <100 / 100-500 / 500-1000 / >1000
    uint32_t retries;       // frames with retry bit set (tallied, not excluded yet)
    int32_t  rssi_sum;      // sum of UPLINK rssi (dBm, negative)
    uint32_t rssi_n;        // count of uplink frames feeding rssi_sum
} dev_entry_t;

// Shared between the Wi-Fi driver task (writes, in the callback) and the status
// loop (reads + resets). No lock yet -- Step 3 hardens this.
static dev_entry_t g_table[TABLE_SIZE];

static volatile uint32_t g_frames_dropped = 0;   // real now: table-full drops (cumulative)
static volatile uint32_t g_data_frames    = 0;   // total data frames seen (for RATE)
static uint8_t           g_base_mac[6];          // our own MAC, filtered as self-traffic

// Find the entry for `mac`, or claim a free slot. Returns NULL if the table is full.
static dev_entry_t *table_get(const uint8_t *mac)
{
    int free_slot = -1;
    for (int i = 0; i < TABLE_SIZE; i++) {
        if (g_table[i].in_use) {
            if (memcmp(g_table[i].mac, mac, 6) == 0) {
                return &g_table[i];
            }
        } else if (free_slot < 0) {
            free_slot = i;   // remember first free, but keep scanning for a match
        }
    }
    if (free_slot < 0) {
        return NULL;         // table full
    }
    dev_entry_t *e = &g_table[free_slot];
    e->in_use = true;
    memcpy(e->mac, mac, 6);  // counters are already zero (reset each window)
    return e;
}

// ---- Promiscuous RX callback (Wi-Fi driver task -- keep tiny) --------------

static void promisc_cb(void *buf, wifi_promiscuous_pkt_type_t type)
{
    if (type != WIFI_PKT_DATA) {
        return;
    }
    const wifi_promiscuous_pkt_t *pkt = (const wifi_promiscuous_pkt_t *)buf;
    uint16_t len = pkt->rx_ctrl.sig_len;   // total length incl. 4-byte FCS
    if (len < 24) {
        return;
    }
    g_data_frames++;

    const uint8_t *p = pkt->payload;
    uint8_t tods   = (p[1] >> 0) & 0x1;
    uint8_t fromds = (p[1] >> 1) & 0x1;
    uint8_t retry  = (p[1] >> 3) & 0x1;
    const uint8_t *ra = p + 4;    // Addr1 = receiver
    const uint8_t *ta = p + 10;   // Addr2 = transmitter
    int8_t rssi = pkt->rx_ctrl.rssi;

    // Which station does this frame belong to, and in which direction?
    const uint8_t *sta;
    bool is_up;
    if (tods && !fromds) {
        sta = ta;  is_up = true;    // uplink: the station transmitted it
    } else if (!tods && fromds) {
        sta = ra;  is_up = false;   // downlink: the station is the receiver
    } else {
        return;                     // WDS / IBSS -- not infrastructure, ignore
    }

    // Drop group-addressed "stations" (broadcast/multicast) and our own board.
    if (sta[0] & 0x01) {
        return;
    }
    if (memcmp(sta, g_base_mac, 6) == 0) {
        return;
    }

#if PRINT_RAW
    // Rate-capped echo of the frames we actually track, so D lines can be
    // hand-verified against them. Columns: RAW, station, dir, len, rssi, retry.
    {
        static int64_t last_print_us = 0;
        int64_t now_us = esp_timer_get_time();
        if (now_us - last_print_us >= 40000) {   // <= 25 lines/sec
            last_print_us = now_us;
            printf("RAW,%02x:%02x:%02x:%02x:%02x:%02x,%s,%u,%d,%u\n",
                   sta[0], sta[1], sta[2], sta[3], sta[4], sta[5],
                   is_up ? "UP" : "DN", (unsigned)len, (int)rssi, (unsigned)retry);
            fflush(stdout);
        }
    }
#endif

    // Aggregate into the table.
    dev_entry_t *e = table_get(sta);
    if (e == NULL) {
        g_frames_dropped++;   // table full -> a real dropped frame
        return;
    }
    e->frames++;
    if (is_up) {
        e->bytes_up += len;
        e->rssi_sum += rssi;   // station's own signal
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

    // This build defaults the radio to 5 GHz-only (band mode 0x2); select 2.4 GHz
    // explicitly before tuning a 2.4 GHz channel, or set_channel() panics.
    ESP_ERROR_CHECK(esp_wifi_set_band_mode(WIFI_BAND_MODE_2G_ONLY));

    wifi_promiscuous_filter_t filt = { .filter_mask = WIFI_PROMIS_FILTER_MASK_DATA };
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous_filter(&filt));

    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous_rx_cb(promisc_cb));

    ESP_ERROR_CHECK(esp_wifi_set_channel(SNIFF_CHANNEL, WIFI_SECOND_CHAN_NONE));

    ESP_LOGI(TAG, "sniffer up: 2.4 GHz ch %d, DATA-only, promiscuous, table=%d",
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

    // Our own MAC -- filtered as self-traffic in the callback.
    ESP_ERROR_CHECK(esp_read_mac(g_base_mac, ESP_MAC_WIFI_STA));

    wifi_sniffer_start();

    uint32_t last_frames = 0;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(1000));

        // Emit one D line per active device, then reset the window.
        // (Reading + zeroing the table unlocked -- Step 3 makes this race-safe.)
        for (int i = 0; i < TABLE_SIZE; i++) {
            if (!g_table[i].in_use || g_table[i].frames == 0) {
                continue;
            }
            dev_entry_t *e = &g_table[i];
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

        memset(g_table, 0, sizeof(g_table));   // reset for next 1-second window

        // Status heartbeat -- frames_dropped is REAL now (cumulative table-full drops).
        uint32_t uptime_ms = (uint32_t)(esp_timer_get_time() / 1000);
        uint32_t free_heap = esp_get_free_heap_size();
        printf("S,%u,%u,%u\n",
               (unsigned)uptime_ms, (unsigned)free_heap, (unsigned)g_frames_dropped);

        // RATE diagnostic: total data frames seen this second (>= sum of D-line
        // frames; the difference is the broadcasts/multicast we correctly skipped).
        uint32_t now_frames = g_data_frames;
        printf("RATE,%u\n", (unsigned)(now_frames - last_frames));
        last_frames = now_frames;

        fflush(stdout);
    }
}
