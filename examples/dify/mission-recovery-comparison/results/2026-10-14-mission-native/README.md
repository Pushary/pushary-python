# Mission-reported native reproduction — October 14, 2026

Source: [gomission’s public report](https://github.com/langgenius/dify/discussions/42319#discussioncomment-18926607), posted at 14:57:45 UTC (16:57:45 Europe/Berlin).

This record attributes findings to Mission. Pushary has not inspected Mission’s retained local raw evidence and does not present this as a new Pushary-run test or a general audit.

| Item | Mission’s report |
| --- | --- |
| Environment | macOS 26.3.1 arm64, Node v22.23.2, native execution with a sanitized environment |
| Input | Fresh extraction of unchanged r2 |
| Additional checks | All seven passed |
| Supplied checks | Six boundary tests and four recovery scenarios passed |
| Baseline evidence verifier | Passed |
| Integrity | Pinned manifest and all 88 listed files matched before and after execution |
| Mismatches | None surfaced within this scope |
| Docker isolation | Not reproduced |
| Retained evidence | Mission reports retaining its local TAP output and raw journals |

Archive SHA-256: `0478a2500e6e6b02804fa4e39ff363ac659928b1bbf8ce2da59425eee5c1b4f9`.

Harness SHA-256: `d209935e4470165d98177e8a37c5320ec1dea5117aaa3310c645d7c458aeb0d3`. Mission confirmed the same hash at tested source `477d9f0f0b76f569199bcc483ab412279ef97b4b` and comparison snapshot `b2faa1135824844870aa0610c5621858d017c429`.

Mission distinguishes the two deterministic callback interleavings from the twelve-worker race across processes on one local filesystem. The result remains bounded by synthetic authority/provider fixtures, single-host storage and no production guarantees. The Pushary comparison continues to reference historical revision `f6759b24e62c2d70425706ee3434fbda98e78503`.

Pushary’s original macOS traces and hosted Linux temporary raw journals were deleted. Public Linux artifacts retain summaries. Mission’s fresh local evidence does not reconstruct those earlier traces. Its confirmation of the comparison column is not agreement to joint publication or distribution; final manuscript review remains pending.
