"""ARP sweep to enumerate live hosts on the local subnet."""

from scapy.all import ARP, Ether, srp, conf
from camscan.lan.iface import detect_active


def sweep(subnet=None, iface=None, timeout=3, retry=1, verbose=False):
    """Broadcast ARP requests across the subnet. Returns list of dicts."""
    if subnet is None or iface is None:
        name, ip, gw, net = detect_active()
        subnet = subnet or net
        iface = iface or name

    pkt = Ether(dst="ff:ff:ff:ff:ff:ff") / ARP(pdst=str(subnet))

    answered, _ = srp(
        pkt, iface=iface, timeout=timeout, retry=retry, verbose=verbose
    )

    hosts = []
    for _, received in answered:
        hosts.append({"ip": received.psrc, "mac": received.hwsrc.lower()})

    hosts.sort(key=lambda h: tuple(int(o) for o in h["ip"].split(".")))
    return hosts


if __name__ == "__main__":
    name, ip, gw, net = detect_active()
    print(f"sweeping {net} via {name}\n")

    hosts = sweep(subnet=net, iface=name)

    for h in hosts:
        tag = ""
        if h["ip"] == ip:
            tag = "  <- this laptop"
        elif h["ip"] == gw:
            tag = "  <- gateway"
        print(f'{h["ip"]:<16} {h["mac"]}{tag}')

    print(f"\n{len(hosts)} host(s) found")
