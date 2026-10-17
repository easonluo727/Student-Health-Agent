"""Byte-level fast path: skip uninteresting MRF array items without tokenizing them.

A full JSON tokenizer (even ijson's C backend) runs at ~30 MB/s decompressed. UnitedHealthcare
files compress ~22:1 (a 9.4 GB file is ~200 GB of JSON), so tokenizing everything costs hours per
file. Almost all of that is items we discard. This module finds item boundaries with a regex on
the item's first key (C speed), reads the billing code or provider_group_id from the first bytes
of each item, and only parses the items that are wanted.

Safety rules (otherwise `FastPathUnavailable` is raised and the caller falls back to ijson):
- the first key of the first item must be one that cannot occur in nested objects of the same
  section (e.g. `negotiation_arrangement`, `provider_group_id`), so it marks item starts only;
- the wanted field must appear in the item head (first HEAD_BYTES bytes).
Top-level section markers are distinguished from nested keys of the same name by what follows:
the top-level `provider_references` array holds objects (`[{`), the nested one holds IDs.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Iterator

import ijson

from stream import CHUNK

HEAD_BYTES = 4096
OVERLAP = 256
SECTION_START = {
    "in_network": re.compile(rb'"in_network"\s*:\s*\['),
    "provider_references": re.compile(rb'"provider_references"\s*:\s*\[(?=\s*[{\]])'),
}
SAFE_FIRST_KEYS = {
    "in_network": {b"negotiation_arrangement", b"negotiated_rates"},
    "provider_references": {b"provider_group_id", b"network_name", b"provider_groups", b"location"},
}
FIRST_KEY = re.compile(rb'\s*\{\s*"([A-Za-z_]+)"\s*:')
EMPTY = re.compile(rb'\s*\]')


class FastPathUnavailable(Exception):
    pass


def _str_field(name: str) -> re.Pattern:
    return re.compile(rb'"' + name.encode() + rb'"\s*:\s*("(?:[^"\\]|\\.)*"|-?[0-9.eE+]+|null)')


ITEM_FIELDS = {k: _str_field(k) for k in ("negotiation_arrangement", "name", "billing_code_type",
                                           "billing_code_type_version", "billing_code", "description")}
GID_FIELD = _str_field("provider_group_id")


def head_fields(head: bytes, fields: dict[str, re.Pattern]) -> dict:
    out = {}
    for k, rx in fields.items():
        m = rx.search(head)
        if m:
            out[k] = json.loads(m.group(1))
    return out


class _Reader:
    def __init__(self, stream):
        self.s = stream
        self.eof = False

    def read(self) -> bytes:
        if self.eof:
            return b""
        chunk = self.s.read(CHUNK)
        if not chunk:
            self.eof = True
        return chunk


def iter_segments(stream, section: str, other_section: str,
                  want: Callable[[bytes], object]) -> Iterator[tuple[object, Iterator[bytes]]]:
    """Yield (want_result, byte_pieces) for each item of `section` whose head `want(head)` is truthy.
    The caller must exhaust byte_pieces before advancing. Pieces of the last item may carry the
    section's closing bytes; callers tolerate trailing data after the item."""
    rd = _Reader(stream)
    buf = b""
    start = SECTION_START[section]
    while True:  # 1. find the section start
        m = start.search(buf)
        if m:
            buf = buf[m.end():]
            break
        if rd.eof:
            return
        buf = buf[-OVERLAP:] + rd.read()
    while len(buf) < OVERLAP and not rd.eof:
        buf += rd.read()
    if EMPTY.match(buf):
        return
    m = FIRST_KEY.match(buf)
    if not m:
        raise FastPathUnavailable(f"{section}: first item does not start with a key")
    key = m.group(1)
    if key not in SAFE_FIRST_KEYS[section]:
        raise FastPathUnavailable(f"{section}: first key {key!r} may also occur in nested objects")
    head_rx = re.compile(rb'\{\s*"' + re.escape(key) + rb'"\s*:')
    end_rx = SECTION_START[other_section]
    buf = buf[buf.index(b"{"):]  # drop whitespace before the first item

    while buf:  # 2. one iteration per item; buf starts at the item's '{'
        while len(buf) < HEAD_BYTES and not rd.eof:
            buf += rd.read()
        wanted = want(buf[:HEAD_BYTES])
        state = {"buf": buf, "last": False}

        def pieces(state=state, wanted=wanted):
            b = state["buf"]
            pos = 1
            while True:
                nh = head_rx.search(b, pos)
                # Only look for the section end before the next item head: searching the whole buffer
                # for every item made files with many small items (Cigna, L.A. Care) quadratic.
                ne = end_rx.search(b, pos, nh.start() + 1 if nh else len(b))
                cut = min([x.start() for x in (nh, ne) if x] or [None]) if (nh or ne) else None
                if cut is not None:
                    piece, rest = b[:cut], b[cut:]
                    if ne and ne.start() == cut:
                        state["last"] = True
                        rest = b""
                    if wanted:
                        yield piece.rstrip().rstrip(b",")
                    state["buf"] = rest
                    return
                if rd.eof:
                    if wanted:
                        yield b
                    state["buf"], state["last"] = b"", True
                    return
                keep = max(len(b) - OVERLAP, 0)
                if wanted and keep:
                    yield b[:keep]
                b = b[keep:] + rd.read()
                pos = max(pos - keep, 0)  # never re-find this item's own head at offset 0

        gen = pieces()
        yield wanted, gen
        for _ in gen:  # drain if the caller did not
            pass
        if state["last"]:
            return
        buf = state["buf"]


def parse_rates(pieces: Iterator[bytes]) -> Iterator[dict]:
    """Stream negotiated_rates objects out of one in_network item's bytes."""
    sink = ijson.sendable_list()
    coro = ijson.items_coro(sink, "negotiated_rates.item", use_float=True)
    try:
        for p in pieces:
            coro.send(p)
            yield from sink
            del sink[:]
        coro.close()
    except ijson.common.JSONError:
        pass  # trailing section/document bytes after the last item
    yield from sink


def parse_object(pieces: Iterator[bytes]) -> dict:
    """Decode one (small) object, ignoring trailing bytes."""
    data = b"".join(pieces).decode("utf-8")
    obj, _ = json.JSONDecoder().raw_decode(data)
    return obj
