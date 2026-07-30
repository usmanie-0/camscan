"""camscan command-line interface."""

import argparse
import json
import sys

from camscan import store
from camscan.analysis.scoring import score, LAN_CEILING
from camscan.lan.collect import collect, persist

COLOURS = {"LIKELY CAMERA": "red", "SUSPICIOUS": "yellow", "CLEAR": "green"}
LEVEL_COLOUR = {
    "definitive": "bold red", "high": "red", "medium": "yellow",
    "negative": "dim", "gap": "cyan",
}


def cmd_scan(args):
    con = store.connect()
    quiet = args.json

    scan_id, ctx, hosts = collect(
        con,
        skip_ports=args.fast,
        note=args.note,
        progress=None if quiet else (lambda m: print(f"  {m}", file=sys.stderr)),
    )

    for h in hosts:
        score(h, con)
    persist(con, scan_id, hosts)
    hosts.sort(key=lambda h: -h["raw_score"])

    result = {"scan_id": scan_id, **ctx,
              "host_count": len(hosts), "hosts": hosts}

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        _render(result, show_all=args.all)
    con.close()


def _render(result, show_all=False):
    from rich.console import Console
    from rich.table import Table

    con = Console()
    con.print(f'\n[bold]{result["subnet"]}[/bold]  via {result["interface"]}')
    con.print(f'scan #{result["scan_id"]}  -  {result["host_count"]} host(s)\n')

    shown = [h for h in result["hosts"] if show_all or h["raw_score"] > 0]

    if not shown:
        con.print("[green]No devices scored above zero on LAN evidence.[/green]")
    else:
        t = Table(show_lines=True)
        t.add_column("IP", width=15)
        t.add_column("Vendor", width=24, overflow="fold")
        t.add_column("Score", justify="right", width=8)
        t.add_column("Verdict", width=14)
        t.add_column("Evidence", overflow="fold")

        for h in shown:
            vendor = h["vendor"] or ("randomized" if h["randomized"] else "unknown")
            if h["is_self"]:
                vendor += " (this laptop)"
            elif h["is_gateway"]:
                vendor += " (gateway)"

            score_cell = f'{h["score"]}/{h["ceiling"]}'
            if h["clamped"]:
                score_cell += f'\n[dim]raw {h["raw_score"]}[/dim]'

            lines = [f'[{LEVEL_COLOUR.get(lvl, "white")}]{txt}[/]'
                     for lvl, txt in h["reasons"]]
            lines += [f'[cyan]{g}[/cyan]' for g in h["unchecked"]]

            t.add_row(h["ip"], vendor, score_cell,
                      f'[{COLOURS[h["verdict"]]}]{h["verdict"]}[/]',
                      "\n".join(lines) or "-")
        con.print(t)

    con.print(
        f"[dim]LAN evidence only; scores cap at {LAN_CEILING}. "
        "A low score means nothing was found on the network, not that the "
        "room is clear. RF scanning is required for full coverage.[/dim]\n"
    )


def cmd_history(args):
    con = store.connect()
    rows = con.execute(
        """SELECT d.mac, d.vendor, d.randomized, d.cam_vendor_weight,
                  COUNT(DISTINCT o.scan_id) AS seen,
                  MAX(o.ip)         AS last_ip,
                  MAX(o.raw_score)  AS best_score,
                  d.first_seen, d.last_seen
             FROM devices d
             LEFT JOIN observations o ON o.mac = d.mac
            GROUP BY d.mac
            ORDER BY best_score DESC NULLS LAST, seen DESC"""
    ).fetchall()
    total = con.execute("SELECT COUNT(*) AS n FROM scans").fetchone()["n"]

    if args.json:
        print(json.dumps({"scan_total": total,
                          "devices": [dict(r) for r in rows]}, indent=2))
    else:
        from rich.console import Console
        from rich.table import Table
        c = Console()
        c.print(f"\n[bold]{len(rows)} device(s)[/bold] across {total} scan(s)\n")
        t = Table()
        t.add_column("Last IP", width=15)
        t.add_column("MAC", width=18)
        t.add_column("Vendor", width=30, overflow="fold")
        t.add_column("Seen", justify="right")
        t.add_column("Best", justify="right")
        for r in rows:
            vendor = r["vendor"] or ("randomized" if r["randomized"] else "unknown")
            style = "red" if r["cam_vendor_weight"] == "high" else ""
            t.add_row(r["last_ip"] or "-", r["mac"], vendor,
                      f'{r["seen"]}/{total}', str(r["best_score"] or 0),
                      style=style)
        c.print(t)
        c.print()
    con.close()


def cmd_db(args):
    con = store.connect()
    print(f"database : {store.DB_PATH}")
    for k, v in store.stats(con).items():
        print(f"{k:9}: {v}")
    con.close()


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="camscan", description="Hidden networked camera detection")
    sub = ap.add_subparsers(dest="command", required=True)

    s = sub.add_parser("scan", help="scan the current network")
    s.add_argument("--json", action="store_true")
    s.add_argument("--fast", action="store_true", help="skip the port scan")
    s.add_argument("--all", action="store_true", help="include zero-score hosts")
    s.add_argument("--note", help="label this scan")
    s.set_defaults(func=cmd_scan)

    h = sub.add_parser("history", help="devices seen across all scans")
    h.add_argument("--json", action="store_true")
    h.set_defaults(func=cmd_history)

    d = sub.add_parser("db", help="store statistics")
    d.set_defaults(func=cmd_db)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
