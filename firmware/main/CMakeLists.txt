idf_component_register(SRCS "camscan_fw.c"
                       INCLUDE_DIRS "."
                       REQUIRES esp_wifi nvs_flash esp_event esp_timer bt)

