# Fresh Linux verification — October 14, 2026

[Successful GitHub-hosted run](https://github.com/Pushary/pushary-python/actions/runs/38579400224), completed at 14:19 UTC (16:19 Europe/Berlin).

Tested source commit: `477d9f0f0b76f569199bcc483ab412279ef97b4b`.

Environment: Ubuntu 24.04 hosted runner; pinned Node v24.3.0 container, Linux x64. Six supplied boundary tests, four supplied recovery cases and seven recovered independent tests passed. Baseline execution was 1,010 ms; the additional test runner reported 1,423.86 ms. These single-run durations are not benchmarks.

The independent journal/trace/receipt verification passed. All 88 manifest-listed vendor files matched before and after execution. The container isolation probe passed. The downloaded pack and raw journals were deleted from the disposable job workspace by the runner's exit cleanup. Only these scoped summaries were retained.

This is a separate Codex-assisted verification run. It does not replace the deleted original macOS traces. Mission subsequently [reported its own native macOS reproduction](https://github.com/langgenius/dify/discussions/42319#discussioncomment-18926607) on October 14; that report does not establish reproduction of our Docker setup or reconstruct these deleted journals. See the [attributed Mission record](../2026-10-14-mission-native/README.md).
