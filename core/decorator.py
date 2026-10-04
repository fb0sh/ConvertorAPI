"""@convertor 装饰器：把一个普通函数注册成一个 HTTP convertor。"""

from typing import Any, Callable

from core.registry import registry

# 已被框架占用、不允许作为路由首段的名字
RESERVED = {"convertors", "docs", "redoc", "openapi.json", "favicon.ico"}


def convertor(
    method: str = "POST",
    path: str | None = None,
    category: str = "text",
    produces: str | None = None,
    example_input: Any = None,
    example_output: Any = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """把函数注册成 convertor；返回原函数本身，不做任何包装。"""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        route = path or f"/{fn.__name__}"
        first_segment = route.strip("/").split("/")[0]
        if first_segment in RESERVED:
            raise ValueError(
                f"route {route!r} starts with reserved segment {first_segment!r}; "
                f"reserved: {sorted(RESERVED)}"
            )
        registry.register(
            fn,
            method=method,
            path=route,
            category=category,
            produces=produces,
            example_input=example_input,
            example_output=example_output,
        )
        return fn

    return decorator
