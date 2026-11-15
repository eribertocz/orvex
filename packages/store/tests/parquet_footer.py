"""A Parquet footer rewriter for tests (#260): it drops `converted_type` from every schema element
that also carries a `logicalType`, as writers other than pyarrow do. pyarrow writes both.

The footer is Thrift's compact protocol: it is read into plain tuples and written back with the
one field removed; nothing else changes."""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

Value = Any


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data, self.at = data, 0

    def byte(self) -> int:
        self.at += 1
        return self.data[self.at - 1]

    def varint(self) -> int:
        shift = result = 0
        while True:
            b = self.byte()
            result |= (b & 0x7F) << shift
            if not b & 0x80:
                return result
            shift += 7

    def zigzag(self) -> int:
        n = self.varint()
        return (n >> 1) ^ -(n & 1)

    def raw(self, size: int) -> bytes:
        self.at += size
        return self.data[self.at - size : self.at]

    def value(self, kind: int) -> Value:
        if kind in (1, 2):  # booleans in a list
            return kind == 1
        if kind == 3:
            return ("byte", self.byte())
        if kind in (4, 5, 6):
            return ("int", kind, self.zigzag())
        if kind == 7:
            return ("double", self.raw(8))
        if kind == 8:
            return ("binary", self.raw(self.varint()))
        if kind in (9, 10):
            header = self.byte()
            size, element = header >> 4, header & 0x0F
            if size == 15:
                size = self.varint()
            return ("list", kind, element, [self.element(element) for _ in range(size)])
        if kind == 12:
            return ("struct", self.struct())
        raise ValueError(kind)

    def element(self, kind: int) -> Value:
        if kind in (1, 2):
            return ("bool", self.byte() == 1)
        return self.value(kind)

    def struct(self) -> list[tuple[int, int, Value]]:
        fields: list[tuple[int, int, Value]] = []
        last = 0
        while True:
            header = self.byte()
            if header == 0:
                return fields
            delta, kind = header >> 4, header & 0x0F
            field = last + delta if delta else self.zigzag()
            if kind in (1, 2):
                value: Value = ("bool", kind == 1)
            else:
                value = self.value(kind)
            fields.append((field, kind, value))
            last = field


class _Writer:
    def __init__(self) -> None:
        self.out = bytearray()

    def varint(self, n: int) -> None:
        while True:
            if n < 0x80:
                self.out.append(n)
                return
            self.out.append((n & 0x7F) | 0x80)
            n >>= 7

    def zigzag(self, n: int) -> None:
        self.varint((n << 1) ^ (n >> 63))

    def value(self, value: Value) -> None:
        tag = value[0]
        if tag == "byte":
            self.out.append(value[1])
        elif tag == "int":
            self.zigzag(value[2])
        elif tag == "double":
            self.out += value[1]
        elif tag == "binary":
            self.varint(len(value[1]))
            self.out += value[1]
        elif tag == "list":
            _, _, element, items = value
            if len(items) < 15:
                self.out.append((len(items) << 4) | element)
            else:
                self.out.append(0xF0 | element)
                self.varint(len(items))
            for item in items:
                if item[0] == "bool":
                    self.out.append(1 if item[1] else 2)
                else:
                    self.value(item)
        elif tag == "struct":
            self.struct(value[1])

    def struct(self, fields: list[tuple[int, int, Value]]) -> None:
        last = 0
        for field, kind, value in fields:
            if value[0] == "bool":
                kind = 1 if value[1] else 2
            delta = field - last
            if 0 < delta <= 15:
                self.out.append((delta << 4) | kind)
            else:
                self.out.append(kind)
                self.zigzag(field)
            if value[0] != "bool":
                self.value(value)
            last = field
        self.out.append(0)


def without_converted_types(path: Path) -> None:
    """Rewrite `path`'s footer: every schema element with a logical annotation (decimals, strings,
    integers, dates, lists, maps…) loses its converted one, as some writers emit them."""
    data = path.read_bytes()
    size = struct.unpack("<I", data[-8:-4])[0]
    footer = data[-8 - size : -8]
    meta = _Reader(footer).struct()
    for index, (field, kind, value) in enumerate(meta):
        if field == 2:  # schema: list<SchemaElement>
            elements = value[3]
            for element in elements:
                fields = element[1]
                if any(f == 10 for f, _, _ in fields):  # has a logicalType
                    element[1][:] = [f for f in fields if f[0] != 6]  # drop converted_type
            meta[index] = (field, kind, value)
    writer = _Writer()
    writer.struct(meta)
    rewritten = bytes(writer.out)
    path.write_bytes(data[: -8 - size] + rewritten + struct.pack("<I", len(rewritten)) + b"PAR1")
