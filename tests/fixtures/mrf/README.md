# MRF parser fixtures

Unmodified example files from the CMS Transparency in Coverage schema repository
(<https://github.com/CMSgov/price-transparency-guide>, public domain, US Government work):

- `cms_v2/`: `examples/` at commit `66898bbd09f00612ffaaac76ea6a740e453904e4` (schema 2.x).
  Provider references are always inline at the top level; `network_name` is required.
- `cms_v1_3_3/`: `examples/` at tag `v1.3.3` (`a019c754bb064692a347d25c9465e3fa2648627c`).
  Schema 1.x allows external provider references (`provider_references[].location`) and
  inline `negotiated_rates[].provider_groups`; there is no `network_name`.

Tests derive extra cases (references after `in_network`, external references actually used
by a rate, gzip) from these files at run time instead of storing edited copies.
