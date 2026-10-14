import hashlib
import json
import pathlib
import sys


def verify_results(root: pathlib.Path) -> None:
    result = json.loads((root / 'result.json').read_text())
    assert result['ok'] is True and result['synthetic'] is True
    assert result['packaged_boundary_tests'] == 6
    for name, expected in [('normal', 1), ('lost-response', 1), ('missing-evidence', 1), ('revoked', 0)]:
        case = root / name
        trace = [json.loads(line) for line in (case / 'trace.jsonl').read_text().splitlines()]
        workers = [json.loads(line) for line in (case / 'workers.jsonl').read_text().splitlines()]
        provider = list((case / 'provider/requests').glob('*/*.json'))
        receipts = list((case / 'host/receipts').glob('*/*.json'))
        consumed = list((case / 'host/consumed').glob('*/*.json'))
        assert len(provider) == len(receipts) == len(consumed) == expected, name
        assert sum(row['kind'] == 'dispatch_attempt' for row in trace) == expected, name
        assert sum(row['kind'] == 'effect_committed' for row in trace) == expected, name
        if expected:
            evidence = json.loads(provider[0].read_text())['value']
            receipt = json.loads(receipts[0].read_text())['value']
            digest = hashlib.sha256(json.dumps(evidence['result'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            assert receipt['providerResultHash'] == 'sha256:' + digest
            assert receipt['actionHash'] == evidence['actionHash']
        if name in ['lost-response', 'missing-evidence']:
            killed = [worker for worker in workers if worker['signal'] == 'SIGKILL']
            assert len(killed) == 1 and killed[0]['checkpoint'] == 'effect_committed'
            assert workers[-1]['pid'] != killed[0]['pid']
        print(f'{name}: independent journal, trace and receipt verification passed')


if __name__ == '__main__':
    verify_results(pathlib.Path(sys.argv[1]))
