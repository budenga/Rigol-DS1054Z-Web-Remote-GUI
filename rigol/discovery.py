"""LXI/VXI-11 device discovery for Rigol oscilloscopes.

Two methods:
1. TCP probe — try to connect to port 5555 on common subnet IPs and send *IDN?
2. mDNS/DNS-SD — look for _scpi-raw._tcp.local. services (requires zeroconf)

The TCP probe is more reliable and doesn't need extra packages.
"""

import asyncio
import ipaddress
import logging
import socket
import struct

log = logging.getLogger(__name__)

SCPI_PORT = 5555
PROBE_TIMEOUT = 0.8  # seconds per host
IDN_PATTERN = "RIGOL"


async def probe_host(ip: str, port: int = SCPI_PORT, timeout: float = PROBE_TIMEOUT) -> dict | None:
    """Try to connect to a host and query *IDN?. Returns info dict or None."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port),
            timeout=timeout,
        )
        writer.write(b"*IDN?\n")
        await writer.drain()
        data = await asyncio.wait_for(reader.readline(), timeout=timeout)
        writer.close()
        await writer.wait_closed()

        idn = data.decode().strip()
        if idn:
            parts = idn.split(",")
            return {
                "ip": ip,
                "port": port,
                "idn": idn,
                "vendor": parts[0].strip() if len(parts) > 0 else "",
                "model": parts[1].strip() if len(parts) > 1 else "",
                "serial": parts[2].strip() if len(parts) > 2 else "",
                "firmware": parts[3].strip() if len(parts) > 3 else "",
            }
    except (OSError, asyncio.TimeoutError, UnicodeDecodeError):
        pass
    return None


def _get_local_subnets() -> list[str]:
    """Get local network prefixes (e.g. '192.168.1') from all interfaces."""
    import netifaces
    prefixes = set()
    for iface in netifaces.interfaces():
        addrs = netifaces.ifaddresses(iface)
        for info in addrs.get(netifaces.AF_INET, []):
            ip = info.get("addr", "")
            mask = info.get("netmask", "")
            if ip.startswith("127.") or ip.startswith("169.254."):
                continue
            try:
                net = ipaddress.IPv4Network(f"{ip}/{mask}", strict=False)
                if net.prefixlen >= 24:
                    # /24 or smaller — scan it
                    prefixes.add(str(net.network_address.packed[:3].hex()))
                    prefix = ".".join(ip.split(".")[:3])
                    prefixes.add(prefix)
            except ValueError:
                pass
    return list(prefixes)


def _get_local_prefixes_simple() -> list[str]:
    """Get local /24 prefixes without netifaces (fallback)."""
    prefixes = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("127.") or ip.startswith("169.254."):
                continue
            prefix = ".".join(ip.split(".")[:3])
            prefixes.add(prefix)
    except socket.gaierror:
        pass

    # Also try getting the default route IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        prefix = ".".join(ip.split(".")[:3])
        prefixes.add(prefix)
    except OSError:
        pass

    if not prefixes:
        prefixes.add("192.168.1")

    return list(prefixes)


def get_local_prefixes() -> list[str]:
    """Get local /24 prefixes, trying netifaces first."""
    try:
        return _get_local_subnets()
    except ImportError:
        return _get_local_prefixes_simple()


async def discover(
    subnets: list[str] | None = None,
    port: int = SCPI_PORT,
    timeout: float = PROBE_TIMEOUT,
    max_concurrent: int = 50,
    progress_callback=None,
) -> list[dict]:
    """Scan local subnets for Rigol scopes on the SCPI port.

    Args:
        subnets: list of /24 prefixes to scan (e.g. ["192.168.1"]).
                 If None, auto-detect from local interfaces.
        port: SCPI port (default 5555)
        timeout: per-host timeout in seconds
        max_concurrent: max simultaneous probes
        progress_callback: called with (scanned, total) counts

    Returns:
        List of dicts with ip, model, serial, etc.
    """
    if subnets is None:
        subnets = get_local_prefixes()

    # Build list of IPs to probe
    ips = []
    for prefix in subnets:
        for i in range(1, 255):
            ips.append(f"{prefix}.{i}")

    results = []
    sem = asyncio.Semaphore(max_concurrent)
    scanned = 0
    total = len(ips)

    async def _probe(ip):
        nonlocal scanned
        async with sem:
            info = await probe_host(ip, port, timeout)
            scanned += 1
            if progress_callback and scanned % 20 == 0:
                progress_callback(scanned, total)
            if info:
                results.append(info)

    tasks = [asyncio.create_task(_probe(ip)) for ip in ips]
    await asyncio.gather(*tasks)

    # Sort by IP
    results.sort(key=lambda r: tuple(int(x) for x in r["ip"].split(".")))
    return results


async def discover_quick(
    port: int = SCPI_PORT,
    timeout: float = PROBE_TIMEOUT,
) -> list[dict]:
    """Quick discovery — scan local subnets with progress output."""
    prefixes = get_local_prefixes()
    print(f"  Scanning subnets: {', '.join(p + '.0/24' for p in prefixes)}")
    print(f"  Port: {port}, timeout: {timeout}s per host")
    print()

    def _progress(done, total):
        pct = done * 100 // total
        print(f"\r  Scanning... {done}/{total} ({pct}%)", end="", flush=True)

    results = await discover(
        subnets=prefixes,
        port=port,
        timeout=timeout,
        progress_callback=_progress,
    )

    print(f"\r  Scanning... done.{' ' * 20}")
    return results


async def find_first_scope(port: int = SCPI_PORT, timeout: float = PROBE_TIMEOUT) -> dict | None:
    """Find and return the first Rigol scope on the network, or None."""
    results = await discover_quick(port=port, timeout=timeout)
    for r in results:
        if IDN_PATTERN in r.get("vendor", "").upper():
            return r
    # Return first result even if not Rigol
    return results[0] if results else None
