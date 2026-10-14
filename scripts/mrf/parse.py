"""Streaming filter for Transparency in Coverage in-network rate MRFs (no LLM, no whole-file loads).

One pass over HTTP -> gzip -> ijson events. Two filters run during the stream:

1. `provider_references`: a group is kept only if its `network_name` matches a target network
   (exact, case-insensitive) and at least one of its NPIs is in the target NPI set. Only the
   matching NPIs are kept. Raw TINs never leave this module: they are hashed here.
2. `in_network`: only items whose `billing_code_type` and `billing_code` are targets are built;
   every other item is skipped event by event. Each negotiated price yields one row per kept
   provider group.

Layouts handled: references before `in_network` (common), references after `in_network`
(rates for target codes are spilled to a temp JSONL file and replayed once the references are
known), external references (`provider_references[].location`, schema 1.x; fetched only when a
target-code rate needs them), and inline `negotiated_rates[].provider_groups` (schema 1.x).

Yields ("group", Group) the first time a group is kept, and ("rate", dict) per rate row.
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
from ijson.common import ObjectBuilder

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from stream import open_source, resolve  # noqa: E402

DEFAULT_CODE_TYPES = frozenset({"CPT", "HCPCS"})
HEADER_FIELDS = frozenset({
    "reporting_entity_name", "reporting_entity_type", "issuer_name", "plan_name", "plan_id_type",
    "plan_id", "plan_sponsor_name", "plan_market_type", "last_updated_on", "version",
})
ITEM_FIELDS = ("negotiation_arrangement", "name", "billing_code_type", "billing_code_type_version",
               "billing_code", "description")
_START = ("start_map", "start_array")
_END = ("end_map", "end_array")


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
    section_order: list[str] = field(default_factory=list)
    provider_refs_seen: int = 0
    provider_refs_network_match: int = 0
    provider_refs_kept: int = 0
    external_refs_pending: int = 0
    external_refs_fetched: int = 0
    in_network_items_seen: int = 0
    in_network_items_matched: int = 0
    negotiated_rates_matched: int = 0
    negotiated_prices_matched: int = 0
    rate_rows_emitted: int = 0
    rates_spilled: int = 0
    inline_groups_seen: int = 0
    inline_groups_dropped_no_network: int = 0
    skipped_code_type: int = 0
    compressed_bytes: int = 0
    decompressed_bytes: int = 0
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


def _build(first_event: str, first_value, events) -> object:
    """Build the JSON value that starts with (first_event, first_value) from the event stream."""
    b = ObjectBuilder()
    b.event(first_event, first_value)
    if first_event not in _START:
        return b.value
    depth = 1
    for _, e, v in events:
        b.event(e, v)
        if e in _START:
            depth += 1
        elif e in _END:
            depth -= 1
            if depth == 0:
                break
    return b.value


def _skip(first_event: str, events) -> None:
    if first_event not in _START:
        return
    depth = 1
    for _, e, _v in events:
        if e in _START:
            depth += 1
        elif e in _END:
            depth -= 1
            if depth == 0:
                return


class InNetworkParser:
    def __init__(
        self,
        codes: Iterable[str],
        networks: Iterable[str] | None = None,
        npis: Iterable[str] | None = None,
        code_types: Iterable[str] = DEFAULT_CODE_TYPES,
        opener: Callable = open_source,
        spill_dir: str | None = None,
    ):
        self.codes = {str(c).strip() for c in codes}
        self.networks = None if networks is None else {n.strip().upper() for n in networks}
        self.npis = None if npis is None else {str(n).strip() for n in npis}
        self.code_types = {t.upper() for t in code_types}
        self.opener = opener
        self.spill_dir = spill_dir
        self.header: dict = {}
        self.stats = ParseStats()
        self.kept: dict[str, Group] = {}
        self._emitted_groups: set[str] = set()
        self._pending_external: dict[str, tuple[str, list[str]]] = {}
        self._refs_complete = False

    # ---------- provider groups ----------
    def _network_ok(self, names: list[str] | None) -> bool:
        if self.networks is None:
            return True
        return any(str(n).strip().upper() in self.networks for n in names or [])

    def _members(self, provider_groups: list[dict]) -> list[Member]:
        out: list[Member] = []
        seen = set()
        for pg in provider_groups or []:
            tin_type, tin_hash, last4, bname = hash_tin(pg.get("tin"))
            for raw in pg.get("npi") or []:
                npi = norm_npi(raw)
                if self.npis is not None and npi not in self.npis:
                    continue
                m = Member(npi, tin_type, tin_hash, last4, bname)
                if m not in seen:
                    seen.add(m)
                    out.append(m)
        return out

    def _keep_group(self, gid: str, names: list[str], provider_groups: list[dict], source: str) -> None:
        members = self._members(provider_groups)
        if members:
            self.kept[gid] = Group(gid, list(names or []), members, source)
            self.stats.provider_refs_kept += 1

    def _on_reference(self, ref: dict) -> None:
        self.stats.provider_refs_seen += 1
        names = ref.get("network_name") or []
        if not self._network_ok(names):
            return
        self.stats.provider_refs_network_match += 1
        gid = norm_gid(ref.get("provider_group_id"))
        if ref.get("provider_groups") is not None:
            self._keep_group(gid, names, ref["provider_groups"], "inline_reference")
        elif ref.get("location"):
            self._pending_external[gid] = (ref["location"], list(names))
            self.stats.external_refs_pending += 1

    def _fetch_external(self, gids: set[str], base: str) -> None:
        for gid in sorted(gids):
            if gid not in self._pending_external:
                continue
            location, names = self._pending_external.pop(gid)
            stream = self.opener(resolve(base, location))
            try:
                groups = list(ijson.items(stream, "provider_groups.item", use_float=True))
            finally:
                stream.close()
            self.stats.external_refs_fetched += 1
            self._keep_group(gid, names, groups, "external_reference")

    def _inline_group(self, provider_groups: list[dict]) -> str | None:
        self.stats.inline_groups_seen += 1
        if self.networks is not None:  # inline groups carry no network_name; can't verify network
            self.stats.inline_groups_dropped_no_network += 1
            return None
        members = self._members(provider_groups)
        if not members:
            return None
        key = json.dumps([asdict(m) for m in sorted(members, key=lambda m: (m.npi or "", m.tin_sha256 or ""))])
        gid = "inline:" + hashlib.sha256(key.encode()).hexdigest()[:16]
        if gid not in self.kept:
            self.kept[gid] = Group(gid, [], members, "inline_rate")
        return gid

    def _group_event(self, gid: str):
        if gid not in self._emitted_groups:
            self._emitted_groups.add(gid)
            yield ("group", self.kept[gid])

    # ---------- rates ----------
    def _rows(self, meta: dict, rate: dict, gids: list[str]):
        for gid in gids:
            if gid not in self.kept:
                continue
            yield from self._group_event(gid)
            for p in rate.get("negotiated_prices") or []:
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

    def _on_rate(self, meta: dict, rate: dict, spill):
        self.stats.negotiated_rates_matched += 1
        self.stats.negotiated_prices_matched += len(rate.get("negotiated_prices") or [])
        gids = [norm_gid(g) for g in rate.get("provider_references") or []]
        for pgs in ([rate["provider_groups"]] if rate.get("provider_groups") else []):
            gid = self._inline_group(pgs)
            if gid:
                gids.append(gid)
        ready = [g for g in gids if g in self.kept]
        unresolved = [g for g in gids if g not in self.kept
                      and (g in self._pending_external or not self._refs_complete)]
        yield from self._rows(meta, rate, ready)
        if unresolved:
            self.stats.rates_spilled += 1
            spill.write(json.dumps({"meta": meta, "rate": {"negotiated_prices": rate.get("negotiated_prices")},
                                    "gids": unresolved}) + "\n")

    def _on_item(self, events, spill):
        """Consume one in_network item. Builds only negotiated_rates of target codes."""
        self.stats.in_network_items_seen += 1
        meta: dict = {}
        buffered: list[dict] = []  # only used if billing_code appears after negotiated_rates
        target: bool | None = None
        # Nested values are consumed by _build/_skip, so every event here is at item depth.
        for prefix, e, v in events:
            if e == "map_key":
                continue
            if prefix.startswith("in_network.item."):
                key = prefix[len("in_network.item."):]
                if key in ITEM_FIELDS and e not in _START:
                    meta[key] = v
                    if "billing_code" in meta and "billing_code_type" in meta and target is None:
                        target = self._is_target(meta)
                    continue
                if key == "negotiated_rates" and e == "start_array":
                    yield from self._on_rates_array(events, meta, target, buffered, spill)
                    continue
                if e in _START:
                    _skip(e, events)
                continue
            if e == "end_map" and prefix == "in_network.item":
                break
        if target is None:
            target = self._is_target(meta)
        if target:
            self.stats.in_network_items_matched += 1
            m = self._meta(meta)
            for rate in buffered:
                yield from self._on_rate(m, rate, spill)
        elif meta.get("billing_code_type", "").upper() not in self.code_types:
            self.stats.skipped_code_type += 1

    def _on_rates_array(self, events, meta, target, buffered, spill):
        for prefix, e, v in events:
            if e == "end_array" and prefix.endswith("negotiated_rates"):
                return
            if e == "start_map":
                if target is False:
                    _skip(e, events)
                elif target is True:
                    yield from self._on_rate(self._meta(meta), _build(e, v, events), spill)
                else:
                    buffered.append(_build(e, v, events))

    def _is_target(self, meta: dict) -> bool:
        return (str(meta.get("billing_code_type", "")).upper() in self.code_types
                and str(meta.get("billing_code", "")).strip() in self.codes)

    @staticmethod
    def _meta(meta: dict) -> dict:
        return {
            "billing_code": str(meta.get("billing_code", "")).strip(),
            "billing_code_type": meta.get("billing_code_type"),
            "billing_code_type_version": meta.get("billing_code_type_version"),
            "negotiation_arrangement": meta.get("negotiation_arrangement"),
            "name": meta.get("name"),
        }

    # ---------- driver ----------
    def parse(self, src: str) -> Iterator[tuple[str, object]]:
        t0 = time.time()
        stream = self.opener(src)
        fd, spill_path = tempfile.mkstemp(prefix="mrf_spill_", suffix=".jsonl", dir=self.spill_dir)
        try:
            with os.fdopen(fd, "w") as spill:
                events = ijson.parse(stream, use_float=True)
                for prefix, e, v in events:
                    if prefix in HEADER_FIELDS and e not in _START and e != "map_key":
                        self.header[prefix] = v
                    elif prefix in ("provider_references", "in_network") and e == "start_array":
                        self.stats.section_order.append(prefix)
                    elif prefix == "provider_references" and e == "end_array":
                        self._refs_complete = True
                    elif prefix == "provider_references.item" and e == "start_map":
                        self._on_reference(_build(e, v, events))
                    elif prefix == "in_network.item" and e == "start_map":
                        yield from self._on_item(events, spill)
            self._refs_complete = True
            yield from self._replay(spill_path, src)
        finally:
            stream.close()
            os.unlink(spill_path)
            self.stats.compressed_bytes = stream.stats.compressed_bytes
            self.stats.decompressed_bytes = stream.stats.decompressed_bytes
            self.stats.wall_seconds = round(time.time() - t0, 3)
            self.stats.peak_rss_mb = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)

    def _replay(self, spill_path: str, src: str):
        if not self.stats.rates_spilled:
            return
        needed: set[str] = set()
        with open(spill_path) as f:
            for line in f:
                needed.update(g for g in json.loads(line)["gids"] if g in self._pending_external)
        self._fetch_external(needed, src)
        with open(spill_path) as f:
            for line in f:
                rec = json.loads(line)
                yield from self._rows(rec["meta"], rec["rate"], rec["gids"])


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
