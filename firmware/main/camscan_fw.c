// camscan_fw.c  --  Phase 4, Step 1: promiscuous capture + 802.11 header parse (print-only)
//
// What this does:
//   - Brings Wi-Fi up in NULL mode (joins no network) and enables promiscuous RX.
//   - Filters to DATA frames only, on a single fixed channel (SNIFF_CHANNEL).
//   - In the RX callback, parses the 802.11 MAC header and prints a rate-limited
//     "RAW,..." line per frame so we can eyeball the fields against a known device.
//   - Keeps the Phase 3 "S,..." status heartbeat once per second, and adds a
//     "RATE,<n>" line = data frames seen that second (so the rate cap doesn't hide volume).
//
// What this deliberately does NOT do yet:
//   - No per-MAC aggregation table (Step 2).
//   - No concurrency locking (Step 3) -- see note on g_data_frames below.
//   - No channel hopping / 5 GHz (Steps 5-6).
//   - printf() inside the RX callback is a Step-1-only crutch for eyeballing; it is
//     rate-capped so it stays light. Step 3 replaces it with a callback->task hand-off.
//
// Protocol lines use printf()+fflush(stdout), NOT ESP_LOGI (which prepends "I (ts) tag:"
// and would break the Python parser). ESP_LOGI is used only for human boot/status text.

#include <stdio.h>
#include <stdint.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_timer.h"
#include "esp_system.h"
#include "esp_log.h"
#include "nvs_flash.h"

// ---- Config ---------------------------------------------------------------

// 2.4 GHz channel to listen on. IMPORTANT for verification: set this to the
// channel your HOME 2.4 GHz AP actually uses, or you'll only hear neighbours.
// (Check your own router's page / a Wi-Fi analyzer app -- this is your network,
// so that's fine. We never touch a *host's* router in the field.)
#define SNIFF_CHANNEL   6

static const char *TAG = "camscan";

// Count of every data frame the callback sees (before the print rate-cap).
// Single writer (the Wi-Fi driver task, in the callback) and single reader
// (app_main's status loop). A 32-bit aligned load/store is atomic on RISC-V,
// and only the callback ever writes it, so no lock is needed *yet*. Real
// shared-mutable state (the per-MAC table) arrives in Step 2 and THAT is where
// locking becomes necessary. 'volatile' stops the compiler caching the reader's view.
static volatile uint32_t g_data_frames = 0;

// ---- Helpers --------------------------------------------------------------

static const char *dir_str(int tods, int fromds)
{
    if (tods && fromds) return "WDS";   // 1,1 -> WDS / mesh bridge
    if (tods)           return "UP";    // 1,0 -> station -> AP  (uplink)
    if (fromds)         return "DN";    // 0,1 -> AP -> station  (downlink)
    return "IBSS";                      // 0,0 -> ad-hoc / not infrastructure
}

// ---- Promiscuous RX callback (runs in the Wi-Fi driver task -- keep tiny) --

