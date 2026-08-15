// camscan_fw.c  --  Phase 4 (scan) + Phase 6 (hunt mode)
//
// SCAN MODE (HUNT_ENABLE 0, the default): dual-band hop + focus survey, exactly
// as before. Emits D/C/S lines, plus a "CAM?" highlight for camera-shaped
// devices (up-heavy + close RSSI) as a Step 7 diagnostic aid.
//
//   D,<mac>,<band>,<ch>,<rssi_avg>,<frames>,<bytes_up>,<bytes_down>,<h0>,<h1>,<h2>,<h3>,<retries>
//   C,<band>,<ch>,<dwell_ms>,<frames>,<bytes>,<stations>,<strongest>,<max_up>
//   S,<uptime_ms>,<free_heap>,<frames_dropped>
//   CAM?,<mac>,<band>,<ch>,<rssi_avg>,<bytes_up>,<bytes_down>,<h2>,<h3>,<frames>
//
// HUNT MODE (HUNT_ENABLE 1): park on ONE channel, stream a live RSSI meter for
// ONE target MAC several times a second. This is the Phase 6 field step that
// tells "camera in MY room" from "neighbour's, through the wall". Set the
// target (MAC + band + channel) from a `camscan scan`, reflash, and walk the
// room: the reading climbs as you near the transmitter, falls as you leave it.
//
//   H,<mac>,<ch>,<rssi_avg>,<frames>,<age_ms>
//     rssi_avg : averaged RSSI over the last HUNT_WIN_MS window (the needle)
//     frames   : target frames heard this window (0 = not heard this tick)
//     age_ms   : ms since the target was last actually heard (freshness)

#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <ctype.h>

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

static const chan_t CHANS[] = {
    {false,1},{false,2},{false,3},{false,4},{false,5},{false,6},{false,7},
    {false,8},{false,9},{false,10},{false,11},{false,12},{false,13},
    {true,36},{true,40},{true,44},{true,48},
    {true,52},{true,56},{true,60},{true,64},
    {true,149},{true,153},{true,157},{true,161},{true,165},
};
#define N_CH        (sizeof(CHANS) / sizeof(CHANS[0]))
#define SURVEY_MS   300
#define FOCUS_SECS  3
#define FOCUS_TOP_K 3
#define TABLE_SIZE  64

// Camera-candidate highlight thresholds (Step 7). A CAM? line is emitted when
// a device in a focus window meets ALL of these:
#define CAM_MIN_UP    5000   // bytes_up in the window (a real uplink stream)
#define CAM_MIN_RSSI  (-70)  // uplink rssi_avg at least this strong (i.e. close)
// (frame size is intentionally NOT a gate -- see the CAM? block below)

// ---- Hunt mode (Phase 6) --------------------------------------------------
// When HUNT_ENABLE is 1, app_main parks on ONE channel and streams an H line
// for ONE target MAC every HUNT_WIN_MS, instead of hopping/surveying. Set the
// target from a `camscan scan` (MAC + band + channel), then reflash to hunt.
// Set HUNT_ENABLE back to 0 to restore the normal scanner.
//
// Why hunt records RSSI on EVERY frame from the target (not just uplink like
// scan does): here we've already chosen the target, so we just want the most
// RSSI samples possible for a smooth needle. Restricting to uplink would throw
// away most frames (and the up/down split mislabels some traffic anyway).
#define HUNT_ENABLE   1
#define HUNT_CH       11
#define HUNT_IS_5G    0
#define HUNT_WIN_MS   200   // meter window: average RSSI over this, then emit
static const uint8_t HUNT_MAC[6] = { 0x46, 0x06, 0xcc, 0xe1, 0x15, 0xb0 };

// Runtime hunt target (Phase 6, Step 4). The #defines above are only the BOOT
// DEFAULT -- a `HUNT,<mac>,<ch>` command over serial (parsed by the RX task
// below) overwrites these live, so the target can be changed from
// `camscan hunt <mac>` WITHOUT a reflash. hop_to() needs a band; we derive it
// from the channel (1..14 => 2G, else 5G) so the command stays `HUNT,<mac>,<ch>`.
static uint8_t          g_hunt_mac[6];
static volatile uint8_t g_hunt_ch    = HUNT_CH;
static volatile bool    g_hunt_is5g  = HUNT_IS_5G;
static volatile bool    g_hunt_retarget = false;  // RX task sets; hunt loop consumes
static portMUX_TYPE     g_hunt_mux = portMUX_INITIALIZER_UNLOCKED;

static inline bool ch_is_5g(uint8_t ch) { return ch >= 15; }

