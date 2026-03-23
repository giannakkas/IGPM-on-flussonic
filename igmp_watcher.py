#!/usr/bin/env python3
"""
IGMP Watcher for Flussonic On-Demand + UDP Push
================================================
Monitors IGMP join/leave packets on the yacht LAN.
When a MAG box tunes to a multicast group (e.g. 239.0.0.1),
this script detects the IGMP join and pokes Flussonic's HTTP
endpoint to wake the on-demand stream. It keeps poking every
POKE_INTERVAL seconds to prevent the stream from dying.
When the last MAG leaves the group, poking stops and Flussonic's
on_demand timeout shuts down the stream.

Supports multiple channel groups (e.g. TV + Radio) with different
multicast ranges and prefixes.

Requirements:
  - Python 3.6+
  - Must run as root (raw sockets for IGMP sniffing)
  - Flussonic on_demand should be set to ON_DEMAND_TIMEOUT (default 30s)
  - POKE_INTERVAL must be less than ON_DEMAND_TIMEOUT

Usage:
  sudo python3 igmp_watcher.py
  
Or install as systemd service (see install.sh)
"""

import configparser
import socket
import struct
import threading
import time
import logging
import signal
import sys
import os
from urllib.request import Request, urlopen
from base64 import b64encode

# ── Load Configuration ──────────────────────────────────────────
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.ini')

config = configparser.ConfigParser()
config.read(CONFIG_PATH)

INTERFACE       = config.get('network', 'interface', fallback='eth0')
FLUSSONIC_BASE  = config.get('flussonic', 'url', fallback='http://localhost:8080')
FLUSSONIC_USER  = config.get('flussonic', 'user', fallback='')
FLUSSONIC_PASS  = config.get('flussonic', 'password', fallback='')
POKE_INTERVAL   = config.getint('timing', 'poke_interval', fallback=20)
LEAVE_GRACE     = config.getint('timing', 'leave_grace', fallback=5)
POKE_TIMEOUT    = config.getint('timing', 'poke_timeout', fallback=2)

# ── Logging ─────────────────────────────────────────────────────
LOG_PATH = config.get('logging', 'path', fallback='/var/log/igmp-watcher.log')
LOG_LEVEL = config.get('logging', 'level', fallback='INFO')

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_PATH),
    ]
)
log = logging.getLogger('igmp-watcher')

# ── Channel Groups ──────────────────────────────────────────────
# Each group defines a multicast range -> Flussonic stream name mapping
# Format: [group:NAME] section in config.ini

class ChannelGroup:
    def __init__(self, name, prefix, multicast_start, count):
        self.name = name
        self.prefix = prefix
        self.count = count
        self.base_ip = list(map(int, multicast_start.split('.')))
    
    def ip_to_channel(self, ip_str):
        """Convert multicast IP to channel name, or None if not in this group."""
        try:
            parts = list(map(int, ip_str.split('.')))
            if parts[:3] != self.base_ip[:3]:
                return None
            offset = parts[3] - self.base_ip[3]
            if 0 <= offset < self.count:
                return f"{self.prefix}{offset + 1}"
        except (ValueError, IndexError):
            pass
        return None
    
    def last_ip(self):
        """Return the last multicast IP in this group's range."""
        return f"{self.base_ip[0]}.{self.base_ip[1]}.{self.base_ip[2]}.{self.base_ip[3] + self.count - 1}"
    
    def first_ip(self):
        return '.'.join(map(str, self.base_ip))


# Load channel groups from config
channel_groups = []

for section in config.sections():
    if section.startswith('group:'):
        group_name = section.split(':', 1)[1]
        group = ChannelGroup(
            name=group_name,
            prefix=config.get(section, 'prefix'),
            multicast_start=config.get(section, 'multicast_start'),
            count=config.getint(section, 'count'),
        )
        channel_groups.append(group)

# Fallback: if no groups defined, use legacy single-group config
if not channel_groups:
    channel_groups.append(ChannelGroup(
        name='tv',
        prefix=config.get('channels', 'prefix', fallback='TV'),
        multicast_start=config.get('channels', 'multicast_start', fallback='239.0.0.1'),
        count=config.getint('channels', 'count', fallback=120),
    ))


def multicast_ip_to_channel(ip_str):
    """Try all channel groups to resolve a multicast IP to a stream name."""
    for group in channel_groups:
        channel = group.ip_to_channel(ip_str)
        if channel is not None:
            return channel
    return None


