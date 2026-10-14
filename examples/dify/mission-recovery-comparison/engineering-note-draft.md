# The agent lost the response. Did the action happen?

Draft revised by Pushary after Mission reproduced the additional checks and reviewed its comparison column. This revision awaits final review; joint publication and distribution are not yet agreed. Evidence is dated; this is not a fresh retest of the Pushary example.

A worker can commit an action and die before returning its result. On restart, two questions matter: can another execution be prevented, and can the original outcome be independently confirmed?

An approval answers who may execute which action. A spent permit can stop another attempt. Neither, by itself, proves that the backend committed the first action. Recovery needs evidence from the system that performed it.

## Two runnable examples

Pushary's Dify example commits a local SQLite order and its exact-parameter receipt in the same transaction. A fresh worker reads both back. Matching evidence returns `released` with `recovered: true`, without another permit consumption or order execution. Missing or inconsistent evidence stops for reconciliation. Mission [independently reproduced these checks](https://github.com/langgenius/dify/discussions/42319#discussioncomment-18626578) at [the pinned Pushary revision](https://github.com/Pushary/pushary-python/tree/f6759b24e62c2d70425706ee3434fbda98e78503/examples/dify).

Mission's [r2 offline pack](https://github.com/gomission/trust-graduation/releases/tag/recovery-evaluation-20261014-r2) uses a separate synthetic provider ledger and host receipt journal. After killing the worker at the commit checkpoint, a fresh process looks up provider evidence and writes a matching recovery receipt. Pushary reproduced its four scenarios on macOS arm64 with Node v24.3.0, then ran seven additional checks. [Reported results](https://github.com/langgenius/dify/discussions/42319#discussioncomment-18925125).

| Question | Pushary SQLite example | Mission r2 synthetic pack |
| --- | --- | --- |
| Where is the effect recorded? | SQLite order row | Provider-owned local journal |
| What supports recovery? | Matching order and exact-parameter receipt committed atomically | Matching provider evidence and separately verified host receipt |
| What happens after commit and worker death? | Readback reports the existing order | Lookup confirms the effect and reconstructs the host receipt |
| What if evidence is unavailable? | Stop for reconciliation; no automatic repeat | Report outcome unknown; no automatic repeat |
| What remains outside this proof? | Live Dify/phone recovery and repair of a stranded hosted execution receipt | Real authority/provider integration and concurrent production revocation |

The receipt distinction matters. Pushary's SQLite backend receipt can confirm the order while Pushary's hosted execution receipt remains marked running after a hard kill. Recovering the order does not repair that hosted record. Mission's pack instead tests reconstruction of its local host receipt from the synthetic provider ledger. Those are different storage arrangements, not interchangeable guarantees.

## What the additional checks showed

Against Mission's unchanged candidate, twelve fresh workers competing to resume one operation produced one dispatch and one effect. Wrong or corrupt provider evidence and receipts did not become successful outcomes. Restoring valid evidence allowed recovery without resending. Revocation after an already committed effect did not prevent read-only confirmation or trigger another consumption. Expiry during consumption prevented dispatch.

Two deterministic callback tests also checked stale work: a delayed provider error and a stale negative lookup could not overwrite a newer verified completion. These tests used the real local file store in one process; the concurrent-resume check used separate worker processes.

No counterexample was found within these checks. Both examples use synthetic hosted services or authority/provider fixtures. They do not establish live human authentication, distributed fencing, physical effects, external exactly-once behavior, or an official Dify integration. Pushary’s evaluations were Codex-assisted and are not unaided usability measurements. Mission’s separately reported reproduction is described below.

## Fresh reproduction artifact

The seven-test script was recovered verbatim from the recorded creation command after the original local evaluation files were deleted. A [fresh GitHub-hosted Linux run](https://github.com/Pushary/pushary-python/actions/runs/38579400224) passed against source revision `477d9f0f0b76f569199bcc483ab412279ef97b4b`. The vendor pack remained unchanged; its hashes and the resulting journals were checked independently of the runner summary. [Commands, source and limits](README.md) and [retained result summaries](results/2026-10-14-linux/README.md) accompany this draft.

On October 14, 2026, [Mission reported reproducing the seven checks](https://github.com/langgenius/dify/discussions/42319#discussioncomment-18926607) against a fresh extraction of unchanged r2 using Node v22.23.2 on macOS 26.3.1 arm64, natively with a sanitized environment. All seven checks, the six supplied boundary tests, four recovery scenarios and baseline journal/receipt verifier passed. Mission confirmed that the pinned manifest and all 88 listed files matched before and after execution, and that the harness hash matched both public revisions. They reported no mismatch in this scope. They did not reproduce Pushary’s Docker isolation setup.

Pushary’s original macOS traces and the hosted Linux run’s temporary raw journals were deleted. The public Linux artifacts retain summaries. Mission reports retaining its own fresh local TAP output and raw journals; those records do not reconstruct Pushary’s deleted traces. This draft cites Mission’s public report, not a separate inspection by Pushary of those local records.

## Applying the comparison

For a backend that pauses for a customer's phone approval, keep the approval bound to the exact action and enforce it at the execution boundary. Persist the operation identity before dispatch. After an uncertain response, query the backend's authoritative evidence before reporting success; missing evidence must not silently authorize another attempt.

The [Pushary Dify example](https://github.com/Pushary/pushary-python/tree/f6759b24e62c2d70425706ee3434fbda98e78503/examples/dify) provides an importable workflow and a runnable SQLite demonstration. Its README explains customer phone approval through Pushary Partner and the separate live verification still required. Mission's pinned pack provides the independent synthetic recovery comparison.

## Review before publication

Mission [confirmed that its column matches the bounded evidence](https://github.com/langgenius/dify/discussions/42319#discussioncomment-18926607) and requested the reproduction and evidence-retention updates incorporated here. The historical Pushary pin and the synthetic-authority/provider, single-host and no-production-guarantee limits remain unchanged. The test script and Linux summaries are already public. Final text review is pending; joint publication, attribution and distribution will be agreed separately.