// Runtime firmware mode (Phase 6, Step 4d). The board can switch between
// SURVEY (the normal dual-band hop, emits D/C/S) and HUNT (park on one channel,
// emit H for one MAC) WITHOUT a reflash, driven by serial commands:
//   SCAN                 -> switch to survey mode (for `camscan hunt --all` to
//                           discover candidates + their channels)
//   HUNT,<mac>,<ch>      -> switch to hunt mode on that target
// app_main runs whichever mode is active; each mode loop returns when the
// requested mode changes, and app_main dispatches again. HUNT_ENABLE sets the
// boot mode.
typedef enum { MODE_SURVEY = 0, MODE_HUNT = 1 } fw_mode_t;
#if HUNT_ENABLE
static volatile fw_mode_t g_mode = MODE_HUNT;
#else
static volatile fw_mode_t g_mode = MODE_SURVEY;
#endif

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

// Hunt-only: when true, promisc_cb tables ONLY the target MAC and records RSSI
// on every one of its frames (both directions). Set once before the hunt loop.
static volatile bool g_hunt_mode = false;

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

    // Hunt: ignore every station except the target, and record RSSI on ALL of
    // its frames so the meter gets the densest possible sample stream. The
    // target is the RUNTIME g_hunt_mac (settable live), not the #define.
    if (g_hunt_mode) {
        if (memcmp(sta, g_hunt_mac, 6) != 0) {
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
        e->rssi_sum += rssi;   // every direction, unlike scan mode
        e->rssi_n++;
        e->last_seq = seq;
        e->seq_valid = true;
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

            // Camera-candidate highlight: the robust signal is up-heavy VOLUME
            // at close/strong RSSI. Frame size is NOT gated -- a video call
            // uploads small packets (h0/h1) while an H.264 camera sends big ones,
            // so h2/h3 are printed for context but not required.
            if (e->rssi_n > 0 && rssi_avg >= CAM_MIN_RSSI &&
                e->bytes_up > e->bytes_down &&
                e->bytes_up >= CAM_MIN_UP) {
                printf("CAM?,%02x:%02x:%02x:%02x:%02x:%02x,%s,%u,%d,%u,%u,%u,%u,%u\n",
                       e->mac[0], e->mac[1], e->mac[2], e->mac[3], e->mac[4], e->mac[5],
                       band, (unsigned)g_cur_channel, rssi_avg,
                       (unsigned)e->bytes_up, (unsigned)e->bytes_down,
                       (unsigned)e->h[2], (unsigned)e->h[3], (unsigned)e->frames);
            }
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

// ---- Hunt emit (Phase 6) --------------------------------------------------
// Swap the buffer, pull out ONLY the target's averaged RSSI + frame count for
// this window, and emit one H line. age_ms carries freshness so the Python
// meter can hold/decay the needle when the target goes briefly silent.

static int64_t g_hunt_last_heard_us = 0;

static void emit_hunt_window(uint32_t win_ms)
{
    (void)win_ms;

    taskENTER_CRITICAL(&g_swap_mux);
    dev_entry_t *snap = g_active;
    g_active = (snap == g_buf[0]) ? g_buf[1] : g_buf[0];
    taskEXIT_CRITICAL(&g_swap_mux);

    int      rssi_avg = 0;
    uint32_t frames   = 0;
    for (int i = 0; i < TABLE_SIZE; i++) {
        if (snap[i].in_use && memcmp(snap[i].mac, g_hunt_mac, 6) == 0) {
            if (snap[i].rssi_n > 0) {
                rssi_avg = (int)(snap[i].rssi_sum / (int32_t)snap[i].rssi_n);
            }
            frames = snap[i].frames;
            break;
        }
    }

    int64_t now_us = esp_timer_get_time();
    if (frames > 0) {
        g_hunt_last_heard_us = now_us;
    }
    uint32_t age_ms = (g_hunt_last_heard_us == 0)
        ? 0
        : (uint32_t)((now_us - g_hunt_last_heard_us) / 1000);

    // H,<mac>,<ch>,<rssi_avg>,<frames>,<age_ms>
    printf("H,%02x:%02x:%02x:%02x:%02x:%02x,%u,%d,%u,%u\n",
           g_hunt_mac[0], g_hunt_mac[1], g_hunt_mac[2],
           g_hunt_mac[3], g_hunt_mac[4], g_hunt_mac[5],
           (unsigned)g_cur_channel, rssi_avg,
           (unsigned)frames, (unsigned)age_ms);
    fflush(stdout);

    memset(snap, 0, sizeof(g_buf[0]));
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

    ESP_ERROR_CHECK(esp_wifi_set_band_mode(WIFI_BAND_MODE_2G_ONLY));
    g_cur_band_mode = WIFI_BAND_MODE_2G_ONLY;

    wifi_promiscuous_filter_t filt = { .filter_mask = WIFI_PROMIS_FILTER_MASK_DATA };
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous_filter(&filt));

    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous_rx_cb(promisc_cb));

    ESP_LOGI(TAG, "sniffer up: dual-band hop (%d chans, survey %dms, focus top-%d x %ds), CAM? on",
             (int)N_CH, SURVEY_MS, FOCUS_TOP_K, FOCUS_SECS);
}

