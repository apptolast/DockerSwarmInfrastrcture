"""Offline safety tests: fake Docker API only; no daemon or host mutations."""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import io
import json
import os
import stat
from pathlib import Path
import tarfile
import tempfile
import unittest

import yaml
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / 'ansible/roles/ax_lab/files/node_core_pattern_guard.py'
spec = importlib.util.spec_from_file_location('ax_core_guard', PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
CID = 'a' * 64


def directories(protected=False):
    result = {directory: {} for directory in m.DIRECTORIES}
    result['/usr/lib/sysctl.d'][m.BASENAME] = (b'kernel.core_pattern=core\n', 0o644)
    result['/etc/sysctl.d']['10-network.conf'] = (b'net.ipv4.ip_forward=1\n', 0o644)
    if protected:
        result['/etc/sysctl.d'][m.BASENAME] = (m.MASK, 0o644)
    return result


class FakeProof:
    cid = CID
    def __init__(self):
        self.reads = 0
        self.fail_at = None

    def revalidate(self):
        self.reads += 1
        if self.reads == self.fail_at:
            raise m.GuardError('host_file_changed')


class FakeDocker:
    def __init__(self, protected=False):
        self.node = dict(id=CID, name='/kind-control-plane', image=m.IMAGE,
                         cluster='kind', role='control-plane', restart='no', state='exited', mounts=[])
        self.files = directories(protected)
        self.puts = []
        self.inspects = self.reads = 0
        self.inspect_mutation = self.read_mutation = None
        self.bad_install = False

    def inspect(self, cid):
        assert cid == CID
        self.inspects += 1
        if self.inspect_mutation:
            self.inspect_mutation(self)
        return copy.deepcopy(self.node)

    def directories(self, cid):
        assert cid == CID
        self.reads += 1
        if self.read_mutation:
            self.read_mutation(self)
        return copy.deepcopy(self.files)

    def install(self, cid):
        self.puts.append(cid)
        self.files['/etc/sysctl.d'][m.BASENAME] = (
            b'kernel.core_pattern=core\n' if self.bad_install else m.MASK, 0o644)


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.core = patch.object(m, 'core_pattern')
        self.core_mock = self.core.start()
        self.addCleanup(self.core.stop)
        self.proof = FakeProof()
        self.docker = FakeDocker()
        self.authorize = unittest.mock.Mock()

    def run_guard(self, *args, **kwargs):
        return m.guard(self.docker, self.proof, *args,
                       authorize=self.authorize, **kwargs)

    def rejects(self, reason, apply=True):
        with self.assertRaisesRegex(m.GuardError, '^' + reason + '$'):
            self.run_guard(apply)

    def test_plan_never_puts_or_changes_inputs(self):
        before = copy.deepcopy(self.docker.files)
        result = self.run_guard()
        self.assertEqual(self.docker.puts, [])
        self.assertEqual(self.docker.files, before)
        self.assertTrue(result['change_required'])
        self.assertFalse(result['protected'])
        self.assertFalse(result['changed'])

    def test_apply_installs_only_exact_mask_and_is_idempotent(self):
        before = copy.deepcopy(self.docker.files)
        result = self.run_guard(True)
        self.assertTrue(result['protected'])
        self.assertTrue(result['changed'])
        self.assertFalse(result['change_required'])
        before['/etc/sysctl.d'][m.BASENAME] = (m.MASK, 0o644)
        self.assertEqual(self.docker.files, before)
        self.assertEqual(self.docker.puts, [CID])
        self.authorize.assert_called_once_with()
        result = self.run_guard(True)
        self.assertFalse(result['changed'])
        self.assertEqual(self.docker.puts, [CID])

    def test_apply_without_final_operation_proof_cannot_put(self):
        with self.assertRaisesRegex(m.GuardError, 'apply_operation_proof_required'):
            m.guard(self.docker, self.proof, apply=True)
        self.assertEqual(self.docker.puts, [])

    def test_lost_operation_proof_cannot_put(self):
        self.authorize.side_effect = m.GuardError('apply_operation_not_proven')
        self.rejects('apply_operation_not_proven')
        self.assertEqual(self.docker.puts, [])

    def test_creation_state_can_be_guarded_without_start(self):
        self.docker.node['state'] = 'created'
        self.run_guard(True)
        self.assertEqual(self.docker.node['state'], 'created')

    def test_running_unprotected_node_rejected_before_put(self):
        self.docker.node['state'] = 'running'
        self.rejects('running_node_requires_reviewed_stop')
        self.assertEqual(self.docker.puts, [])

    def test_running_protected_node_is_readonly(self):
        self.docker.files = directories(True)
        self.docker.node['state'] = 'running'
        result = self.run_guard(True, True)
        self.assertTrue(result['protected'])
        self.assertFalse(result['changed'])
        self.assertEqual(self.docker.puts, [])

    def test_verify_requires_mask(self):
        with self.assertRaisesRegex(m.GuardError, 'node_guard_missing'):
            self.run_guard(verify_only=True)
        self.assertEqual(self.docker.puts, [])

    def test_wrong_node_id_image_labels_name_or_restart_never_put(self):
        for key, value in [('id', 'b' * 64), ('name', '/other'), ('image', 'other'),
                           ('cluster', 'other'), ('role', 'worker'), ('restart', 'always')]:
            with self.subTest(key=key):
                self.docker = FakeDocker()
                self.docker.node[key] = value
                self.rejects('node_identity_differs')
                self.assertEqual(self.docker.puts, [])

    def test_transient_states_never_put(self):
        for state in ('paused', 'restarting', 'dead', 'removing', ''):
            with self.subTest(state=state):
                self.docker.node['state'] = state
                self.rejects('node_state_unsafe')
                self.assertEqual(self.docker.puts, [])

    def test_foreign_mask_content_or_mode_rejected(self):
        for entry in [(b'# unrelated\n', 0o644), (m.MASK, 0o600), (m.MASK, 0o666)]:
            with self.subTest(entry=entry):
                self.docker.files['/etc/sysctl.d'][m.BASENAME] = entry
                self.rejects('foreign_mask_file')
                self.assertEqual(self.docker.puts, [])

    def test_vendor_hash_or_mode_drift_rejected(self):
        for entry in [(b'kernel.core_pattern = core\n', 0o644),
                      (b'kernel.core_pattern=core\n', 0o600)]:
            with self.subTest(entry=entry):
                self.docker.files['/usr/lib/sysctl.d'][m.BASENAME] = entry
                self.rejects('vendor_config_differs')
                self.assertEqual(self.docker.puts, [])

    def test_unknown_same_basename_shadow_rejected(self):
        for directory in m.DIRECTORIES[1:3]:
            with self.subTest(directory=directory):
                self.docker = FakeDocker()
                self.docker.files[directory][m.BASENAME] = (b'# unknown\n', 0o644)
                self.rejects('foreign_vendor_shadow')
                self.assertEqual(self.docker.puts, [])

    def test_other_core_assignments_and_systemd_path_aliases_rejected(self):
        for key in ('kernel.core_pattern', 'kernel/core_pattern', '-kernel.core_pattern',
                    '/kernel/core_pattern', 'kernel//core_pattern',
                    '/kernel/./core_pattern', '//kernel///core_pattern',
                    'kernel..core_pattern'):
            with self.subTest(key=key):
                self.docker = FakeDocker()
                self.docker.files['/etc/sysctl.d']['99-other.conf'] = (
                    f'{key}=unsafe\n'.encode(), 0o644)
                self.rejects('other_core_pattern_assignment')
                self.assertEqual(self.docker.puts, [])

    def test_ambiguous_globs_escapes_and_keys_fail_closed(self):
        for key in ('kernel.*', 'kernel/*', 'kernel.?ore_pattern',
                    'kernel.[c]ore_pattern', '[[:lower:]]ernel/core_pattern',
                    r'\kernel/core_pattern', '[^x]ernel/core_pattern',
                    'unrelated.*', 'net:setting', '/kernel/../kernel/core_pattern',
                    '--kernel.core_pattern', '', '.kernel.core_pattern'):
            with self.subTest(key=key):
                self.docker = FakeDocker()
                self.docker.files['/etc/sysctl.d']['99-other.conf'] = (
                    f'{key}=unsafe\n'.encode(), 0o644)
                self.rejects('ambiguous_sysctl_key')
                self.assertEqual(self.docker.puts, [])

    def test_simple_systemd_interface_keys_remain_supported(self):
        self.docker.files['/etc/sysctl.d']['20-interface.conf'] = (
            b'net.ipv4.conf.enp3s0/200.forwarding=1\n'
            b'net/ipv4/conf/enp3s0.200/forwarding=1\n', 0o644)
        self.run_guard(True)
        self.assertEqual(self.docker.puts, [CID])

    def test_protected_mount_tree_never_puts_even_readonly(self):
        for destination in ('/', '/etc', '/etc/sysctl.d',
                            '/etc/sysctl.d/' + m.BASENAME, '/etc/sysctl.d/subtree'):
            for kind in ('bind', 'volume', 'tmpfs'):
                for readonly in (False, True):
                    with self.subTest(destination=destination, kind=kind, readonly=readonly):
                        self.docker = FakeDocker()
                        self.docker.node['mounts'] = [dict(destination=destination,
                                                         type=kind, readonly=readonly)]
                        self.rejects('protected_container_mount')
                        self.assertEqual(self.docker.puts, [])
                        self.assertEqual(self.docker.reads, 0)

    def test_mount_metadata_unknown_or_noncanonical_is_rejected(self):
        valid = dict(destination='/var', type='volume', readonly=False)
        variants = [None, {}, [None], [dict(valid, readonly=0)],
                    [dict(valid, type='unknown')], [dict(valid, unexpected='value')],
                    [dict(valid, destination='relative')], [dict(valid, destination='//etc')],
                    [dict(valid, destination='/etc//sysctl.d')],
                    [dict(valid, destination='/etc/sysctl.d/../other')],
                    [dict(valid, destination='/var/')], [dict(valid, destination='/var\x00')]]
        for mounts in variants:
            with self.subTest(mounts=mounts):
                self.docker = FakeDocker()
                self.docker.node['mounts'] = mounts
                self.rejects('unsafe_container_mounts')
                self.assertEqual(self.docker.puts, [])
        self.docker = FakeDocker()
        del self.docker.node['mounts']
        self.rejects('unsafe_container_mounts')

    def test_unrelated_mounts_are_allowed_and_remain_unchanged(self):
        self.docker.node['mounts'] = [dict(destination='/lib/modules', type='bind', readonly=True),
                                     dict(destination='/var', type='volume', readonly=False)]
        before = copy.deepcopy(self.docker.node['mounts'])
        self.run_guard(True)
        self.assertEqual(self.docker.puts, [CID])
        self.assertEqual(self.docker.node['mounts'], before)

    def test_mount_change_between_inspections_blocks_before_put(self):
        self.docker.inspect_mutation = lambda d: d.node.update(mounts=[
            dict(destination='/etc', type='bind', readonly=False)]) if d.inspects == 2 else None
        self.rejects('node_changed_during_observation')
        self.assertEqual(self.docker.puts, [])

    def test_lower_priority_shadowed_config_is_not_effective(self):
        self.docker.files['/usr/lib/sysctl.d']['90-other.conf'] = (
            b'kernel.core_pattern=core\n', 0o644)
        self.docker.files['/etc/sysctl.d']['90-other.conf'] = (b'# disabled\n', 0o644)
        self.run_guard(True)
        self.assertEqual(self.docker.puts, [CID])

    def test_host_core_drift_blocks_before_put(self):
        self.core_mock.side_effect = m.GuardError('host_core_pattern_differs')
        self.rejects('host_core_pattern_differs')
        self.assertEqual(self.docker.puts, [])

    def test_host_proof_change_before_put_blocks(self):
        self.proof.fail_at = 2
        self.rejects('host_file_changed')
        self.assertEqual(self.docker.puts, [])

    def test_node_start_between_reads_blocks_before_put(self):
        self.docker.inspect_mutation = lambda d: d.node.update(
            state='running') if d.inspects == 2 else None
        self.rejects('node_changed_during_observation')
        self.assertEqual(self.docker.puts, [])

    def test_sysctl_change_between_reads_blocks_before_put(self):
        self.docker.read_mutation = lambda d: d.files['/etc/sysctl.d'].update(
            {'99-other.conf': (b'# new\n', 0o644)}) if d.reads == 2 else None
        self.rejects('sysctl_changed_during_observation')
        self.assertEqual(self.docker.puts, [])

    def test_corrupted_put_is_not_reported_success(self):
        self.docker.bad_install = True
        self.rejects('sysctl_changed_after_install')
        self.assertEqual(self.docker.puts, [CID])

    def test_unexpected_file_after_put_is_not_reported_success(self):
        self.docker.read_mutation = lambda d: d.files['/etc/sysctl.d'].update(
            {'99-other.conf': (b'# unexpected\n', 0o644)}) if d.reads == 3 else None
        self.rejects('sysctl_changed_after_install')
        self.assertEqual(self.docker.puts, [CID])


class ArchiveTests(unittest.TestCase):
    def archive(self, member=None, duplicate=False, root_mode=0o755):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w') as tf:
            root = tarfile.TarInfo('sysctl.d')
            root.type, root.mode = tarfile.DIRTYPE, root_mode
            tf.addfile(root)
            item = member or tarfile.TarInfo('sysctl.d/a.conf')
            if item.isfile():
                item.size = 4
                tf.addfile(item, io.BytesIO(b'#ok\n'))
            else:
                tf.addfile(item)
            if duplicate:
                tf.addfile(item, io.BytesIO(b'#ok\n') if item.isfile() else None)
        return output.getvalue()

    def test_regular_directory_archive_reads_without_extraction(self):
        self.assertEqual(m.directory_archive(self.archive()), {'a.conf': (b'#ok\n', 0o644)})

    def test_symlink_hardlink_fifo_directory_rejected(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.DIRTYPE):
            with self.subTest(kind=kind):
                item = tarfile.TarInfo('sysctl.d/a.conf')
                item.type, item.linkname = kind, '/tmp/other'
                with self.assertRaises(m.GuardError):
                    m.directory_archive(self.archive(item))

    def test_parent_traversal_absolute_or_nested_members_rejected(self):
        for name in ('/etc/a.conf', 'sysctl.d/../a.conf', 'sysctl.d/..',
                     'sysctl.d/nested/a.conf', 'sysctl.d/.'):
            with self.subTest(name=name):
                with self.assertRaises(m.GuardError):
                    m.directory_archive(self.archive(tarfile.TarInfo(name)))

    def test_duplicate_members_rejected(self):
        with self.assertRaisesRegex(m.GuardError, 'duplicate_archive_member'):
            m.directory_archive(self.archive(duplicate=True))

    def test_wrong_owner_or_writable_file_rejected(self):
        for key, value in [('uid', 1), ('gid', 1), ('mode', 0o666)]:
            item = tarfile.TarInfo('sysctl.d/a.conf')
            setattr(item, key, value)
            with self.subTest(key=key), self.assertRaises(m.GuardError):
                m.directory_archive(self.archive(item))

    def test_group_writable_directory_rejected(self):
        with self.assertRaises(m.GuardError):
            m.directory_archive(self.archive(root_mode=0o775))

    def test_body_limit_rejected(self):
        with self.assertRaisesRegex(m.GuardError, 'archive_too_large'):
            m.directory_archive(b'x' * (m.MAX_ARCHIVE + 1))

    def test_more_than_64_archive_members_rejected(self):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w') as tf:
            root = tarfile.TarInfo('sysctl.d')
            root.type = tarfile.DIRTYPE
            tf.addfile(root)
            for n in range(64):
                item = tarfile.TarInfo('sysctl.d/' + str(n) + '.conf')
                tf.addfile(item)
        with self.assertRaisesRegex(m.GuardError, 'archive_member_limit'):
            m.directory_archive(output.getvalue())

    def test_oversized_file_rejected_even_in_bounded_archive(self):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w') as tf:
            root = tarfile.TarInfo('sysctl.d')
            root.type = tarfile.DIRTYPE
            tf.addfile(root)
            item = tarfile.TarInfo('sysctl.d/a.conf')
            item.size = m.MAX_FILE + 1
            tf.addfile(item, io.BytesIO(b'x' * item.size))
        with self.assertRaisesRegex(m.GuardError, 'unsafe_container_file'):
            m.directory_archive(output.getvalue())

    def test_install_archive_has_exact_single_fixed_regular_member(self):
        docker = m.Docker()
        captured = []
        def request(method, route, data, limit):
            captured.append((method, route, data))
            return 200, b''
        docker.request = request
        docker.install(CID)
        self.assertEqual(captured[0][0], 'PUT')
        self.assertIn('/containers/' + CID + '/archive?', captured[0][1])
        self.assertIn('copyUIDGID=true', captured[0][1])
        self.assertIn('noOverwriteDirNonDir=true', captured[0][1])
        with tarfile.open(fileobj=io.BytesIO(captured[0][2])) as tf:
            self.assertEqual([i.name for i in tf], [m.BASENAME])
            item = tf.getmember(m.BASENAME)
            self.assertTrue(item.isfile())
            self.assertEqual((item.uid, item.gid, item.mode), (0, 0, 0o644))
            with tf.extractfile(item) as stream:
                self.assertEqual(stream.read(), m.MASK)


class SecureProofTests(unittest.TestCase):
    def setUp(self):
        # Trust only this offline fixture's UID/GID; production constants stay 0.
        self.tmp = tempfile.TemporaryDirectory(prefix='ax-guard-test-', dir=Path.home())
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stack = unittest.mock.patch.multiple(m, OWNER_UID=os.getuid(),
                                                  OWNER_GID=os.getgid(), ROOT=self.root)
        self.stack.start()
        self.addCleanup(self.stack.stop)
        (self.root / 'state').mkdir(mode=0o700)
        self.config = self.root / 'kind-config.yaml'
        self.config.write_bytes(b'# pinned kind config\n')
        self.config.chmod(0o640)
        self.digest = hashlib.sha256(self.config.read_bytes()).hexdigest()
        self.state = self.root / 'state/cluster.json'
        self.document = dict(schema_version=1, cluster_name='kind',
                             node_container='kind-control-plane', node_image=m.IMAGE,
                             kind_version='v0.33.0', kind_config_sha256=self.digest,
                             node_container_id=CID)
        self.write_state()

    def write_state(self):
        self.state.write_text(json.dumps(self.document))
        self.state.chmod(0o600)

    def test_retained_proof_and_sibling_creation(self):
        proof = m.Proof(CID, self.digest)
        self.addCleanup(proof.close)
        (self.root / 'unrelated').write_text('x')
        proof.revalidate()

    def test_changed_config_or_state_detected_after_read(self):
        for target in (self.state, self.config):
            with self.subTest(target=target):
                self.write_state()
                self.config.write_bytes(b'# pinned kind config\n')
                proof = m.Proof(CID, self.digest)
                try:
                    target.write_text('changed')
                    with self.assertRaises(m.GuardError):
                        proof.revalidate()
                finally:
                    proof.close()

    def test_symlink_and_hardlinked_state_rejected(self):
        other = self.root / 'other'
        self.state.rename(other)
        for kind in ('symlink', 'hardlink'):
            with self.subTest(kind=kind):
                if kind == 'symlink':
                    self.state.symlink_to(other)
                else:
                    os.link(other, self.state)
                with self.assertRaises((OSError, m.GuardError)):
                    m.Proof(CID, self.digest)
                self.state.unlink()

    def test_group_writable_parent_rejected(self):
        (self.root / 'state').chmod(0o770)
        with self.assertRaisesRegex(m.GuardError, 'unsafe_host_directory'):
            m.Proof(CID, self.digest)

    def test_state_extra_duplicate_bool_version_or_wrong_cid_rejected(self):
        variants = [dict(self.document, unexpected=1), dict(self.document, schema_version=True),
                    dict(self.document, node_container_id='b' * 64)]
        for document in variants:
            with self.subTest(document=document):
                self.state.write_text(json.dumps(document))
                with self.assertRaises(m.GuardError):
                    m.Proof(CID, self.digest)
        self.state.write_text(json.dumps(self.document)[:-1] + ',"schema_version":1}')
        with self.assertRaisesRegex(m.GuardError, 'duplicate_json_key'):
            m.Proof(CID, self.digest)

    def test_wrong_mode_or_config_digest_rejected(self):
        self.config.chmod(0o644)
        with self.assertRaisesRegex(m.GuardError, 'unsafe_host_file'):
            m.Proof(CID, self.digest)
        self.config.chmod(0o640)
        with self.assertRaisesRegex(m.GuardError, 'kind_config_mismatch'):
            m.Proof(CID, 'b' * 64)

    def test_core_pattern_exact_value_and_safe_metadata(self):
        target=self.root/'core_pattern'
        target.write_bytes(b'|/bin/false\n')
        target.chmod(0o644)
        with patch.object(m,'CORE',target):
            m.core_pattern()
            for value in (b'core\n', b'|/bin/false \n', b'|/bin/false\nextra', b'x'*65):
                target.write_bytes(value)
                with self.assertRaises(m.GuardError):
                    m.core_pattern()
            target.write_bytes(b'|/bin/false\n')
            target.chmod(0o666)
            with self.assertRaises(m.GuardError):
                m.core_pattern()

    def test_invalid_id_argument_rejected_without_read(self):
        for value in ('kind-control-plane', 'a' * 12, '../bad', 'A' * 64):
            with self.subTest(value=value), self.assertRaisesRegex(m.GuardError, 'invalid_identity'):
                m.Proof(value, self.digest)


class OperationTests(unittest.TestCase):
    def test_apply_requires_operation_metadata_before_subprocess(self):
        with patch.object(m.subprocess, 'run') as run:
            with self.assertRaisesRegex(m.GuardError, 'apply_operation_proof_required'):
                m.prove_apply(argparse.Namespace())
            run.assert_not_called()

    def test_apply_rejects_invalid_operation_identity(self):
        args = argparse.Namespace(operation_id='a'*64, source_revision='b'*40,
                                  contract_sha256='c'*64, controller='local-admin')
        for key, value in [('operation_id','bad'),('source_revision','../bad'),
                           ('contract_sha256','bad'),('controller','x;uname')]:
            with self.subTest(key=key), patch.object(m.subprocess, 'run') as run:
                bad = argparse.Namespace(**vars(args))
                setattr(bad,key,value)
                with self.assertRaisesRegex(m.GuardError, 'invalid_operation_identity'):
                    m.prove_apply(bad)
                run.assert_not_called()



class FakeResponse:
    def __init__(self, status=200, body=b''):
        self.status = status
        self.body = io.BytesIO(body)

    def read(self, count):
        return self.body.read(count)


class FakeConnection:
    responses = []
    instances = []

    def __init__(self, host, timeout):
        self.host, self.timeout = host, timeout
        self.sock = unittest.mock.Mock()
        self.calls = []
        self.closed = False
        self.instances.append(self)

    def request(self, method, route, **kwargs):
        self.calls.append((method, route, kwargs))

    def getresponse(self):
        return self.responses.pop(0)

    def close(self):
        self.closed = True


class UnixConnectionTests(unittest.TestCase):
    def socket_stat(self, mode=stat.S_IFSOCK | 0o660, uid=0, inode=123):
        return argparse.Namespace(st_dev=1, st_ino=inode, st_mode=mode, st_uid=uid,
                                  st_gid=0, st_nlink=1, st_size=0, st_mtime_ns=1,
                                  st_ctime_ns=1)

    def test_unsafe_socket_metadata_never_connects(self):
        variants = [self.socket_stat(mode=stat.S_IFLNK | 0o660),
                    self.socket_stat(mode=stat.S_IFREG | 0o660),
                    self.socket_stat(uid=1001),
                    self.socket_stat(mode=stat.S_IFSOCK | 0o667)]
        for metadata in variants:
            with self.subTest(metadata=metadata), patch.object(m.Path, 'lstat', return_value=metadata), \
                 patch.object(m.socket, 'socket') as factory:
                connection = m.UnixConnection('localhost', timeout=15)
                with self.assertRaisesRegex(m.GuardError, 'unsafe_docker_socket'):
                    connection.connect()
                factory.assert_not_called()
                self.assertIsNone(connection.sock)

    def test_verified_socket_uses_fixed_unix_path(self):
        metadata = self.socket_stat()
        transport = unittest.mock.Mock()
        with patch.object(m.Path, 'lstat', return_value=metadata), \
             patch.object(m.socket, 'socket', return_value=transport) as factory:
            connection = m.UnixConnection('localhost', timeout=15)
            connection.connect()
            factory.assert_called_once_with(m.socket.AF_UNIX, m.socket.SOCK_STREAM)
            transport.connect.assert_called_once_with('/run/docker.sock')
            transport.settimeout.assert_called_once_with(15)
            connection.close()
            transport.close.assert_called_once_with()

    def test_socket_inode_swap_after_connect_is_rejected_and_closed(self):
        transport = unittest.mock.Mock()
        with patch.object(m.Path, 'lstat', side_effect=[self.socket_stat(), self.socket_stat(inode=124)]), \
             patch.object(m.socket, 'socket', return_value=transport):
            connection = m.UnixConnection('localhost', timeout=15)
            with self.assertRaisesRegex(m.GuardError, 'docker_socket_changed'):
                connection.connect()
            transport.close.assert_called_once_with()
            self.assertIsNone(connection.sock)

    def test_failed_socket_connect_is_closed(self):
        transport = unittest.mock.Mock()
        transport.connect.side_effect = OSError('fixture-private-value')
        with patch.object(m.Path, 'lstat', return_value=self.socket_stat()), \
             patch.object(m.socket, 'socket', return_value=transport):
            connection = m.UnixConnection('localhost', timeout=15)
            with self.assertRaises(OSError):
                connection.connect()
            transport.close.assert_called_once_with()
            self.assertIsNone(connection.sock)


class TransportTests(unittest.TestCase):
    def setUp(self):
        FakeConnection.responses = []
        FakeConnection.instances = []
        self.connection = patch.object(m, 'UnixConnection', FakeConnection)
        self.connection.start()
        self.addCleanup(self.connection.stop)

    def test_request_uses_pinned_api_local_connection_and_closes(self):
        FakeConnection.responses = [FakeResponse(body=b'12345678')]
        with patch.dict(os.environ, {'DOCKER_HOST': 'tcp://untrusted.invalid:2375'}):
            self.assertEqual(m.Docker().request('GET', '/test', limit=8), (200,b'12345678'))
        conn = FakeConnection.instances[0]
        self.assertEqual(conn.host, 'localhost')
        self.assertEqual(conn.calls[0][0:2], ('GET', '/v1.45/test'))
        self.assertTrue(conn.closed)
        conn.sock.settimeout.assert_called()

    def test_oversized_response_rejected_and_closed(self):
        FakeConnection.responses = [FakeResponse(body=b'123456789')]
        with self.assertRaisesRegex(m.GuardError, 'docker_response_too_large'):
            m.Docker().request('GET', '/test', limit=8)
        self.assertTrue(FakeConnection.instances[0].closed)

    def test_elapsed_read_deadline_rejected_and_closed(self):
        FakeConnection.responses = [FakeResponse(body=b'x')]
        with patch.object(m.time, 'monotonic', side_effect=[0,16]):
            with self.assertRaisesRegex(m.GuardError, 'docker_read_timeout'):
                m.Docker().request('GET', '/test')
        self.assertTrue(FakeConnection.instances[0].closed)

    def test_inspect_returns_allowlist_only(self):
        document = dict(Id=CID, Name='/kind-control-plane',
                        Config=dict(Image=m.IMAGE, Env=['PRIVATE=fixture-value'],
                                    Labels={'io.x-k8s.kind.cluster':'kind',
                                            'io.x-k8s.kind.role':'control-plane',
                                            'unrelated':'fixture-value'}),
                        State=dict(Status='exited'), HostConfig=dict(RestartPolicy=dict(Name='no')),
                        Mounts=[dict(Destination='/var', Type='volume', RW=True,
                                     Source='/fixture-private-source')])
        FakeConnection.responses = [FakeResponse(body=json.dumps(document).encode())]
        result = m.Docker().inspect(CID)
        self.assertEqual(set(result), {'id','name','image','state','cluster','role','restart','mounts'})
        self.assertNotIn('fixture-value', json.dumps(result))
        self.assertNotIn('fixture-private-source', json.dumps(result))
        self.assertEqual(result['mounts'], [dict(destination='/var', type='volume', readonly=False)])

    def mount_document(self, mounts):
        return dict(Id=CID, Name='/kind-control-plane',
                    Config=dict(Image=m.IMAGE,
                                Labels={'io.x-k8s.kind.cluster':'kind',
                                        'io.x-k8s.kind.role':'control-plane'}),
                    State=dict(Status='running'), HostConfig=dict(RestartPolicy=dict(Name='no')),
                    Mounts=mounts)

    def mount_fixture(self):
        return [dict(Destination='/var', Type='volume', RW=True),
                dict(Destination='/lib/modules', Type='bind', RW=False)]

    def mount_guard(self, sequences):
        docker = FakeDocker(protected=True)
        docker.inspect = m.Docker().inspect
        FakeConnection.responses = [FakeResponse(body=json.dumps(self.mount_document(mounts)).encode())
                                    for mounts in sequences]
        with patch.object(m, 'core_pattern'):
            result = m.guard(docker, FakeProof(), verify_only=True)
        self.assertEqual(docker.puts, [])
        return result

    def test_inspect_mount_order_is_canonical_without_discarding_entries(self):
        mounts = self.mount_fixture()
        FakeConnection.responses = [FakeResponse(body=json.dumps(self.mount_document(value)).encode())
                                    for value in (mounts, list(reversed(mounts)))]
        first, second = m.Docker().inspect(CID), m.Docker().inspect(CID)
        self.assertEqual(first, second)
        self.assertEqual(len(first['mounts']), len(mounts))
        self.assertEqual(first['mounts'], [dict(destination='/lib/modules', type='bind', readonly=True),
                                         dict(destination='/var', type='volume', readonly=False)])

    def test_guard_accepts_api_mount_order_variation_only(self):
        mounts = self.mount_fixture()
        result = self.mount_guard((mounts, list(reversed(mounts)), mounts))
        self.assertTrue(result['protected'])
        self.assertFalse(result['changed'])

    def test_guard_still_rejects_mount_semantic_and_cardinality_changes(self):
        mounts = self.mount_fixture()
        variants = []
        for key, value in (('RW', False), ('Type', 'bind'), ('Destination', '/other')):
            changed = copy.deepcopy(mounts)
            changed[0][key] = value
            variants.append(changed)
        variants.extend((mounts + [dict(Destination='/data', Type='volume', RW=True)], mounts[1:]))
        for changed in variants:
            with self.subTest(changed=changed):
                docker = FakeDocker(protected=True)
                docker.inspect = m.Docker().inspect
                FakeConnection.responses = [FakeResponse(body=json.dumps(self.mount_document(value)).encode())
                                            for value in (mounts, changed)]
                with patch.object(m, 'core_pattern'):
                    with self.assertRaisesRegex(m.GuardError, '^node_changed_during_observation$'):
                        m.guard(docker, FakeProof(), verify_only=True)
                self.assertEqual(docker.puts, [])

    def test_duplicate_mount_destination_is_ambiguous_even_if_identical(self):
        mount = self.mount_fixture()[0]
        for second in (copy.deepcopy(mount), dict(mount, RW=False), dict(mount, Type='bind')):
            with self.subTest(second=second):
                FakeConnection.responses = [FakeResponse(body=json.dumps(self.mount_document([mount, second])).encode())]
                with self.assertRaisesRegex(m.GuardError, '^unsafe_container_mounts$'):
                    m.Docker().inspect(CID)

    def test_inspect_mount_shape_is_not_silently_coerced(self):
        for mounts in (None, {}, [None], [dict(Destination='/var', Type='volume')],
                       [dict(Destination='/var', Type='volume', RW=0)],
                       [dict(Destination=None, Type='volume', RW=True)],
                       [dict(Destination='/var', Type=None, RW=True)]):
            with self.subTest(mounts=mounts):
                document = dict(Id=CID, Name='/kind-control-plane', Mounts=mounts)
                FakeConnection.responses = [FakeResponse(body=json.dumps(document).encode())]
                with self.assertRaisesRegex(m.GuardError, 'unsafe_container_mounts'):
                    m.Docker().inspect(CID)

    def test_json_depth_counts_structure_only_and_recursion_is_fixed(self):
        raw = json.dumps({'text': '[{\"' * 100}).encode()
        self.assertEqual(m.strict_json(raw), json.loads(raw))
        with patch.object(m.json, 'loads', side_effect=RecursionError('private-detail')):
            with self.assertRaisesRegex(m.GuardError, '^json_depth_exceeded$'):
                m.strict_json(b'{}')

    def test_nested_json_has_fixed_error_and_transport_closes(self):
        FakeConnection.responses = [FakeResponse(body=b'{"nest":' + b'[' * 2000 +
                                                b'0' + b']' * 2000 + b'}')]
        with self.assertRaisesRegex(m.GuardError, 'json_depth_exceeded'):
            m.Docker().inspect(CID)
        self.assertTrue(FakeConnection.instances[0].closed)

    def test_inspect_non200_and_duplicate_json_rejected(self):
        for status, body in [(404, b'{}'),(200,b'{"Id":"one","Id":"two"}')]:
            with self.subTest(status=status):
                FakeConnection.responses = [FakeResponse(status,body)]
                with self.assertRaises(m.GuardError):
                    m.Docker().inspect(CID)

    def directory_setup(self, status=404, message=None, parent_link=False):
        archive = ArchiveTests().archive()
        expected = message if message is not None else (
            'Could not find the file {directory} in container ' + CID)
        def request(method, route):
            self.assertEqual(method, 'GET')
            from urllib.parse import parse_qs,urlsplit
            directory = parse_qs(urlsplit(route).query)['path'][0]
            if directory in m.DIRECTORIES[1:3]:
                return status,json.dumps({'message':expected.format(directory=directory)}).encode()
            return 200,archive
        docker = m.Docker()
        docker.request = request
        header = tarfile.TarInfo('etc')
        header.type, header.mode = (tarfile.SYMTYPE if parent_link else tarfile.DIRTYPE),0o755
        FakeConnection.responses = [FakeResponse(body=header.tobuf()),
                                    FakeResponse(body=header.tobuf())]
        return docker

    def test_missing_optional_directories_require_exact_file_absence(self):
        docker = self.directory_setup()
        result = docker.directories(CID)
        self.assertEqual(result['/run/sysctl.d'], {})
        self.assertEqual(result['/usr/local/lib/sysctl.d'], {})
        self.assertTrue(all(c.closed for c in FakeConnection.instances))
        self.assertTrue(all(call[0]=='GET' for c in FakeConnection.instances for call in c.calls))

    def test_container_absence_or_generic_error_is_not_optional_directory_absence(self):
        for message in ('No such container: '+CID,'unknown','Could not find the file /other in container '+CID):
            with self.subTest(message=message):
                docker = self.directory_setup(message=message)
                with self.assertRaisesRegex(m.GuardError,'directory_absence_unproved'):
                    docker.directories(CID)
        docker = self.directory_setup(status=500)
        with self.assertRaisesRegex(m.GuardError,'sysctl_directory_read_failed'):
            docker.directories(CID)

    def test_parent_symlink_rejected(self):
        with self.assertRaisesRegex(m.GuardError,'unsafe_container_parent'):
            self.directory_setup(parent_link=True).directories(CID)
        self.assertTrue(all(c.closed for c in FakeConnection.instances))

    def test_failed_put_not_reported_success(self):
        docker=m.Docker()
        docker.request=unittest.mock.Mock(return_value=(403,b'{}'))
        with self.assertRaisesRegex(m.GuardError,'guard_install_failed'):
            docker.install(CID)


class FinalOperationProofTests(unittest.TestCase):
    def args(self):
        return argparse.Namespace(operation_id='a'*64,source_revision='b'*40,
                                  contract_sha256='c'*64,controller='admin@local:fixture')

    def test_installed_prover_checks_exact_operation_and_revalidates_file(self):
        source=unittest.mock.Mock()
        args=self.args()
        result=argparse.Namespace(returncode=0,stdout=('PROVEN:'+args.operation_id+'\n').encode())
        with patch.object(m,'SecureRead',return_value=source) as read, \
             patch.object(m.subprocess,'run',return_value=result) as run:
            m.prove_apply(args)
        self.assertEqual(read.call_args.args[1],0o755)
        argv=run.call_args.args[0]
        self.assertEqual(argv[:2],['/usr/bin/python3','-I'])
        for name,value in [('--mode','apply'),('--profile','production'),('--playbook','ax-lab'),
                           ('--owner-uid','1001'),('--owner-gid','1001')]:
            self.assertEqual(argv[argv.index(name)+1],value)
        source.revalidate.assert_called_once_with()
        source.close.assert_called_once_with()

    def test_wrong_proof_stdout_or_failure_denied(self):
        args=self.args()
        for result in [argparse.Namespace(returncode=1,stdout=b''),
                       argparse.Namespace(returncode=0,stdout=b'PROVEN:wrong\n'),
                       argparse.Namespace(returncode=0,stdout=('PROVEN:'+args.operation_id+'\nEXTRA\n').encode())]:
            with self.subTest(result=result), patch.object(m,'SecureRead',return_value=unittest.mock.Mock()), \
                 patch.object(m.subprocess,'run',return_value=result):
                with self.assertRaisesRegex(m.GuardError,'apply_operation_not_proven'):
                    m.prove_apply(args)

    def test_cli_errors_do_not_reflect_private_diagnostics(self):
        output=io.StringIO()
        import sys
        with patch.object(sys,'argv',['guard','plan','--node-id',CID,'--kind-config-sha256','b'*64]), \
             patch.object(m.os,'geteuid',return_value=0), \
             patch.object(m,'Proof',side_effect=OSError('fixture-private-value')), \
             patch('sys.stdout',output):
            self.assertEqual(m.main(),1)
        self.assertEqual(json.loads(output.getvalue()),dict(status='error',reason='observation_failed'))
        self.assertNotIn('fixture-private-value',output.getvalue())

    def test_cli_recursion_error_is_fixed_and_private_details_not_reflected(self):
        import sys
        output = io.StringIO()
        with patch.object(sys, 'argv', ['guard','plan','--node-id',CID,'--kind-config-sha256','b'*64]), \
             patch.object(m.os, 'geteuid', return_value=0), \
             patch.object(m, 'Proof', side_effect=RecursionError('fixture-private-value')), \
             patch('sys.stdout', output):
            self.assertEqual(m.main(), 1)
        self.assertEqual(json.loads(output.getvalue()), dict(status='error', reason='observation_failed'))
        self.assertNotIn('fixture-private-value', output.getvalue())

    def test_cli_deadline_is_cancelled_after_failure(self):
        import sys
        with patch.object(sys,'argv',['guard','plan','--node-id',CID,'--kind-config-sha256','b'*64]), \
             patch.object(m.os,'geteuid',return_value=0), \
             patch.object(m,'Proof',side_effect=m.GuardError('operation_timeout')), \
             patch.object(m.signal,'alarm') as alarm, patch('sys.stdout',io.StringIO()):
            self.assertEqual(m.main(),1)
        self.assertEqual(alarm.call_args_list,[unittest.mock.call(120),unittest.mock.call(0)])

    def test_cli_apply_without_operation_never_contacts_docker(self):
        import sys
        with patch.object(sys,'argv',['guard','apply','--node-id',CID,'--kind-config-sha256','b'*64]), \
             patch.object(m.os,'geteuid',return_value=0), patch.object(m,'Docker') as docker, \
             patch('sys.stdout',io.StringIO()):
            self.assertEqual(m.main(),1)
        docker.assert_not_called()

class AnsibleWiringTests(unittest.TestCase):
    def tasks(self, basename):
        path = PATH.parent.parent / 'tasks' / basename
        return yaml.safe_load(path.read_text())

    def imports(self, tasks):
        return {task['ansible.builtin.import_tasks']: (index, task)
                for index, task in enumerate(tasks) if 'ansible.builtin.import_tasks' in task}

    def test_plan_fresh_creation_refusal_precedes_role_writes(self):
        imports = self.imports(self.tasks('main.yml'))
        plan_index, plan = imports['node_core_pattern_read.yml']
        self.assertLess(imports['inspect.yml'][0], plan_index)
        self.assertNotIn('when', plan)
        for writer in ('host.yml', 'ax_host.yml', 'examples.yml', 'artifacts.yml',
                       'cluster.yml', 'images.yml', 'ax_images.yml', 'node.yml',
                       'substrate.yml', 'ax.yml', 'web.yml'):
            self.assertLess(plan_index, imports[writer][0])
        tasks = self.tasks('node_core_pattern_read.yml')
        self.assertIn('ax_lab_node is not none', tasks[0]['ansible.builtin.assert']['that'])
        self.assertLess(0, next(i for i, t in enumerate(tasks) if 'ansible.builtin.script' in t))
        script = next(t for t in tasks if 'ansible.builtin.script' in t)
        self.assertIn('"plan"', script['ansible.builtin.script']['cmd'])
        self.assertNotIn('"apply"', script['ansible.builtin.script']['cmd'])
        self.assertIs(script['check_mode'], False)
        self.assertIs(script['changed_when'], False)

    def test_check_mode_cannot_enter_guard_apply_or_node_start(self):
        imports = self.imports(self.tasks('main.yml'))
        for writer in ('artifacts.yml', 'cluster.yml', 'node.yml'):
            self.assertEqual(imports[writer][1]['when'], 'not ansible_check_mode')
        node = self.tasks('node.yml')
        self.assertFalse(any(task.get('check_mode') is False for task in node))
        apply = self.tasks('node_core_pattern_apply.yml')
        self.assertFalse(any(task.get('check_mode') is False for task in apply))
        self.assertIn('"apply"', apply[0]['ansible.builtin.script']['cmd'])
        for name in ('operation_lock_guard_operation_id', 'deployment_metadata_source_revision',
                     'deployment_metadata_contract_sha256', 'operation_lock_guard_controller'):
            self.assertIn(name, apply[0]['ansible.builtin.script']['cmd'])

    def test_guard_precedes_owned_id_start_and_poststart_verification(self):
        tasks = self.tasks('node.yml')
        imports = self.imports(tasks)
        guard_index = imports['node_core_pattern_apply.yml'][0]
        start_index = next(i for i,t in enumerate(tasks)
                           if t['name'] == 'Start the lab node when it is stopped')
        self.assertLess(guard_index, start_index)
        start = tasks[start_index]
        self.assertEqual(start['ansible.builtin.command']['argv'],
                         ['/usr/bin/docker', 'start', '{{ ax_lab_node.id }}'])
        self.assertEqual(start['when'], 'ax_lab_node.status in ["created", "exited"]')
        verify_index = next(i for i,t in enumerate(tasks) if
                            t['name'] == 'Verify host hardening and the AX node mask after startup')
        ready_index = next(i for i,t in enumerate(tasks) if
                           t['name'] == 'Wait for the lab node to report Ready')
        self.assertLess(start_index, ready_index)
        self.assertLess(ready_index, verify_index)
        verify = tasks[verify_index]
        self.assertIn('"verify"', verify['ansible.builtin.script']['cmd'])
        self.assertIs(verify['changed_when'], False)
        self.assertNotIn('check_mode', verify)
        next_writer = next(i for i,t in enumerate(tasks) if
                           t['name'] == 'Read proxy_arp inside the lab node')
        self.assertLess(verify_index, next_writer)

if __name__ == '__main__':
    unittest.main()
