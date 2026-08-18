from typing import Any, Callable

class _Window:
    def is_open(self) -> bool: ...
    def close(self) -> None: ...
    def post(self, message: Any) -> None: ...

class _UI:
    def create_window(
        self,
        *,
        html: str,
        title: str,
        width: int,
        height: int,
        on_message: Callable[..., Any],
        on_close: Callable[..., Any],
    ) -> _Window: ...

class _Host:
    ui: _UI

host: _Host

class _ScriptNamespace:
    class ScriptPluginCapabilityBase:
        def __init__(self, *args: Any, **kwargs: Any) -> None: ...

script: _ScriptNamespace

class ExecutionResult:
    @staticmethod
    def success() -> Any: ...

class base: ...

def plugin(cls: type[Any]) -> type[Any]: ...
def register_capability(capability: type[Any]) -> None: ...
