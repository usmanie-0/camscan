"""Tests for camscan.rf.consolidate. Pure synthetic data -- no board, no serial.

Run from the project root with the venv active:

    python camscan\\rf\\test_consolidate.py

Prints one line per check; raises AssertionError on the first failure and stops.
Grouped by build step so you can see exactly what each step added.
"""

from __future__ import annotations

from camscan.rf.parser import DeviceLine
from camscan.rf.consolidate import (
    ConsolidatedDevice,
    consolidate,
    _channels_adjacent,
    _group_by_mac_band,
    _cluster_adjacent,
    _bleed_clusters,
    _merge_cluster,
    ADJ_THRESHOLD_2G,
    ADJ_THRESHOLD_5G,
)


def check(desc: str, cond: bool) -> None:
    assert cond, f"FAILED: {desc}"
    print(f"  ok: {desc}")


# A tiny factory so tests read as "a device on this mac/band/channel with this
# rssi and traffic" without spelling out every field each time.
def dev(mac: str, band: str, channel: int, rssi: int = -60,
        frames: int = 1, bytes_up: int = 0, bytes_down: int = 0,
        h0: int = 0, h1: int = 0, h2: int = 0, h3: int = 0,
        retries: int = 0) -> DeviceLine:
    return DeviceLine(
        mac=mac, band=band, channel=channel, rssi_avg=rssi,
        frames=frames, bytes_up=bytes_up, bytes_down=bytes_down,
        h0=h0, h1=h1, h2=h2, h3=h3, retries=retries,
    )


def channels_of(cluster: list[DeviceLine]) -> list[int]:
    return sorted(d.channel for d in cluster)


def by_channel(devices: list[ConsolidatedDevice]) -> "dict[int, ConsolidatedDevice]":
    return {d.channel: d for d in devices}


# --- Step 1: adjacency helper + dataclass ----------------------------------

def test_2g_adjacency() -> None:
    print(f"[step 1] 2.4GHz adjacency (threshold = {ADJ_THRESHOLD_2G})")
    check("6 <-> 7 adjacent", _channels_adjacent("2G", 6, 7))
    check("6 <-> 5 adjacent", _channels_adjacent("2G", 6, 5))
    check("6 <-> 9 adjacent (diff 3)", _channels_adjacent("2G", 6, 9))
    check("6 <-> 3 adjacent (diff 3)", _channels_adjacent("2G", 6, 3))
    check("6 <-> 10 NOT adjacent (diff 4)", not _channels_adjacent("2G", 6, 10))
    check("1 <-> 11 NOT adjacent (real channel change, not bleed)",
          not _channels_adjacent("2G", 1, 11))
    check("6 <-> 6 adjacent (same channel still merges)",
          _channels_adjacent("2G", 6, 6))
    check("symmetric: (7,6) == (6,7)",
          _channels_adjacent("2G", 7, 6) == _channels_adjacent("2G", 6, 7))


def test_5g_adjacency() -> None:
    print(f"[step 1] 5GHz adjacency (threshold = {ADJ_THRESHOLD_5G})")
    check("36 <-> 40 adjacent (diff 4, list neighbours)",
          _channels_adjacent("5G", 36, 40))
    check("36 <-> 44 NOT adjacent (diff 8)",
          not _channels_adjacent("5G", 36, 44))
    check("149 <-> 153 adjacent (UNII-3 neighbours)",
          _channels_adjacent("5G", 149, 153))
    check("157 <-> 165 NOT adjacent (diff 8)",
          not _channels_adjacent("5G", 157, 165))
    check("161 <-> 161 adjacent (same channel)",
          _channels_adjacent("5G", 161, 161))


def test_band_guard() -> None:
    print("[step 1] band handling")
    check("2G and 5G thresholds differ",
          ADJ_THRESHOLD_2G != ADJ_THRESHOLD_5G)
    raised = False
    try:
        _channels_adjacent("6G", 1, 1)
    except ValueError:
        raised = True
    check("unknown band raises ValueError (strict primitive)", raised)


