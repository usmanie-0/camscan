"""Consolidate adjacent-channel bleed across Wi-Fi ``D``-line device records.

Wi-Fi radios aren't brick-wall filters and channels overlap, so a single
strong device is picked up by the sniffer while it listens on *neighbouring*
channels too. The result is one physical device showing up as several ``D``
lines -- same MAC, same band, adjacent channels -- with its traffic split
across the rows and its RSSI varying. Left alone, one camera looks like a
handful of weak devices and its score gets diluted.

This module merges each such bleed cluster back into ONE consolidated device,
attributed to the channel where the device was actually strongest, with the
split traffic recombined. It is pure: no serial I/O, no hardware, no scoring.

Public API:
    consolidate(device_lines) -> list[ConsolidatedDevice]

Everything else is an internal helper (leading underscore).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from camscan.rf.parser import DeviceLine


# --- Tunable adjacency thresholds ------------------------------------------
# Two channels IN THE SAME BAND are treated as adjacent-channel-bleed
# candidates when their channel NUMBERS differ by no more than this value.
# Kept as named constants so they're easy to tune against real captures.
#
# 2.4GHz: channels are only 5 MHz apart but ~20 MHz wide, so a strong device
# on ch 6 bleeds onto 3,4,5,7,8,9. A threshold of 3 captures that spill
# without reaching clear across the band.
ADJ_THRESHOLD_2G = 3
#
# 5GHz: the standard 20 MHz channels are numbered 4 apart (36,40,44,48,... and
# 149,153,157,161,165), and bleed is much weaker and rarer. A threshold of 4
# means "immediate neighbour in the list only": 36<->40 adjacent, 36<->44 not.
ADJ_THRESHOLD_5G = 4

_ADJ_THRESHOLD_BY_BAND = {
    "2G": ADJ_THRESHOLD_2G,
    "5G": ADJ_THRESHOLD_5G,
}


@dataclass
class ConsolidatedDevice:
    """One physical device after adjacent-channel bleed has been merged.

    It mirrors the numeric fields of a ``DeviceLine`` so downstream scoring can
    read it exactly the same way, with two deliberate differences:

      * it represents a MERGED device -- attributed to the channel where it was
        actually strongest (``channel``/``rssi_avg``), with traffic counts
        (``frames``, ``bytes_*``, ``h0..h3``, ``retries``) SUMMED across the
        whole bleed cluster; and
      * ``bled_channels`` records every channel the device was seen on, sorted
        and de-duplicated, so the consolidation is transparent and debuggable.

    A device seen on only ONE channel becomes a ``ConsolidatedDevice`` too (a
    cluster of size 1, ``bled_channels`` == ``[channel]``), so downstream code
    only ever has to handle one shape.
    """
    mac: str
    band: str                   # "2G" or "5G"
    channel: int                # the STRONGEST channel -- the device's true home
    rssi_avg: int               # strongest (max) rssi in the cluster; 0 iff all 0
    frames: int                 # everything below is SUMMED across the cluster
    bytes_up: int
    bytes_down: int
    h0: int
    h1: int
    h2: int
    h3: int
    retries: int
    bled_channels: list[int]    # every channel merged in, sorted ascending


def _channels_adjacent(band: str, ch_a: int, ch_b: int) -> bool:
    """Return ``True`` if two channels in ``band`` are close enough to be bleed.

    Symmetric: argument order doesn't matter. The same channel counts as
    adjacent (difference 0), so a defensive duplicate -- same MAC, same band,
    same channel -- also merges.

    Raises ``ValueError`` on an unrecognized band. This is a strict PRIMITIVE
    contract: it only knows "2G"/"5G", and anything else is a programming bug
    worth surfacing loudly. The pipeline never feeds it a bad band -- callers
    filter unknown bands out first (see ``_cluster_adjacent``) -- so in normal
    operation this raise is an unreachable dev-time guardrail.
    """
    try:
        threshold = _ADJ_THRESHOLD_BY_BAND[band]
    except KeyError:
        raise ValueError(f"unknown band {band!r}; expected '2G' or '5G'")
    return abs(ch_a - ch_b) <= threshold


# --- Grouping and clustering: WHICH lines are the same device --------------


def _group_by_mac_band(
    lines: list[DeviceLine],
) -> "dict[tuple[str, str], list[DeviceLine]]":
    """Bucket ``D`` lines by (mac, band).

    MAC is the primary identity of a physical device; band is part of the key
    because a dual-band device (same MAC on 2G and 5G) is genuinely two radios
    and must stay two records -- we never merge across bands.
    """
    groups: "dict[tuple[str, str], list[DeviceLine]]" = defaultdict(list)
    for d in lines:
        groups[(d.mac, d.band)].append(d)
    return groups


def _cluster_adjacent(
    band: str,
    lines: list[DeviceLine],
) -> list[list[DeviceLine]]:
    """Split one (mac, band) group into adjacent-channel clusters.

    All ``lines`` here share a MAC and a band, so they differ only by channel.
    We sort by channel and start a new cluster wherever the gap to the previous
    channel exceeds the band's adjacency threshold.

    Clustering is TRANSITIVE by design: on 2.4GHz (threshold 3), channels
    3-6-9 chain into one cluster (3<->6 and 6<->9 each within 3) even though
    3 and 9 differ by 6. That's correct for real bleed -- a strong device on
    ch 6 spills across a contiguous run of neighbours, so the run really is one
    device. The strongest-RSSI winner (see ``_merge_cluster``) still pins the
    true channel however wide the cluster got, and the reach is bounded by the
    tunable threshold -- so this is the behaviour we want; it's the one knob to
    revisit if real captures ever over-merge.

    ROBUSTNESS: an unrecognized band (corrupted wire data that slipped past the
    parser, which doesn't validate the band string) can't have its adjacency
    reasoned about. Rather than crash the whole scan, each such line passes
    through as its own singleton cluster -- we neither merge nor DROP it, so a
    possibly-real detection is preserved for scoring.

    Because adjacency here is "channel numbers within a threshold" on a single
    axis, sorting and cutting at over-threshold gaps yields exactly the
    connected components -- no union-find needed.
    """
    if not lines:
        return []
    if band not in _ADJ_THRESHOLD_BY_BAND:
        return [[d] for d in lines]
    ordered = sorted(lines, key=lambda d: d.channel)
    clusters: list[list[DeviceLine]] = []
    current = [ordered[0]]
    for cur in ordered[1:]:
        # current[-1] is the largest channel seen so far in this run (sorted
        # ascending), so this compares cur against its nearest lower neighbour.
        if _channels_adjacent(band, current[-1].channel, cur.channel):
            current.append(cur)
        else:
            clusters.append(current)
            current = [cur]
    clusters.append(current)
    return clusters


def _bleed_clusters(lines: list[DeviceLine]) -> list[list[DeviceLine]]:
    """All bleed clusters across every device: group, then cluster each group.

    Each returned cluster is the set of ``D`` lines that will merge into one
    ``ConsolidatedDevice``. A device seen on a single channel yields a one-line
    cluster; it still goes through the same path.
    """
    clusters: list[list[DeviceLine]] = []
    for (_mac, band), group in _group_by_mac_band(lines).items():
        clusters.extend(_cluster_adjacent(band, group))
    return clusters


# --- Merging: collapse one cluster into one device -------------------------


def _rssi_strength(rssi: int) -> float:
    """Sort key for 'how strong is this signal', with the 0 sentinel handled.

    rssi_avg is signed dBm where closer to 0 (from the negative side) is
    stronger, EXCEPT that 0 itself means 'no uplink frames seen this window'
    -- unknown, NOT a strong signal. We map the sentinel to -inf so it always
    ranks as the weakest and can never be chosen as a cluster's strongest.
    """
    return float("-inf") if rssi == 0 else float(rssi)


def _merge_cluster(cluster: list[DeviceLine]) -> ConsolidatedDevice:
    """Collapse a bleed cluster into one ``ConsolidatedDevice``.

    The STRONGEST member wins the identity fields (channel/band/rssi): its
    channel is the device's true home. Ties (equal real RSSI) break toward more
    frames, then the lower channel number, purely for deterministic output.
    Traffic fields are SUMMED -- the split counts really were all one device.
    ``rssi_avg`` is the winner's, which by construction is the strongest real
    RSSI in the cluster, or 0 only if every member was the 0 sentinel.
    """
    winner = max(
        cluster,
        key=lambda d: (_rssi_strength(d.rssi_avg), d.frames, -d.channel),
    )
    return ConsolidatedDevice(
        mac=winner.mac,
        band=winner.band,
        channel=winner.channel,
        rssi_avg=winner.rssi_avg,
        frames=sum(d.frames for d in cluster),
        bytes_up=sum(d.bytes_up for d in cluster),
        bytes_down=sum(d.bytes_down for d in cluster),
        h0=sum(d.h0 for d in cluster),
        h1=sum(d.h1 for d in cluster),
        h2=sum(d.h2 for d in cluster),
        h3=sum(d.h3 for d in cluster),
        retries=sum(d.retries for d in cluster),
        bled_channels=sorted({d.channel for d in cluster}),
    )


# --- Public entry point ----------------------------------------------------


def consolidate(device_lines: Iterable[DeviceLine]) -> list[ConsolidatedDevice]:
    """Consolidate adjacent-channel bleed in one scan's ``D`` lines.

    Takes any iterable of ``DeviceLine`` records and returns one
    ``ConsolidatedDevice`` per physical device (per band), with bleed-duplicate
    channels merged onto the strongest channel and their traffic recombined.
    Pure and side-effect-free; safe to run before scoring.
    """
    lines = list(device_lines)
    return [_merge_cluster(cluster) for cluster in _bleed_clusters(lines)]
