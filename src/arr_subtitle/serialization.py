import dataclasses
import json
from decimal import Decimal


def camel(name):
    first, *rest = name.split("_")
    return first + "".join(word.capitalize() for word in rest)


def plain(value):
    if dataclasses.is_dataclass(value):
        return {camel(f.name): plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set)):
        return [plain(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    return value


def dumps(value):
    return json.dumps(plain(value), ensure_ascii=False, indent=2, allow_nan=False)