def test_dataclass_shape() -> None:
    print("[step 1] ConsolidatedDevice shape")
    d = ConsolidatedDevice(
        mac="aa:bb:cc:dd:ee:ff", band="2G", channel=6, rssi_avg=-55,
        frames=10, bytes_up=1000, bytes_down=200,
        h0=1, h1=2, h2=3, h3=4, retries=0, bled_channels=[5, 6, 7],
    )
    check("attributed to the strongest channel", d.channel == 6)
    check("remembers every channel it bled across", d.bled_channels == [5, 6, 7])
    check("carries the same numeric fields as a DeviceLine",
          (d.frames, d.bytes_up, d.bytes_down, d.h3, d.retries)
          == (10, 1000, 200, 4, 0))


# --- Step 2: grouping and clustering ---------------------------------------

def test_group_by_mac_band() -> None:
    print("[step 2] grouping by (mac, band)")
    groups = _group_by_mac_band([dev("aa", "2G", 6),
                                 dev("bb", "2G", 6),
                                 dev("aa", "2G", 7)])
    check("two distinct MACs -> two groups", len(groups) == 2)
    check("same MAC lands together", len(groups[("aa", "2G")]) == 2)
    check("other MAC stays separate", len(groups[("bb", "2G")]) == 1)


def test_dual_band_splits_into_two_groups() -> None:
    print("[step 2] dual-band device -> one group per band")
    groups = _group_by_mac_band([dev("aa", "2G", 6), dev("aa", "5G", 36)])
    check("same MAC on 2G and 5G -> two groups", len(groups) == 2)
    check("2G group present", ("aa", "2G") in groups)
    check("5G group present", ("aa", "5G") in groups)


def test_cluster_2g_bleed() -> None:
    print("[step 2] 2.4GHz bleed cluster")
    clusters = _cluster_adjacent("2G", [dev("aa", "2G", 5),
                                        dev("aa", "2G", 6),
                                        dev("aa", "2G", 7)])
    check("ch 5,6,7 form ONE cluster", len(clusters) == 1)
    check("cluster holds all three channels",
          channels_of(clusters[0]) == [5, 6, 7])


def test_cluster_non_adjacent_stays_split() -> None:
    print("[step 2] non-adjacent same-MAC channels stay separate")
    clusters = _cluster_adjacent("2G", [dev("aa", "2G", 1), dev("aa", "2G", 11)])
    check("ch 1 and ch 11 -> TWO clusters", len(clusters) == 2)


def test_cluster_transitive_chain() -> None:
    print("[step 2] transitive chaining (surfaced design decision)")
    clusters = _cluster_adjacent("2G", [dev("aa", "2G", 3),
                                        dev("aa", "2G", 6),
                                        dev("aa", "2G", 9)])
    check("3-6-9 chain into ONE cluster", len(clusters) == 1)
    check("cluster spans 3..9", channels_of(clusters[0]) == [3, 6, 9])


def test_cluster_gap_splits() -> None:
    print("[step 2] a gap wider than the threshold splits the run")
    clusters = _cluster_adjacent("2G", [dev("aa", "2G", 5), dev("aa", "2G", 6),
                                        dev("aa", "2G", 11), dev("aa", "2G", 12)])
    check("{5,6} and {11,12} -> TWO clusters", len(clusters) == 2)
    got = sorted((channels_of(c) for c in clusters), key=lambda x: x[0])
    check("clusters are exactly {5,6} and {11,12}", got == [[5, 6], [11, 12]])


def test_cluster_same_channel_dup() -> None:
    print("[step 2] defensive same-channel duplicate")
    clusters = _cluster_adjacent("2G", [dev("aa", "2G", 6), dev("aa", "2G", 6)])
    check("two lines on the same channel -> ONE cluster of size 2",
          len(clusters) == 1 and len(clusters[0]) == 2)


