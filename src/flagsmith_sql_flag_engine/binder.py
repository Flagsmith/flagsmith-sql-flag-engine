from typing import Protocol


class ParamStyle(Protocol):
    """A driver's placeholder syntax for a named bound parameter."""

    def placeholder(self, name: str) -> str:
        """The placeholder token referencing bound parameter `name`."""
        ...


class PyformatParamStyle:
    """`%(name)s`

    Used by `clickhouse-driver` which substitutes parameters
    client-side via `query % params`."""

    def placeholder(self, name: str) -> str:
        return f"%({name})s"


class ClickHouseServerParamStyle:
    """`{name:String}`

    ClickHouse's native server-side parameter syntax,
    used by `clickhouse-connect`."""

    def placeholder(self, name: str) -> str:
        return "{" + name + ":String}"


class Binder:
    """Collects bound parameter values and mints their placeholders.

    Not thread-safe; use one `Binder` per predicate translation.
    """

    def __init__(self, style: ParamStyle, prefix: str = "") -> None:
        self.params: dict[str, str] = {}
        self._style = style
        self._prefix = prefix
        self._count = 0

    def add(self, value: str) -> str:
        """Record `value` under a fresh namespaced name and return its
        placeholder token for the active paramstyle."""
        name = f"{self._prefix}p{self._count}"
        self._count += 1
        self.params[name] = value
        return self._style.placeholder(name)
