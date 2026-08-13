// camscan_fw.c  --  Phase 4, Step 5: two-stage 2.4 GHz channel hop + C lines
//
// The fixed single channel is gone. The app loop now sweeps the band:
//
//   STAGE 1 (survey): hop channels 1..13, dwell SURVEY_MS each, emit one C line
//                     per channel (channel summary). No D lines -- coarse pass.
//   STAGE 2 (focus):  rank channels by activity, dwell FOCUS_SECS seconds on the
//                     top FOCUS_TOP_K, emitting detailed D lines (tagged with the
//                     current channel) plus a C line each 1-second window.
//   ...then loop back to survey.
//
// The promiscuous callback is UNCHANGED from Step 4 (aggregation + retry dedup).
// Everything new is in app_main / the hop + emit helpers.
//
// Line contracts:
//   D,<mac>,<band>,<ch>,<rssi_avg>,<frames>,<bytes_up>,<bytes_down>,<h0..h3>,<retries>
//   C,<band>,<ch>,<dwell_ms>,<frames>,<bytes>,<stations>,<strongest_rssi>,<max_up_bytes>
//       strongest_rssi = closest (max) uplink rssi_avg on the channel; 0 = no uplink
//       max_up_bytes   = largest single station's bytes_up (camera hint)
//   S,<uptime_ms>,<free_heap>,<frames_dropped>
//
// set_channel is NOT wrapped in ESP_ERROR_CHECK: region-blocked channels
// (e.g. 12/13) are skipped, not fatal.

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

static const uint8_t CH_LIST[] = {1,2,3,4,5,6,7,8,9,10,11,12,13};
#define N_CH        (sizeof(CH_LIST) / sizeof(CH_LIST[0]))
#define SURVEY_MS   300     // per-channel dwell in the survey pass
#define FOCUS_SECS  3       // per-channel dwell (in 1 s windows) in the focus pass
#define FOCUS_TOP_K 3       // how many busiest channels to focus on
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
static volatile uint32_t g_dups           = 0;   // deduped (kept for future use)
static uint8_t           g_base_mac[6];
static uint8_t           g_cur_channel = 0;       // channel the loop is parked on

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

// ---- Promiscuous RX callback (unchanged from Step 4) ----------------------

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

// ---- Hop + emit helpers (run in the loop task) ----------------------------

// Retune to `ch` and start it clean. Returns false if the driver rejects the
// channel (region-blocked) -- caller skips it. Discards any straddling frames
// by swapping to the already-cleared buffer and memset-ing the one we leave.
static bool hop_to(uint8_t ch)
{
    if (esp_wifi_set_channel(ch, WIFI_SECOND_CHAN_NONE) != ESP_OK) {
        return false;
    }
    g_cur_channel = ch;

    taskENTER_CRITICAL(&g_swap_mux);
    dev_entry_t *snap = g_active;
    g_active = (snap == g_buf[0]) ? g_buf[1] : g_buf[0];
    taskEXIT_CRITICAL(&g_swap_mux);
    memset(snap, 0, sizeof(g_buf[0]));   // discard old-channel tail
    return true;
}

// Swap out the current window, emit its C summary (+ D lines if with_d), clear
// it, and return the channel's frame count (used as the survey activity score).
static uint32_t emit_window(uint32_t dwell_ms, bool with_d)
{
    taskENTER_CRITICAL(&g_swap_mux);
    dev_entry_t *snap = g_active;
    g_active = (snap == g_buf[0]) ? g_buf[1] : g_buf[0];
    taskEXIT_CRITICAL(&g_swap_mux);

    uint32_t tot_frames = 0, tot_bytes = 0, stations = 0, max_up = 0;
    int strongest = 1;   // sentinel (rssi is always negative; 1 = "none seen")

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
            printf("D,%02x:%02x:%02x:%02x:%02x:%02x,2G,%u,%d,%u,%u,%u,%u,%u,%u,%u,%u\n",
                   e->mac[0], e->mac[1], e->mac[2], e->mac[3], e->mac[4], e->mac[5],
                   (unsigned)g_cur_channel,
                   rssi_avg,
                   (unsigned)e->frames,
                   (unsigned)e->bytes_up, (unsigned)e->bytes_down,
                   (unsigned)e->h[0], (unsigned)e->h[1], (unsigned)e->h[2], (unsigned)e->h[3],
                   (unsigned)e->retries);
        }
    }

    int strongest_out = (strongest == 1) ? 0 : strongest;
    printf("C,2G,%u,%u,%u,%u,%u,%d,%u\n",
           (unsigned)g_cur_channel, (unsigned)dwell_ms,
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

// ---- Wi-Fi bring-up (no fixed channel; the hop loop drives channels) ------

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

    ESP_LOGI(TAG, "sniffer up: 2.4 GHz hop (survey %d ch x %dms, focus top-%d x %ds)",
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
            if (!hop_to(CH_LIST[i])) {
                continue;   // region-blocked channel -> skip (no C line)
            }
            vTaskDelay(pdMS_TO_TICKS(SURVEY_MS));
            score[i] = emit_window(SURVEY_MS, false);   // C line only
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
                break;   // no more active channels
            }
            used[best] = true;
            focus_idx[n_focus++] = best;
        }

        // ---- STAGE 2: FOCUS ----
        for (int f = 0; f < n_focus; f++) {
            if (!hop_to(CH_LIST[focus_idx[f]])) {
                continue;
            }
            for (int s = 0; s < FOCUS_SECS; s++) {
                vTaskDelay(pdMS_TO_TICKS(1000));
                emit_window(1000, true);   // D lines + C line
            }
            print_status();
        }
    }
}
