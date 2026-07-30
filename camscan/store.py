"""SQLite persistence for camscan."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "camscan.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    subnet      TEXT,
    interface   TEXT,
    our_ip      TEXT,
    gateway     TEXT,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS devices (
    mac           TEXT PRIMARY KEY,
    vendor        TEXT,
    randomized    INTEGER NOT NULL DEFAULT 0,
    cam_vendor_weight TEXT,
    cam_vendor_note   TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    label         TEXT
);

CREATE TABLE IF NOT EXISTS observations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id     INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
    mac         TEXT NOT NULL REFERENCES devices(mac),
    ip          TEXT,
    open_ports  TEXT,
    onvif       TEXT,
    rtsp        TEXT,
    raw_score   INTEGER,
    seen_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_obs_mac  ON observations(mac);
CREATE INDEX IF NOT EXISTS idx_obs_scan ON observations(scan_id);
"""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path=None):
    """Open the database, creating schema on first use."""
    path = Path(path) if path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    return con


def start_scan(con, subnet, interface, our_ip, gateway, note=None):
    cur = con.execute(
        "INSERT INTO scans (started_at, subnet, interface, our_ip, gateway, note)"
        " VALUES (?,?,?,?,?,?)",
        (_now(), str(subnet), interface, our_ip, gateway, note),
    )
    con.commit()
    return cur.lastrowid


def record(con, scan_id, host):
    """Upsert the device and insert one observation."""
    now = _now()
    mac = host["mac"]

    con.execute(
        """INSERT INTO devices
             (mac, vendor, randomized, cam_vendor_weight, cam_vendor_note,
              first_seen, last_seen)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(mac) DO UPDATE SET
             vendor = COALESCE(excluded.vendor, devices.vendor),
             cam_vendor_weight = COALESCE(excluded.cam_vendor_weight,
                                          devices.cam_vendor_weight),
             cam_vendor_note = COALESCE(excluded.cam_vendor_note,
                                        devices.cam_vendor_note),
             last_seen = excluded.last_seen""",
        (mac, host.get("vendor"), int(bool(host.get("randomized"))),
         host.get("cam_vendor_weight"), host.get("cam_vendor_note"), now, now),
    )

    con.execute(
        """INSERT INTO observations
             (scan_id, mac, ip, open_ports, onvif, rtsp, raw_score, seen_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (scan_id, mac, host.get("ip"),
         json.dumps(host.get("open_ports") or []),
         json.dumps(host.get("onvif")) if host.get("onvif") else None,
         json.dumps(host.get("rtsp")) if host.get("rtsp") else None,
         host.get("raw_score"), now),
    )
    con.commit()


def history(con, mac, limit=50):
    """Recent observations for one device, newest first."""
    rows = con.execute(
        "SELECT * FROM observations WHERE mac = ?"
        " ORDER BY seen_at DESC LIMIT ?", (mac, limit)
    ).fetchall()
    return [dict(r) for r in rows]


def persistence(con, mac):
    """(times_seen, total_scans) for this device."""
    seen = con.execute(
        "SELECT COUNT(DISTINCT scan_id) AS n FROM observations WHERE mac = ?",
        (mac,)
    ).fetchone()["n"]
    total = con.execute("SELECT COUNT(*) AS n FROM scans").fetchone()["n"]
    return seen, total


def stats(con):
    return {
        "scans": con.execute("SELECT COUNT(*) AS n FROM scans").fetchone()["n"],
        "devices": con.execute("SELECT COUNT(*) AS n FROM devices").fetchone()["n"],
        "observations": con.execute(
            "SELECT COUNT(*) AS n FROM observations").fetchone()["n"],
    }


if __name__ == "__main__":
    con = connect()
    print(f"database: {DB_PATH}")
    tables = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    print("tables  :", ", ".join(r["name"] for r in tables))
    print("counts  :", stats(con))
    con.close()