# ── Flussonic HTTP Poker ────────────────────────────────────────
auth_header = None
if FLUSSONIC_USER:
    creds = b64encode(f"{FLUSSONIC_USER}:{FLUSSONIC_PASS}".encode()).decode()
    auth_header = f"Basic {creds}"


def poke_stream(channel):
    """
    Send a brief HTTP request to Flussonic to wake/keepalive a stream.
    Connects to the MPEG-TS endpoint, reads a tiny amount, then closes.
    This counts as a client connection, resetting the on_demand timer.
    Since this goes to localhost, zero network bandwidth is consumed.
    """
    url = f"{FLUSSONIC_BASE}/{channel}/mpegts"
    try:
        req = Request(url)
        if auth_header:
            req.add_header('Authorization', auth_header)
        resp = urlopen(req, timeout=POKE_TIMEOUT)
        resp.read(1024)  # read just enough to trigger the stream
        resp.close()
        return True
    except Exception as e:
        log.debug(f"Poke {channel}: {e}")
        return False


# ── Stream Tracker ──────────────────────────────────────────────
class StreamTracker:
    """
    Tracks which multicast groups have active IGMP members.
    Manages the lifecycle: join -> keep alive -> leave -> grace -> stop.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.streams = {}

    def handle_join(self, channel):
        """Called when an IGMP join/report is detected for a channel."""
        with self.lock:
            if channel not in self.streams:
                log.info(f"▶ WAKE  {channel} — IGMP join detected, poking Flussonic")
                self.streams[channel] = {
                    "active": True,
                    "leave_time": None,
                    "viewers": 1,
                }
                threading.Thread(target=poke_stream, args=(channel,), daemon=True).start()
            else:
                info = self.streams[channel]
                if not info["active"] or info["leave_time"] is not None:
                    log.info(f"▶ REJOIN {channel} — cancelled pending shutdown")
                info["active"] = True
                info["leave_time"] = None
                info["viewers"] = max(info["viewers"], 1)

    def handle_leave(self, channel):
        """Called when an IGMP leave is detected for a channel."""
        with self.lock:
            if channel in self.streams:
                info = self.streams[channel]
                if info["active"] and info["leave_time"] is None:
                    log.info(f"⏸ LEAVE {channel} — grace period {LEAVE_GRACE}s started")
                    info["leave_time"] = time.time()

    def get_channels_to_poke(self):
        """
        Returns list of channels that need keepalive pokes.
        Also cleans up channels past their grace period.
        """
        with self.lock:
            now = time.time()
            to_remove = []

            for ch, info in self.streams.items():
                if info["leave_time"] is not None:
                    elapsed = now - info["leave_time"]
                    if elapsed >= LEAVE_GRACE:
                        log.info(f"⏹ STOP  {ch} — grace expired, Flussonic will shut down in ~{30 - LEAVE_GRACE}s")
                        to_remove.append(ch)

            for ch in to_remove:
                del self.streams[ch]

            return [
                ch for ch, info in self.streams.items()
                if info["active"] and info["leave_time"] is None
            ]

    def get_status(self):
        """Returns current status for logging."""
        with self.lock:
            active = [ch for ch, info in self.streams.items()
                      if info["active"] and info["leave_time"] is None]
            leaving = [ch for ch, info in self.streams.items()
                       if info["leave_time"] is not None]
            return active, leaving


tracker = StreamTracker()

# ── IGMP Packet Parser ──────────────────────────────────────────

IGMP_V1_REPORT = 0x12
IGMP_V2_REPORT = 0x16
IGMP_V2_LEAVE  = 0x17
IGMP_V3_REPORT = 0x22
IGMP_QUERY     = 0x11


def parse_igmp_packet(data):
    """
    Parse raw IP packet containing IGMP.
    Returns list of (action, group_ip) tuples.
    action is 'join' or 'leave'.
    """
    results = []

    if len(data) < 20:
        return results

    ihl = (data[0] & 0x0F) * 4

    if len(data) < ihl + 8:
        return results

    igmp_type = data[ihl]

    if igmp_type in (IGMP_V1_REPORT, IGMP_V2_REPORT):
        group = socket.inet_ntoa(data[ihl + 4:ihl + 8])
        results.append(('join', group))

    elif igmp_type == IGMP_V2_LEAVE:
        group = socket.inet_ntoa(data[ihl + 4:ihl + 8])
        results.append(('leave', group))

    elif igmp_type == IGMP_V3_REPORT:
        if len(data) < ihl + 8:
            return results
        num_records = struct.unpack('!H', data[ihl + 6:ihl + 8])[0]
        offset = ihl + 8

        for _ in range(num_records):
            if offset + 8 > len(data):
                break

            rec_type = data[offset]
            aux_len = data[offset + 1]
            num_sources = struct.unpack('!H', data[offset + 2:offset + 4])[0]
            group = socket.inet_ntoa(data[offset + 4:offset + 8])

            if rec_type in (2, 4, 5):
                results.append(('join', group))
            elif rec_type in (1, 3, 6):
                if num_sources == 0 and rec_type in (1, 3):
                    results.append(('leave', group))
                elif rec_type == 6:
                    results.append(('leave', group))
                else:
                    results.append(('join', group))

            offset += 8 + (num_sources * 4) + (aux_len * 4)

    return results


# ── IGMP Sniffer Thread ─────────────────────────────────────────
running = True


def igmp_sniffer():
    """Listen for IGMP packets using a raw socket."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_IGMP)
    except PermissionError:
        log.error("Must run as root to capture IGMP packets!")
        sys.exit(1)

    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    SO_BINDTODEVICE = 25
    try:
        sock.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, INTERFACE.encode() + b'\0')
        log.info(f"Bound to interface: {INTERFACE}")
    except OSError as e:
        log.warning(f"Could not bind to {INTERFACE}: {e} — listening on all interfaces")

    sock.settimeout(2.0)

    for g in channel_groups:
        log.info(f"Watching: {g.name} → {g.first_ip()}-{g.last_ip()} ({g.prefix}1-{g.prefix}{g.count})")

    while running:
        try:
            data, addr = sock.recvfrom(65535)
            events = parse_igmp_packet(data)

            for action, group_ip in events:
                channel = multicast_ip_to_channel(group_ip)
                if channel is None:
                    continue

                if action == 'join':
                    tracker.handle_join(channel)
                elif action == 'leave':
                    tracker.handle_leave(channel)

        except socket.timeout:
            continue
        except Exception as e:
            log.error(f"Sniffer error: {e}")
            time.sleep(1)

    sock.close()
    log.info("IGMP sniffer stopped")


