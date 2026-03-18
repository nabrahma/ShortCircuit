import asyncio
import uvicorn
import logging
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from dashboard_bridge import get_dashboard_bridge

# Silence uvicorn/fastapi noise
logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

app = FastAPI(title="ShortCircuit Jarvis HUD V1")
bridge = get_dashboard_bridge()

# --- Connection Manager ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception:
                pass

manager = ConnectionManager()

# --- Endpoints ---
@app.get("/")
async def get():
    return HTMLResponse(content=open("dashboard/index.html", "r").read())

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # Keep connection alive
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# --- Background Broadcaster ---
async def broadcast_worker():
    """Reads from bridge queue and pushes to all connected WebSockets."""
    logger = logging.getLogger("DashboardServer")
    logger.info("📡 Dashboard Broadcaster Started (Slot 8555)")
    
    while True:
        try:
            msg = await bridge.get_next_message()
            import json
            await manager.broadcast(json.dumps(msg))
        except Exception as e:
            logger.error(f"Broadcaster Error: {e}")
            await asyncio.sleep(1)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(broadcast_worker())

def start_dashboard_server(host: str = "127.0.0.1", port: int = 8555):
    """Entry point to run server in a separate thread/process."""
    uvicorn.run(app, host=host, port=port, log_level="warning")

if __name__ == "__main__":
    # For standalone testing
    start_dashboard_server()
