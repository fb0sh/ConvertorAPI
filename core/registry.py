"""转换器注册表：保存所有已注册转换器的元数据。"""

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class ConvertorEntry:
    """一个已注册的转换器。"""

    fn: Callable[..., Any]
    name: str
    method: str
    path: str
    category: str
    produces: str | None = None
    example_input: Any = None
    example_output: Any = None


class Registry:
    """进程内的转换器注册表。"""

    def __init__(self) -> None:
        self._entries: dict[str, ConvertorEntry] = {}

    def register(
        self,
        fn: Callable[..., Any],
        method: str = "POST",
        path: str | None = None,
        category: str = "text",
        produces: str | None = None,
        example_input: Any = None,
        example_output: Any = None,
    ) -> ConvertorEntry:
        """注册一个转换器；名字或路由重复时抛出 ValueError。"""
        name = fn.__name__
        route = path or f"/{name}"
        http_method = method.upper()

        if name in self._entries:
            raise ValueError(f"converter name already registered: {name}")
        for existing in self._entries.values():
            if existing.method == http_method and existing.path == route:
                raise ValueError(
                    f"route already registered: {http_method} {route} "
                    f"(conflicts with converter {existing.name!r})"
                )

        entry = ConvertorEntry(
            fn=fn,
            name=name,
            method=http_method,
            path=route,
            category=category,
            produces=produces,
            example_input=example_input,
            example_output=example_output,
        )
        self._entries[name] = entry
        return entry

    def get(self, name: str) -> ConvertorEntry | None:
        """按函数名取转换器，取不到返回 None。"""
        return self._entries.get(name)

    def all(self) -> list[ConvertorEntry]:
        """按注册顺序返回全部转换器。"""
        return list(self._entries.values())


registry = Registry()
