// camscan_fw.c  --  Phase 4, Step 6: dual-band hop (2.4 GHz + 5 GHz)
//
// Extends Step 5's two-stage hop across both bands. The ESP32-C5 is
// single-band-at-a-time, so a 5 GHz channel needs band mode 5G_ONLY and a
// 2.4 GHz channel needs 2G_ONLY -- hop_to() switches band mode only when the
// band actually changes (the channel list is ordered 2.4 first, then 5 GHz).
//
// DFS channels (52-64 here): we are RX-only and never transmit, so passive
// listening is permitted. The driver may still refuse set_channel on some of
// them depending on region -- that's handled by the same graceful skip as any
// other rejected channel (not ESP_ERROR_CHECK'd).
//
// Line contracts (band field now "2G" or "5G"):
//   D,<mac>,<band>,<ch>,<rssi_avg>,<frames>,<bytes_up>,<bytes_down>,<h0..h3>,<retries>
//   C,<band>,<ch>,<dwell_ms>,<frames>,<bytes>,<stations>,<strongest_rssi>,<max_up_bytes>
//   S,<uptime_ms>,<free_heap>,<frames_dropped>
//
// Callback UNCHANGED from Step 4/5 (aggregation + retry dedup).

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

typedef struct { bool is_5g; uint8_t ch; } chan_t;

// 2.4 GHz first (1..13), then 5 GHz (UNII-1, lower DFS block, UNII-3).
// Ordered so the band mode flips at most once per survey pass.
static const chan_t CHANS[] = {
    {false,1},{false,2},{false,3},{false,4},{false,5},{false,6},{false,7},
    {false,8},{false,9},{false,10},{false,11},{false,12},{false,13},
    {true,36},{true,40},{true,44},{true,48},           // UNII-1 (non-DFS)
    {true,52},{true,56},{true,60},{true,64},           // UNII-2A (DFS)
    {true,149},{true,153},{true,157},{true,161},{true,165}, // UNII-3 (non-DFS)
};
#define N_CH        (sizeof(CHANS) / sizeof(CHANS[0]))
#define SURVEY_MS   300
#define FOCUS_SECS  3
#define FOCUS_TOP_K 3
#define TABLE_SIZE  64

static const char *TAG = "camscan";

// ---- Per-MAC table (double-buffered) --------------------------------------

typedef struct {
    bool     in_use;
    uint8_t  mac[6];
    uint32_t frames;
    uint32_t bytes_up;
    uint32_t bytes_down;
    uint32_t h[4];
    uint32_t retries;
    int32_t  rssi_sum;
    uint32_t rssi_n;
    uint16_t last_seq;
    bool     seq_valid;
} dev_entry_t;

static dev_entry_t         g_buf[2][TABLE_SIZE];
static dev_entry_t * volatile g_active = g_buf[0];
static portMUX_TYPE        g_swap_mux = portMUX_INITIALIZER_UNLOCKED;

static volatile uint32_t g_frames_dropped = 0;
static volatile uint32_t g_dups           = 0;
static uint8_t           g_base_mac[6];
static uint8_t           g_cur_channel = 0;
static bool              g_cur_is5g    = false;
static wifi_band_mode_t  g_cur_band_mode = WIFI_BAND_MODE_2G_ONLY;

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

// ---- Promiscuous RX callback (unchanged) ----------------------------------

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

    const uint8_t *p = pkt->payload;
    uint8_t tods   = (p[1] >> 0) & 0x1;
    uint8_t fromds = (p[1] >> 1) & 0x1;
    uint8_t retry  = (p[1] >> 3) & 0x1;
    const uint8_t *ra = p + 4;
    const uint8_t *ta = p + 10;
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

    if (retry && e->seq_valid && seq == e->last_seq) {
        e->retries++;
        g_dups++;
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
    e->last_seq = seq;
    e->seq_valid = true;
}

// ---- Hop + emit helpers ---------------------------------------------------

// Retune to (band, ch). Switches band mode only on a band change. Returns
// false if the driver rejects the band or channel (region/DFS) -> caller skips.
static bool hop_to(bool is_5g, uint8_t ch)
{
    wifi_band_mode_t want = is_5g ? WIFI_BAND_MODE_5G_ONLY : WIFI_BAND_MODE_2G_ONLY;
    if (want != g_cur_band_mode) {
        if (esp_wifi_set_band_mode(want) != ESP_OK) {
            return false;
        }
        g_cur_band_mode = want;
    }
    if (esp_wifi_set_channel(ch, WIFI_SECOND_CHAN_NONE) != ESP_OK) {
        return false;
    }
    g_cur_channel = ch;
    g_cur_is5g = is_5g;

    taskENTER_CRITICAL(&g_swap_mux);
    dev_entry_t *snap = g_active;
    g_active = (snap == g_buf[0]) ? g_buf[1] : g_buf[0];
    taskEXIT_CRITICAL(&g_swap_mux);
    memset(snap, 0, sizeof(g_buf[0]));
    return true;
}

