"""A very small Prometheus text-format reader.

Open5GS exposes ``/metrics`` in the 0.0.4 text format and we want a handful of
gauges out of it. Pulling in a Prometheus client library to read six numbers
would add a dependency to a project that has kept to six, so this parses the
subset that format actually uses: ``# HELP``/``# TYPE`` comments to skip, then
``name{label="value",...} number`` or ``name number``.

Deliberately not a general parser. Histograms and summaries come through as
their individual ``_bucket``/``_sum``/``_count`` series, which is all anything
here needs, and exemplars (a ``#`` after the value) are dropped.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Sample:
    name: str
    labels: dict
    value: float


def parse(text: str) -> list[Sample]:
    out: list[Sample] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        head, _, tail = line.partition("{")
        if tail:
            label_text, _, rest = tail.rpartition("}")
            labels = _labels(label_text)
            name = head.strip()
        else:
            parts = line.split()
            if len(parts) < 2:
                continue
            name, rest, labels = parts[0], parts[1], {}
        value = _value(rest)
        if value is None:
            continue
        out.append(Sample(name=name, labels=labels, value=value))
    return out


def _labels(text: str) -> dict:
    labels: dict = {}
    for chunk in _split_labels(text):
        key, _, raw = chunk.partition("=")
        key = key.strip()
        raw = raw.strip()
        if not key:
            continue
        if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
            raw = raw[1:-1]
        labels[key] = raw.replace('\\"', '"').replace("\\\\", "\\")
    return labels


def _split_labels(text: str) -> list[str]:
    """Split on commas that are not inside a quoted value — a label value may
    legitimately contain one (an S-NSSAI SD, a device name)."""
    parts, buf, in_quotes, escaped = [], [], False, False
    for ch in text:
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if ch == "\\":
            buf.append(ch)
            escaped = True
            continue
        if ch == '"':
            in_quotes = not in_quotes
            buf.append(ch)
            continue
        if ch == "," and not in_quotes:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return [p for p in (s.strip() for s in parts) if p]


def _value(rest: str) -> float | None:
    token = rest.strip().split("#")[0].strip().split()
    if not token:
        return None
    try:
        return float(token[0])
    except ValueError:
        return None


def total(samples: list[Sample], name: str) -> float | None:
    """Sum every series of ``name``, or None if the metric is absent.

    Absent and zero are different answers — "the core does not publish this"
    versus "the core publishes it and nobody is attached" — and a caller that
    cannot tell them apart draws an empty map either way with no idea which it
    is looking at.
    """
    hits = [s.value for s in samples if s.name == name]
    return sum(hits) if hits else None
