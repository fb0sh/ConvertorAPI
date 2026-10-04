"""把注册表里的 convertor 挂载成 FastAPI 路由。"""

import inspect
import json
from typing import Any, Callable, get_type_hints

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, create_model

from core.docs import infer_output
from core.registry import ConvertorEntry, registry


def _request_model(entry: ConvertorEntry) -> type[BaseModel]:
    """按函数签名生成请求模型：参数名 = 字段名，注解 = 字段类型。"""
    signature = inspect.signature(entry.fn)
    try:
        hints = get_type_hints(entry.fn)
    except Exception:  # 注解无法解析时按 str 处理
        hints = {}

    fields: dict[str, tuple[Any, Any]] = {}
    for name, param in signature.parameters.items():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        annotation = hints.get(name, str)
        default = ... if param.default is inspect.Parameter.empty else param.default
        fields[name] = (annotation, default)

    model_name = "".join(part.capitalize() for part in entry.name.split("_")) + "Request"
    return create_model(model_name, **fields)


def _render(result: Any, content_type: str) -> Response:
    """把函数返回值转成响应本体。"""
    if isinstance(result, bytes):
        return Response(content=result, media_type=content_type)
    if isinstance(result, (dict, list)):
        return Response(
            content=json.dumps(result, ensure_ascii=False),
            media_type=content_type,
        )
    return Response(content=str(result), media_type=content_type)


def _invoke(fn: Callable[..., Any], params: BaseModel, content_type: str) -> Response:
    """调用 convertor 函数并处理异常。"""
    try:
        result = fn(**params.model_dump())
    except HTTPException:
        raise
    except Exception as exc:  # 任何异常统一转 400
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _render(result, content_type)


def build_endpoint(entry: ConvertorEntry) -> Callable[..., Any]:
    """为一个 convertor 构造 FastAPI 端点函数。"""
    request_model = _request_model(entry)
    content_type = entry.produces or infer_output(entry.fn)[0]

    if entry.method == "GET":
        async def endpoint(params: request_model = Depends()) -> Response:  # type: ignore[valid-type]
            return _invoke(entry.fn, params, content_type)
    else:
        async def endpoint(params: request_model) -> Response:  # type: ignore[valid-type]
            return _invoke(entry.fn, params, content_type)

    endpoint.__name__ = entry.name
    endpoint.__doc__ = inspect.getdoc(entry.fn)
    return endpoint


def mount_all(app: FastAPI) -> None:
    """把注册表里的全部 convertor 挂到应用上。"""
    for entry in registry.all():
        app.add_api_route(
            entry.path,
            build_endpoint(entry),
            methods=[entry.method],
            name=entry.name,
            summary=inspect.getdoc(entry.fn) or entry.name,
            tags=[entry.category],
        )
