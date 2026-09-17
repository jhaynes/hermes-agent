"""Disposable serving writers for readiness transport tests (no live services)."""
import asyncio
import json
import socket
import sys
from pathlib import Path

root, home, ready, stop = map(Path, sys.argv[1:])
sys.path.insert(0, str(root))


async def serve():
    import uvicorn
    from fastapi import FastAPI
    from gateway.control_socket import GatewayControlServer
    from plugins.kanban.dashboard.plugin_api import router
    from hermes_cli import kanban_review_state
    assert Path(kanban_review_state.__file__).resolve().is_relative_to(root.resolve())
    app = FastAPI()
    app.include_router(router)
    sock = socket.socket()
    sock.bind(('127.0.0.1',0))
    control = GatewayControlServer(home=home)
    assert await control.start()
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off'))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            if task.done():
                await task
                raise RuntimeError('dashboard did not start')
            await asyncio.sleep(.01)
        ready.write_text(json.dumps({'port':sock.getsockname()[1]}))
        while not stop.exists():
            await asyncio.sleep(.02)
    finally:
        server.should_exit = True
        await task
        await control.stop()
        sock.close()


asyncio.run(serve())