static uint32_t emit_window(uint32_t dwell_ms, bool with_d)
{
    taskENTER_CRITICAL(&g_swap_mux);
    dev_entry_t *snap = g_active;
    g_active = (snap == g_buf[0]) ? g_buf[1] : g_buf[0];
    taskEXIT_CRITICAL(&g_swap_mux);

    const char *band = g_cur_is5g ? "5G" : "2G";
    uint32_t tot_frames = 0, tot_bytes = 0, stations = 0, max_up = 0;
    int strongest = 1;

    for (int i = 0; i < TABLE_SIZE; i++) {
        if (!snap[i].in_use || snap[i].frames == 0) {
            continue;
        }
        dev_entry_t *e = &snap[i];
        stations++;
        tot_frames += e->frames;
        tot_bytes  += e->bytes_up + e->bytes_down;
        if (e->bytes_up > max_up) {
            max_up = e->bytes_up;
        }
        int rssi_avg = 0;
        if (e->rssi_n > 0) {
            rssi_avg = (int)(e->rssi_sum / (int32_t)e->rssi_n);
            if (strongest == 1 || rssi_avg > strongest) {
                strongest = rssi_avg;
            }
        }
        if (with_d) {
            printf("D,%02x:%02x:%02x:%02x:%02x:%02x,%s,%u,%d,%u,%u,%u,%u,%u,%u,%u,%u\n",
                   e->mac[0], e->mac[1], e->mac[2], e->mac[3], e->mac[4], e->mac[5],
                   band, (unsigned)g_cur_channel,
                   rssi_avg,
                   (unsigned)e->frames,
                   (unsigned)e->bytes_up, (unsigned)e->bytes_down,
                   (unsigned)e->h[0], (unsigned)e->h[1], (unsigned)e->h[2], (unsigned)e->h[3],
                   (unsigned)e->retries);
        }
    }

    int strongest_out = (strongest == 1) ? 0 : strongest;
    printf("C,%s,%u,%u,%u,%u,%u,%d,%u\n",
           band, (unsigned)g_cur_channel, (unsigned)dwell_ms,
           (unsigned)tot_frames, (unsigned)tot_bytes, (unsigned)stations,
           strongest_out, (unsigned)max_up);
    fflush(stdout);

    memset(snap, 0, sizeof(g_buf[0]));
    return tot_frames;
}

static void print_status(void)
{
    uint32_t uptime_ms = (uint32_t)(esp_timer_get_time() / 1000);
    uint32_t free_heap = esp_get_free_heap_size();
    printf("S,%u,%u,%u\n",
           (unsigned)uptime_ms, (unsigned)free_heap, (unsigned)g_frames_dropped);
    fflush(stdout);
}

// ---- Wi-Fi bring-up -------------------------------------------------------

static void wifi_sniffer_start(void)
{
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_NULL));
    ESP_ERROR_CHECK(esp_wifi_start());

    // Start in 2.4 GHz; hop_to() switches bands as it sweeps.
    ESP_ERROR_CHECK(esp_wifi_set_band_mode(WIFI_BAND_MODE_2G_ONLY));
    g_cur_band_mode = WIFI_BAND_MODE_2G_ONLY;

    wifi_promiscuous_filter_t filt = { .filter_mask = WIFI_PROMIS_FILTER_MASK_DATA };
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous_filter(&filt));

    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous_rx_cb(promisc_cb));

    ESP_LOGI(TAG, "sniffer up: dual-band hop (%d chans, survey %dms, focus top-%d x %ds)",
             (int)N_CH, SURVEY_MS, FOCUS_TOP_K, FOCUS_SECS);
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

    for (;;) {
        // ---- STAGE 1: SURVEY ----
        uint32_t score[N_CH];
        for (int i = 0; i < (int)N_CH; i++) {
            score[i] = 0;
            if (!hop_to(CHANS[i].is_5g, CHANS[i].ch)) {
                continue;   // band/channel refused (region/DFS) -> skip
            }
            vTaskDelay(pdMS_TO_TICKS(SURVEY_MS));
            score[i] = emit_window(SURVEY_MS, false);
        }
        print_status();

        // ---- pick the top-K busiest channels (nonzero only) ----
        int focus_idx[FOCUS_TOP_K];
        int n_focus = 0;
        bool used[N_CH] = { false };
        for (int k = 0; k < FOCUS_TOP_K; k++) {
            int best = -1;
            uint32_t best_score = 0;
            for (int i = 0; i < (int)N_CH; i++) {
                if (!used[i] && score[i] > best_score) {
                    best_score = score[i];
                    best = i;
                }
            }
            if (best < 0) {
                break;
            }
            used[best] = true;
            focus_idx[n_focus++] = best;
        }

        // ---- STAGE 2: FOCUS ----
        for (int f = 0; f < n_focus; f++) {
            if (!hop_to(CHANS[focus_idx[f]].is_5g, CHANS[focus_idx[f]].ch)) {
                continue;
            }
            for (int s = 0; s < FOCUS_SECS; s++) {
                vTaskDelay(pdMS_TO_TICKS(1000));
                emit_window(1000, true);
            }
            print_status();
        }
    }
}
