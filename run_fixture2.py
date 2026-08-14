from camscan.rf.wifi_evidence import collect_from_file
from camscan.analysis import scoring

wifi = collect_from_file("data/fixture_wifi_camera.txt")

# A realistic Wi-Fi camera: vendor OUI + an open RTSP port (LAN evidence),
# PLUS the up-heavy RF flow. This is what an actual camera looks like.
cam = {
    "mac": "AA:BB:CC:00:00:07",
    "cam_vendor_weight": "high",
    "cam_vendor_note": "IP camera maker",
    "open_ports": [{"port": 554, "weight": "high", "note": "RTSP"}],
}
scoring.score(cam, wifi=wifi)

s = cam["score"]
c = cam["ceiling"]
v = cam["verdict"]
print(f"camera: {s}/{c}  {v}")
for lvl, txt in cam["reasons"]:
    print(f"    [{lvl}] {txt}")
print()
print("has_rf:", cam["has_rf"], "(expect True)")
print("verdict LIKELY CAMERA:", v == "LIKELY CAMERA")
print("score above LAN ceiling 70:", s, "->", s > 70)
