"""ConvertorAPI 应用入口。"""

import importlib
import inspect
import pkgutil
from typing import Any, Callable, get_type_hints

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse

from api.mount import mount_all
from core.docs import infer_output, render_api_doc
from core.registry import ConvertorEntry, registry

app = FastAPI(
    title="ConvertorAPI",
    description="Many convertors, one API.",
    version="0.1.0",
)


def _doc_of(entry: ConvertorEntry) -> str:
    return inspect.getdoc(entry.fn) or ""


def _entry_summary(entry: ConvertorEntry) -> dict[str, Any]:
    return {
        "name": entry.name,
        "method": entry.method,
        "path": entry.path,
        "category": entry.category,
        "doc": _doc_of(entry),
    }


def _param_docs(fn: Callable[..., Any]) -> list[dict[str, Any]]:
    signature = inspect.signature(fn)
    try:
        hints = get_type_hints(fn)
    except Exception:
        hints = {}

    params: list[dict[str, Any]] = []
    for name, param in signature.parameters.items():
        annotation = hints.get(name, str)
        required = param.default is inspect.Parameter.empty
        params.append(
            {
                "name": name,
                # 联合类型/泛型别名没有 __name__（int | None 会显示成 Union），退回字符串写法
                "type": annotation.__name__
                if isinstance(annotation, type)
                else str(annotation).replace("typing.", ""),
                "required": required,
                "default": None if required else param.default,
            }
        )
    return params


@app.get("/", tags=["meta"])
async def api_index() -> PlainTextResponse:
    """返回 ConvertorAPI 的 Markdown 文档。"""
    return PlainTextResponse(render_api_doc(), media_type="text/markdown")


@app.get("/convertors", tags=["meta"])
async def list_convertors(category: str | None = None) -> list[dict[str, Any]]:
    """列出所有 convertor，可用 ?category= 过滤。"""
    entries = registry.all()
    if category:
        entries = [entry for entry in entries if entry.category == category]
    return [_entry_summary(entry) for entry in entries]


@app.get("/convertors/{name}", tags=["meta"])
async def get_convertor(name: str) -> dict[str, Any]:
    """返回单个 convertor 的完整用法。"""
    entry = registry.get(name)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"convertor not found: {name}")

    content_type, schema = infer_output(entry.fn)
    return {
        **_entry_summary(entry),
        "input": {
            "params": _param_docs(entry.fn),
            "example": entry.example_input,
        },
        "output": {
            "produces": entry.produces or content_type,
            "schema": schema,
            "example": entry.example_output,
        },
    }


def _load_convertors() -> None:
    """扫描 convertors 包，导入每个模块以触发装饰器注册。"""
    import convertors

    for module_info in pkgutil.iter_modules(convertors.__path__):
        importlib.import_module(f"convertors.{module_info.name}")


_load_convertors()
mount_all(app)
