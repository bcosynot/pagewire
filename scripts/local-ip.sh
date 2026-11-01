#!/bin/sh
# Print this host's LAN address, the one the device must be able to reach.
#
# The device joins the home network for its sleep-entry drain, so a 0.0.0.0 bind is not
# something it can use: it needs a real address on the same network. Order of attempts
# below is macOS first, then Linux, then anything with ifconfig, then give up loudly.
#
# Exits 1 and prints nothing when no address can be determined, so callers can tell the
# difference between "no address" and "0.0.0.0".

ip=""

# macOS
if command -v ipconfig >/dev/null 2>&1; then
    iface=$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')
    if [ -n "$iface" ]; then
        ip=$(ipconfig getifaddr "$iface" 2>/dev/null)
    fi
fi

# Linux (iproute2)
if [ -z "$ip" ] && command -v ip >/dev/null 2>&1; then
    ip=$(ip route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')
fi

# Linux (hostname)
if [ -z "$ip" ]; then
    ip=$(hostname -I 2>/dev/null | awk '{print $1}')
fi

# anything with ifconfig (macOS and BSD read "inet 192.0.2.1 netmask ...")
if [ -z "$ip" ]; then
    ip=$(ifconfig 2>/dev/null | awk '/inet /{print $2}' | grep -v '^127\.' | head -1)
fi

# Reject loopback: the device cannot reach it, and it usually means the Wi-Fi is down.
case "$ip" in
    127.*|"") exit 1 ;;
esac

printf '%s\n' "$ip"
