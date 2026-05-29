import asyncio
import json
import websockets
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate
from aiortc.sdp import candidate_from_sdp
from ws_state import connected_browser_ws
import ws_state

pcs = set()


async def handler(ws):
    ws_state.connected_browser_ws = ws
    print("🌐 Browser connected & registered")

    pc = RTCPeerConnection()
    pc.addTransceiver("audio", direction="sendrecv")
    pcs.add(pc)

    @pc.on("connectionstatechange")
    async def on_state():
        print("📡 PC state:", pc.connectionState)

    try:
        async for message in ws:
            msg = json.loads(message)

            if msg["type"] == "offer":
                print("📨 Received SDP offer from browser")

                offer = RTCSessionDescription(
                    sdp=msg["sdp"],
                    type="offer"
                )
                await pc.setRemoteDescription(offer)

                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)

                await ws.send(json.dumps({
                    "type": "answer",
                    "sdp": pc.localDescription.sdp
                }))

                print("📤 Sent SDP answer to browser")

            elif msg["type"] == "ice":
                if pc.iceConnectionState == "closed":
                    continue

                cand = msg.get("candidate")
                if not cand:
                    continue

                try:
                    ice = candidate_from_sdp(cand["candidate"])
                    ice.sdpMid = cand.get("sdpMid")
                    ice.sdpMLineIndex = cand.get("sdpMLineIndex")

                    await pc.addIceCandidate(ice)
                except Exception as e:
                    print("⚠️ ICE ignored:", e)

    except websockets.ConnectionClosed:
        print("🔌 Browser disconnected")

    finally:
        
        ws_state.connected_browser_ws = None
        pcs.discard(pc)
        await pc.close()


async def main():
    async with websockets.serve(handler, "0.0.0.0", 9000):
        print("WebSocket server running")
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())

