#!/usr/bin/env python3
"""Tiny TCP proxy that adds a fixed delay per forwarded chunk upstream and caps downstream bandwidth.

Used by the launch rig to emulate a slow, high-latency link in front of the local WebDAV server (this container has
no `tc netem`). The conditions are EMULATED: they are not a Wi-Fi, NAS or SD-card measurement.
Usage: shaping_proxy.py LISTEN_PORT UPSTREAM_PORT DELAY_MS RATE_KBYTES_PER_S [ignored tag arguments...]
"""
import asyncio
import sys

LISTEN, UPSTREAM, DELAY_MS, RATE = int(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])


async def pump(reader, writer, delay, rate):
    try:
        while True:
            data = await reader.read(16384)
            if not data:
                break
            if delay:
                await asyncio.sleep(delay)
            writer.write(data)
            await writer.drain()
            if rate:
                await asyncio.sleep(len(data) / (rate * 1024))
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()


async def handle(client_r, client_w):
    try:
        up_r, up_w = await asyncio.open_connection("127.0.0.1", UPSTREAM)
    except OSError:
        client_w.close()
        return
    await asyncio.gather(pump(client_r, up_w, DELAY_MS / 1000, 0), pump(up_r, client_w, 0, RATE))


async def main():
    server = await asyncio.start_server(handle, "127.0.0.1", LISTEN)
    async with server:
        await server.serve_forever()

asyncio.run(main())
