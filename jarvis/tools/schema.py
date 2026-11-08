"""Araç girdileri için küçük JSON Schema doğrulayıcısı.

Araçların kullandığı alt küme desteklenir: type, enum, const, properties,
required, additionalProperties=false, items, minimum/maximum,
minLength/maxLength, pattern, minItems/maxItems.

Claude'un `strict: true` modu sayı/metin sınırlarını desteklemez; bu sınırlar
`for_claude()` ile şemadan çıkarılır ve açıklamaya yazılır. Asıl kontrol her
zaman burada, yerelde yapılır: modelden gelen girdiye güvenilmez.
"""

from __future__ import annotations

import copy
import re
from typing import Any

_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
    "null": (type(None),),
}
_TR_TYPES = {"string": "metin", "integer": "tam sayı", "number": "sayı", "boolean": "evet/hayır",
             "array": "liste", "object": "nesne", "null": "boş"}

# strict modda API'nin kabul etmediği anahtarlar
_UNSUPPORTED_FOR_STRICT = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
                           "minLength", "maxLength", "pattern", "maxItems")


class SchemaError(ValueError):
    """Geçersiz girdi. `errors` kullanıcıya/Claude'a gösterilebilir Türkçe açıklamalardır."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def _type_ok(value: Any, t: str) -> bool:
    if t in ("integer", "number") and isinstance(value, bool):
        return False
    if t == "integer" and isinstance(value, float):
        return value.is_integer()
    return isinstance(value, _TYPES[t])


def _check(schema: dict, value: Any, path: str, errors: list[str]) -> None:
    where = path or "girdi"
    if "const" in schema and value != schema["const"]:
        errors.append(f"{where}: {schema['const']!r} olmalı")
        return
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_type_ok(value, x) for x in types):
            errors.append(f"{where}: {' ya da '.join(_TR_TYPES.get(x, x) for x in types)} olmalı")
            return
    if "enum" in schema and value not in schema["enum"]:
        allowed = ", ".join(map(str, schema["enum"][:12]))
        errors.append(f"{where}: izin verilen değerlerden biri olmalı ({allowed})")
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{where}: en az {schema['minimum']} olmalı")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{where}: en çok {schema['maximum']} olmalı")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{where}: en az {schema['minLength']} karakter olmalı")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{where}: en çok {schema['maxLength']} karakter olabilir")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            errors.append(f"{where}: biçimi geçersiz")
        if "\x00" in value:
            errors.append(f"{where}: geçersiz karakter içeriyor")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{where}: en az {schema['minItems']} öğe olmalı")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{where}: en çok {schema['maxItems']} öğe olabilir")
        if "items" in schema:
            for i, item in enumerate(value):
                _check(schema["items"], item, f"{where}[{i}]", errors)
    if isinstance(value, dict):
        props: dict = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in value:
                errors.append(f"{where}: '{req}' alanı eksik")
        if schema.get("additionalProperties") is False:
            extra = [k for k in value if k not in props]
            if extra:
                errors.append(f"{where}: bilinmeyen alan(lar): {', '.join(map(str, extra[:5]))}")
        for key, sub in props.items():
            if key in value:
                _check(sub, value[key], f"{path}.{key}" if path else key, errors)


def validate(schema: dict, value: Any) -> None:
    """Geçersizse `SchemaError` fırlatır."""
    errors: list[str] = []
    _check(schema, value, "", errors)
    if errors:
        raise SchemaError(errors)


def check_strict(schema: dict, path: str = "") -> list[str]:
    """Şemanın Claude strict moduna uygun olup olmadığını denetler; sorunları döndürür."""
    problems: list[str] = []
    t = schema.get("type")
    if t == "object":
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is not False:
            problems.append(f"{path or 'kök'}: additionalProperties false olmalı")
        if sorted(schema.get("required", [])) != sorted(props):
            problems.append(f"{path or 'kök'}: tüm alanlar required olmalı")
        for k, sub in props.items():
            problems += check_strict(sub, f"{path}.{k}")
    elif t == "array" and "items" in schema:
        problems += check_strict(schema["items"], f"{path}[]")
    if "description" in schema and not isinstance(schema["description"], str):
        problems.append(f"{path}: açıklama metin olmalı")
    return problems


def _constraint_note(schema: dict) -> str:
    parts = []
    if "minimum" in schema and "maximum" in schema:
        parts.append(f"{schema['minimum']}-{schema['maximum']}")
    elif "minimum" in schema:
        parts.append(f">= {schema['minimum']}")
    elif "maximum" in schema:
        parts.append(f"<= {schema['maximum']}")
    if "maxLength" in schema:
        parts.append(f"max {schema['maxLength']} chars")
    if "maxItems" in schema:
        parts.append(f"max {schema['maxItems']} items")
    return f" ({', '.join(parts)})" if parts else ""


def for_claude(schema: dict) -> dict:
    """strict modda desteklenmeyen sınırları çıkarır, açıklamaya ekler (orijinali değiştirmez)."""
    s = copy.deepcopy(schema)

    def walk(node: dict) -> None:
        note = _constraint_note(node)
        if note:
            node["description"] = (node.get("description", "") + note).strip()
        for k in _UNSUPPORTED_FOR_STRICT:
            node.pop(k, None)
        if node.get("minItems", 0) > 1:
            node.pop("minItems")
        for sub in node.get("properties", {}).values():
            walk(sub)
        if isinstance(node.get("items"), dict):
            walk(node["items"])

    walk(s)
    return s


# ---------- şema yazmayı kısaltan yardımcılar ----------
def obj(**props: dict) -> dict:
    """Tüm alanları zorunlu, ek alana kapalı nesne şeması (strict uyumlu)."""
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def string(desc: str, max_length: int = 500, min_length: int = 0, enum: list[str] | None = None,
           pattern: str | None = None) -> dict:
    s: dict = {"type": "string", "description": desc}
    if enum is not None:
        s["enum"] = enum
    else:
        s["maxLength"] = max_length
        if min_length:
            s["minLength"] = min_length
    if pattern:
        s["pattern"] = pattern
    return s


def integer(desc: str, minimum: int, maximum: int) -> dict:
    return {"type": "integer", "description": desc, "minimum": minimum, "maximum": maximum}


def boolean(desc: str) -> dict:
    return {"type": "boolean", "description": desc}
