from camscan.rf.wifi_evidence import collect_from_file
from camscan.analysis import scoring

wifi = collect_from_file("data/fixture_wifi_camera.txt")
print(f"wifi: {len(wifi.devices)} consolidated device(s), {wifi.duration_s:.0f}s")
print()

hosts = {
    "AA:BB:CC:00:00:07": {"mac": "AA:BB:CC:00:00:07"},
    "DD:EE:FF:00:00:20": {"mac": "DD:EE:FF:00:00:20"},
    "11:22:33:00:00:30": {"mac": "11:22:33:00:00:30"},
}

for mac, host in hosts.items():
    scoring.score(host, wifi=wifi)
    s = host["score"]
    c = host["ceiling"]
    v = host["verdict"]
    print(f"{mac}  score {s}/{c}  {v}")
    for lvl, txt in host["reasons"]:
        print(f"    [{lvl}] {txt}")
    print()

cam = hosts["AA:BB:CC:00:00:07"]
dl = wifi.for_mac("AA:BB:CC:00:00:07")
print("--- checks ---")
print("camera home channel:", dl.channel if dl else None, "(expect 6, not 8)")
print("camera bled_channels:", dl.bled_channels if dl else None, "(expect 6 and 8)")
print("camera has_rf:", cam["has_rf"], "(expect True)")
print("camera verdict:", cam["verdict"], "(expect LIKELY CAMERA)")
print("downloader CLEAR:", hosts["DD:EE:FF:00:00:20"]["verdict"] == "CLEAR")
print("far device CLEAR:", hosts["11:22:33:00:00:30"]["verdict"] == "CLEAR")
