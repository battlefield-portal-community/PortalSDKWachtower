import asyncio

from app.watchtower import main


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nExiting...")


if __name__ == "__main__":
    run()
