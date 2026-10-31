"""Socket options for provider WebSocket connections.

Some networks (NAT64/DNS64 hotspots) resolve IPv6 addresses first while the
IPv6 route is dead. asyncio tries addresses one at a time, so a Sarvam
WebSocket waited out its whole open timeout before trying IPv4 and the call
fell back to REST (measured 2026-10-09: IPv6 TCP timed out, IPv4 opened in
390 ms). Happy Eyeballs (RFC 8305) starts the next address family after a
short delay, as browsers do. Passed through to loop.create_connection().
"""
HAPPY_EYEBALLS = {'happy_eyeballs_delay': 0.25, 'interleave': 1}
