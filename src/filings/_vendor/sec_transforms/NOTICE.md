# Vendored SEC Inline XBRL transforms

`__init__.py` is copied byte-for-byte from Arelle/EDGAR release 25.2.1.1 at immutable commit `47a372d099168f8669d20a8a6bbe5cb16bbf71ac`. Its upstream metadata declares Apache-2.0 and carries the U.S. Government work statement citing 17 U.S.C. 105. The commit's `transform/` source does not include the referenced `COPYRIGHT.md`; no missing copyright notice is inferred or recreated here. The complete Apache-2.0 license text is included as `LICENSE-APACHE-2.0.txt`.

`text2num.py` is also copied byte-for-byte from that commit. Its original file header contains the complete MIT license terms and Copyright (c) 2008 Greg Hewgill; that header is preserved in full.

The immutable source URLs, Git blob IDs, SHA-256 checksums, and license metadata are recorded in `UPSTREAM.json`. The runtime loader verifies these pins before Arelle is given the local plugin path. No plugin source is downloaded at parse time.
