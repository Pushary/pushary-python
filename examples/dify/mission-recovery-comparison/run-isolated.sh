#!/usr/bin/env bash
set -euo pipefail

comparison_dir=$(cd -- "$(dirname -- "$0")" && pwd)
results_dir=$(realpath -m -- "${1:?Usage: run-isolated.sh NEW_RESULTS_DIRECTORY}")
if [[ -e "$results_dir" ]]; then
  echo 'Results directory must not already exist.' >&2
  exit 1
fi
mkdir -m 700 -p "$results_dir"
evaluation_dir=$(mktemp -d)
trap 'rm -rf -- "$evaluation_dir"' EXIT
image='node:24.3.0-bookworm-slim@sha256:8225b1806c6e37dced949224b5c0d8278a2fe593967288620e0af69b2cbc4539'
archive_url='https://github.com/gomission/trust-graduation/releases/download/recovery-evaluation-20261014-r2/gomission-independent-recovery-20261014-r2.tar.gz'

curl --fail --location --proto '=https' --tlsv1.2 --max-time 60 "$archive_url" -o "$evaluation_dir/candidate.tar.gz"
mkdir "$evaluation_dir/evaluation"
python3 "$comparison_dir/verify-pack.py" extract "$evaluation_dir/candidate.tar.gz" "$evaluation_dir/evaluation" > "$results_dir/integrity-before.txt"
cp "$comparison_dir/independent-tests.mjs" "$evaluation_dir/evaluation/independent-tests.mjs"
printf '%s  %s\n' 'd209935e4470165d98177e8a37c5320ec1dea5117aaa3310c645d7c458aeb0d3' "$evaluation_dir/evaluation/independent-tests.mjs" | sha256sum --check

docker pull "$image"
docker image inspect "$image" --format '{{json .RepoDigests}}' > "$results_dir/runtime-image.json"
container_args=(--rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges --pids-limit 128 --memory 512m --cpus 2 --user "$(id -u):$(id -g)" --tmpfs /tmp:rw,noexec,nosuid,size=64m --mount "type=bind,src=$evaluation_dir/evaluation,dst=/evaluation" --mount "type=bind,src=$evaluation_dir/evaluation/independent-recovery-pack,dst=/evaluation/independent-recovery-pack,readonly" --mount "type=bind,src=$evaluation_dir/evaluation/independent-tests.mjs,dst=/evaluation/independent-tests.mjs,readonly" --workdir /evaluation)

docker run "${container_args[@]}" "$image" node -e 'const fs=require("node:fs"); const os=require("node:os"); const assert=require("node:assert/strict"); assert.ok(!Object.values(os.networkInterfaces()).flat().some(x=>!x.internal)); assert.throws(()=>fs.writeFileSync("/evaluation/independent-recovery-pack/.isolation-probe","blocked"),e=>["EROFS","EACCES"].includes(e.code)); assert.equal(fs.existsSync("/var/run/docker.sock"),false); assert.ok(!Object.keys(process.env).some(k=>/TOKEN|SECRET|KEY|ACTIONS_|GITHUB_/.test(k))); console.log("Non-loopback networking absent; vendor mount read-only; no Docker socket or credential variables.");' > "$results_dir/isolation.txt"

baseline_status=0
docker run "${container_args[@]}" "$image" node independent-recovery-pack/run.mjs --output ./my-first-evaluation > "$results_dir/baseline.stdout" 2> "$results_dir/baseline.stderr" || baseline_status=$?
if [[ -f "$evaluation_dir/evaluation/my-first-evaluation/result.json" ]]; then
  cp "$evaluation_dir/evaluation/my-first-evaluation/result.json" "$results_dir/baseline-result.json"
fi
if [[ -f "$evaluation_dir/evaluation/my-first-evaluation/packaged-boundary.tap" ]]; then
  cp "$evaluation_dir/evaluation/my-first-evaluation/packaged-boundary.tap" "$results_dir/baseline-boundary.tap"
fi
if (( baseline_status != 0 )); then
  echo "Baseline failed with exit $baseline_status" >&2
  exit "$baseline_status"
fi

independent_status=0
docker run "${container_args[@]}" "$image" node --test --test-reporter=tap --test-timeout=30000 independent-tests.mjs > "$results_dir/independent-tests.tap" 2> "$results_dir/independent-tests.stderr" || independent_status=$?
python3 "$comparison_dir/verify-results.py" "$evaluation_dir/evaluation/my-first-evaluation" > "$results_dir/journal-verification.txt"
python3 "$comparison_dir/verify-pack.py" verify "$evaluation_dir/evaluation/independent-recovery-pack" > "$results_dir/integrity-after.txt"
printf '%s\n' "source_commit=$(git -C "$comparison_dir" rev-parse HEAD)" "archive_sha256=0478a2500e6e6b02804fa4e39ff363ac659928b1bbf8ce2da59425eee5c1b4f9" "independent_exit=$independent_status" > "$results_dir/provenance.txt"
exit "$independent_status"
