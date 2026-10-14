"""Spike: stream a remote gzipped MRF table of contents without downloading it.
HTTP chunks -> zlib gzip decoder -> ijson (yajl2_c) incremental parser. Stops after a time budget."""
import sys, time, zlib, resource, collections, requests, ijson

URL, BUDGET = sys.argv[1], float(sys.argv[2])

class GzStream:
    """File-like object: decompresses an HTTP byte stream on demand (constant memory)."""
    def __init__(self, url):
        self.r = requests.get(url, stream=True, timeout=60)
        self.r.raise_for_status()
        self.it = self.r.iter_content(1 << 20)
        self.d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        self.buf = b""; self.comp = 0; self.raw = 0
    def read(self, n=-1):
        while len(self.buf) < n:
            chunk = next(self.it, None)
            if chunk is None:
                self.buf += self.d.flush(); break
            self.comp += len(chunk)
            out = self.d.decompress(chunk); self.raw += len(out); self.buf += out
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

s = GzStream(URL); t0 = time.time()
n_struct = n_plans = n_files = 0; ca_files = []; markets = collections.Counter(); sample = []
for rs in ijson.items(s, "reporting_structure.item", use_float=True):
    n_struct += 1
    plans = rs.get("reporting_plans", [])
    n_plans += len(plans)
    for p in plans:
        markets[p.get("plan_market_type")] += 1
        if len(sample) < 3: sample.append((p.get("plan_name"), p.get("plan_id_type"), p.get("plan_market_type")))
    for f in rs.get("in_network_files", []):
        n_files += 1
        loc = f.get("location", "")
        if "_CA_" in loc or "/CA" in loc or "california" in (f.get("description") or "").lower():
            if len(ca_files) < 5: ca_files.append((f.get("description"), loc[:140]))
    if time.time() - t0 > BUDGET: break
el = time.time() - t0
rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6  # macOS: bytes
print(f"elapsed {el:.0f}s | compressed read {s.comp/1e6:.0f} MB ({s.comp/1e6/el:.1f} MB/s) | decompressed {s.raw/1e6:.0f} MB")
print(f"reporting_structures {n_struct} | plans {n_plans} | in_network file refs {n_files} | peak RSS {rss:.0f} MB")
print("market types:", dict(markets))
print("sample plans:", sample)
print("CA-looking files:", ca_files)
