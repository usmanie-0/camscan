"""Interface and subnet detection for camscan."""

import ipaddress
import socket
from scapy.all import conf

EXCLUDE = (
    "loopback", "virtualbox", "vmware", "hyper-v", "wi-fi direct",
    "bluetooth", "wan miniport", "tap-", "tunnel",
)


def _outbound_ip():
    """Ask the OS which local IP would carry traffic to the internet.

    No packets are sent. UDP connect() only resolves the route.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def _iface_for_ip(ip):
    """Find the Scapy interface holding this IP. Returns (name, iface)."""
    for iface in conf.ifaces.values():
        if getattr(iface, "ip", None) == ip:
            name = (getattr(iface, "description", None)
                    or getattr(iface, "network_name", None)
                    or getattr(iface, "name", None)
                    or str(iface))
            return name, iface
    return None, None


def _subnet_for(our_ip):
    """Smallest routed network that actually contains our IP."""
    addr = ipaddress.IPv4Address(our_ip)
    best = None
    for net, msk, gw, ifc, oip, metric in conf.route.routes:
        if msk in (0, 0xFFFFFFFF):
            continue
        net_addr = ipaddress.IPv4Address(net)
        if net_addr.is_multicast or net_addr.is_loopback:
            continue
        prefix = bin(msk).count("1")
        try:
            network = ipaddress.ip_network(f"{net_addr}/{prefix}", strict=False)
        except ValueError:
            continue
        if addr not in network or network.num_addresses <= 2:
            continue
        if best is None or network.num_addresses < best.num_addresses:
            best = network
    return best or ipaddress.ip_network(f"{our_ip}/24", strict=False)


def _gateway_for(our_ip):
    for net, msk, gw, ifc, oip, metric in conf.route.routes:
        if msk == 0 and oip == our_ip and gw != "0.0.0.0":
            return gw
    return None


def detect_active():
    """Return (name, ip, gateway, subnet) for the real outbound interface."""
    ip = _outbound_ip()

    if ip.startswith("169.254.") or ip.startswith("127."):
        raise RuntimeError(f"No usable route (got {ip}). Connected to Wi-Fi?")

    name, iface = _iface_for_ip(ip)
    if name is None:
        raise RuntimeError(f"No Scapy interface holds {ip}")
    if any(k in name.lower() for k in EXCLUDE):
        raise RuntimeError(f"Outbound route is on an excluded adapter: {name}")

    return name, ip, _gateway_for(ip), _subnet_for(ip)


if __name__ == "__main__":
    name, ip, gw, subnet = detect_active()
    print(f"interface : {name}")
    print(f"our ip    : {ip}")
    print(f"gateway   : {gw}")
    print(f"subnet    : {subnet}")
    print(f"hosts     : {subnet.num_addresses - 2}")
