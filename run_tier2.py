from camscan.rf.wifi_evidence import collect_from_serial
from camscan.analysis import scoring

print("capturing 60s of Wi-Fi -- put the Pixel on a video call near the board")
wifi = collect_from_serial(seconds=60)
print(f"\ncaptured {len(wifi.devices)} consolidated device(s) over {wifi.duration_s:.0f}s\n")

ranked = sorted(wifi.devices, key=lambda d: d.bytes_up - d.bytes_down, reverse=True)
for d in ranked[:10]:
    asym = d.bytes_up / max(d.bytes_down, 1)
    shape = scoring._wifi_shape(d)
    print(f"{d.mac}  {d.rssi_avg}dBm  {d.band} ch{d.channel}  "
          f"up{d.bytes_up} down{d.bytes_down}  {asym:.1f}x  -> {shape}")
