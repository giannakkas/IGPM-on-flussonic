#!/usr/bin/env python3
"""
IGMP Watcher for Flussonic On-Demand + UDP Push
================================================
Uses AF_PACKET (Layer 2 raw socket) to capture IGMP packets,
same method as tcpdump. This is necessary because Linux kernel
consumes IGMP packets before AF_INET raw sockets can see them.
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

INTERFACE        = config.get('network', 'interface', fallback='eth0')
FLUSSONIC_BASE   = config.get('flussonic', 'url', fallback='http://localhost:8080')
FLUSSONIC_USER   = config.get('flussonic', 'user', fallback='')
FLUSSONIC_PASS   = config.get('flussonic', 'password', fallback='')
POKE_INTERVAL    = config.getint('timing', 'poke_interval', fallback=20)
LEAVE_GRACE      = config.getint('timing', 'leave_grace', fallback=5)
POKE_TIMEOUT     = config.getint('timing', 'poke_timeout', fallback=2)
WATCHDOG_TIMEOUT = config.getint('timing', 'watchdog_timeout', fallback=180)

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

class ChannelGroup:
    def __init__(self, name, prefix, multicast_start, count):
        self.name = name
        self.prefix = prefix
        self.count = count
        self.base_ip = list(map(int, multicast_start.split('.')))
    
    def ip_to_channel(self, ip_str):
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
        return f"{self.base_ip[0]}.{self.base_ip[1]}.{self.base_ip[2]}.{self.base_ip[3] + self.count - 1}"
    
    def first_ip(self):
        return '.'.join(map(str, self.base_ip))


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

if not channel_groups:
    channel_groups.append(ChannelGroup(
        name='tv',
        prefix=config.get('channels', 'prefix', fallback='TV'),
        multicast_start=config.get('channels', 'multicast_start', fallback='239.0.0.1'),
        count=config.getint('channels', 'count', fallback=120),
    ))


def multicast_ip_to_channel(ip_str):
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
    url = f"{FLUSSONIC_BASE}/{channel}/mpegts"
    try:
        req = Request(url)
        if auth_header:
            req.add_header('Authorization', auth_header)
        resp = urlopen(req, timeout=POKE_TIMEOUT)
        resp.read(1024)
        resp.close()
        return True
    except Exception as e:
        log.debug(f"Poke {channel}: {e}")
        return False


def wake_stream(channel):
    """
    Aggressively poke a stream until it comes alive.
    Retries every 2 seconds for up to 15 seconds.
    This handles the delay while Flussonic connects to the SRT source.
    """
    for attempt in range(8):  # 8 attempts x 2s = 16s max
        if not running:
            return
        success = poke_stream(channel)
        if success:
            log.info(f"✓ ALIVE {channel} — stream responded after {(attempt + 1) * 2}s")
            return
        log.debug(f"Wake {channel}: attempt {attempt + 1}/8, retrying in 2s...")
        time.sleep(2)


# ── Stream Tracker ──────────────────────────────────────────────
class StreamTracker:
    def __init__(self):
        self.lock = threading.Lock()
        self.streams = {}

    def handle_join(self, channel):
        with self.lock:
            now = time.time()
            if channel not in self.streams:
                log.info(f"▶ WAKE  {channel} — IGMP join detected, poking Flussonic")
                self.streams[channel] = {
                    "active": True,
                    "leave_time": None,
                    "last_seen": now,
                }
                threading.Thread(target=wake_stream, args=(channel,), daemon=True).start()
            else:
                info = self.streams[channel]
                if not info["active"] or info["leave_time"] is not None:
                    log.info(f"▶ REJOIN {channel} — cancelled pending shutdown")
                info["active"] = True
                info["leave_time"] = None
                info["last_seen"] = now

    def handle_leave(self, channel):
        with self.lock:
            if channel in self.streams:
                info = self.streams[channel]
                if info["active"] and info["leave_time"] is None:
                    log.info(f"⏸ LEAVE {channel} — grace period {LEAVE_GRACE}s started")
                    info["leave_time"] = time.time()

    def get_channels_to_poke(self):
        with self.lock:
            now = time.time()
            to_remove = []

            for ch, info in self.streams.items():
                if info["leave_time"] is not None:
                    elapsed = now - info["leave_time"]
                    if elapsed >= LEAVE_GRACE:
                        log.info(f"⏹ STOP  {ch} — leave grace expired")
                        to_remove.append(ch)
                        continue

                since_last = now - info["last_seen"]
                if since_last >= WATCHDOG_TIMEOUT:
                    log.warning(f"⏹ WATCHDOG {ch} — no IGMP report for {int(since_last)}s, assuming dead")
                    to_remove.append(ch)
                    continue

            for ch in to_remove:
                del self.streams[ch]

            return [
                ch for ch, info in self.streams.items()
                if info["active"] and info["leave_time"] is None
            ]

    def get_status(self):
        with self.lock:
            now = time.time()
            active = []
            leaving = []
            for ch, info in self.streams.items():
                if info["leave_time"] is not None:
                    leaving.append(ch)
                elif info["active"]:
                    age = int(now - info["last_seen"])
                    active.append(f"{ch}({age}s)")
            return active, leaving


tracker = StreamTracker()

# ── IGMP Packet Parser ──────────────────────────────────────────

IGMP_V1_REPORT = 0x12
IGMP_V2_REPORT = 0x16
IGMP_V2_LEAVE  = 0x17
IGMP_V3_REPORT = 0x22
IGMP_QUERY     = 0x11

# IP protocol number for IGMP
IPPROTO_IGMP = 2


def parse_ethernet_frame(raw_data):
    """
    Parse raw Ethernet frame, extract IP packet if it contains IGMP.
    Returns list of (action, group_ip) tuples.
    """
    results = []

    if len(raw_data) < 14:
        return results

    # Ethernet header: 6 dst + 6 src + 2 type
    eth_type = struct.unpack('!H', raw_data[12:14])[0]

    # Check for 802.1Q VLAN tag
    offset = 14
    if eth_type == 0x8100:
        # VLAN tagged - skip 4 bytes
        if len(raw_data) < 18:
            return results
        eth_type = struct.unpack('!H', raw_data[16:18])[0]
        offset = 18

    # Only process IPv4
    if eth_type != 0x0800:
        return results

    # Parse IP header
    if len(raw_data) < offset + 20:
        return results

    ip_data = raw_data[offset:]
    
    version_ihl = ip_data[0]
    ip_version = (version_ihl >> 4) & 0xF
    if ip_version != 4:
        return results

    ihl = (version_ihl & 0x0F) * 4
    
    if len(ip_data) < ihl + 4:
        return results

    # Check protocol field = IGMP (2)
    protocol = ip_data[9]
    if protocol != IPPROTO_IGMP:
        return results

    # Extract source IP for logging
    src_ip = socket.inet_ntoa(ip_data[12:16])

    # Parse IGMP payload
    igmp_data = ip_data[ihl:]
    
    if len(igmp_data) < 8:
        return results

    igmp_type = igmp_data[0]

    if igmp_type in (IGMP_V1_REPORT, IGMP_V2_REPORT):
        group = socket.inet_ntoa(igmp_data[4:8])
        log.debug(f"IGMP v2 report from {src_ip} for group {group}")
        results.append(('join', group))

    elif igmp_type == IGMP_V2_LEAVE:
        group = socket.inet_ntoa(igmp_data[4:8])
        log.debug(f"IGMP v2 leave from {src_ip} for group {group}")
        results.append(('leave', group))

    elif igmp_type == IGMP_V3_REPORT:
        if len(igmp_data) < 8:
            return results
        num_records = struct.unpack('!H', igmp_data[6:8])[0]
        rec_offset = 8

        for _ in range(num_records):
            if rec_offset + 8 > len(igmp_data):
                break

            rec_type = igmp_data[rec_offset]
            aux_len = igmp_data[rec_offset + 1]
            num_sources = struct.unpack('!H', igmp_data[rec_offset + 2:rec_offset + 4])[0]
            group = socket.inet_ntoa(igmp_data[rec_offset + 4:rec_offset + 8])

            log.debug(f"IGMP v3 record type={rec_type} from {src_ip} for group {group}")

            if rec_type in (2, 4, 5):
                results.append(('join', group))
            elif rec_type in (1, 3, 6):
                if num_sources == 0 and rec_type in (1, 3):
                    results.append(('leave', group))
                elif rec_type == 6:
                    results.append(('leave', group))
                else:
                    results.append(('join', group))

            rec_offset += 8 + (num_sources * 4) + (aux_len * 4)

    return results


# ── IGMP Sniffer Thread (AF_PACKET) ────────────────────────────
running = True


def igmp_sniffer():
    """
    Listen for IGMP packets using AF_PACKET (Layer 2 raw socket).
    This is the same method tcpdump uses — it sees ALL packets
    on the wire, including IGMP which the kernel otherwise consumes.
    """
    try:
        # ETH_P_IP = 0x0800 — capture all IPv4 packets
        ETH_P_IP = 0x0800
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_IP))
    except PermissionError:
        log.error("Must run as root to capture packets!")
        sys.exit(1)

    # Bind to specific interface
    try:
        sock.bind((INTERFACE, 0))
        log.info(f"Bound to interface: {INTERFACE}")
    except OSError as e:
        log.error(f"Could not bind to {INTERFACE}: {e}")
        sys.exit(1)

    sock.settimeout(2.0)

    for g in channel_groups:
        log.info(f"Watching: {g.name} → {g.first_ip()}-{g.last_ip()} ({g.prefix}1-{g.prefix}{g.count})")

    packet_count = 0
    igmp_count = 0

    while running:
        try:
            raw_data, addr = sock.recvfrom(65535)
            packet_count += 1
            
            events = parse_ethernet_frame(raw_data)

            if events:
                igmp_count += 1

            for action, group_ip in events:
                channel = multicast_ip_to_channel(group_ip)
                if channel is None:
                    log.debug(f"IGMP {action} for {group_ip} — not our channel, ignoring")
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
    log.info(f"IGMP sniffer stopped (processed {packet_count} packets, {igmp_count} IGMP)")


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
    log.info(f"  Interface:      {INTERFACE}")
    log.info(f"  Flussonic:      {FLUSSONIC_BASE}")
    log.info(f"  Socket:         AF_PACKET (Layer 2, like tcpdump)")
    log.info(f"  Groups:         {len(channel_groups)}")
    for g in channel_groups:
        log.info(f"    {g.name}: {g.prefix}1-{g.prefix}{g.count} → {g.first_ip()}-{g.last_ip()}")
    log.info(f"  Total:          {total_channels} channels")
    log.info(f"  Poke every:     {POKE_INTERVAL}s")
    log.info(f"  Leave grace:    {LEAVE_GRACE}s")
    log.info(f"  Watchdog:       {WATCHDOG_TIMEOUT}s (kills stale channels)")
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
