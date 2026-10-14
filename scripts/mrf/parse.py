"""Streaming filter for Transparency in Coverage in-network rate MRFs (no LLM, no whole-file loads).

Two streaming passes over HTTP -> gzip -> ijson (C backend, `items_coro`), so Python only
touches one JSON object at a time and the section order in the file does not matter:

  Pass 1 (`in_network`): keep items whose `billing_code_type` and `billing_code` are targets.
         Their negotiated rates are spilled to a temp JSONL file; the provider_group_ids they
         reference become the "needed" set. Inline `negotiated_rates[].provider_groups`
         (schema 1.x) are filtered here.
  Pass 2 (`provider_references`): a group is kept only if it is needed, its `network_name`
         matches a target network (exact, case-insensitive) and at least one of its NPIs is in
         the target NPI set. Only matching NPIs are kept; raw TINs are hashed here and never
         leave this module. External references (`location`, schema 1.x) are fetched only for
         needed groups.
  Replay: spilled rates are emitted once per kept group and negotiated price.

Why two passes: Anthem's provider_references alone are >7 GB decompressed per file and list
~10^8 LA-NPI memberships, almost all for groups no target code uses. Knowing the needed group
IDs first keeps memory flat; the cost is reading the file twice.

Yields ("group", Group) for each kept group, then ("rate", dict) per rate row.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Iterable, Iterator

import ijson

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fastscan  # noqa: E402
from stream import CHUNK, open_source, resolve  # noqa: E402

DEFAULT_CODE_TYPES = frozenset({"CPT", "HCPCS"})
HEADER_FIELDS = frozenset({
    "reporting_entity_name", "reporting_entity_type", "issuer_name", "plan_name", "plan_id_type",
    "plan_id", "plan_sponsor_name", "plan_market_type", "last_updated_on", "version",
})
SECTIONS = ("provider_references", "in_network")


@dataclass(frozen=True)
class Member:
    npi: str | None
    tin_type: str | None
    tin_sha256: str | None
    tin_last4: str | None
    business_name: str | None


@dataclass
class Group:
    provider_group_id: str
    network_names: list[str]
    members: list[Member]
    source: str  # "inline_reference" | "external_reference" | "inline_rate"


@dataclass
class ParseStats:
    first_section: str | None = None
    provider_refs_seen: int = 0
    provider_refs_needed: int = 0
    provider_refs_network_match: int = 0
    provider_refs_kept: int = 0
    external_refs_fetched: int = 0
    in_network_items_seen: int = 0
    in_network_items_matched: int = 0
    negotiated_rates_matched: int = 0
    negotiated_prices_matched: int = 0
    groups_referenced: int = 0
    rate_rows_emitted: int = 0
    inline_groups_seen: int = 0
    inline_groups_dropped_no_network: int = 0
    skipped_code_type: int = 0
    member_rows: int = 0
    pass2_stopped_early: bool = False
    fast_path_rates: bool = False
    fast_path_groups: bool = False
    fast_path_note: str | None = None
    compressed_bytes: int = 0
    decompressed_bytes: int = 0
    pass1_seconds: float = 0.0
    pass2_seconds: float = 0.0
    wall_seconds: float = 0.0
    peak_rss_mb: float = 0.0


def norm_gid(x) -> str:
    if isinstance(x, float) and x.is_integer():
        x = int(x)
    return str(x)


def norm_npi(x) -> str | None:
    if x is None:
        return None
    if isinstance(x, float) and x.is_integer():
        x = int(x)
    s = str(x).strip()
    return None if s in ("", "0") else s


def hash_tin(tin: dict | None) -> tuple[str | None, str | None, str | None, str | None]:
    """Return (type, sha256, last4, business_name). The raw value is discarded."""
    if not tin:
        return None, None, None, None
    raw = "".join(ch for ch in str(tin.get("value") or "") if ch.isalnum())
    if not raw:
        return tin.get("type"), None, None, tin.get("business_name")
    return tin.get("type"), hashlib.sha256(raw.encode()).hexdigest(), raw[-4:], tin.get("business_name")


def rss_mb() -> float:
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)


class InNetworkParser:
    def __init__(
        self,
        codes: Iterable[str],
        networks: Iterable[str] | None = None,
        npis: Iterable[str] | None = None,
        code_types: Iterable[str] = DEFAULT_CODE_TYPES,
        opener: Callable = open_source,
        spill_dir: str | None = None,
        progress: Callable[["InNetworkParser"], None] | None = None,
        progress_every: float = 60,
        fast: bool = True,
    ):
        self.codes = {str(c).strip() for c in codes}
        self.networks = None if networks is None else {n.strip().upper() for n in networks}
        self.npis = None if npis is None else {str(n).strip() for n in npis}
        self._npi_ints = None if npis is None else {int(n) for n in self.npis if n.isdigit()}
        self.code_types = {t.upper() for t in code_types}
        self.opener = opener
        self.spill_dir = spill_dir
        self.progress = progress
        self.progress_every = progress_every
        self.fast = fast
        self._next_tick = 0.0
        self.header: dict = {}
        self.stats = ParseStats()
        self.stream = None  # live MRFStream of the current pass (for progress reporting)
        self.pass_name = ""
        self._done_bytes = [0, 0]
        self._kept: set[str] = set()
        self._needed: set[str] = set()
        self._in_network_offset: int | None = None  # decompressed bytes fed when the first item completed

    # ---------- streaming helpers ----------
    def _objects(self, src: str, prefix: str, stop_after: int | None = None) -> Iterator[dict]:
        """Yield each JSON object at `prefix` (C parser), one at a time. Stop reading once
        `stop_after` decompressed bytes have been fed (used when the section is known to end there)."""
        stream = self.stream = self.opener(src)
        sink = ijson.sendable_list()
        coro = ijson.items_coro(sink, prefix, use_float=True)
        nxt = time.time() + self.progress_every
        try:
            while chunk := stream.read(CHUNK):
                coro.send(chunk)
                yield from sink
                del sink[:]
                if self.progress and time.time() >= nxt:
                    nxt = time.time() + self.progress_every
                    self.progress(self)
                if stop_after is not None and stream.stats.decompressed_bytes >= stop_after:
                    self.stats.pass2_stopped_early = True
                    return
            coro.close()
            yield from sink
        finally:
            stream.close()
            self._done_bytes[0] += stream.stats.compressed_bytes
            self._done_bytes[1] += stream.stats.decompressed_bytes

    def _read_header(self, src: str) -> None:
        """Top-level scalars that precede the first big array (cheap: stops there)."""
        stream = self.opener(src)
        try:
            for prefix, e, v in ijson.parse(stream, use_float=True):
                if prefix in HEADER_FIELDS and e not in ("start_map", "start_array", "map_key"):
                    self.header[prefix] = v
                elif prefix in SECTIONS and e == "start_array":
                    self.stats.first_section = prefix
                    break
        finally:
            stream.close()
            self._done_bytes[0] += stream.stats.compressed_bytes

    # ---------- provider groups ----------
    def _network_ok(self, names) -> bool:
        if self.networks is None:
            return True
        return any(str(n).strip().upper() in self.networks for n in names or [])

    def _matching_npis(self, npis: list) -> list[str]:
        if self._npi_ints is None:
            return [n for n in (norm_npi(x) for x in npis) if n]
        if npis and all(type(x) is int for x in npis):
            return [str(x) for x in self._npi_ints.intersection(npis)]
        return [n for n in (norm_npi(x) for x in npis) if n and n in self.npis]

    def _members(self, provider_groups: list[dict]) -> list[Member]:
        out: list[Member] = []
        seen = set()
        for pg in provider_groups or []:
            hits = self._matching_npis(pg.get("npi") or [])
            if not hits:
                continue
            tin_type, tin_hash, last4, bname = hash_tin(pg.get("tin"))
            for npi in hits:
                m = Member(npi, tin_type, tin_hash, last4, bname)
                if m not in seen:
                    seen.add(m)
                    out.append(m)
        return out

    def _group(self, gid: str, names, provider_groups, source: str) -> Group | None:
        members = self._members(provider_groups)
        if not members:
            return None
        self._kept.add(gid)
        self.stats.provider_refs_kept += source != "inline_rate"
        self.stats.member_rows += len(members)
        return Group(gid, list(names or []), members, source)

    def _inline_group(self, provider_groups: list[dict]) -> tuple[str | None, Group | None]:
        """Return (gid, new Group or None). gid is None if the inline group is filtered out."""
        self.stats.inline_groups_seen += 1
        if self.networks is not None:  # inline groups carry no network_name; can't verify network
            self.stats.inline_groups_dropped_no_network += 1
            return None, None
        members = self._members(provider_groups)
        if not members:
            return None, None
        key = json.dumps([asdict(m) for m in sorted(members, key=lambda m: (m.npi or "", m.tin_sha256 or ""))])
        gid = "inline:" + hashlib.sha256(key.encode()).hexdigest()[:16]
        if gid in self._kept:
            return gid, None
        g = self._group(gid, [], provider_groups, "inline_rate")
        return gid, g

    # ---------- pass 1: in_network ----------
    def _tick(self) -> None:
        if self.progress and time.time() >= self._next_tick:
            self._next_tick = time.time() + self.progress_every
            self.progress(self)

    def _is_target(self, meta: dict) -> bool:
        ctype = str(meta.get("billing_code_type", "")).upper()
        code = str(meta.get("billing_code", "")).strip()
        if ctype not in self.code_types:
            self.stats.skipped_code_type += code in self.codes
            return False
        return code in self.codes

    def _want_item(self, head: bytes):
        self.stats.in_network_items_seen += 1
        self._tick()
        meta = fastscan.head_fields(head, fastscan.ITEM_FIELDS)
        if "billing_code" not in meta or "billing_code_type" not in meta:
            raise fastscan.FastPathUnavailable("billing_code not in the first bytes of an in_network item")
        return meta if self._is_target(meta) else None

    def _rate_items_fast(self, src: str):
        stream = self.stream = self.opener(src)
        try:
            for meta, pieces in fastscan.iter_segments(stream, "in_network", "provider_references", self._want_item):
                if meta:
                    yield meta, fastscan.parse_rates(pieces)
        finally:
            stream.close()
            self._done_bytes[0] += stream.stats.compressed_bytes
            self._done_bytes[1] += stream.stats.decompressed_bytes

    def _rate_items_slow(self, src: str):
        for item in self._objects(src, "in_network.item"):
            if self._in_network_offset is None:
                self._in_network_offset = self.stream.stats.decompressed_bytes
            self.stats.in_network_items_seen += 1
            if self._is_target(item):
                yield item, iter(item.get("negotiated_rates") or [])

    def _with_fallback(self, fast_gen, slow_gen, flag: str):
        """Use the byte-level fast path; if the layout rules it out before anything was
        produced, fall back to the ijson path for this pass."""
        if self.fast:
            produced = False
            try:
                for x in fast_gen():
                    produced = True
                    yield x
                setattr(self.stats, flag, True)
                return
            except fastscan.FastPathUnavailable as e:
                if produced:
                    raise
                self.stats.fast_path_note = str(e)
                self.stats.in_network_items_seen = self.stats.skipped_code_type = 0
                self.stats.provider_refs_seen = 0
        yield from slow_gen()

    def _pass_rates(self, src: str, spill) -> Iterator[tuple[str, object]]:
        self.pass_name = "rates"
        t0 = time.time()
        items = self._with_fallback(lambda: self._rate_items_fast(src), lambda: self._rate_items_slow(src),
                                    "fast_path_rates")
        for item, rates in items:
            self.stats.in_network_items_matched += 1
            meta = {
                "billing_code": str(item.get("billing_code")).strip(),
                "billing_code_type": item.get("billing_code_type"),
                "billing_code_type_version": item.get("billing_code_type_version"),
                "negotiation_arrangement": item.get("negotiation_arrangement"),
                "name": item.get("name"),
            }
            for rate in rates:
                self.stats.negotiated_rates_matched += 1
                prices = rate.get("negotiated_prices") or []
                self.stats.negotiated_prices_matched += len(prices)
                gids = [norm_gid(g) for g in rate.get("provider_references") or []]
                self._needed.update(gids)
                if rate.get("provider_groups"):
                    gid, g = self._inline_group(rate["provider_groups"])
                    if g:
                        yield ("group", g)
                    if gid:
                        gids.append(gid)
                if gids:
                    spill.write(json.dumps({"meta": meta, "prices": prices, "gids": gids}) + "\n")
        self.stats.groups_referenced = len(self._needed)
        self.stats.pass1_seconds = round(time.time() - t0, 3)

    # ---------- pass 2: provider_references ----------
    def _want_ref(self, head: bytes):
        self.stats.provider_refs_seen += 1
        self._tick()
        m = fastscan.GID_FIELD.search(head)
        if not m:
            raise fastscan.FastPathUnavailable("provider_group_id not in the first bytes of a reference")
        gid = norm_gid(json.loads(m.group(1)))
        return gid in self._needed and gid not in self._kept

    def _refs_fast(self, src: str):
        stream = self.stream = self.opener(src)
        try:
            for wanted, pieces in fastscan.iter_segments(stream, "provider_references", "in_network", self._want_ref):
                if wanted:
                    yield fastscan.parse_object(pieces)
        finally:
            stream.close()
            self._done_bytes[0] += stream.stats.compressed_bytes
            self._done_bytes[1] += stream.stats.decompressed_bytes

    def _refs_slow(self, src: str):
        # If references precede in_network, every reference is complete before the byte offset at
        # which pass 1 saw its first in_network item, so the rest of the file need not be read.
        stop = self._in_network_offset if self.stats.first_section == "provider_references" else None
        for ref in self._objects(src, "provider_references.item", stop_after=stop):
            self.stats.provider_refs_seen += 1
            yield ref

    def _pass_groups(self, src: str) -> Iterator[tuple[str, object]]:
        self.pass_name = "groups"
        t0 = time.time()
        refs = self._with_fallback(lambda: self._refs_fast(src), lambda: self._refs_slow(src), "fast_path_groups")
        for ref in refs:
            gid = norm_gid(ref.get("provider_group_id"))
            if gid not in self._needed or gid in self._kept:
                continue
            self.stats.provider_refs_needed += 1
            names = ref.get("network_name") or []
            if not self._network_ok(names):
                continue
            self.stats.provider_refs_network_match += 1
            if ref.get("provider_groups") is not None:
                groups, source = ref["provider_groups"], "inline_reference"
            elif ref.get("location"):
                ext = self.opener(resolve(src, ref["location"]))
                try:
                    groups = list(ijson.items(ext, "provider_groups.item", use_float=True))
                finally:
                    ext.close()
                self.stats.external_refs_fetched += 1
                source = "external_reference"
            else:
                continue
            g = self._group(gid, names, groups, source)
            if g:
                yield ("group", g)
        self.stats.pass2_seconds = round(time.time() - t0, 3)

    # ---------- driver ----------
    def parse(self, src: str) -> Iterator[tuple[str, object]]:
        t0 = time.time()
        fd, spill_path = tempfile.mkstemp(prefix="mrf_spill_", suffix=".jsonl", dir=self.spill_dir)
        try:
            self._read_header(src)
            with os.fdopen(fd, "w") as spill:
                yield from self._pass_rates(src, spill)
            if self._needed - self._kept:
                yield from self._pass_groups(src)
            with open(spill_path) as f:
                for line in f:
                    rec = json.loads(line)
                    yield from self._rows(rec["meta"], rec["prices"], rec["gids"])
        finally:
            os.unlink(spill_path)
            self.stats.compressed_bytes, self.stats.decompressed_bytes = self._done_bytes
            self.stats.wall_seconds = round(time.time() - t0, 3)
            self.stats.peak_rss_mb = rss_mb()

    def _rows(self, meta: dict, prices: list[dict], gids: list[str]):
        for gid in dict.fromkeys(gids):
            if gid not in self._kept:
                continue
            for p in prices:
                self.stats.rate_rows_emitted += 1
                yield ("rate", {
                    **meta,
                    "provider_group_id": gid,
                    "negotiated_type": p.get("negotiated_type"),
                    "negotiated_rate": p.get("negotiated_rate"),
                    "expiration_date": p.get("expiration_date"),
                    "billing_class": p.get("billing_class"),
                    "setting": p.get("setting"),
                    "service_code": list(p.get("service_code") or []),
                    "billing_code_modifier": list(p.get("billing_code_modifier") or []),
                    "additional_information": p.get("additional_information"),
                })


def parse_in_network(src: str, codes, networks=None, npis=None, **kw) -> Iterator[tuple[str, object]]:
    return InNetworkParser(codes, networks, npis, **kw).parse(src)


def _read_npis(path: str) -> set[str]:
    import csv
    with open(path, newline="") as f:
        return {row["npi"].strip() for row in csv.DictReader(f) if row.get("npi")}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Stream-filter one in-network MRF and print counts (never raw content).")
    ap.add_argument("source", help="path or URL (.json or .json.gz)")
    ap.add_argument("--code", action="append", required=True)
    ap.add_argument("--network", action="append", help="exact provider_references[].network_name; repeatable")
    ap.add_argument("--npi-csv", help="CSV with an 'npi' column (e.g. data/mrf/la_npis.csv)")
    ap.add_argument("--sample", type=int, default=3, help="print this many example rows")
    a = ap.parse_args(argv)
    p = InNetworkParser(a.code, a.network, _read_npis(a.npi_csv) if a.npi_csv else None)
    n_groups = n_rates = 0
    for kind, obj in p.parse(a.source):
        if kind == "group":
            n_groups += 1
        else:
            n_rates += 1
            if n_rates <= a.sample:
                print("sample:", {k: obj[k] for k in ("billing_code", "negotiated_type", "negotiated_rate",
                                                       "billing_class", "service_code", "provider_group_id")})
    print("header:", {k: p.header.get(k) for k in ("reporting_entity_name", "version", "last_updated_on")})
    print("groups kept:", n_groups, "| rate rows:", n_rates)
    print("stats:", json.dumps(asdict(p.stats)))


if __name__ == "__main__":
    main()
