"""Stream one in-network MRF: read header + provider_references + in_network, filter target codes. Time-budgeted."""
import sys, time, zlib, resource, collections, requests, ijson
URL, BUDGET = sys.argv[1], float(sys.argv[2])
CODES = {"99213","99214","99203","99204","94010","94060","87804","87502","99283","99284","99285","73600","73610","29540","29515","99395"}
r = requests.get(URL, stream=True, timeout=60); it = r.iter_content(1 << 20)
d = zlib.decompressobj(16 + zlib.MAX_WBITS)
class S:
    buf = b""; comp = 0; raw = 0
    def read(self, n):
        while len(self.buf) < n:
            c = next(it, None)
            if c is None: break
            self.comp += len(c); o = d.decompress(c); self.raw += len(o); self.buf += o
        out, self.buf = self.buf[:n], self.buf[n:]; return out
s = S(); t0 = time.time()
networks = collections.Counter(); n_groups = 0; n_items = 0; hits = collections.Counter(); ex = None; order = []
for prefix, event, value in ijson.parse(s, use_float=True):
    if event == "start_array" and prefix in ("provider_references", "in_network"):
        order.append(prefix)
    if prefix == "provider_references.item.network_name.item": networks[value] += 1
    elif prefix == "provider_references.item.provider_group_id": n_groups += 1
    elif prefix == "in_network.item.billing_code":
        n_items += 1
        if value in CODES: hits[value] += 1
    elif prefix == "in_network.item.negotiated_rates.item.negotiated_prices.item.negotiated_rate" and ex is None and hits:
        ex = value
    if time.time() - t0 > BUDGET: break
el = time.time() - t0; rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6
print(f"{URL.rsplit('/',1)[-1]}: {el:.0f}s | compressed {s.comp/1e6:.0f} MB ({s.comp/1e6/el:.1f} MB/s) | decompressed {s.raw/1e6:.0f} MB | peak RSS {rss:.0f} MB")
print("array order:", order, "| provider groups seen:", n_groups, "| in_network items seen:", n_items)
print("networks:", networks.most_common(5))
print("target code hits:", dict(hits), "| example rate:", ex)
