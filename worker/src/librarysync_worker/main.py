import asyncio


async def main() -> None:
    print("librarysync worker stub running")
    while True:
        await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())
