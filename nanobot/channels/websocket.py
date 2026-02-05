"""WebSocket channel implementation for real-time communication."""

import asyncio
import json
from pathlib import Path
from typing import Any

from loguru import logger
import websockets
from websockets.server import WebSocketServerProtocol

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import WebSocketConfig


class WebSocketChannel(BaseChannel):
    """
    WebSocket channel for real-time bidirectional communication.
    
    Supports:
    - Text messages
    - Multimodal content (images, audio)
    - Real-time streaming responses
    - Multiple concurrent clients
    """
    
    name = "websocket"
    
    def __init__(self, config: WebSocketConfig, bus: MessageBus, groq_api_key: str = ""):
        super().__init__(config, bus)
        self.config: WebSocketConfig = config
        self.groq_api_key = groq_api_key
        self._server = None
        self._clients: dict[str, WebSocketServerProtocol] = {}  # client_id -> websocket
        self._client_chat_ids: dict[str, str] = {}  # client_id -> chat_id
    
    async def start(self) -> None:
        """Start the WebSocket server."""
        self._running = True
        
        logger.info(f"Starting WebSocket server on {self.config.host}:{self.config.port}")
        
        try:
            self._server = await websockets.serve(
                self._handle_connection,
                self.config.host,
                self.config.port,
                ping_interval=30,
                ping_timeout=10,
                max_size=20 * 1024 * 1024,  # 20MB max message size (default is 1MB)
                compression=None  # Disable compression for large messages
            )
            
            logger.info(f"WebSocket server listening on ws://{self.config.host}:{self.config.port}")
            
            # Keep running until stopped
            while self._running:
                await asyncio.sleep(1)
                
        except Exception as e:
            logger.error(f"WebSocket server error: {e}")
            raise
    
    async def stop(self) -> None:
        """Stop the WebSocket server."""
        self._running = False
        
        if self._server:
            logger.info("Stopping WebSocket server...")
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        
        # Close all client connections
        for client_id, ws in list(self._clients.items()):
            try:
                await ws.close()
            except Exception:
                pass
        
        self._clients.clear()
        self._client_chat_ids.clear()
    
    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through WebSocket to a specific client."""
        # Find the client by chat_id
        client_id = None
        for cid, chat_id in self._client_chat_ids.items():
            if chat_id == msg.chat_id:
                client_id = cid
                break
        
        if not client_id or client_id not in self._clients:
            logger.warning(f"Client not found for chat_id: {msg.chat_id}")
            return
        
        ws = self._clients[client_id]
        
        try:
            # Send message to client
            response = {
                "type": "message",
                "content": msg.content,
                "chat_id": msg.chat_id,
                "timestamp": asyncio.get_event_loop().time()
            }
            
            await ws.send(json.dumps(response, ensure_ascii=False))
            logger.debug(f"Sent message to client {client_id}: {msg.content[:50]}...")
            
        except Exception as e:
            logger.error(f"Error sending message to client {client_id}: {e}")
            # Remove disconnected client
            self._clients.pop(client_id, None)
            self._client_chat_ids.pop(client_id, None)
    
    async def _handle_connection(self, websocket: WebSocketServerProtocol) -> None:
        """Handle a new WebSocket connection."""
        client_addr = websocket.remote_address
        logger.info(f"New WebSocket connection from {client_addr}")
        
        client_id = None
        
        try:
            # Send welcome message
            await websocket.send(json.dumps({
                "type": "status",
                "status": "connected",
                "message": "Welcome to nanobot WebSocket gateway"
            }))
            
            # Wait for authentication
            auth_msg = await asyncio.wait_for(websocket.recv(), timeout=30.0)
            auth_data = json.loads(auth_msg)
            
            if auth_data.get("type") != "auth":
                await websocket.send(json.dumps({
                    "type": "error",
                    "error": "Authentication required"
                }))
                return
            
            client_id = auth_data.get("client_id", f"ws-{id(websocket)}")
            
            # Check allowlist
            if not self.is_allowed(client_id):
                logger.warning(f"Client {client_id} not in allowlist")
                await websocket.send(json.dumps({
                    "type": "auth",
                    "status": "failed",
                    "error": "Not authorized"
                }))
                return
            
            # Register client
            self._clients[client_id] = websocket
            self._client_chat_ids[client_id] = client_id  # Use client_id as chat_id
            
            logger.info(f"Client authenticated: {client_id}")
            
            # Send auth success
            await websocket.send(json.dumps({
                "type": "auth",
                "status": "success",
                "client_id": client_id
            }))
            
            # Handle messages from this client
            async for message in websocket:
                await self._handle_message_from_client(client_id, message)
        
        except asyncio.TimeoutError:
            logger.warning(f"Authentication timeout for {client_addr}")
            try:
                await websocket.send(json.dumps({
                    "type": "error",
                    "error": "Authentication timeout"
                }))
            except Exception:
                pass
        
        except websockets.exceptions.ConnectionClosed:
            logger.info(f"Client {client_id or client_addr} disconnected")
        
        except Exception as e:
            logger.error(f"Error handling WebSocket connection: {e}")
        
        finally:
            # Clean up
            if client_id:
                self._clients.pop(client_id, None)
                self._client_chat_ids.pop(client_id, None)
                logger.info(f"Client {client_id} cleaned up")
    
    async def _handle_message_from_client(self, client_id: str, message: str) -> None:
        """Handle a message received from a WebSocket client."""
        try:
            data = json.loads(message)
            msg_type = data.get("type")
            
            if msg_type == "message":
                # User message
                content = data.get("content", "")
                media = data.get("media", [])
                
                logger.debug(f"Received message from {client_id}: content_len={len(content)}, media_count={len(media)}")
                
                if not content and not media:
                    return
                
                # Process media files if present
                processed_media = []
                content_parts = [content] if content else []
                
                for media_item in media:
                    if isinstance(media_item, dict):
                        media_type = media_item.get("type", "file")
                        media_url = media_item.get("url", "")
                        media_data = media_item.get("data", "")  # Base64 encoded data
                        
                        if media_data:
                            # Save base64 data to file
                            file_path = await self._save_media(client_id, media_type, media_data)
                            if file_path:
                                processed_media.append(str(file_path))
                                
                                # Handle voice/audio transcription
                                if media_type in ["voice", "audio"]:
                                    transcription = await self._transcribe_audio(file_path)
                                    if transcription:
                                        content_parts.append(f"[transcription: {transcription}]")
                                    else:
                                        content_parts.append(f"[{media_type}: {file_path}]")
                                else:
                                    content_parts.append(f"[{media_type}: {file_path}]")
                        
                        elif media_url:
                            processed_media.append(media_url)
                            content_parts.append(f"[{media_type}: {media_url}]")
                
                final_content = "\n".join(content_parts) if content_parts else "[empty message]"
                
                logger.debug(f"Message from {client_id}: {final_content[:50]}...")
                
                # Forward to message bus
                await self._handle_message(
                    sender_id=client_id,
                    chat_id=client_id,
                    content=final_content,
                    media=processed_media,
                    metadata={
                        "client_id": client_id,
                        "channel": "websocket"
                    }
                )
            
            elif msg_type == "ping":
                # Heartbeat
                ws = self._clients.get(client_id)
                if ws:
                    await ws.send(json.dumps({"type": "pong"}))
            
            else:
                logger.debug(f"Unknown message type from {client_id}: {msg_type}")
        
        except json.JSONDecodeError:
            logger.error(f"Invalid JSON from client {client_id}")
        except Exception as e:
            logger.error(f"Error handling message from {client_id}: {e}")
    
    async def _save_media(self, client_id: str, media_type: str, data: str) -> Path | None:
        """Save base64 encoded media to file."""
        try:
            import base64
            from datetime import datetime
            
            # Decode base64
            media_bytes = base64.b64decode(data)
            
            # Determine extension
            ext_map = {
                "image": ".jpg",
                "voice": ".ogg",
                "audio": ".mp3",
                "video": ".mp4",
                "file": ""
            }
            ext = ext_map.get(media_type, "")
            
            # Save to media directory
            media_dir = Path.home() / ".nanobot" / "media"
            media_dir.mkdir(parents=True, exist_ok=True)
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            file_path = media_dir / f"{client_id}_{timestamp}{ext}"
            
            file_path.write_bytes(media_bytes)
            logger.debug(f"Saved media to {file_path}")
            
            return file_path
        
        except Exception as e:
            logger.error(f"Error saving media: {e}")
            return None
    
    async def _transcribe_audio(self, file_path: Path) -> str | None:
        """Transcribe audio file using Groq."""
        if not self.groq_api_key:
            return None
        
        try:
            from nanobot.providers.transcription import GroqTranscriptionProvider
            transcriber = GroqTranscriptionProvider(api_key=self.groq_api_key)
            transcription = await transcriber.transcribe(file_path)
            
            if transcription:
                logger.info(f"Transcribed audio: {transcription[:50]}...")
            
            return transcription
        
        except Exception as e:
            logger.error(f"Error transcribing audio: {e}")
            return None
