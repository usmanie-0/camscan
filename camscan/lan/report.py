"""LAN-only scoring and ranked output."""

import argparse
import json

from camscan.lan.iface import detect_active
from camscan.lan.arp import sweep
from camscan.lan.oui import annotate as oui_annotate
from camscan.lan.ports import annotate as port_annotate
from camscan.lan.onvif import onvif_discover, annotate as probe_annotate

# LAN-only ceiling. RF signals in Phase 5 lift this to 100.
LAN_MAX = 70


def score(host):
    """Return (points, [reasons]) from LAN evidence only."""
    pts, why = 0, []

    if host.get("onvif"):
        pts += 45
        why.append("ONVIF: identifies as a network video transmitter")

    r = host.get("rtsp")
    if r:
        if r["has_video_track"]:
            pts += 45
            why.append("RTSP: serving a video track")
        elif r["needs_auth"]:
            pts += 25
            why.append("RTSP: server present, credentials required")
        else:
            pts += 20
            why.append(f'RTSP: server responded (code {r["code"]})')

    w = host.get("cam_vendor_weight")
    if w == "high":
        pts += 25
        why.append(f'vendor OUI: {host["cam_vendor_note"]}')
    elif w == "medium":
        pts += 10
        why.append(f'vendor OUI: {host["cam_vendor_note"]}')

    for p in host.get("open_ports", []):
        if p["weight"] == "high":
            pts += 15
            why.append(f'port {p["port"]}: {p["note"]}')
        elif p["weight"] == "medium":
            pts += 8
            why.append(f'port {p["port"]}: {p["note"]}')

    if host.get("randomized"):
        pts -= 10
        why.append("randomized MAC: cameras rarely rotate addresses")

    return max(0, min(LAN_MAX, pts)), why


def verdict(pts):
    if pts >= 45:
        return "LIKELY CAMERA"
    if pts >= 20:
        return "SUSPICIOUS"
    return "CLEAR"


def run():
    name, ip, gw, net = detect_active()
    onvif_map = onvif_discover(ip)
    hosts = sweep(subnet=net, iface=name)

    for h in hosts:
        oui_annotate(h)
        port_annotate(h)
        probe_annotate(h, onvif_map)
        h["score"], h["reasons"] = score(h)
        h["verdict"] = verdict(h["score"])
        h["is_self"] = h["ip"] == ip
        h["is_gateway"] = h["ip"] == gw

    hosts.sort(key=lambda h: -h["score"])
    return {
        "interface": name, "our_ip": ip, "gateway": gw,
        "subnet": str(net), "host_count": len(hosts), "hosts": hosts,
    }


def render(result):
    from rich.console import Console
    from rich.table import Table

    con = Console()
    con.print(f'\n[bold]{result["subnet"]}[/bold] via {result["interface"]}')
    con.print(f'{result["host_count"]} host(s)\n')

    colours = {"LIKELY CAMERA": "red", "SUSPICIOUS": "yellow", "CLEAR": "green"}
    t = Table(show_lines=True)
    t.add_column("IP", width=16)
    t.add_column("Vendor", width=26, overflow="fold")
    t.add_column("Score", justify="right", width=6)
    t.add_column("Verdict", width=14)
    t.add_column("Evidence", overflow="fold")

    for h in result["hosts"]:
        vendor = h["vendor"] or ("randomized" if h["randomized"] else "unknown")
        if h["is_self"]:
            vendor += " (this laptop)"
        elif h["is_gateway"]:
            vendor += " (gateway)"
        colour = colours[h["verdict"]]
        t.add_row(
            h["ip"], vendor, str(h["score"]),
            f'[{colour}]{h["verdict"]}[/{colour}]',
            "\n".join(h["reasons"]) or "-",
        )
    con.print(t)
    con.print(
        "[dim]LAN evidence only; scores cap at 70. Cloud-only cameras "
        "may show nothing here. RF scanning is required for coverage.[/dim]\n"
    )


def main():
    ap = argparse.ArgumentParser(prog="camscan-lan")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args()

    result = run()
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        render(result)


if __name__ == "__main__":
    main()
