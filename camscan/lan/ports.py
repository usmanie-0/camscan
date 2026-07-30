"""Targeted port scan for camera-related services."""

import nmap

CAMERA_PORTS = {
    554:   ("rtsp", "high", "RTSP video streaming"),
    8554:  ("rtsp-alt", "high", "RTSP alternate port"),
    34567: ("dvr-xiongmai", "high", "Xiongmai/Hisilicon DVR protocol"),
    37777: ("dvr-dahua", "high", "Dahua proprietary protocol"),
    8000:  ("hikvision-sdk", "high", "Hikvision SDK port"),
    8899:  ("onvif-alt", "medium", "ONVIF service on some vendors"),
    9000:  ("onvif-alt2", "medium", "ONVIF service on some vendors"),
    80:    ("http", "low", "web interface"),
    443:   ("https", "low", "secure web interface"),
    8080:  ("http-alt", "low", "alternate web interface"),
}

PORT_LIST = ",".join(str(p) for p in sorted(CAMERA_PORTS))


def scan_host(ip, timeout_args="-T4 --host-timeout 60s"):
    """Scan one host. Returns list of open-port dicts."""
    scanner = nmap.PortScanner()
    try:
        scanner.scan(ip, PORT_LIST, arguments=f"-sV --version-light {timeout_args}")
    except nmap.PortScannerError:
        return []

    if ip not in scanner.all_hosts():
        return []

    found = []
    for proto in scanner[ip].all_protocols():
        for port in sorted(scanner[ip][proto].keys()):
            info = scanner[ip][proto][port]
            if info.get("state") != "open":
                continue
            name, weight, note = CAMERA_PORTS.get(port, ("unknown", "low", ""))
            banner = " ".join(
                filter(None, [info.get("product"), info.get("version"),
                              info.get("extrainfo")])
            ).strip()
            found.append({
                "port": port, "label": name, "weight": weight,
                "note": note, "banner": banner or None,
            })
    return found


def annotate(host):
    """Add open_ports to a host dict."""
    host["open_ports"] = scan_host(host["ip"])
    return host


if __name__ == "__main__":
    from camscan.lan.arp import sweep
    from camscan.lan.iface import detect_active
    from camscan.lan.oui import annotate as oui_annotate

    name, ip, gw, net = detect_active()
    hosts = sweep(subnet=net, iface=name)
    print(f"scanning {len(hosts)} host(s) - this takes a minute\n")

    for h in hosts:
        oui_annotate(h)
        annotate(h)
        label = h["vendor"] or ("randomized" if h["randomized"] else "unknown")
        print(f'{h["ip"]:<16} {label}')
        if not h["open_ports"]:
            print("    no camera-relevant ports open")
        for p in h["open_ports"]:
            line = f'    {p["port"]:<6} {p["label"]:<14} [{p["weight"]}] {p["note"]}'
            if p["banner"]:
                line += f'\n           banner: {p["banner"]}'
            print(line)
        print()
