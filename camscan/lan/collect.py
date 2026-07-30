"""Collection pass: sweep, probe, and persist to the store."""

from camscan import store
from camscan.lan.iface import detect_active
from camscan.lan.arp import sweep
from camscan.lan.oui import annotate as oui_annotate
from camscan.lan.ports import annotate as port_annotate
from camscan.lan.onvif import onvif_discover, annotate as probe_annotate


def collect(con, skip_ports=False, note=None, progress=None):
    """Run one full LAN collection pass and persist it.

    Returns (scan_id, context dict, list of host dicts).
    """
    def say(msg):
        if progress:
            progress(msg)

    say("detecting interface")
    iface, our_ip, gateway, subnet = detect_active()

    ctx = {
        "interface": iface, "our_ip": our_ip,
        "gateway": gateway, "subnet": str(subnet),
    }

    scan_id = store.start_scan(con, subnet, iface, our_ip, gateway, note)

    say("ONVIF discovery probe")
    onvif_map = onvif_discover(our_ip)

    say(f"ARP sweep of {subnet}")
    hosts = sweep(subnet=subnet, iface=iface)
    say(f"{len(hosts)} host(s) responded")

    for i, h in enumerate(hosts, 1):
        oui_annotate(h)

        if skip_ports:
            h["open_ports"] = []
        else:
            say(f'port scan {i}/{len(hosts)}: {h["ip"]}')
            port_annotate(h)

        probe_annotate(h, onvif_map)

        h["is_self"] = h["ip"] == our_ip
        h["is_gateway"] = h["ip"] == gateway

    return scan_id, ctx, hosts


def persist(con, scan_id, hosts):
    """Write all hosts from a collection pass into the store."""
    for h in hosts:
        store.record(con, scan_id, h)


if __name__ == "__main__":
    con = store.connect()
    scan_id, ctx, hosts = collect(
        con, skip_ports=True, note="collector smoke test",
        progress=lambda m: print(f"  {m}"),
    )
    persist(con, scan_id, hosts)

    print(f"\nscan id {scan_id} on {ctx['subnet']}")
    print(f"{len(hosts)} host(s) recorded\n")

    for h in hosts:
        seen, total = store.persistence(con, h["mac"])
        vendor = h["vendor"] or ("randomized" if h["randomized"] else "unknown")
        print(f'{h["ip"]:<16} {vendor[:34]:<34} seen {seen}/{total} scan(s)')

    print("\ndb:", store.stats(con))
    con.close()