def test_cluster_unknown_band_robust() -> None:
    print("[step 2] robustness: unknown band never crashes the scan")
    # A corrupted band string that slipped past the parser must NOT raise here
    # and must NOT be dropped -- each line becomes its own singleton.
    clusters = _cluster_adjacent("ZZ", [dev("aa", "ZZ", 6), dev("aa", "ZZ", 7)])
    check("unknown band -> singletons, no raise", len(clusters) == 2)


def test_bleed_clusters_end_to_end() -> None:
    print("[step 2] _bleed_clusters ties grouping + clustering together")
    clusters = _bleed_clusters([
        dev("aa", "2G", 5), dev("aa", "2G", 6), dev("aa", "2G", 7),  # 1 cluster
        dev("aa", "5G", 36),                                          # 1 cluster
        dev("bb", "2G", 1), dev("bb", "2G", 11),                      # 2 clusters
    ])
    check("total clusters across all devices == 4", len(clusters) == 4)
    check("cluster sizes are [1,1,1,3]",
          sorted(len(c) for c in clusters) == [1, 1, 1, 3])


# --- Step 3: merging + public consolidate() --------------------------------

def test_merge_sums_and_strongest_channel() -> None:
    print("[step 3] merge: strongest channel wins, traffic sums")
    cluster = [
        dev("aa", "2G", 5, rssi=-70, frames=2, bytes_up=100, bytes_down=10, h0=2),
        dev("aa", "2G", 6, rssi=-55, frames=3, bytes_up=300, bytes_down=20, h1=3),
        dev("aa", "2G", 7, rssi=-80, frames=1, bytes_up=50,  bytes_down=5,  h0=1),
    ]
    m = _merge_cluster(cluster)
    check("winner is the strongest channel (6 @ -55)", m.channel == 6)
    check("rssi_avg is the strongest value", m.rssi_avg == -55)
    check("frames summed (2+3+1)", m.frames == 6)
    check("bytes_up summed (100+300+50)", m.bytes_up == 450)
    check("bytes_down summed (10+20+5)", m.bytes_down == 35)
    check("histogram buckets summed (h0=3, h1=3)", (m.h0, m.h1) == (3, 3))
    check("bled_channels records the spread", m.bled_channels == [5, 6, 7])


def test_merge_zero_sentinel_never_strongest() -> None:
    print("[step 3] merge: rssi==0 sentinel is never chosen as strongest")
    # ch7 has rssi 0 (no uplink seen). Numerically 0 > -55, but 0 means
    # 'unknown/weakest' -- the winner must be ch6 @ -55, not ch7.
    m = _merge_cluster([dev("aa", "2G", 6, rssi=-55),
                        dev("aa", "2G", 7, rssi=0)])
    check("strongest channel is 6, NOT the 0-sentinel 7", m.channel == 6)
    check("rssi_avg is -55, not 0", m.rssi_avg == -55)


def test_merge_all_zero_keeps_zero() -> None:
    print("[step 3] merge: an all-sentinel cluster keeps rssi 0")
    # No uplink seen on any channel. rssi stays 0; the busiest channel (more
    # frames) wins the tie for 'true home'.
    m = _merge_cluster([dev("aa", "2G", 5, rssi=0, frames=1),
                        dev("aa", "2G", 6, rssi=0, frames=9)])
    check("rssi_avg stays 0", m.rssi_avg == 0)
    check("busier channel (6) wins the all-zero tie", m.channel == 6)


