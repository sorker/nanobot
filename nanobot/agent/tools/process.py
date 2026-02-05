"""Process management tool."""

from typing import Any

from nanobot.agent.tools.base import Tool


class ProcessTool(Tool):
    """
    Tool to manage and query system processes.
    
    Can list processes, get process details, and kill processes.
    Requires psutil package.
    """
    
    @property
    def name(self) -> str:
        return "process"
    
    @property
    def description(self) -> str:
        return (
            "Manage and query system processes. "
            "Can list processes, get process details, and kill processes. "
            "Supports filtering by name, PID, or user. Use with caution when killing processes."
        )
    
    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Action to perform: 'list', 'info', or 'kill'",
                    "enum": ["list", "info", "kill"]
                },
                "pid": {
                    "type": "integer",
                    "description": "Process ID (required for 'info' and 'kill' actions)"
                },
                "name": {
                    "type": "string",
                    "description": "Process name pattern to filter (for 'list' action)"
                },
                "user": {
                    "type": "string",
                    "description": "Username to filter processes (for 'list' action)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of processes to return (for 'list', default: 50)",
                    "maximum": 200
                }
            },
            "required": ["action"]
        }
    
    async def execute(
        self,
        action: str,
        pid: int | None = None,
        name: str | None = None,
        user: str | None = None,
        limit: int = 50,
        **kwargs: Any
    ) -> str:
        try:
            import psutil
        except ImportError:
            return "Error: psutil package not installed. Install with: pip install psutil"
        
        try:
            if action == "list":
                return await self._list_processes(psutil, name, user, limit)
            elif action == "info":
                if pid is None:
                    return "Error: 'pid' parameter is required for 'info' action"
                return await self._get_process_info(psutil, pid)
            elif action == "kill":
                if pid is None:
                    return "Error: 'pid' parameter is required for 'kill' action"
                return await self._kill_process(psutil, pid)
            else:
                return f"Error: Unknown action '{action}'. Supported: list, info, kill"
                
        except Exception as e:
            return f"Process operation failed: {str(e)}"
    
    async def _list_processes(
        self,
        psutil: Any,
        name_pattern: str | None,
        user_filter: str | None,
        limit: int
    ) -> str:
        """List processes."""
        processes = []
        count = 0
        
        name_lower = name_pattern.lower() if name_pattern else None
        
        for proc in psutil.process_iter(['pid', 'name', 'username', 'cpu_percent', 'memory_percent', 'status']):
            try:
                if count >= limit:
                    break
                
                pinfo = proc.info
                
                # Apply filters
                if name_lower and name_lower not in pinfo.get('name', '').lower():
                    continue
                if user_filter and pinfo.get('username') != user_filter:
                    continue
                
                processes.append({
                    "pid": pinfo['pid'],
                    "name": pinfo.get('name', 'N/A'),
                    "user": pinfo.get('username', 'N/A'),
                    "cpu_percent": round(pinfo.get('cpu_percent', 0) or 0, 2),
                    "memory_percent": round(pinfo.get('memory_percent', 0) or 0, 2),
                    "status": pinfo.get('status', 'N/A')
                })
                count += 1
                
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        
        # Sort by CPU usage
        processes.sort(key=lambda x: x['cpu_percent'], reverse=True)
        
        # Format output
        lines = [f"Found {len(processes)} processes:", ""]
        lines.append(f"{'PID':<8} {'Name':<25} {'User':<15} {'CPU%':<8} {'Mem%':<8} {'Status':<10}")
        lines.append("-" * 90)
        
        for proc in processes:
            lines.append(
                f"{proc['pid']:<8} {proc['name'][:24]:<25} {proc['user'][:14]:<15} "
                f"{proc['cpu_percent']:<8.2f} {proc['memory_percent']:<8.2f} {proc['status']:<10}"
            )
        
        if len(processes) >= limit:
            lines.append(f"\n(Showing first {limit} processes)")
        
        return "\n".join(lines)
    
    async def _get_process_info(self, psutil: Any, pid: int) -> str:
        """Get detailed process information."""
        try:
            proc = psutil.Process(pid)
            
            # Get basic attributes
            basic_attrs = [
                'pid', 'name', 'username', 'status', 'create_time',
                'cpu_percent', 'memory_percent', 'memory_info',
                'num_threads'
            ]
            
            pinfo = proc.as_dict(attrs=basic_attrs)
            
            # Try to get optional attributes
            try:
                pinfo['open_files'] = proc.open_files()
            except (psutil.AccessDenied, psutil.NoSuchProcess, AttributeError):
                pinfo['open_files'] = None
            
            try:
                pinfo['connections'] = proc.connections()
            except (psutil.AccessDenied, psutil.NoSuchProcess, AttributeError):
                pinfo['connections'] = None
            
            # Format output
            lines = [f"Process Information (PID: {pid}):", ""]
            lines.append(f"Name: {pinfo.get('name', 'N/A')}")
            lines.append(f"User: {pinfo.get('username', 'N/A')}")
            lines.append(f"Status: {pinfo.get('status', 'N/A')}")
            lines.append(f"CPU %: {pinfo.get('cpu_percent', 0):.2f}")
            lines.append(f"Memory %: {pinfo.get('memory_percent', 0):.2f}")
            
            mem_info = pinfo.get('memory_info')
            if mem_info:
                lines.append(f"Memory RSS: {self._format_bytes(mem_info.rss)}")
                lines.append(f"Memory VMS: {self._format_bytes(mem_info.vms)}")
            
            lines.append(f"Threads: {pinfo.get('num_threads', 'N/A')}")
            
            open_files = pinfo.get('open_files')
            if open_files is not None:
                lines.append(f"Open Files: {len(open_files)}")
            else:
                lines.append("Open Files: N/A (permission denied)")
            
            connections = pinfo.get('connections')
            if connections is not None:
                lines.append(f"Connections: {len(connections)}")
            else:
                lines.append("Connections: N/A (permission denied)")
            
            create_time = pinfo.get('create_time')
            if create_time:
                from datetime import datetime
                lines.append(f"Created: {datetime.fromtimestamp(create_time)}")
            
            return "\n".join(lines)
            
        except psutil.NoSuchProcess:
            return f"Error: Process with PID {pid} not found"
        except psutil.AccessDenied:
            return f"Error: Access denied to process {pid}"
    
    async def _kill_process(self, psutil: Any, pid: int) -> str:
        """Kill a process."""
        try:
            proc = psutil.Process(pid)
            proc_name = proc.name()
            
            # Terminate process
            proc.terminate()
            
            # Wait for process to terminate (max 5 seconds)
            try:
                proc.wait(timeout=5)
                status = "terminated"
            except psutil.TimeoutExpired:
                # Force kill
                proc.kill()
                proc.wait(timeout=5)
                status = "killed (forced)"
            
            return f"Process {pid} ({proc_name}) {status} successfully"
            
        except psutil.NoSuchProcess:
            return f"Error: Process with PID {pid} not found"
        except psutil.AccessDenied:
            return f"Error: Access denied to process {pid}"
    
    def _format_bytes(self, bytes_value: int) -> str:
        """Format bytes in human-readable format."""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if bytes_value < 1024.0:
                return f"{bytes_value:.2f} {unit}"
            bytes_value /= 1024.0
        return f"{bytes_value:.2f} PB"