static void promisc_cb(void *buf, wifi_promiscuous_pkt_type_t type)
{
    // We set a DATA-only filter below, but verify anyway -- don't trust, check.
    if (type != WIFI_PKT_DATA) {
        return;
    }

    const wifi_promiscuous_pkt_t *pkt = (const wifi_promiscuous_pkt_t *)buf;
    uint16_t len = pkt->rx_ctrl.sig_len;   // total frame length incl. 4-byte FCS

    // Minimum data header (FC2 + Dur2 + A1/A2/A3 18 + SeqCtrl2) = 24 bytes.
    // Guard before we index into the header so we never read past the buffer.
    if (len < 24) {
        return;
    }

    g_data_frames++;   // honest per-second count, independent of the print cap

    // Rate-cap the raw print to <= ~25 lines/sec so we don't flood serial or
    // hog the driver task. Almost every frame returns right here.
    static int64_t last_print_us = 0;
    int64_t now_us = esp_timer_get_time();
    if (now_us - last_print_us < 40000) {   // 40 ms gap => <= 25 prints/sec
        return;
    }
    last_print_us = now_us;

    const uint8_t *p = pkt->payload;

    // Frame Control is the first two octets.
    // byte0: [b0-1 protocol ver][b2-3 type][b4-7 subtype]
    // byte1: [b0 ToDS][b1 FromDS][b2 MoreFrag][b3 Retry]...
    uint8_t tods   = (p[1] >> 0) & 0x1;
    uint8_t fromds = (p[1] >> 1) & 0x1;
    uint8_t retry  = (p[1] >> 3) & 0x1;

    const uint8_t *ra = p + 4;    // Address 1 = Receiver  (who it's going TO)
    const uint8_t *ta = p + 10;   // Address 2 = Transmitter (who SENT it) <- our key later

    // Sequence Control: low 4 bits = fragment, high 12 bits = sequence number.
    // Fixed offset 22 regardless of QoS/Addr4, which come AFTER it.
    uint16_t seq_ctrl = (uint16_t)p[22] | ((uint16_t)p[23] << 8);
    uint16_t seq_num  = (seq_ctrl >> 4) & 0x0FFF;

    int8_t   rssi = pkt->rx_ctrl.rssi;
    uint8_t  ch   = pkt->rx_ctrl.channel;

    // Columns: RAW, TA, RA, dir, retry, seq, len, rssi(dBm), channel
    printf("RAW,%02x:%02x:%02x:%02x:%02x:%02x,%02x:%02x:%02x:%02x:%02x:%02x,%s,%u,%u,%u,%d,%u\n",
           ta[0], ta[1], ta[2], ta[3], ta[4], ta[5],
           ra[0], ra[1], ra[2], ra[3], ra[4], ra[5],
           dir_str(tods, fromds),
           (unsigned)retry, (unsigned)seq_num, (unsigned)len,
           (int)rssi, (unsigned)ch);
    fflush(stdout);
}

// ---- Wi-Fi bring-up (the order proven in Phase 0.5) -----------------------

static void wifi_sniffer_start(void)
{
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    // NULL mode: we are neither a station nor an AP -- a silent listener.
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_NULL));
    ESP_ERROR_CHECK(esp_wifi_start());

    // The C5 is single-band-at-a-time and this build defaults the radio to
    // 5 GHz-only (the boot log shows "band mode:0x2"). SNIFF_CHANNEL is a
    // 2.4 GHz channel, so we MUST select the 2.4 GHz band before tuning it --
    // otherwise esp_wifi_set_channel() rejects the channel as ESP_ERR_INVALID_ARG.
    // Don't rely on the default band; set it explicitly. (Later phases switch
    // band mode deliberately as part of the dual-band hop.)
    ESP_ERROR_CHECK(esp_wifi_set_band_mode(WIFI_BAND_MODE_2G_ONLY));

    // Only deliver DATA frames to the callback. Keeps the callback off the
    // hot path for beacons/control frames. We'll widen this in later steps.
    wifi_promiscuous_filter_t filt = { .filter_mask = WIFI_PROMIS_FILTER_MASK_DATA };
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous_filter(&filt));

    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous_rx_cb(promisc_cb));

    ESP_ERROR_CHECK(esp_wifi_set_channel(SNIFF_CHANNEL, WIFI_SECOND_CHAN_NONE));

    ESP_LOGI(TAG, "sniffer up: 2.4 GHz ch %d, DATA-only, promiscuous", SNIFF_CHANNEL);
}

// ---- app_main -------------------------------------------------------------

void app_main(void)
{
    // NVS is required by the Wi-Fi driver (it caches calibration data there).
    esp_err_t r = nvs_flash_init();
    if (r == ESP_ERR_NVS_NO_FREE_PAGES || r == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        r = nvs_flash_init();
    }
    ESP_ERROR_CHECK(r);

    wifi_sniffer_start();

    // Status loop: one S heartbeat + one RATE line per second.
    uint32_t last_frames = 0;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(1000));

        uint32_t uptime_ms = (uint32_t)(esp_timer_get_time() / 1000);
        uint32_t free_heap = esp_get_free_heap_size();

        // S contract is FIXED (Phase 3): S,uptime_ms,free_heap,frames_dropped.
        // frames_dropped stays 0 until Step 2 wires it to real table-full events.
        printf("S,%u,%u,%u\n", (unsigned)uptime_ms, (unsigned)free_heap, 0u);

        // RATE is a Step-1 diagnostic (not a protocol line): data frames seen
        // this second, so the print rate-cap doesn't hide the true volume.
        uint32_t now_frames = g_data_frames;
        printf("RATE,%u\n", (unsigned)(now_frames - last_frames));
        last_frames = now_frames;

        fflush(stdout);
    }
}
