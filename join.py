"""
One-command setup to join the network as a worker node.

Usage:
    python join.py http://ORCHESTRATOR_IP:8000

Does everything:
  1. Checks if Ollama is installed and running
  2. Pulls the model if needed
  3. Registers with the orchestrator
  4. Starts polling for tasks
"""

import asyncio
import subprocess
import sys

from ollama_client import check_ollama, DEFAULT_MODEL


async def ensure_ollama():
    """Make sure Ollama is running and has the right model."""
    status = await check_ollama()
    if not status["ok"]:
        print("Ollama is not running.")
        print("Start it with: ollama serve")
        print("Download from: https://ollama.com")
        return False

    # Check if model is available
    if not any(DEFAULT_MODEL in m for m in status["models"]):
        print(f"Model {DEFAULT_MODEL} not found. Pulling it now...")
        print(f"This downloads ~2-3GB. Please wait.\n")
        result = subprocess.run(
            ["ollama", "pull", DEFAULT_MODEL],
            capture_output=False,
        )
        if result.returncode != 0:
            print(f"Failed to pull {DEFAULT_MODEL}.")
            return False
        print(f"\n{DEFAULT_MODEL} ready.\n")

    return True


async def main():
    if len(sys.argv) < 2:
        print("Usage: python join.py http://ORCHESTRATOR_IP:8000")
        print()
        print("Example:")
        print("  python join.py http://192.168.1.50:8000")
        sys.exit(1)

    server = sys.argv[1].rstrip("/")
    print(f"Joining network at: {server}\n")

    # Step 1: Ensure Ollama is ready
    if not await ensure_ollama():
        sys.exit(1)

    print("Ollama is ready.\n")

    # Step 2: Import and run node
    from node import register, poll_and_execute
    import platform

    node_id = platform.node()
    print(f"Node ID: {node_id}")
    print(f"Model: {DEFAULT_MODEL}\n")

    # Register
    try:
        reg = await register(server, node_id)
        print(f"Connected! {reg.get('message', '')}\n")
    except Exception as e:
        print(f"Could not connect to orchestrator at {server}")
        print(f"Error: {e}")
        print("\nMake sure the orchestrator is running:")
        print("  python -m uvicorn server:app --host 0.0.0.0 --port 8000")
        sys.exit(1)

    # Poll for tasks
    print("Waiting for tasks... (Ctrl+C to stop)\n")
    while True:
        try:
            task_id = await poll_and_execute(server, node_id)
            if task_id is None:
                await asyncio.sleep(3)
        except KeyboardInterrupt:
            print("\nLeft the network. Thanks for contributing!")
            break
        except Exception as e:
            print(f"Error: {e}. Retrying...")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
