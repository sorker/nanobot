"""Tool registry for dynamic tool management."""

from __future__ import annotations

from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool


class ToolRegistry:
    """
    Registry for agent tools.
    
    Allows dynamic registration and execution of tools.
    
    Auto-registration
    -----------------
    调用 :meth:`auto_register_all` 即可自动发现所有声明了
    ``AUTO_REGISTER_DEPS`` 的 Tool 子类，并根据提供的依赖字典
    自动实例化和注册。新增工具只需：
      1. 在 ``nanobot/agent/tools/`` 下创建文件
      2. 在 ``__init__.py`` 中 import
      3. 在类上声明 ``AUTO_REGISTER_DEPS``
    无需修改 ``loop.py``。
    """
    
    def __init__(self):
        self._tools: dict[str, Tool] = {}
    
    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool
    
    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        self._tools.pop(name, None)
    
    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)
    
    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return name in self._tools
    
    def get_definitions(self) -> list[dict[str, Any]]:
        """Get all tool definitions in OpenAI format."""
        return [tool.to_schema() for tool in self._tools.values()]

    # ----------------------------------------------------------
    # Auto-registration
    # ----------------------------------------------------------

    def auto_register_all(self, deps: dict[str, Any]) -> None:
        """自动发现并注册所有声明了 ``AUTO_REGISTER_DEPS`` 的 Tool 子类.

        工作流程:
          1. 递归遍历 ``Tool.__subclasses__()``
          2. 对每个定义了 ``AUTO_REGISTER_DEPS`` (非 None) 的子类:
             a. 检查 deps 中是否包含其所需的全部依赖
             b. 若缺少任意依赖则跳过（打印 debug 日志）
             c. 依赖中值为 None 的也视为不可用，跳过
             d. 实例化并注册

        Args:
            deps: 可用的依赖字典，如
                  ``{"provider": provider_instance, "oss_service": oss_service_instance}``
        """
        for cls in _all_subclasses(Tool):
            required = getattr(cls, "AUTO_REGISTER_DEPS", None)
            if required is None:
                continue

            # 已经注册过同名工具则跳过（避免重复）
            try:
                tool_name = cls.name.fget(cls)  # type: ignore[attr-defined]
            except Exception:
                tool_name = None
            if tool_name and tool_name in self._tools:
                continue

            # 检查依赖是否齐全
            kwargs: dict[str, Any] = {}
            missing: list[str] = []
            for ctor_param, dep_key in required.items():
                val = deps.get(dep_key)
                if val is None:
                    missing.append(dep_key)
                else:
                    kwargs[ctor_param] = val

            if missing:
                logger.debug(
                    f"跳过自动注册 {cls.__name__}: 缺少依赖 {missing}"
                )
                continue

            try:
                tool_instance = cls(**kwargs)
                self.register(tool_instance)
                logger.info(
                    f"自动注册工具: {tool_instance.name} ({cls.__name__})"
                )
            except Exception as exc:
                logger.warning(
                    f"自动注册 {cls.__name__} 失败: {exc}"
                )

    async def execute(self, name: str, params: dict[str, Any]) -> str:
        """
        Execute a tool by name with given parameters.
        
        Args:
            name: Tool name.
            params: Tool parameters.
        
        Returns:
            Tool execution result as string.
        
        Raises:
            KeyError: If tool not found.
        """
        tool = self._tools.get(name)
        if not tool:
            return f"Error: Tool '{name}' not found"

        try:
            errors = tool.validate_params(params)
            if errors:
                return f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors)
            return await tool.execute(**params)
        except Exception as e:
            return f"Error executing {name}: {str(e)}"
    
    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return list(self._tools.keys())
    
    def __len__(self) -> int:
        return len(self._tools)
    
    def __contains__(self, name: str) -> bool:
        return name in self._tools


# ----------------------------------------------------------------
# helpers
# ----------------------------------------------------------------

def _all_subclasses(cls: type) -> list[type]:
    """递归收集所有（直接 + 间接）子类。"""
    result: list[type] = []
    for sub in cls.__subclasses__():
        result.append(sub)
        result.extend(_all_subclasses(sub))
    return result
