import hashlib
import json
import pathlib
import sys
import tarfile

ARCHIVE_SHA256 = '0478a2500e6e6b02804fa4e39ff363ac659928b1bbf8ce2da59425eee5c1b4f9'
MANIFEST_SHA256 = 'c344b39b9256529ef20f709fc7176a241a702638e0427654683d48ab45deef48'


def verify_pack(pack: pathlib.Path) -> None:
    manifest_path = pack / 'manifest.json'
    assert not manifest_path.is_symlink()
    manifest_bytes = manifest_path.read_bytes()
    assert hashlib.sha256(manifest_bytes).hexdigest() == MANIFEST_SHA256
    manifest = json.loads(manifest_bytes)
    assert manifest['artifact_id'] == 'gomission-independent-recovery-20261014-r2'
    assert isinstance(manifest['files'], dict) and len(manifest['files']) == 88
    for name, expected in manifest['files'].items():
        relative = pathlib.PurePosixPath(name)
        assert not relative.is_absolute() and '..' not in relative.parts
        source = pack / relative
        assert source.is_file() and not source.is_symlink()
        assert source.resolve().is_relative_to(pack.resolve())
        assert hashlib.sha256(source.read_bytes()).hexdigest() == expected, name
    print('Pinned manifest and all 88 listed files verified.')


def extract_pack(archive: pathlib.Path, destination: pathlib.Path) -> None:
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == ARCHIVE_SHA256
    assert not (destination / 'independent-recovery-pack').exists()
    with tarfile.open(archive) as bundle:
        members = bundle.getmembers()
        names = set()
        for member in members:
            relative = pathlib.PurePosixPath(member.name)
            assert not relative.is_absolute() and '..' not in relative.parts
            assert relative.parts and relative.parts[0] == 'independent-recovery-pack'
            assert member.isfile() or member.isdir()
            assert member.name not in names
            names.add(member.name)
        assert sum(member.size for member in members) < 2_000_000
        assert sum(member.isfile() for member in members) == 89
        bundle.extractall(destination, filter='data')
    verify_pack(destination / 'independent-recovery-pack')


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == 'extract':
        extract_pack(pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]))
    elif len(sys.argv) == 3 and sys.argv[1] == 'verify':
        verify_pack(pathlib.Path(sys.argv[2]))
    else:
        raise SystemExit('Usage: verify-pack.py extract ARCHIVE DESTINATION | verify PACK')