// ---- Hunt command RX (Phase 6, Step 4) ------------------------------------
// The firmware now LISTENS as well as speaks. A dedicated task reads stdin
// lines and parses `HUNT,<mac>,<ch>` to retarget live (no reflash). We start
// with plain fgets(stdin): on the console UART this delivers line-buffered
// input fine. If it ever drops chars under load, swap to an explicit
// uart_driver_install path -- but keep this simple version first.
//
// Only the hunt target is accepted; anything else is ignored (so stray bytes
// or a mis-typed line never disturb a running hunt).

static bool parse_hunt_cmd(const char *line, uint8_t out_mac[6], uint8_t *out_ch)
{
    if (strncmp(line, "HUNT,", 5) != 0) {
        return false;
    }
    const char *p = line + 5;
    uint8_t mac[6];
    for (int i = 0; i < 6; i++) {
        if (!isxdigit((unsigned char)p[0]) || !isxdigit((unsigned char)p[1])) {
            return false;
        }
        char byte[3] = { p[0], p[1], 0 };
        mac[i] = (uint8_t)strtol(byte, NULL, 16);
        p += 2;
        if (i < 5) {
            if (*p != ':') return false;
            p++;
        }
    }
    if (*p != ',') return false;
    p++;
    if (!isdigit((unsigned char)*p)) return false;
    int ch = 0, n = 0;
    while (isdigit((unsigned char)*p) && n < 3) {
        ch = ch * 10 + (*p - '0');
        p++; n++;
    }
    while (*p == '\r' || *p == '\n' || *p == ' ' || *p == '\t') p++;
    if (*p != '\0') return false;
    if (ch < 1 || ch > 165) return false;
    memcpy(out_mac, mac, 6);
    *out_ch = (uint8_t)ch;
    return true;
}

static void hunt_rx_task(void *arg)
{
    (void)arg;
    char buf[64];
    for (;;) {
        if (fgets(buf, sizeof(buf), stdin) == NULL) {
            // No line available this tick; yield so we don't busy-spin.
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }

        // SCAN: switch to survey mode (used by `camscan hunt --all` to discover
        // candidates + channels). Accept a bare "SCAN" line, any trailing WS.
        if (strncmp(buf, "SCAN", 4) == 0) {
            const char *p = buf + 4;
            while (*p == '\r' || *p == '\n' || *p == ' ' || *p == '\t') p++;
            if (*p == '\0') {
                if (g_mode != MODE_SURVEY) {
                    g_mode = MODE_SURVEY;   // hunt loop will see this and return
                    ESP_LOGI(TAG, "mode -> SURVEY");
                }
                continue;
            }
        }

        uint8_t mac[6], ch;
        if (parse_hunt_cmd(buf, mac, &ch)) {
            // Only re-fire on a genuine change of target (the console can
            // deliver a typed line more than once); and switch to hunt mode.
            bool changed;
            taskENTER_CRITICAL(&g_hunt_mux);
            changed = (g_mode != MODE_HUNT) ||
                      (memcmp(mac, g_hunt_mac, 6) != 0) || (ch != g_hunt_ch);
            if (changed) {
                memcpy(g_hunt_mac, mac, 6);
                g_hunt_ch = ch;
                g_hunt_is5g = ch_is_5g(ch);
                g_hunt_retarget = true;   // hunt loop will re-park
                g_mode = MODE_HUNT;       // survey loop will see this and return
            }
            taskEXIT_CRITICAL(&g_hunt_mux);
            if (changed) {
                ESP_LOGI(TAG, "retarget -> %02x:%02x:%02x:%02x:%02x:%02x on %s ch %d",
                         mac[0],mac[1],mac[2],mac[3],mac[4],mac[5],
                         ch_is_5g(ch) ? "5G" : "2G", ch);
            }
        }
    }
}

