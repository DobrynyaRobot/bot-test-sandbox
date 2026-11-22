"""Install the exact archived npm engine lock with lifecycle scripts disabled."""

import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from zipfile import ZipFile

ENGINE = Path(__file__).resolve().parent / 'engine'
INSTALL_RECEIPT = '.installed-lock.sha256'


def integrity(data):
    return 'sha512-' + base64.b64encode(hashlib.sha512(data).digest()).decode('ascii')


def configure_offline(lock, archive, staging):
    required = {item['integrity'] for name, item in lock['packages'].items() if name}
    cache = {}
    directory = staging / 'archives'
    directory.mkdir()
    with ZipFile(archive) as source:
        for item in source.infolist():
            if not (item.filename.startswith('audit/AACT/offline_npm/')
                    and item.filename.endswith('.tgz') and item.file_size < 20_000_000):
                continue
            data = source.read(item)
            digest = integrity(data)
            if digest in required and digest not in cache:
                local = directory / f'{len(cache):02}.tgz'
                local.write_bytes(data)
                cache[digest] = local.as_uri()
    for name, item in lock['packages'].items():
        if name:
            digest = item['integrity']
            if digest not in cache:
                raise ValueError(f'Pinned offline archive missing dependency {name}')
            item['resolved'] = cache[digest]


def dependency_tree_sha256(modules):
    digest = hashlib.sha256()
    for path in sorted(modules.rglob('*')):
        relative = path.relative_to(modules).as_posix()
        if relative == INSTALL_RECEIPT:
            continue
        if path.is_symlink():
            kind, value = 'link', os.readlink(path)
        elif path.is_dir():
            kind, value = 'dir', ''
        elif path.is_file():
            kind, value = 'file', hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            raise OSError(f'Unexpected dependency entry: {relative}')
        digest.update(json.dumps((relative, kind, value)).encode('ascii') + b'\n')
    return digest.hexdigest()


def installation_matches(modules, lock_sha256):
    receipt = modules / INSTALL_RECEIPT
    if (modules.is_symlink() or receipt.is_symlink() or not receipt.is_file()
            or not (modules / 'aact/dist/cli/index.mjs').is_file()):
        return False
    try:
        recorded = receipt.read_text(encoding='ascii').splitlines()
        return (len(recorded) == 2 and recorded[0] == lock_sha256
                and recorded[1] == dependency_tree_sha256(modules))
    except (OSError, ValueError):
        return False


def _install_locked(archive):
    node = shutil.which('node')
    npm = shutil.which('npm')
    if not node or not npm:
        raise RuntimeError('Install Node.js 22+ and npm first.')
    lock_bytes = (ENGINE / 'package-lock.json').read_bytes()
    pin = hashlib.sha256(lock_bytes).hexdigest()
    modules = ENGINE / 'node_modules'
    if installation_matches(modules, pin):
        return
    with tempfile.TemporaryDirectory(prefix='.install-', dir=ENGINE) as area:
        staging = Path(area)
        lock = json.loads(lock_bytes)
        argv = [npm, 'ci', '--ignore-scripts', '--no-audit', '--no-fund']
        if archive:
            configure_offline(lock, archive, staging)
            argv.append('--offline')
        shutil.copyfile(ENGINE / 'package.json', staging / 'package.json')
        (staging / 'package-lock.json').write_text(json.dumps(lock), encoding='utf-8')
        environment = {
            'PATH': os.pathsep.join(dict.fromkeys((str(Path(node).parent), str(Path(npm).parent), os.defpath))),
            'NPM_CONFIG_USERCONFIG': str(staging / 'user.npmrc'),
            'NPM_CONFIG_GLOBALCONFIG': str(staging / 'global.npmrc'),
            'LANG': 'C.UTF-8',
            'TZ': 'UTC',
            'NPM_CONFIG_CACHE': str(staging / 'npm-cache'),
        }
        subprocess.run(argv, cwd=staging, env=environment, check=True, timeout=180)
        installed = staging / 'node_modules'
        tree_sha256 = dependency_tree_sha256(installed)
        (installed / INSTALL_RECEIPT).write_text(f'{pin}\n{tree_sha256}\n', encoding='ascii')
        # Keep the previous complete tree until the replacement has been downloaded and verified.
        backup = staging / 'previous'
        if os.path.lexists(modules):
            modules.rename(backup)
        try:
            installed.rename(modules)
        except OSError:
            if os.path.lexists(backup) and not os.path.lexists(modules):
                backup.rename(modules)
            raise


def install(archive=None):
    with (ENGINE / '.engine-install.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _install_locked(archive)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, help='Optional original final-audit ZIP providing all engine tarballs')
    args = parser.parse_args()
    install(args.archive)
    print('AACT 3.0.1 and YAML parser installed from the pinned lock.')
