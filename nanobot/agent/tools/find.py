"""Find files tool using glob patterns."""

from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool


class FindTool(Tool):
    """
    Tool to find files using glob patterns.
    
    Supports patterns like '*.py', '**/*.md' for recursive search.
    """
    
    def __init__(self, workspace_dir: str = "."):
        self.workspace_dir = workspace_dir
    
    @property
    def name(self) -> str:
        return "find"
    
    @property
    def description(self) -> str:
        return (
            "Find files matching a glob pattern. Supports patterns like '*.py', '**/*.md' (recursive). "
            "Use '**' for recursive directory search. Can filter by file extension, name, or path."
        )
    
    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern to search for (e.g., '*.py', '**/*.md', 'test_*.py')"
                },
                "directory": {
                    "type": "string",
                    "description": "Directory to search in (default: workspace root)"
                },
                "max_depth": {
                    "type": "integer",
                    "description": "Maximum recursion depth (default: unlimited)"
                },
                "include_hidden": {
                    "type": "boolean",
                    "description": "Include hidden files/directories starting with '.' (default: false)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default: 100)",
                    "maximum": 500
                }
            },
            "required": ["pattern"]
        }
    
    async def execute(
        self,
        pattern: str,
        directory: str = ".",
        max_depth: int | None = None,
        include_hidden: bool = False,
        limit: int = 100,
        **kwargs: Any
    ) -> str:
        try:
            # Resolve paths
            workspace_path = Path(self.workspace_dir).expanduser().resolve()
            search_dir = (workspace_path / directory).resolve()
            
            # Validate directory
            if not search_dir.exists():
                return f"Error: Directory not found: {directory}"
            if not search_dir.is_dir():
                return f"Error: Not a directory: {directory}"
            
            # Find matches
            matches = []
            try:
                for file_path in search_dir.glob(pattern):
                    # Skip hidden files unless requested
                    if not include_hidden and any(part.startswith('.') for part in file_path.parts):
                        continue
                    
                    # Check depth limit
                    if max_depth is not None:
                        try:
                            relative = file_path.relative_to(search_dir)
                            depth = len(relative.parts) - 1
                            if depth > max_depth:
                                continue
                        except ValueError:
                            continue
                    
                    # Get relative path from workspace
                    try:
                        rel_path = file_path.relative_to(workspace_path)
                    except ValueError:
                        rel_path = file_path
                    
                    # Add to matches
                    is_dir = file_path.is_dir()
                    size = file_path.stat().st_size if file_path.is_file() else None
                    
                    matches.append({
                        "path": str(rel_path),
                        "type": "directory" if is_dir else "file",
                        "size": size
                    })
                    
                    if len(matches) >= limit:
                        break
                        
            except Exception as e:
                return f"Error during search: {e}"
            
            # Format output
            if not matches:
                return f"No files found matching pattern: {pattern}\nSearch directory: {directory}"
            
            # Sort by type and path
            matches.sort(key=lambda x: (x["type"] == "file", x["path"]))
            
            lines = [
                f"Found {len(matches)} match(es) for pattern: {pattern}",
                f"Search directory: {directory}",
                ""
            ]
            
            # Group by type
            dirs = [m for m in matches if m["type"] == "directory"]
            files = [m for m in matches if m["type"] == "file"]
            
            if dirs:
                lines.append("Directories:")
                for item in dirs:
                    lines.append(f"  📁 {item['path']}")
                lines.append("")
            
            if files:
                lines.append("Files:")
                for item in files:
                    size_str = self._format_size(item["size"]) if item["size"] is not None else ""
                    size_part = f" ({size_str})" if size_str else ""
                    lines.append(f"  📄 {item['path']}{size_part}")
            
            if len(matches) >= limit:
                lines.append(f"\n(Results limited to {limit} items)")
            
            return "\n".join(lines)
            
        except Exception as e:
            return f"Error finding files: {str(e)}"
    
    def _format_size(self, size: int) -> str:
        """Format file size in human-readable format."""
        if size < 1024:
            return f"{size} B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        elif size < 1024 * 1024 * 1024:
            return f"{size / (1024 * 1024):.1f} MB"
        else:
            return f"{size / (1024 * 1024 * 1024):.1f} GB"