// ---- Hunt loop (Phase 6) --------------------------------------------------
// Park on the target's channel and stream an H line every HUNT_WIN_MS forever.
// No hopping: the radio stares at one channel so RSSI updates are frequent and
// smooth -- the whole point of hunt vs the sparse, bursty scan stream.

static void hunt_loop(void)
{
    ESP_LOGI(TAG, "HUNT mode: %02x:%02x:%02x:%02x:%02x:%02x on %s ch %d, %dms window "
                  "(send HUNT,<mac>,<ch> to retarget, SCAN to survey)",
             g_hunt_mac[0], g_hunt_mac[1], g_hunt_mac[2],
             g_hunt_mac[3], g_hunt_mac[4], g_hunt_mac[5],
             g_hunt_is5g ? "5G" : "2G", g_hunt_ch, HUNT_WIN_MS);

    if (!hop_to(g_hunt_is5g, g_hunt_ch)) {
        ESP_LOGE(TAG, "HUNT: failed to park on ch %d -- check band/channel", g_hunt_ch);
        return;
    }

    g_hunt_mode = true;   // promisc_cb now filters to g_hunt_mac only

    uint32_t ticks = 0;
    while (g_mode == MODE_HUNT) {
        // Apply a retarget if the RX task set one: re-park on the new channel
        // and clear the target's stale table entry so the meter starts fresh.
        if (g_hunt_retarget) {
            uint8_t ch; bool is5g;
            taskENTER_CRITICAL(&g_hunt_mux);
            ch = g_hunt_ch; is5g = g_hunt_is5g;
            g_hunt_retarget = false;
            taskEXIT_CRITICAL(&g_hunt_mux);

            g_hunt_mode = false;              // pause filtering during the hop
            if (!hop_to(is5g, ch)) {          // hop_to also swaps+clears buffers
                ESP_LOGE(TAG, "HUNT: failed to park on ch %d", ch);
            }
            g_hunt_last_heard_us = 0;         // reset freshness for new target
            g_hunt_mode = true;
        }

        vTaskDelay(pdMS_TO_TICKS(HUNT_WIN_MS));
        emit_hunt_window(HUNT_WIN_MS);
        // Occasional heartbeat so we can see the board's alive when the target
        // is silent (H lines still stream, but S confirms uptime/heap/drops).
        if (++ticks % 25 == 0) {   // ~ every 5s at 200ms windows
            print_status();
        }
    }

    // Mode changed to SURVEY: stop hunt filtering and return to the dispatcher.
    g_hunt_mode = false;
}

// ---- Survey loop (Phase 4 scan, now a returning mode) ---------------------
// The original dual-band hop + focus survey, unchanged, but it RETURNS when the
// mode switches to HUNT (a HUNT command arrived) instead of looping forever.

static void survey_loop(void)
{
    ESP_LOGI(TAG, "SURVEY mode: dual-band hop (%d chans), send HUNT,<mac>,<ch> to hunt",
             (int)N_CH);

    while (g_mode == MODE_SURVEY) {
        uint32_t score[N_CH];
        for (int i = 0; i < (int)N_CH && g_mode == MODE_SURVEY; i++) {
            score[i] = 0;
            if (!hop_to(CHANS[i].is_5g, CHANS[i].ch)) {
                continue;
            }
            vTaskDelay(pdMS_TO_TICKS(SURVEY_MS));
            score[i] = emit_window(SURVEY_MS, false);
        }
        print_status();

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

        for (int f = 0; f < n_focus && g_mode == MODE_SURVEY; f++) {
            if (!hop_to(CHANS[focus_idx[f]].is_5g, CHANS[focus_idx[f]].ch)) {
                continue;
            }
            for (int s = 0; s < FOCUS_SECS && g_mode == MODE_SURVEY; s++) {
                vTaskDelay(pdMS_TO_TICKS(1000));
                emit_window(1000, true);
            }
            print_status();
        }
    }
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

    // Seed the hunt target once from the boot-default #define (a later
    // HUNT command overwrites it). Start the command reader once; it drives
    // mode switches for the lifetime of the board.
    memcpy(g_hunt_mac, HUNT_MAC, 6);
    g_hunt_ch   = HUNT_CH;
    g_hunt_is5g = HUNT_IS_5G;
    xTaskCreate(hunt_rx_task, "hunt_rx", 3072, NULL, 5, NULL);

    // Dispatch: run whichever mode is active; each loop returns when the mode
    // changes, and we dispatch again. Boot mode is set by HUNT_ENABLE.
    for (;;) {
        if (g_mode == MODE_HUNT) {
            hunt_loop();
        } else {
            survey_loop();
        }
    }
}
