"""Repo name validation against the standard."""

from .standard import Standard


def check_name(name: str, kind: str, std: Standard) -> list[str]:
    """Return the problems with `name` for `kind`; an empty list means it is valid."""
    if kind not in std.kinds:
        return [f"unknown kind {kind!r}"]
    if name in std.special_names:
        return [] if kind == "special" else [f"{name!r} is a special name, kind must be 'special'"]

    problems: list[str] = []
    if not std.name_pattern.fullmatch(name):
        problems.append(f"{name!r} does not match {std.name_pattern.pattern}")

    spec = std.kinds[kind]
    if spec.prefix and not (name.startswith(spec.prefix) and len(name) > len(spec.prefix)):
        problems.append(f"kind {kind!r} needs prefix {spec.prefix!r} followed by a subject")
    if spec.suffix and not (name.endswith(spec.suffix) and len(name) > len(spec.suffix)):
        problems.append(f"kind {kind!r} needs suffix {spec.suffix!r} preceded by a subject")

    if kind == "product":
        for other in std.kinds.values():
            if other.id == "product":
                continue
            if other.prefix and name.startswith(other.prefix):
                problems.append(f"product name must not start with {other.prefix!r} ({other.id})")
            if other.suffix and name.endswith(other.suffix):
                problems.append(f"product name must not end with {other.suffix!r} ({other.id})")
    return problems
