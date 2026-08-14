idf_component_register(SRCS "ble_scan_test.c"
                       INCLUDE_DIRS "."
                       REQUIRES esp_wifi nvs_flash esp_event esp_timer bt)
