# -*- coding: utf-8 -*-
"""OSS upload tools: file upload and text upload."""

import os
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool
from nanobot.utils.oss_service import OSSService


class OSSUploadFileTool(Tool):
    """Tool to upload a local file to Alibaba Cloud OSS."""
    
    def __init__(self, oss_service: OSSService):
        self._oss_service = oss_service
    
    @property
    def name(self) -> str:
        return "oss_upload_file"
    
    @property
    def description(self) -> str:
        return (
            "Upload a local file to Alibaba Cloud OSS. "
            "The object_key must follow the format: session_id/request_id/message_id/filename. "
            "Returns the public URL of the uploaded file."
        )
    
    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "The local file path to upload"
                },
                "session_id": {
                    "type": "string",
                    "description": "Session identifier"
                },
                "request_id": {
                    "type": "string",
                    "description": "Request identifier"
                },
                "message_id": {
                    "type": "string",
                    "description": "Message identifier"
                },
                "filename": {
                    "type": "string",
                    "description": "Target filename in OSS (e.g. 'report.pdf'). If not provided, uses the original filename."
                }
            },
            "required": ["file_path", "session_id", "request_id", "message_id"]
        }
    
    async def execute(
        self,
        file_path: str,
        session_id: str,
        request_id: str,
        message_id: str,
        filename: str | None = None,
        **kwargs: Any,
    ) -> str:
        if not self._oss_service.is_enabled():
            return "Error: OSS service is not configured. Please set OSS credentials in config.json."
        
        local_path = Path(file_path).expanduser()
        if not local_path.exists():
            return f"Error: File not found: {file_path}"
        if not local_path.is_file():
            return f"Error: Not a file: {file_path}"
        
        # 使用原始文件名或自定义文件名
        target_filename = filename or local_path.name
        
        # 构建 object_key: session_id/request_id/message_id/filename
        object_key = f"{session_id}/{request_id}/{message_id}/{target_filename}"
        
        try:
            url = self._oss_service.upload_file(str(local_path), object_key)
            if url:
                return f"File uploaded successfully.\nURL: {url}\nObject Key: {object_key}"
            else:
                return "Error: Failed to upload file to OSS."
        except Exception as e:
            logger.error(f"OSS file upload failed: {e}")
            return f"Error uploading file to OSS: {str(e)}"


class OSSUploadTextTool(Tool):
    """Tool to upload text content (HTML, JSON, Markdown, etc.) to Alibaba Cloud OSS."""
    
    def __init__(self, oss_service: OSSService):
        self._oss_service = oss_service
    
    @property
    def name(self) -> str:
        return "oss_upload_text"
    
    @property
    def description(self) -> str:
        return (
            "Upload text content to Alibaba Cloud OSS. "
            "Supports HTML, JSON, Markdown, XML, CSS, CSV, YAML, plain text, and more. "
            "The content type is auto-detected from the filename extension. "
            "The object_key must follow the format: session_id/request_id/message_id/filename. "
            "Returns the public URL of the uploaded content."
        )
    
    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The text content to upload"
                },
                "filename": {
                    "type": "string",
                    "description": (
                        "Target filename with extension (e.g. 'report.html', 'data.json', 'notes.md'). "
                        "The extension determines the content type."
                    )
                },
                "session_id": {
                    "type": "string",
                    "description": "Session identifier"
                },
                "request_id": {
                    "type": "string",
                    "description": "Request identifier"
                },
                "message_id": {
                    "type": "string",
                    "description": "Message identifier"
                },
                "content_type": {
                    "type": "string",
                    "description": (
                        "Override content type (optional). "
                        "If not specified, auto-detected from filename extension. "
                        "Examples: 'text/html; charset=utf-8', 'application/json; charset=utf-8'"
                    )
                },
                "encoding": {
                    "type": "string",
                    "description": "Text encoding (default: utf-8)"
                }
            },
            "required": ["content", "filename", "session_id", "request_id", "message_id"]
        }
    
    async def execute(
        self,
        content: str,
        filename: str,
        session_id: str,
        request_id: str,
        message_id: str,
        content_type: str | None = None,
        encoding: str = "utf-8",
        **kwargs: Any,
    ) -> str:
        if not self._oss_service.is_enabled():
            return "Error: OSS service is not configured. Please set OSS credentials in config.json."
        
        if not content:
            return "Error: Content cannot be empty."
        
        if not filename:
            return "Error: Filename is required."
        
        # 构建 object_key: session_id/request_id/message_id/filename
        object_key = f"{session_id}/{request_id}/{message_id}/{filename}"
        
        try:
            url = self._oss_service.upload_text(
                text=content,
                object_key=object_key,
                encoding=encoding,
                content_type=content_type,
            )
            if url:
                detected_ct = content_type or OSSService._guess_content_type(object_key)
                return (
                    f"Text uploaded successfully.\n"
                    f"URL: {url}\n"
                    f"Object Key: {object_key}\n"
                    f"Content-Type: {detected_ct}"
                )
            else:
                return "Error: Failed to upload text to OSS."
        except Exception as e:
            logger.error(f"OSS text upload failed: {e}")
            return f"Error uploading text to OSS: {str(e)}"
