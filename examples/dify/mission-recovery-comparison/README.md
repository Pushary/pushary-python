# Independent recovery checks for Mission r2

Seven additional checks reported by Pushary in [Dify discussion #42319](https://github.com/langgenius/dify/discussions/42319#discussioncomment-18925125), prepared for [Mission's reproduction request](https://github.com/langgenius/dify/discussions/42319#discussioncomment-18925551).

This is a standalone evidence branch, not a change to the Python SDK or a Mission integration. The repository's `main` branch remains managed by Pushary's monorepo mirror. Do not merge this branch into the mirror as an SDK feature.

## Provenance

The original pack, temporary runtime, raw evidence and local test script were deleted after the October 14 macOS evaluation. `independent-tests.mjs` was subsequently recovered verbatim from the recorded command that created it, rather than rewritten from the test descriptions. Its SHA-256 is:

```text
d209935e4470165d98177e8a37c5320ec1dea5117aaa3310c645d7c458aeb0d3
```

The original macOS Node v24.3.0 run reported six supplied boundary tests, four supplied recovery scenarios and all seven additional checks passing. Its TAP summaries remain available to the evaluator, but its raw journals were deleted. A new Linux result is a separate evaluation and does not replace the original traces. These evaluations are Codex-assisted, not unaided usability studies or an independent audit by Mission.

## Fresh verification

The [hosted Linux run](https://github.com/Pushary/pushary-python/actions/runs/38579400224) passed against source commit `477d9f0f0b76f569199bcc483ab412279ef97b4b`: six boundary tests, four recovery cases, and seven additional checks. [Versioned results](results/2026-10-14-linux/README.md) include the isolation, integrity and journal checks.

## Pinned input

- [Mission r2 release](https://github.com/gomission/trust-graduation/releases/tag/recovery-evaluation-20261014-r2)
- Archive: `gomission-independent-recovery-20261014-r2.tar.gz`
- Archive SHA-256: `0478a2500e6e6b02804fa4e39ff363ac659928b1bbf8ce2da59425eee5c1b4f9`
- Manifest SHA-256: `c344b39b9256529ef20f709fc7176a241a702638e0427654683d48ab45deef48`
- The archive contains local candidates MCP 0.3.0-beta.4 and core 0.2.0-beta.6, not published npm releases.

No vendor code is included in this branch. No package installation is required.

## Reproduce in an isolated disposable Linux environment

Requires Python 3.12+, curl and Docker. This command downloads the pack and runs it; use a disposable Linux machine, not a machine with customer data. The GitHub workflow uses an ephemeral standard hosted Ubuntu 24.04 runner.

```sh
bash examples/dify/mission-recovery-comparison/run-isolated.sh /tmp/mission-results-NEW
```

The results directory must be new. The runner checks the archive, rejects unsafe archive members, verifies the pinned manifest, and checks the recovered script's hash. Node v24.3.0 is supplied by an immutable container digest. Vendor execution has network disabled, a read-only root and vendor mount, no added capabilities, no new privileges, bounded resources, and no credentials or Docker socket. Only the temporary evaluation directory is writable. The archive and runtime are fetched before entering that execution boundary. The host uploads only summaries after execution.

The pack and independent script run with these commands inside the container:

```sh
node independent-recovery-pack/run.mjs --output ./my-first-evaluation
node --test --test-reporter=tap --test-timeout=30000 independent-tests.mjs
```

The wrapper also independently verifies baseline provider/receipt/consumption journals, trace counts and killed/fresh worker identities. It checks all 88 manifest-listed files after execution and deletes its temporary directory on exit. The workflow retains summary artifacts for 14 days; any checked-in results are separately identified by their source commit and CI run.

## Assertions

| Check | Required observation |
| --- | --- |
| Twelve fresh workers resume one operation concurrently | One dispatch, one durable effect, matching receipt hash |
| Wrong action/input hashes and corrupt provider JSON after commit/crash | Unknown outcome, no receipt or redispatch; valid evidence restores completion |
| Mismatched/corrupt receipt after completion | Unknown outcome, no redispatch; original receipt restores completion |
| Revocation after a committed effect | Read-only completion with one consumption and one effect |
| Expiry during atomic grant consumption | Expired denial with zero provider calls or receipts |
| A late provider failure after another operation confirms completion | Confirmed state and receipt remain intact |
| An older missing-evidence lookup returns after a newer successful lookup | Confirmed state remains intact |

The first four use the pack's worker in fresh processes. The last three invoke unchanged public modules with deterministic synthetic callbacks. The two late-result interleavings use the real local file store within one process. Fixture corruption is an error-handling test, not protection against an attacker controlling trusted storage.

## Limits and comparison

No live Dify/phone-service recovery, production revocation, real human authentication, distributed fencing, power-loss durability or external exactly-once guarantee is established. No installed Mission product or customer adoption is implied. Missing evidence must not authorize another effect. Permission to execute, proof of a committed effect and hosted/workflow status remain separate observations.

The [engineering-note draft](engineering-note-draft.md) compares these results with the [previously tested Pushary SQLite example](https://github.com/Pushary/pushary-python/tree/f6759b24e62c2d70425706ee3434fbda98e78503/examples/dify). Mission has agreed that a note would be useful, but has not reviewed or approved this manuscript. Its reproduction of these seven checks is still pending.
