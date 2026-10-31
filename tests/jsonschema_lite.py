# SPDX-License-Identifier: GPL-3.0-or-later
"""The subset of JSON Schema that `survivemem schema` uses, enough to validate CLI output."""

import re
from typing import Any

TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
    "null": (type(None),),
}


def _is(instance: Any, name: str) -> bool:
    if name in ("integer", "number") and isinstance(instance, bool):
        return False
    return isinstance(instance, TYPES[name])


def validate(instance: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    errors: list[str] = []
    types = schema.get("type")
    if types is not None:
        names = [types] if isinstance(types, str) else types
        if not any(_is(instance, n) for n in names):
            return [f"{path}: {instance!r} is not {names}"]
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} not in {schema['enum']}")
    if (
        "pattern" in schema
        and isinstance(instance, str)
        and not re.search(schema["pattern"], instance)
    ):
        errors.append(f"{path}: {instance!r} does not match {schema['pattern']}")
    if isinstance(instance, dict):
        errors += [
            f"{path}: missing {key}" for key in schema.get("required", []) if key not in instance
        ]
        for key, sub in schema.get("properties", {}).items():
            if key in instance:
                errors += validate(instance[key], sub, f"{path}.{key}")
    if isinstance(instance, list) and "items" in schema:
        for i, item in enumerate(instance):
            errors += validate(item, schema["items"], f"{path}[{i}]")
    return errors
