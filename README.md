# IGMP Watcher for Flussonic On-Demand + UDP

## The Problem
Flussonic on-demand streams only activate when an HTTP client connects.
But MAG boxes play via UDP multicast, and Flussonic doesn't see UDP viewers.
So on-demand + UDP push = streams never wake up.

## The Solution
This script sits on the Flussonic server and sniffs IGMP join/leave packets
on the LAN. When a MAG tunes to a multicast group (e.g. 239.0.0.1), the script
detects the IGMP join and pokes Flussonic's HTTP endpoint to wake the stream.
It keeps poking every 20 seconds to prevent the on-demand timeout from killing
the stream. When the last MAG leaves the group, poking stops.

## Flow
```
MAG tunes to channel    →  IGMP join on LAN
Script detects join     →  HTTP poke to Flussonic (localhost)
Flussonic wakes stream  →  Pulls SRT source, starts UDP push
MAG receives UDP        →  Viewer watches TV

MAG changes channel     →  IGMP leave on LAN  
Script detects leave    →  Stops poking after 5s grace
Flussonic on_demand 30  →  Stream dies 30s after last poke
```

## Requirements
- Python 3.6+ (no pip packages needed)
- Root access (raw sockets for IGMP)
- Managed switch with IGMP snooping enabled
- Flussonic streams configured with `on_demand 30` and `push udp://`

## Install
```bash
cd /path/to/igmp-watcher
sudo bash install.sh
```

## Configure
```bash
sudo nano /opt/igmp-watcher/config.ini
```

Key settings:
- `interface` — the LAN-facing network interface on the Flussonic box
- `flussonic.url` — usually http://localhost:8080
- `channels.count` — number of channels (120)

## Control
```bash
sudo systemctl start igmp-watcher     # start
sudo systemctl stop igmp-watcher      # stop
sudo systemctl restart igmp-watcher   # restart
sudo systemctl enable igmp-watcher    # auto-start on boot
sudo journalctl -u igmp-watcher -f    # live logs
```

## Troubleshooting

**No IGMP joins detected:**
- Verify switch has IGMP snooping enabled
- Check Flussonic server's port is seen as a router port by the switch
- Run: `tcpdump -i eth0 igmp` to see if IGMP packets arrive

**Stream wakes but dies quickly:**
- Increase `on_demand` in Flussonic to 30+ seconds
- Verify `poke_interval` (20s) is less than `on_demand` timeout

**Stream doesn't wake:**
- Check Flussonic URL is reachable: `curl http://localhost:8080/TV1/mpegts | head -c 100`
- Check credentials in config.ini