def test_consolidate_full_scan() -> None:
    print("[step 3] consolidate(): the full brief, one realistic scan")
    lines = [
        # A 2.4GHz camera-like device bleeding across 5,6,7; strongest on 6.
        dev("cam", "2G", 5, rssi=-72, frames=4, bytes_up=2000),
        dev("cam", "2G", 6, rssi=-58, frames=9, bytes_up=8000),
        dev("cam", "2G", 7, rssi=-79, frames=2, bytes_up=1500),
        # Same MAC, non-adjacent channels 1 and 11 -> stays TWO devices.
        dev("roamer", "2G", 1, rssi=-60, frames=3),
        dev("roamer", "2G", 11, rssi=-62, frames=3),
        # A dual-band device: same MAC on 2G and 5G -> two records, no merge.
        dev("dual", "2G", 6, rssi=-50, frames=5),
        dev("dual", "5G", 36, rssi=-48, frames=5),
        # A lone single-channel device -> passes through unchanged.
        dev("solo", "5G", 149, rssi=-40, frames=7, bytes_up=9000),
        # A different MAC that happens to share channel 6 -> never merges in.
        dev("other", "2G", 6, rssi=-55, frames=1),
    ]
    out = consolidate(lines)

    macs = sorted(d.mac for d in out)
    check("expected 7 consolidated records total", len(out) == 7)
    check("record MACs are right",
          macs == ["cam", "dual", "dual", "other", "roamer", "roamer", "solo"])

    cam = next(d for d in out if d.mac == "cam")
    check("cam merged onto strongest channel 6", cam.channel == 6)
    check("cam traffic recombined (frames 4+9+2=15)", cam.frames == 15)
    check("cam bytes_up recombined (2000+8000+1500)", cam.bytes_up == 11500)
    check("cam bled_channels == [5,6,7]", cam.bled_channels == [5, 6, 7])

    roamers = [d for d in out if d.mac == "roamer"]
    check("roamer stayed TWO separate devices", len(roamers) == 2)
    check("roamer channels are 1 and 11",
          sorted(d.channel for d in roamers) == [1, 11])

    duals = [d for d in out if d.mac == "dual"]
    check("dual-band device is TWO records", len(duals) == 2)
    check("dual bands are 2G and 5G",
          sorted(d.band for d in duals) == ["2G", "5G"])

    solo = next(d for d in out if d.mac == "solo")
    check("solo passed through unchanged (channel 149)", solo.channel == 149)
    check("solo bled_channels is just [149]", solo.bled_channels == [149])
    check("solo traffic untouched", (solo.frames, solo.bytes_up) == (7, 9000))

    other = next(d for d in out if d.mac == "other")
    check("different MAC on ch6 never absorbed into cam",
          other.frames == 1 and other.bled_channels == [6])


def test_consolidate_empty() -> None:
    print("[step 3] consolidate(): empty input -> empty output")
    check("no lines -> no devices", consolidate([]) == [])


if __name__ == "__main__":
    test_2g_adjacency()
    test_5g_adjacency()
    test_band_guard()
    test_dataclass_shape()
    test_group_by_mac_band()
    test_dual_band_splits_into_two_groups()
    test_cluster_2g_bleed()
    test_cluster_non_adjacent_stays_split()
    test_cluster_transitive_chain()
    test_cluster_gap_splits()
    test_cluster_same_channel_dup()
    test_cluster_unknown_band_robust()
    test_bleed_clusters_end_to_end()
    test_merge_sums_and_strongest_channel()
    test_merge_zero_sentinel_never_strongest()
    test_merge_all_zero_keeps_zero()
    test_consolidate_full_scan()
    test_consolidate_empty()
    print("\nAll checks passed (steps 1-3).")

    # --- Eyeball demo: see the actual consolidated output, not just asserts ---
    print("\n--- demo: one 2.4GHz device bleeding across ch 5/6/7 ---")
    demo = [
        DeviceLine("6e:c1:c0:00:00:01", "2G", 5, -72, 4, 2000, 0, 0, 0, 0, 0, 0),
        DeviceLine("6e:c1:c0:00:00:01", "2G", 6, -58, 9, 8000, 0, 0, 0, 0, 0, 0),
        DeviceLine("6e:c1:c0:00:00:01", "2G", 7, -79, 2, 1500, 0, 0, 0, 0, 0, 0),
    ]
    print("in : 3 D lines on channels 5, 6, 7 (same MAC)")
    for d in consolidate(demo):
        print(f"out: mac={d.mac} band={d.band} channel={d.channel} "
              f"rssi={d.rssi_avg} frames={d.frames} bytes_up={d.bytes_up} "
              f"bled_channels={d.bled_channels}")
