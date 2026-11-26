# /// script
# dependencies = ["sonos-websocket", "aiohttp"]
# ///
"""Play an audio URL on a Sonos speaker as an announcement clip (ducks, then resumes what was playing).

    uv run sonos_fallback.py <sonos-ip> <clip-url> <volume>

runner.py calls this when the main speaker doesn't answer.
"""

import asyncio
import sys

import aiohttp
from sonos_websocket import SonosWebsocket


async def main(ip, url, volume):
    async with aiohttp.ClientSession() as session:
        ws = SonosWebsocket(ip, session=session)
        print(await ws.play_clip(url, volume=int(volume)))
        await ws.close()


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    asyncio.run(main(*sys.argv[1:4]))
