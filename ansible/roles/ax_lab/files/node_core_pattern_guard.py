#!/usr/bin/env python3
"""Preboot sysctl mask for an owned EXISTING AX node; never starts a node.

The Ansible wrapper owns locking and source cleanliness. Apply additionally
requires its installed lock proof. Docker traffic uses the local Unix socket;
no Docker environment, network endpoint, extraction, exec or restart is used.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.client
import io
import json
import os
import posixpath
from pathlib import Path
import re
import socket
import signal
import stat
import subprocess
import tarfile
import time
from urllib.parse import urlencode

OWNER_UID = 0
OWNER_GID = 0
ROOT = Path('/opt/dockerswarm/ax-lab')
CORE = Path('/proc/sys/kernel/core_pattern')
SOCKET = Path('/run/docker.sock')
IMAGE = ('docker.io/kindest/node:v1.37.0@sha256:'
         'a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5')
VENDOR_SHA256 = '3d5c444546b233789f329861de2561afa8cd58340ef70aa81ca0ccbbd5a55068'
BASENAME = '10-coredump-debian.conf'
MASK = (b'# Managed by ax-lab: leave the host core_pattern unchanged.\n'
        b'# Same basename masks /usr/lib/sysctl.d/10-coredump-debian.conf.\n')
DIRECTORIES = ('/etc/sysctl.d', '/run/sysctl.d',
               '/usr/local/lib/sysctl.d', '/usr/lib/sysctl.d')
HEX64 = re.compile(r'[0-9a-f]{64}\Z')
MAX_ARCHIVE = 2 * 1024 * 1024
MAX_FILE = 65536
SIMPLE_SYSCTL_KEY = re.compile(r'[A-Za-z0-9_./-]+\Z')


class GuardError(RuntimeError):
    """Fixed public reason only; do not echo daemon errors or file contents."""


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise GuardError(reason)


def strict_json(raw: bytes) -> dict:
    # Bound nesting before parsing; do not count brackets inside JSON strings.
    depth, quoted, escaped = 0, False, False
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            require(depth <= 32, 'json_depth_exceeded')
        elif byte in (93, 125):
            depth -= 1
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'duplicate_json_key')
            result[key] = value
        return result
    try:
        result = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(
                                GuardError('invalid_json_number')))
    except RecursionError:
        raise GuardError('json_depth_exceeded') from None
    require(type(result) is dict, 'invalid_json_object')
    return result


def identity(s):
    return (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_gid,
            s.st_nlink, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


class SecureRead:
    """Retain every parent and file FD; reject substitutions on revalidation."""
    def __init__(self, path: Path, mode: int, limit: int):
        self.path = path
        self.stack = contextlib.ExitStack()
        self.entries = []
        try:
            require(path.is_absolute(), 'unsafe_host_path')
            parent = None
            parts = ('/', *path.parts[1:-1])
            current = Path('/')
            for part in parts:
                if part != '/':
                    current /= part
                fd = os.open(part, os.O_RDONLY | os.O_CLOEXEC |
                             os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                self.stack.callback(os.close, fd)
                s = os.fstat(fd)
                require(stat.S_ISDIR(s.st_mode) and
                        s.st_uid in (0, OWNER_UID) and
                        s.st_gid in (0, OWNER_GID) and
                        not s.st_mode & 0o022, 'unsafe_host_directory')
                self.entries.append((current, fd, s))
                parent = fd
            fd = os.open(path.name, os.O_RDONLY | os.O_CLOEXEC |
                         os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            self.stack.callback(os.close, fd)
            s = os.fstat(fd)
            require(stat.S_ISREG(s.st_mode) and s.st_uid == OWNER_UID and
                    s.st_gid == OWNER_GID and stat.S_IMODE(s.st_mode) == mode
                    and s.st_nlink == 1 and s.st_size <= limit,
                    'unsafe_host_file')
            self.entries.append((path, fd, s))
            self.data = os.read(fd, limit + 1)
            require(len(self.data) <= limit, 'host_file_too_large')
            require(path == CORE or len(self.data) == s.st_size,
                    'host_file_read_incomplete')
            self.revalidate()
        except BaseException:
            self.stack.close()
            raise

    def revalidate(self):
        for path, fd, before in self.entries:
            after = os.fstat(fd)
            live = path.lstat()
            if stat.S_ISDIR(before.st_mode):
                # Sibling creation can change a directory's timestamps.
                require(identity(after)[:5] == identity(before)[:5] and
                        identity(live)[:5] == identity(before)[:5],
                        'host_directory_changed')
            else:
                require(identity(after) == identity(before) == identity(live),
                        'host_file_changed')

    def close(self):
        self.stack.close()


class Proof:
    def __init__(self, cid: str, digest: str):
        require(bool(HEX64.fullmatch(cid)) and bool(HEX64.fullmatch(digest)),
                'invalid_identity_argument')
        self.cid = cid
        self.stack = contextlib.ExitStack()
        try:
            self.state = SecureRead(ROOT / 'state/cluster.json', 0o600, 8192)
            self.stack.callback(self.state.close)
            self.config = SecureRead(ROOT / 'kind-config.yaml', 0o640, 8192)
            self.stack.callback(self.config.close)
            require(hashlib.sha256(self.config.data).hexdigest() == digest,
                    'kind_config_mismatch')
            expected = dict(schema_version=1, cluster_name='kind',
                            node_container='kind-control-plane', node_image=IMAGE,
                            kind_version='v0.33.0', kind_config_sha256=digest,
                            node_container_id=cid)
            actual = strict_json(self.state.data)
            require(type(actual.get('schema_version')) is int and
                    actual == expected, 'node_ownership_mismatch')
        except BaseException:
            self.stack.close()
            raise

    def revalidate(self):
        self.state.revalidate()
        self.config.revalidate()

    def close(self):
        self.stack.close()


def core_pattern():
    with contextlib.closing(SecureRead(CORE, 0o644, 64)) as source:
        require(source.data == b'|/bin/false\n', 'host_core_pattern_differs')
        source.revalidate()


def directory_archive(raw: bytes):
    """Read metadata/contents in memory; never extract any member to disk."""
    require(len(raw) <= MAX_ARCHIVE, 'archive_too_large')
    files = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as archive:
        members = archive.getmembers()
        require(1 <= len(members) <= 64, 'archive_member_limit')
        top = members[0]
        require(top.name.rstrip('/') == 'sysctl.d' and top.isdir() and
                top.uid == 0 and top.gid == 0 and not top.mode & 0o022,
                'unsafe_container_directory')
        seen = set()
        for member in members:
            name = member.name.rstrip('/')
            require(name not in seen, 'duplicate_archive_member')
            seen.add(name)
            if member is top:
                continue
            require(name.startswith('sysctl.d/') and
                    name.count('/') == 1 and name.split('/')[1] not in ('', '.', '..'),
                    'unsafe_archive_path')
            require(member.isfile() and member.uid == 0 and member.gid == 0
                    and not member.mode & 0o022 and member.size <= MAX_FILE,
                    'unsafe_container_file')
            stream = archive.extractfile(member)
            require(stream is not None, 'missing_archive_file')
            with stream:
                value = stream.read(MAX_FILE + 1)
            require(len(value) == member.size, 'archive_size_mismatch')
            if name.endswith('.conf'):
                files[name.split('/')[1]] = (value, member.mode)
    return files


class UnixConnection(http.client.HTTPConnection):
    def connect(self):
        before = SOCKET.lstat()
        require(stat.S_ISSOCK(before.st_mode) and before.st_uid == 0 and
                not before.st_mode & 0o007, 'unsafe_docker_socket')
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.settimeout(self.timeout)
            self.sock.connect(str(SOCKET))
            require(identity(SOCKET.lstat()) == identity(before),
                    'docker_socket_changed')
        except BaseException:
            self.sock.close()
            self.sock = None
            raise


class Docker:
    def request(self, method, route, data=None, limit=MAX_ARCHIVE):
        connection = UnixConnection('localhost', timeout=15)
        try:
            deadline = time.monotonic() + 15
            connection.request(method, '/v1.45' + route, body=data,
                               headers={'Content-Type': 'application/x-tar'}
                               if data is not None else {})
            transport = connection.sock
            response = connection.getresponse()
            parts, total = [], 0
            while True:
                remaining = deadline - time.monotonic()
                require(remaining > 0, 'docker_read_timeout')
                transport.settimeout(remaining)
                chunk = response.read(min(65536, limit + 1 - total))
                if not chunk:
                    break
                parts.append(chunk)
                total += len(chunk)
                require(total <= limit, 'docker_response_too_large')
            return response.status, b''.join(parts)
        finally:
            connection.close()

    def inspect(self, cid):
        status, raw = self.request('GET', f'/containers/{cid}/json', limit=524288)
        require(status == 200, 'node_inspect_failed')
        # The daemon returns a full document. Never log/retain environment,
        # labels other than these two, mount sources or other unrelated fields.
        document = strict_json(raw)
        require(type(document.get('Mounts')) is list, 'unsafe_container_mounts')
        mounts = []
        destinations = set()
        for member in document['Mounts']:
            require(type(member) is dict and type(member.get('RW')) is bool and
                    type(member.get('Destination')) is str and
                    type(member.get('Type')) is str, 'unsafe_container_mounts')
            require(member['Destination'] not in destinations, 'unsafe_container_mounts')
            destinations.add(member['Destination'])
            mounts.append(dict(destination=member['Destination'],
                               type=member['Type'], readonly=not member['RW']))
        # Docker's inspect Mounts order is not an identity property. Preserve
        # every entry while comparing its destination/type/read-only semantics.
        mounts.sort(key=lambda member: (member['destination'], member['type'],
                                       member['readonly']))
        return {'id': document['Id'], 'name': document['Name'],
                'image': document['Config']['Image'],
                'state': document['State']['Status'],
                'cluster': (document['Config']['Labels'] or {}).get(
                    'io.x-k8s.kind.cluster'),
                'role': (document['Config']['Labels'] or {}).get(
                    'io.x-k8s.kind.role'),
                'restart': document['HostConfig']['RestartPolicy']['Name'],
                'mounts': mounts}

    def directories(self, cid):
        result = {}
        for directory in DIRECTORIES:
            status, raw = self.request('GET', f'/containers/{cid}/archive?' +
                                       urlencode({'path': directory}))
            if status == 404 and directory in DIRECTORIES[1:3]:
                error = strict_json(raw)
                require(error.get('message') ==
                        f'Could not find the file {directory} in container {cid}',
                        'directory_absence_unproved')
                result[directory] = {}
            else:
                require(status == 200, 'sysctl_directory_read_failed')
                result[directory] = directory_archive(raw)
        # Parent metadata: exactly one tar directory header, no body files read.
        for parent in ('/', '/etc'):
            connection = UnixConnection('localhost', timeout=15)
            try:
                connection.request('GET', '/v1.45/containers/' + cid + '/archive?' +
                                   urlencode({'path': parent}))
                response = connection.getresponse()
                require(response.status == 200, 'parent_directory_read_failed')
                header = tarfile.TarInfo.frombuf(response.read(512), 'utf-8', 'strict')
                require(header.isdir() and header.uid == header.gid == 0 and
                        not header.mode & 0o022, 'unsafe_container_parent')
            finally:
                connection.close()
        return result

    def install(self, cid):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w', format=tarfile.USTAR_FORMAT) as archive:
            member = tarfile.TarInfo(BASENAME)
            member.uid = member.gid = 0
            member.mode, member.size, member.mtime = 0o644, len(MASK), 0
            archive.addfile(member, io.BytesIO(MASK))
        status, _ = self.request('PUT', f'/containers/{cid}/archive?' + urlencode(
            {'path': '/etc/sysctl.d', 'noOverwriteDirNonDir': 'true',
             'copyUIDGID': 'true'}), output.getvalue(), limit=4096)
        require(status == 200, 'guard_install_failed')


def validate_node(node, cid):
    require(node['id'] == cid and node['name'] == '/kind-control-plane'
            and node['image'] == IMAGE and node['cluster'] == 'kind'
            and node['role'] == 'control-plane' and node['restart'] == 'no',
            'node_identity_differs')
    require(node['state'] in ('running', 'created', 'exited'), 'node_state_unsafe')
    mounts = node.get('mounts')
    require(type(mounts) is list, 'unsafe_container_mounts')
    for member in mounts:
        require(type(member) is dict and set(member) ==
                {'destination', 'type', 'readonly'} and
                type(member['destination']) is str and
                member['type'] in ('bind', 'volume', 'tmpfs') and
                type(member['readonly']) is bool, 'unsafe_container_mounts')
        destination = member['destination']
        require(destination.startswith('/') and
                posixpath.normpath(destination) == destination and
                not destination.startswith('//') and
                '\x00' not in destination, 'unsafe_container_mounts')
        protected = '/etc/sysctl.d'
        require(destination != '/' and destination != protected and
                not protected.startswith(destination + '/') and
                not destination.startswith(protected + '/'),
                'protected_container_mount')


def configuration_status(directories):
    vendor = directories['/usr/lib/sysctl.d'].get(BASENAME)
    require(vendor is not None and vendor[1] == 0o644 and
            hashlib.sha256(vendor[0]).hexdigest() == VENDOR_SHA256,
            'vendor_config_differs')
    require(all(BASENAME not in directories[directory]
                for directory in DIRECTORIES[1:3]), 'foreign_vendor_shadow')
    mask = directories['/etc/sysctl.d'].get(BASENAME)
    require(mask is None or mask == (MASK, 0o644), 'foreign_mask_file')
    effective = {}
    for directory in reversed(DIRECTORIES):
        effective.update(directories[directory])
    for name, (raw, _) in effective.items():
        if name == BASENAME:
            continue
        for line in raw.decode('utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith(('#', ';')) or '=' not in line:
                continue
            key = line.split('=', 1)[0].strip()
            if key.startswith('-'):
                key = key[1:]  # systemd's single ignore-failure prefix
            # No partial emulation of glibc glob/escape syntax: ambiguous
            # assignments fail closed, including unrelated settings. For
            # simple names, systemd reverses dot/slash only when the first
            # separator is a dot, then simplifies the path.
            require(bool(SIMPLE_SYSCTL_KEY.fullmatch(key)) and
                    key[0] not in '-.' and '..' not in key.split('/'),
                    'ambiguous_sysctl_key')
            first_separator = re.search(r'[./]', key)
            if first_separator is not None and first_separator.group() == '.':
                key = key.translate(str.maketrans('./', '/.'))
            key = posixpath.normpath('/' + key.lstrip('/')).lstrip('/')
            require(key != 'kernel/core_pattern', 'other_core_pattern_assignment')
    return mask is not None


def prove_apply(args):
    require(all(getattr(args, key, None) for key in
                ('operation_id', 'source_revision', 'contract_sha256', 'controller')),
            'apply_operation_proof_required')
    require(bool(HEX64.fullmatch(args.operation_id)) and
            bool(HEX64.fullmatch(args.contract_sha256)) and
            bool(re.fullmatch('[0-9a-f]{40}', args.source_revision)) and
            bool(re.fullmatch('[A-Za-z0-9_.@:-]{1,128}', args.controller)),
            'invalid_operation_identity')
    helper = ROOT / 'bin/ansible-operation-lock.py'
    with contextlib.closing(SecureRead(helper, 0o755, 131072)) as source:
        command = ['/usr/bin/python3', '-I', str(helper), 'prove',
                   '--operation-id', args.operation_id,
                   '--source-revision', args.source_revision,
                   '--contract-sha256', args.contract_sha256,
                   '--controller', args.controller,
                   '--playbook', 'ax-lab', '--profile', 'production',
                   '--mode', 'apply', '--lock-path', '/run/lock/dockerswarm-iac.lock',
                   '--marker-path', '/run/lock/dockerswarm-ansible.marker',
                   '--owner-uid', '1001', '--owner-gid', '1001']
        result = subprocess.run(command, capture_output=True, timeout=15,
                                env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'},
                                check=False)
        source.revalidate()
        require(result.returncode == 0 and result.stdout ==
                f'PROVEN:{args.operation_id}\n'.encode('ascii'),
                'apply_operation_not_proven')


def guard(docker, proof, apply=False, verify_only=False, authorize=None):
    cid = proof.cid
    proof.revalidate()
    core_pattern()
    before = docker.inspect(cid)
    validate_node(before, cid)
    snapshot = docker.directories(cid)
    protected = configuration_status(snapshot)
    if verify_only:
        require(protected, 'node_guard_missing')
    if not protected:
        require(before['state'] in ('created', 'exited'),
                'running_node_requires_reviewed_stop')
    changed = apply and not protected
    # Revalidate retained host proof, node status and sysctl inputs immediately
    # before the only PUT. Cooperative root writers are excluded by the lock.
    proof.revalidate()
    core_pattern()
    require(docker.inspect(cid) == before, 'node_changed_during_observation')
    require(docker.directories(cid) == snapshot, 'sysctl_changed_during_observation')
    if changed:
        require(callable(authorize), 'apply_operation_proof_required')
        authorize()
        docker.install(cid)
    after = docker.inspect(cid)
    require(after == before, 'node_changed_during_observation')
    final = docker.directories(cid)
    expected = {directory: dict(files) for directory, files in snapshot.items()}
    if changed:
        expected['/etc/sysctl.d'][BASENAME] = (MASK, 0o644)
    require(final == expected, 'sysctl_changed_after_install')
    proof.revalidate()
    core_pattern()
    return dict(schema_version=1, protected=configuration_status(final),
                change_required=not configuration_status(final), changed=bool(changed),
                scope='existing_owned_node_only')


def operation_timeout(_signum, _frame):
    raise GuardError('operation_timeout')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('plan', 'apply', 'verify'))
    parser.add_argument('--node-id', required=True)
    parser.add_argument('--kind-config-sha256', required=True)
    for key in ('operation-id', 'source-revision', 'contract-sha256', 'controller'):
        parser.add_argument('--' + key)
    args = parser.parse_args()
    previous_handler = signal.signal(signal.SIGALRM, operation_timeout)
    signal.alarm(120)
    try:
        require(os.geteuid() == 0, 'root_required')
        if args.mode == 'apply':
            prove_apply(args)
        with contextlib.closing(Proof(args.node_id, args.kind_config_sha256)) as proof:
            result = guard(Docker(), proof, args.mode == 'apply', args.mode == 'verify',
                           authorize=lambda: prove_apply(args))
        print(json.dumps(result, sort_keys=True))
        return 0
    except GuardError as error:
        print(json.dumps({'status': 'error', 'reason': str(error)}))
    except (OSError, ValueError, KeyError, TypeError, RecursionError, tarfile.TarError,
            http.client.HTTPException, subprocess.SubprocessError):
        print(json.dumps({'status': 'error', 'reason': 'observation_failed'}))
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
