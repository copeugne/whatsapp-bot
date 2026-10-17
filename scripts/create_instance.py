"""Create and connect a WhatsApp instance via Evolution API.

Usage:
    uv run python scripts/create_instance.py
"""

import asyncio
import sys
import time

from whatsapp_bot.config import settings
from whatsapp_bot.evolution import EvolutionClient, EvolutionAPIError

POLL_INTERVAL = 5  # seconds
MAX_POLL_ATTEMPTS = 60  # 5 minutes total


async def main() -> None:
    client = EvolutionClient(
        base_url=settings.evolution_api_url,
        api_key=settings.evolution_api_key,
    )
    await client.start()

    instance_name = settings.instance_name
    print(f"Instance name: {instance_name}")
    print(f"Evolution API: {settings.evolution_api_url}")
    print()

    # Step 1: Create instance
    print("Creating instance...")
    try:
        result = await client.create_instance(instance_name)
        print(f"Instance created: {result}")
    except EvolutionAPIError as e:
        if e.status_code == 403:
            print(f"Instance may already exist (403). Proceeding to connect...")
        else:
            print(f"Error creating instance: {e}", file=sys.stderr)
            await client.close()
            sys.exit(1)

    print()

    # Step 2: Connect instance (get QR code)
    print("Connecting instance...")
    try:
        connect_result = await client.connect_instance(instance_name)
        print(f"Connect result: {connect_result}")

        # Extract QR code info
        base64_qr = connect_result.get("base64")
        pairing_code = connect_result.get("pairingCode")
        code = connect_result.get("code")

        if pairing_code:
            print(f"\n{'='*50}")
            print(f"  PAIRING CODE: {pairing_code}")
            print(f"{'='*50}")
            print("Enter this code on your phone:")
            print("  WhatsApp > Linked Devices > Link a Device > Link with phone number")

        if code:
            print(f"\nQR Code content: {code}")

        if base64_qr:
            print(f"\nQR code base64 (first 100 chars): {base64_qr[:100]}...")
            print("Scan the QR code in your WhatsApp app:")
            print("  WhatsApp > Linked Devices > Link a Device")
    except EvolutionAPIError as e:
        print(f"Error connecting: {e}", file=sys.stderr)
        await client.close()
        sys.exit(1)

    print()

    # Step 3: Poll connection state
    print("Waiting for connection...")
    for attempt in range(1, MAX_POLL_ATTEMPTS + 1):
        try:
            state_result = await client.connection_state(instance_name)
            state = state_result.get("instance", {}).get("state", "unknown")
            print(f"  [{attempt}] State: {state}")

            if state == "open":
                print(f"\nConnected successfully!")
                await client.close()
                return
        except EvolutionAPIError as e:
            print(f"  [{attempt}] Error polling state: {e}")

        time.sleep(POLL_INTERVAL)

    print(
        f"\nTimed out waiting for connection after {MAX_POLL_ATTEMPTS * POLL_INTERVAL}s",
        file=sys.stderr,
    )
    await client.close()
    sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
