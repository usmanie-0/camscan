"""ONVIF WS-Discovery and RTSP DESCRIBE probes."""

import re
import socket
import uuid

WSD_ADDR = "239.255.255.250"
WSD_PORT = 3702

_PROBE = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
            xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
            xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
            xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <e:Header>
    <w:MessageID>uuid:{msgid}</w:MessageID>
    <w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
    <w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
  </e:Header>
  <e:Body>
    <d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe>
  </e:Body>
</e:Envelope>"""


def onvif_discover(bind_ip, timeout=4):
    """Multicast an ONVIF probe. Returns {ip: [service urls]}."""
    msg = _PROBE.format(msgid=uuid.uuid4()).encode("utf-8")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                    socket.inet_aton(bind_ip))
    sock.bind((bind_ip, 0))
    sock.settimeout(timeout)

    replies = {}
    try:
        sock.sendto(msg, (WSD_ADDR, WSD_PORT))
        while True:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                break
            text = data.decode("utf-8", errors="replace")
            urls = re.findall(r"https?://[^\s<>\"]+", text)
            replies.setdefault(addr[0], []).extend(urls)
    finally:
        sock.close()

    return {ip: sorted(set(u)) for ip, u in replies.items()}


def rtsp_describe(ip, port=554, timeout=4):
    """Send one unauthenticated DESCRIBE. Returns a result dict or None."""
    req = (
        f"DESCRIBE rtsp://{ip}:{port}/ RTSP/1.0\r\n"
        "CSeq: 1\r\n"
        "User-Agent: camscan\r\n"
        "Accept: application/sdp\r\n\r\n"
    )
    try:
        with socket.create_connection((ip, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(req.encode("ascii"))
            chunks = []
            while len(b"".join(chunks)) < 8192:
                try:
                    chunk = s.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
            raw = b"".join(chunks).decode("utf-8", errors="replace")
    except (OSError, socket.timeout):
        return None

    if not raw.startswith("RTSP/"):
        return None

    status_line = raw.split("\r\n", 1)[0]
    code_match = re.match(r"RTSP/\d\.\d\s+(\d+)", status_line)
    code = int(code_match.group(1)) if code_match else None
    server = None
    srv = re.search(r"^Server:\s*(.+)$", raw, re.M | re.I)
    if srv:
        server = srv.group(1).strip()

    has_video = bool(re.search(r"^m=video", raw, re.M))
    needs_auth = code == 401

    return {
        "responded": True, "code": code, "server": server,
        "has_video_track": has_video, "needs_auth": needs_auth,
    }


def annotate(host, onvif_map):
    """Attach ONVIF and RTSP findings to a host dict."""
    host["onvif"] = onvif_map.get(host["ip"])
    host["rtsp"] = None
    for p in host.get("open_ports", []):
        if p["port"] in (554, 8554):
            host["rtsp"] = rtsp_describe(host["ip"], p["port"])
            if host["rtsp"]:
                break
    return host


if __name__ == "__main__":
    from camscan.lan.arp import sweep
    from camscan.lan.iface import detect_active
    from camscan.lan.oui import annotate as oui_annotate
    from camscan.lan.ports import annotate as port_annotate

    name, ip, gw, net = detect_active()

    print("sending ONVIF discovery probe...")
    onvif_map = onvif_discover(ip)
    if onvif_map:
        for host_ip, urls in onvif_map.items():
            print(f"  ONVIF REPLY from {host_ip}")
            for u in urls[:3]:
                print(f"    {u}")
    else:
        print("  no ONVIF replies")
    print()

    hosts = sweep(subnet=net, iface=name)
    print(f"probing {len(hosts)} host(s)...\n")

    for h in hosts:
        oui_annotate(h)
        port_annotate(h)
        annotate(h, onvif_map)

        signals = []
        if h["onvif"]:
            signals.append("ONVIF camera reply [DEFINITIVE]")
        if h["rtsp"]:
            r = h["rtsp"]
            if r["has_video_track"]:
                signals.append("RTSP serving video track [DEFINITIVE]")
            elif r["needs_auth"]:
                signals.append("RTSP server present, auth required [HIGH]")
            else:
                signals.append(f'RTSP responded code {r["code"]}')
            if r["server"]:
                signals.append(f'RTSP server header: {r["server"]}')
        if h["cam_vendor_weight"]:
            signals.append(f'camera vendor OUI [{h["cam_vendor_weight"]}]')

        if signals:
            label = h["vendor"] or "unknown"
            print(f'{h["ip"]:<16} {label}')
            for s in signals:
                print(f"    {s}")
            print()

    print("done")
