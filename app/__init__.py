import sys

if sys.platform == "win32":  # psycopg async cannot run on the Proactor loop (dev machines only; prod is Linux)
    import asyncio

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
