"""JSON 与 YAML 互转的转换器。"""

import json

import yaml

from core.decorator import convertor


@convertor(
    method="POST",
    category="format",
    produces="text/yaml; charset=utf-8",
    example_input={"json_text": '{"name":"alice","age":30}', "indent": 2},
    example_output="name: alice\nage: 30\n",
)
def json_to_yaml(json_text: str, indent: int = 2) -> str:
    """把 JSON 文本转换成 YAML 文本。"""
    try:
        obj = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON input: {exc}") from exc
    return yaml.safe_dump(obj, allow_unicode=True, indent=indent, sort_keys=False)


@convertor(
    method="POST",
    category="format",
    produces="application/json",
    example_input={"yaml_text": "name: alice\nage: 30\n", "indent": 2},
    example_output={"name": "alice", "age": 30},
)
def yaml_to_json(yaml_text: str, indent: int = 2) -> dict:
    """把 YAML 文本转换成 JSON 对象。"""
    # indent 仅用于与 json_to_yaml 保持一致的签名，转成对象时不需要它
    try:
        obj = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML input: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError(
            "YAML top level must be a mapping, got "
            + type(obj).__name__
        )
    return obj
