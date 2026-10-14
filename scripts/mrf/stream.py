"""Open an MRF (local path or URL, gzip or plain JSON) as a constant-memory byte stream.

HTTP chunks -> optional gzip decoder -> file-like object that ijson reads incrementally.
Nothing is written to disk and nothing larger than one decompressed chunk is held in RAM.
"""
from __future__ import annotations

import io
import os
import time
import zlib
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

USER_AGENT = (
    "amd-health-agent-mrf/0.1 (Transparency in Coverage research; "
    "+https://github.com/mr-jude-justin/amd-health-agent)"
)
CHUNK = 1 << 20
GZIP_MAGIC = b"\x1f\x8b"


class HostRefused(RuntimeError):
    """The host rate-limited or blocked us (403/429). Stop and report; never evade."""


@dataclass
class StreamStats:
    compressed_bytes: int = 0    # bytes read from the network or disk
    decompressed_bytes: int = 0  # bytes handed to the JSON parser
    gzip: bool = False


def is_url(src: str) -> bool:
    return urlparse(str(src)).scheme in ("http", "https")


def resolve(base: str, location: str) -> str:
    """Resolve a (possibly relative) location found inside `base`, for URLs and paths alike."""
    if is_url(location) or os.path.isabs(location):
        return location
    if is_url(base):
        return urljoin(base, location)
    return os.path.join(os.path.dirname(os.path.abspath(base)), location)


def _http_chunks(url: str, timeout: float, retries: int):
    import requests  # imported lazily so local-file tests need no network stack

    delay = 2.0
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, stream=True, timeout=timeout, headers={"User-Agent": USER_AGENT})
            if r.status_code in (403, 429):
                raise HostRefused(f"{r.status_code} from {urlparse(url).netloc}")
            if r.status_code >= 500 and attempt < retries:
                r.close()
                raise requests.ConnectionError(f"HTTP {r.status_code}")
            r.raise_for_status()
            break
        except requests.ConnectionError:
            if attempt == retries:
                raise
            time.sleep(delay)
            delay *= 2
    try:
        yield from r.iter_content(CHUNK)
    finally:
        r.close()


def _file_chunks(path: str):
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            yield chunk


class MRFStream(io.RawIOBase):
    """Readable binary stream over a path or URL. Detects gzip by magic bytes (handles
    multi-member gzip) and passes plain JSON through. `stats` counts bytes as they flow."""

    def __init__(self, src: str, *, timeout: float = 60, retries: int = 4):
        self.src = str(src)
        self.stats = StreamStats()
        self._chunks = _http_chunks(self.src, timeout, retries) if is_url(self.src) else _file_chunks(self.src)
        self._buf = bytearray()
        self._d = None
        self._started = False
        self._eof = False

    def readable(self) -> bool:
        return True

    def _feed(self) -> bool:
        chunk = next(self._chunks, None)
        if chunk is None:
            if self._d is not None:
                out = self._d.flush()
                self.stats.decompressed_bytes += len(out)
                self._buf += out
            self._eof = True
            return False
        self.stats.compressed_bytes += len(chunk)
        if not self._started:
            self._started = True
            if chunk[:2] == GZIP_MAGIC:
                self.stats.gzip = True
                self._d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        if self._d is None:
            out = chunk
        else:
            out = self._d.decompress(chunk)
            while self._d.eof and self._d.unused_data:  # next gzip member
                rest = self._d.unused_data
                self._d = zlib.decompressobj(16 + zlib.MAX_WBITS)
                out += self._d.decompress(rest)
        self.stats.decompressed_bytes += len(out)
        self._buf += out
        return True

    def readinto(self, b) -> int:
        while not self._buf and not self._eof:
            self._feed()
        n = min(len(b), len(self._buf))
        b[:n] = self._buf[:n]
        del self._buf[:n]
        return n

    def close(self) -> None:
        close = getattr(self._chunks, "close", None)
        if close:
            close()
        super().close()


def open_source(src: str, **kw) -> MRFStream:
    return MRFStream(src, **kw)