# ── Keepalive Poker Thread ──────────────────────────────────────
def keepalive_loop():
    log.info(f"Keepalive loop started — poking every {POKE_INTERVAL}s")

    while running:
        time.sleep(POKE_INTERVAL)
        if not running:
            break

        channels = tracker.get_channels_to_poke()
        if channels:
            log.debug(f"Poking {len(channels)} active channels: {', '.join(channels)}")
            for ch in channels:
                threading.Thread(target=poke_stream, args=(ch,), daemon=True).start()


# ── Status Reporter Thread ──────────────────────────────────────
def status_reporter():
    while running:
        time.sleep(60)
        if not running:
            break
        active, leaving = tracker.get_status()
        if active or leaving:
            log.info(f"📊 Status — Active: {len(active)} [{', '.join(sorted(active))}] | Leaving: {len(leaving)} [{', '.join(sorted(leaving))}]")
        else:
            log.debug("📊 Status — No active streams")


# ── Main ────────────────────────────────────────────────────────
def shutdown(sig, frame):
    global running
    log.info("Shutting down...")
    running = False


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)


def main():
    total_channels = sum(g.count for g in channel_groups)
    
    log.info("=" * 60)
    log.info("IGMP Watcher for Flussonic — Starting")
    log.info(f"  Interface:    {INTERFACE}")
    log.info(f"  Flussonic:    {FLUSSONIC_BASE}")
    log.info(f"  Groups:       {len(channel_groups)}")
    for g in channel_groups:
        log.info(f"    {g.name}: {g.prefix}1-{g.prefix}{g.count} → {g.first_ip()}-{g.last_ip()}")
    log.info(f"  Total:        {total_channels} channels")
    log.info(f"  Poke every:   {POKE_INTERVAL}s")
    log.info(f"  Leave grace:  {LEAVE_GRACE}s")
    log.info("=" * 60)

    if POKE_INTERVAL >= 30:
        log.warning("POKE_INTERVAL should be less than Flussonic's on_demand timeout (30s)!")

    threads = [
        threading.Thread(target=keepalive_loop, daemon=True, name="keepalive"),
        threading.Thread(target=status_reporter, daemon=True, name="status"),
    ]
    for t in threads:
        t.start()

    igmp_sniffer()

    log.info("IGMP Watcher stopped")


if __name__ == '__main__':
    main()
