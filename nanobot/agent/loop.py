"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator

from loguru import logger

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider, StreamDelta
from nanobot.agent.context import ContextBuilder
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.filesystem import ReadFileTool, WriteFileTool, EditFileTool, ListDirTool
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.web import WebSearchTool, WebFetchTool
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.agent.subagent import SubagentManager
from nanobot.session.manager import SessionManager

if TYPE_CHECKING:
    from nanobot.config.schema import ExecToolConfig
    from nanobot.sse.context import RequestContext
    from nanobot.sse.emitter import SSEEmitter


class AgentLoop:
    """
    The agent loop is the core processing engine.
    
    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """
    
    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 20,
        brave_api_key: str | None = None,
        exec_config: "ExecToolConfig | None" = None,
    ):
        from nanobot.config.schema import ExecToolConfig
        self.bus = bus
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.brave_api_key = brave_api_key
        self.exec_config = exec_config or ExecToolConfig()
        
        self.context = ContextBuilder(workspace)
        self.sessions = SessionManager(workspace)
        self.tools = ToolRegistry()
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            brave_api_key=brave_api_key,
            exec_config=self.exec_config,
        )
        
        self._running = False
        self._register_default_tools()
    
    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        # File tools
        self.tools.register(ReadFileTool())
        self.tools.register(WriteFileTool())
        self.tools.register(EditFileTool())
        self.tools.register(ListDirTool())
        
        # Shell tool
        self.tools.register(ExecTool(
            working_dir=str(self.workspace),
            timeout=self.exec_config.timeout,
            restrict_to_workspace=self.exec_config.restrict_to_workspace,
        ))
        
        # Web tools
        self.tools.register(WebSearchTool(api_key=self.brave_api_key))
        self.tools.register(WebFetchTool())
        
        # Message tool
        message_tool = MessageTool(send_callback=self.bus.publish_outbound)
        self.tools.register(message_tool)
        
        # Spawn tool (for subagents)
        spawn_tool = SpawnTool(manager=self.subagents)
        self.tools.register(spawn_tool)
    
    async def run(self) -> None:
        """Run the agent loop, processing messages from the bus."""
        self._running = True
        logger.info("Agent loop started")
        
        while self._running:
            try:
                # Wait for next message
                msg = await asyncio.wait_for(
                    self.bus.consume_inbound(),
                    timeout=1.0
                )
                
                # Process it
                try:
                    response = await self._process_message(msg)
                    if response:
                        await self.bus.publish_outbound(response)
                except Exception as e:
                    logger.error(f"Error processing message: {e}")
                    # Send error response
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=f"Sorry, I encountered an error: {str(e)}"
                    ))
            except asyncio.TimeoutError:
                continue
    
    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")
    
    async def _process_message(self, msg: InboundMessage) -> OutboundMessage | None:
        """
        Process a single inbound message.
        
        Args:
            msg: The inbound message to process.
        
        Returns:
            The response message, or None if no response needed.
        """
        # Handle system messages (subagent announces)
        # The chat_id contains the original "channel:chat_id" to route back to
        if msg.channel == "system":
            return await self._process_system_message(msg)
        
        logger.info(f"Processing message from {msg.channel}:{msg.sender_id}")
        
        # Get or create session
        session = self.sessions.get_or_create(msg.session_key)
        
        # Update tool contexts
        message_tool = self.tools.get("message")
        if isinstance(message_tool, MessageTool):
            message_tool.set_context(msg.channel, msg.chat_id)
        
        spawn_tool = self.tools.get("spawn")
        if isinstance(spawn_tool, SpawnTool):
            spawn_tool.set_context(msg.channel, msg.chat_id)
        
        # Build initial messages (use get_history for LLM-formatted messages)
        messages = self.context.build_messages(
            history=session.get_history(),
            current_message=msg.content,
            media=msg.media if msg.media else None,
        )
        
        # Agent loop
        iteration = 0
        final_content = None
        
        while iteration < self.max_iterations:
            iteration += 1
            
            # Call LLM
            response = await self.provider.chat(
                messages=messages,
                tools=self.tools.get_definitions(),
                model=self.model
            )
            
            # Handle tool calls
            if response.has_tool_calls:
                # Add assistant message with tool calls
                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments)  # Must be JSON string
                        }
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts
                )
                
                # Execute tools
                for tool_call in response.tool_calls:
                    args_str = json.dumps(tool_call.arguments)
                    logger.debug(f"Executing tool: {tool_call.name} with arguments: {args_str}")
                    
                    # Send tool execution event to client
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=f"正在执行工具: {tool_call.name}",
                        metadata={
                            "type": "tool",
                            "tool_name": tool_call.name,
                            "arguments": tool_call.arguments
                        }
                    ))
                    
                    result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
            else:
                # No tool calls, we're done
                final_content = response.content
                break
        
        if final_content is None:
            final_content = "I've completed processing but have no response to give."
        
        # Save to session
        session.add_message("user", msg.content)
        session.add_message("assistant", final_content)
        self.sessions.save(session)
        
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content
        )
    
    async def _process_system_message(self, msg: InboundMessage) -> OutboundMessage | None:
        """
        Process a system message (e.g., subagent announce).
        
        The chat_id field contains "original_channel:original_chat_id" to route
        the response back to the correct destination.
        """
        logger.info(f"Processing system message from {msg.sender_id}")
        
        # Parse origin from chat_id (format: "channel:chat_id")
        if ":" in msg.chat_id:
            parts = msg.chat_id.split(":", 1)
            origin_channel = parts[0]
            origin_chat_id = parts[1]
        else:
            # Fallback
            origin_channel = "cli"
            origin_chat_id = msg.chat_id
        
        # Use the origin session for context
        session_key = f"{origin_channel}:{origin_chat_id}"
        session = self.sessions.get_or_create(session_key)
        
        # Update tool contexts
        message_tool = self.tools.get("message")
        if isinstance(message_tool, MessageTool):
            message_tool.set_context(origin_channel, origin_chat_id)
        
        spawn_tool = self.tools.get("spawn")
        if isinstance(spawn_tool, SpawnTool):
            spawn_tool.set_context(origin_channel, origin_chat_id)
        
        # Build messages with the announce content
        messages = self.context.build_messages(
            history=session.get_history(),
            current_message=msg.content
        )
        
        # Agent loop (limited for announce handling)
        iteration = 0
        final_content = None
        
        while iteration < self.max_iterations:
            iteration += 1
            
            response = await self.provider.chat(
                messages=messages,
                tools=self.tools.get_definitions(),
                model=self.model
            )
            
            if response.has_tool_calls:
                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments)
                        }
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts
                )
                
                for tool_call in response.tool_calls:
                    args_str = json.dumps(tool_call.arguments)
                    logger.debug(f"Executing tool: {tool_call.name} with arguments: {args_str}")
                    
                    # Send tool execution event to client
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=origin_channel,
                        chat_id=origin_chat_id,
                        content=f"正在执行工具: {tool_call.name}",
                        metadata={
                            "type": "tool",
                            "tool_name": tool_call.name,
                            "arguments": tool_call.arguments
                        }
                    ))
                    
                    result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
            else:
                final_content = response.content
                break
        
        if final_content is None:
            final_content = "Background task completed."
        
        # Save to session (mark as system message in history)
        session.add_message("user", f"[System: {msg.sender_id}] {msg.content}")
        session.add_message("assistant", final_content)
        self.sessions.save(session)
        
        return OutboundMessage(
            channel=origin_channel,
            chat_id=origin_chat_id,
            content=final_content
        )
    
    async def process_direct(self, content: str, session_key: str = "cli:direct") -> str:
        """
        Process a message directly (for CLI usage).
        
        Args:
            content: The message content.
            session_key: Session identifier.
        
        Returns:
            The agent's response.
        """
        msg = InboundMessage(
            channel="cli",
            sender_id="user",
            chat_id="direct",
            content=content
        )
        
        response = await self._process_message(msg)
        return response.content if response else ""

    # ==================================================================
    # SSE streaming / non-streaming processing
    # ==================================================================

    async def process_sse(
        self,
        ctx: "RequestContext",
        emitter: "SSEEmitter",
        openai_messages: list[dict[str, Any]],
    ) -> AsyncIterator[str]:
        """Process a request and yield SSE event strings.

        This is the unified entry point used by the SSE HTTP handler.
        It supports both streaming and non-streaming modes controlled
        by ``ctx.stream``.

        Args:
            ctx: The request context carrying session_id, request_id, etc.
            emitter: The SSE emitter bound to this request context.
            openai_messages: OpenAI-format message list from the client.

        Yields:
            Ready-to-send SSE ``data: ...\\n\\n`` strings.
        """
        from nanobot.sse.context import RequestContext
        from nanobot.sse.emitter import SSEEmitter

        try:
            # 1. Get or create session
            session = self.sessions.get_or_create(ctx.session_key)

            # 2. Build tool registry with optional filtering
            tools_defs = self._get_filtered_tool_definitions(ctx.tool_list)

            # 3. Build messages (system prompt + history + current)
            current_text, current_media = self._extract_last_user_message(openai_messages)
            messages = self.context.build_messages(
                history=session.get_history(),
                current_message=current_text,
                skill_names=ctx.skill_list if ctx.skill_list != ["*"] else None,
                media=current_media,
            )

            # 4. Agent loop
            if ctx.stream:
                async for event in self._sse_stream_loop(
                    ctx, emitter, messages, tools_defs, session
                ):
                    yield event
            else:
                async for event in self._sse_non_stream_loop(
                    ctx, emitter, messages, tools_defs, session
                ):
                    yield event

            # 5. Done
            yield emitter.emit_done()

        except Exception as e:
            logger.error(f"SSE processing error: {e}")
            yield emitter.emit_error(str(e))
            yield emitter.emit_done()

    # ------------------------------------------------------------------
    # Streaming loop
    # ------------------------------------------------------------------

    async def _sse_stream_loop(
        self,
        ctx: "RequestContext",
        emitter: "SSEEmitter",
        messages: list[dict[str, Any]],
        tools_defs: list[dict[str, Any]] | None,
        session: Any,
    ) -> AsyncIterator[str]:
        """Run the agent loop in *streaming* mode, yielding SSE events."""
        iteration = 0

        while iteration < self.max_iterations:
            iteration += 1

            # -- Generate a new message_id for this LLM turn --
            turn_msg_id = ctx.new_message_id()

            # -- Accumulate tool call fragments --
            pending_tool_calls: dict[int, dict[str, Any]] = {}
            accumulated_content: list[str] = []
            finish_reason: str | None = None

            async for delta in self.provider.stream_chat(
                messages=messages,
                tools=tools_defs,
                model=self.model,
                enable_thinking=ctx.enable_thinking,
            ):
                # Thinking delta
                if delta.thinking_delta and ctx.enable_thinking:
                    yield emitter.emit_thinking_delta(delta.thinking_delta, turn_msg_id)

                # Text content delta
                if delta.content_delta:
                    accumulated_content.append(delta.content_delta)
                    yield emitter.emit_text_delta(delta.content_delta, turn_msg_id)

                # Tool call deltas (accumulate)
                if delta.tool_call_index is not None:
                    idx = delta.tool_call_index
                    if idx not in pending_tool_calls:
                        pending_tool_calls[idx] = {
                            "id": delta.tool_call_id or "",
                            "name": delta.tool_call_name or "",
                            "arguments_parts": [],
                        }
                    tc = pending_tool_calls[idx]
                    if delta.tool_call_id:
                        tc["id"] = delta.tool_call_id
                    if delta.tool_call_name:
                        tc["name"] = delta.tool_call_name
                    if delta.tool_call_arguments_delta:
                        tc["arguments_parts"].append(delta.tool_call_arguments_delta)

                # Finish
                if delta.finish_reason:
                    finish_reason = delta.finish_reason

            # -- End of this LLM turn --
            full_content = "".join(accumulated_content) or None

            # If tool calls present, execute them and continue the loop
            if pending_tool_calls:
                # Build assistant message with tool_calls
                tool_call_dicts = []
                for _idx, tc in sorted(pending_tool_calls.items()):
                    args_str = "".join(tc["arguments_parts"])
                    tool_call_dicts.append({
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": args_str,
                        },
                    })

                messages = self.context.add_assistant_message(
                    messages, full_content, tool_call_dicts
                )

                # Execute each tool
                for tc_dict in tool_call_dicts:
                    tc_id = tc_dict["id"]
                    tc_name = tc_dict["function"]["name"]
                    tc_args_str = tc_dict["function"]["arguments"]

                    try:
                        tc_args = json.loads(tc_args_str) if tc_args_str else {}
                    except json.JSONDecodeError:
                        tc_args = {}

                    # Emit tool call event
                    yield emitter.emit_tool_call(tc_name, tc_args, ctx.new_message_id())

                    # Execute
                    result = await self.tools.execute(tc_name, tc_args)

                    # Emit tool result event
                    yield emitter.emit_tool_result(tc_name, result, ctx.current_message_id)

                    # Add tool result to messages
                    messages = self.context.add_tool_result(
                        messages, tc_id, tc_name, result
                    )

                # Continue loop for next LLM turn
                continue

            # No tool calls — we're done
            if full_content:
                session.add_message("user", self._extract_last_user_message(
                    [m for m in messages if m.get("role") == "user"]
                )[0] or "")
                session.add_message("assistant", full_content)
                self.sessions.save(session)
            break

    # ------------------------------------------------------------------
    # Non-streaming loop
    # ------------------------------------------------------------------

    async def _sse_non_stream_loop(
        self,
        ctx: "RequestContext",
        emitter: "SSEEmitter",
        messages: list[dict[str, Any]],
        tools_defs: list[dict[str, Any]] | None,
        session: Any,
    ) -> AsyncIterator[str]:
        """Run the agent loop in *non-streaming* mode, yielding SSE events."""
        iteration = 0
        final_content: str | None = None

        while iteration < self.max_iterations:
            iteration += 1

            response = await self.provider.chat(
                messages=messages,
                tools=tools_defs,
                model=self.model,
            )

            turn_msg_id = ctx.new_message_id()

            # Thinking (if available in non-stream response)
            if response.thinking and ctx.enable_thinking:
                yield emitter.emit_thinking_complete(response.thinking, turn_msg_id)

            if response.has_tool_calls:
                # Build assistant message
                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts
                )

                # Execute each tool
                for tool_call in response.tool_calls:
                    tool_msg_id = ctx.new_message_id()
                    yield emitter.emit_tool_call(
                        tool_call.name, tool_call.arguments, tool_msg_id
                    )

                    result = await self.tools.execute(tool_call.name, tool_call.arguments)

                    yield emitter.emit_tool_result(
                        tool_call.name, result, tool_msg_id
                    )

                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
                continue

            # No tool calls — final answer
            final_content = response.content
            break

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        # Emit the complete text in a single event
        yield emitter.emit_text_complete(final_content, ctx.new_message_id())

        # Save to session
        user_text = self._extract_last_user_message(
            [m for m in messages if m.get("role") == "user"]
        )[0] or ""
        session.add_message("user", user_text)
        session.add_message("assistant", final_content)
        self.sessions.save(session)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_filtered_tool_definitions(
        self, tool_list: list[str]
    ) -> list[dict[str, Any]] | None:
        """Return tool definitions, optionally filtered by *tool_list*."""
        all_defs = self.tools.get_definitions()
        if not tool_list or tool_list == ["*"]:
            return all_defs if all_defs else None

        allowed = set(tool_list)
        filtered = [d for d in all_defs if d.get("function", {}).get("name") in allowed]
        return filtered if filtered else None

    @staticmethod
    def _extract_last_user_message(
        openai_messages: list[dict[str, Any]],
    ) -> tuple[str, list[str] | None]:
        """Extract text and media from the last user message.

        Returns:
            ``(text, media_list | None)``
        """
        # Walk backwards to find last user message
        for msg in reversed(openai_messages):
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if isinstance(content, str):
                return content, None
            # Multimodal: list of content parts
            if isinstance(content, list):
                texts: list[str] = []
                media: list[str] = []
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "text":
                            texts.append(part.get("text", ""))
                        elif part.get("type") == "image_url":
                            url = part.get("image_url", {}).get("url", "")
                            if url:
                                media.append(url)
                return " ".join(texts), media if media else None
        return "", None
