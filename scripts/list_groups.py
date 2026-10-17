"""List all WhatsApp groups and their JIDs.

Usage:
    uv run python scripts/list_groups.py
"""

import asyncio

from whatsapp_bot.config import settings
from whatsapp_bot.evolution import EvolutionClient


async def main() -> None:
    client = EvolutionClient(
        base_url=settings.evolution_api_url,
        api_key=settings.evolution_api_key,
    )
    await client.start()
    try:
        groups = await client.fetch_all_groups(settings.instance_name)
    finally:
        await client.close()

    if not groups:
        print("No groups found.")
        return

    print(f"Found {len(groups)} group(s):\n")
    for g in groups:
        jid = g.get("id", "?")
        subject = g.get("subject", "(no subject)")
        size = g.get("size", "?")
        print(f"  JID:     {jid}")
        print(f"  Name:    {subject}")
        print(f"  Members: {size}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
