"""Collect unique CA in-network file URLs from the streamed Anthem TOC within a time budget."""
import sys, time, zlib, requests, ijson
URL, BUDGET = sys.argv[1], float(sys.argv[2])
r = requests.get(URL, stream=True, timeout=60); it = r.iter_content(1 << 20)
d = zlib.decompressobj(16 + zlib.MAX_WBITS)
class S:
    buf = b""; comp = 0
    def read(self, n):
        while len(self.buf) < n:
            c = next(it, None)
            if c is None: break
            self.comp += len(c); self.buf += d.decompress(c)
        out, self.buf = self.buf[:n], self.buf[n:]; return out
s = S(); t0 = time.time(); urls = {}
for f in ijson.items(s, "reporting_structure.item.in_network_files.item"):
    loc = f.get("location", "")
    name = loc.rsplit("/", 1)[-1]
    if name.startswith("CA_"): urls[name] = loc
    if time.time() - t0 > BUDGET: break
print(f"scanned {s.comp/1e9:.2f} GB compressed in {time.time()-t0:.0f}s; unique CA files: {len(urls)}")
open("/tmp/mrfspike/work/ca_urls.txt", "w").write("\n".join(sorted(urls.values())))
