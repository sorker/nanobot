"""Grep tool for searching file contents."""

import re
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool


class GrepTool(Tool):
    """
    Tool to search for patterns in file contents.
    
    Supports regular expressions and can search in files or directories.
    """
    
    def __init__(self, workspace_dir: str = "."):
        self.workspace_dir = workspace_dir
    
    @property
    def name(self) -> str:
        return "grep"
    
    @property
    def description(self) -> str:
        return (
            "Search for a pattern in file(s) using regular expressions. "
            "Can search in a single file or recursively in a directory. "
            "Returns matching lines with file names and line numbers."
        )
    
    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Pattern to search for (supports regex)"
                },
                "path": {
                    "type": "string",
                    "description": "Path to file or directory to search in"
                },
                "case_sensitive": {
                    "type": "boolean",
                    "description": "Whether the search should be case sensitive (default: true)"
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default: 100)",
                    "maximum": 500
                },
                "context_lines": {
                    "type": "integer",
                    "description": "Number of context lines to show before and after match (default: 0)",
                    "maximum": 10
                }
            },
            "required": ["pattern", "path"]
        }
    
    async def execute(
        self,
        pattern: str,
        path: str,
        case_sensitive: bool = True,
        max_results: int = 100,
        context_lines: int = 0,
        **kwargs: Any
    ) -> str:
        try:
            # Compile regex
            flags = 0 if case_sensitive else re.IGNORECASE
            try:
                regex = re.compile(pattern, flags)
            except re.error as e:
                return f"Error: Invalid regex pattern: {e}"
            
            # Resolve paths
            workspace_path = Path(self.workspace_dir).expanduser().resolve()
            full_path = (workspace_path / path).resolve()
            
            # Validate path
            if not full_path.exists():
                return f"Error: Path not found: {path}"
            
            # Search
            results = []
            if full_path.is_file():
                results = await self._search_file(full_path, regex, workspace_path, max_results, context_lines)
            elif full_path.is_dir():
                results = await self._search_directory(full_path, regex, workspace_path, max_results, context_lines)
            else:
                return f"Error: Invalid path type: {path}"
            
            # Format output
            if not results:
                return f"No matches found for pattern: {pattern}"
            
            lines = [f"Found {len(results)} match(es) for pattern: {pattern}", ""]
            
            for result in results:
                file_path = result["file"]
                line_num = result["line_num"]
                line_content = result["line"]
                
                lines.append(f"{file_path}:{line_num}: {line_content}")
                
                # Add context if requested
                if "context" in result and result["context"]:
                    for ctx_line in result["context"]:
                        lines.append(f"  {ctx_line}")
            
            return "\n".join(lines)
            
        except Exception as e:
            return f"Error searching: {str(e)}"
    
    async def _search_file(
        self,
        file_path: Path,
        regex: re.Pattern,
        workspace_path: Path,
        max_results: int,
        context_lines: int = 0
    ) -> list[dict[str, Any]]:
        """Search in a single file."""
        results = []
        
        try:
            # Get relative path for display
            try:
                display_path = file_path.relative_to(workspace_path)
            except ValueError:
                display_path = file_path
            
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
                
                for line_num, line in enumerate(all_lines, start=1):
                    line = line.rstrip("\n")
                    
                    if regex.search(line):
                        result = {
                            "file": str(display_path),
                            "line_num": line_num,
                            "line": line
                        }
                        
                        # Add context lines if requested
                        if context_lines > 0:
                            context = []
                            # Before context
                            for i in range(max(0, line_num - context_lines - 1), line_num - 1):
                                context.append(f"  {i+1}: {all_lines[i].rstrip()}")
                            # After context
                            for i in range(line_num, min(len(all_lines), line_num + context_lines)):
                                context.append(f"  {i+1}: {all_lines[i].rstrip()}")
                            result["context"] = context
                        
                        results.append(result)
                        
                        if len(results) >= max_results:
                            break
        
        except (UnicodeDecodeError, PermissionError, IsADirectoryError):
            # Skip files that can't be read
            pass
        
        return results
    
    async def _search_directory(
        self,
        dir_path: Path,
        regex: re.Pattern,
        workspace_path: Path,
        max_results: int,
        context_lines: int = 0
    ) -> list[dict[str, Any]]:
        """Search recursively in a directory."""
        results = []
        
        # Recursively traverse all files
        for file_path in dir_path.rglob("*"):
            if file_path.is_file():
                # Skip hidden files and common binary/large files
                if file_path.name.startswith('.'):
                    continue
                
                # Skip common binary extensions
                binary_exts = {'.pyc', '.so', '.o', '.a', '.dylib', '.dll', '.exe', 
                              '.png', '.jpg', '.jpeg', '.gif', '.pdf', '.zip', '.tar', '.gz'}
                if file_path.suffix.lower() in binary_exts:
                    continue
                
                # Search file
                file_results = await self._search_file(
                    file_path, regex, workspace_path, 
                    max_results - len(results), context_lines
                )
                results.extend(file_results)
                
                if len(results) >= max_results:
                    break
        
        return results
