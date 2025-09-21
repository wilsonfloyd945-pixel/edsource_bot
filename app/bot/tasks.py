import asyncio

def fire_and_forget(coro):
    try:
        asyncio.create_task(coro)
    except RuntimeError:
        asyncio.get_event_loop().create_task(coro)
