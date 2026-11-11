# DUMB fsGroup workarounds — loaded via PYTHONPATH (see dumb-release.yaml).
# 1) setgid on private directories (2.23.x); 2) group bits on private files.
#
# Kubernetes fsGroup makes the kubelet set S_ISGID on every directory of the
# pod's volumes, so any directory DUMB creates with mode 0o700 inherits setgid
# and comes out 0o2700. DUMB 2.23.0 added ~25 exact-mode safety checks
# (stat.S_IMODE(mode) != 0o700) that then reject it, e.g. Decypharr's
# pre-install: "Unsafe archive workspace ownership or permissions"
# (utils/download.py _ArchiveWorkspace). Setgid grants no access, so for
# PRIVATE directories only (requested mode has no group/other bits) we clear
# the inherited setgid right after creation — restoring exactly the mode DUMB
# asked for. Shared directories keep setgid (normal fsGroup inheritance).
#
# Remove this shim (and its ConfigMap/mount/PYTHONPATH) once upstream DUMB
# tolerates fsGroup's setgid dirs and group-readable files. Tracking: kubernetes-lab memory
# project_dumb_2_23_setgid_archive_workspace_bug.
import importlib.util
import os
import stat
import sys

_real_mkdir = os.mkdir


def _mkdir_clear_inherited_setgid(path, mode=0o777, *, dir_fd=None):
    _real_mkdir(path, mode, dir_fd=dir_fd)
    if mode & 0o077 or mode & stat.S_ISGID:
        return
    try:
        info = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISGID:
            os.chmod(path, stat.S_IMODE(info.st_mode) & ~stat.S_ISGID, dir_fd=dir_fd)
    except OSError:
        pass


os.mkdir = _mkdir_clear_inherited_setgid

# Same root cause, for FILES (seen on the 2026-11-11 dev build): with the
# default fsGroupChangePolicy the kubelet re-adds group rw to every file on
# the pod's volumes at each start, so DUMB's 0600 ownership ledgers (e.g.
# /nzbdav/.dumb-arr-links.json) come back 0660, and utils.private_files.
# read_private_text rejects anything with group/other bits: "Ownership
# metadata must be a bounded private regular file ...". Before each such read
# we strip ONLY the group bits, and only on a plain regular file with one
# link, no other bits and our fsGroup as its group. That restores the 0600
# DUMB itself writes (atomic_write_private_text) and never loosens anything.
import importlib.abc
import importlib.machinery

_FS_GROUP = 1000


def _strip_fsgroup_bits(path):
    try:
        info = os.lstat(path)
    except OSError:
        return
    if (
        stat.S_ISREG(info.st_mode)
        and info.st_nlink == 1
        and info.st_gid == _FS_GROUP
        and info.st_mode & 0o070
        and not info.st_mode & 0o007
    ):
        try:
            os.chmod(path, stat.S_IMODE(info.st_mode) & ~0o070, follow_symlinks=False)
        except (OSError, NotImplementedError):
            pass


def _patch_private_files(module):
    real = getattr(module, "read_private_text", None)
    if real is None or getattr(real, "_fsgroup_shim", False):
        return

    def read_private_text(path, *args, **kwargs):
        _strip_fsgroup_bits(path)
        return real(path, *args, **kwargs)

    read_private_text._fsgroup_shim = True
    read_private_text.__wrapped__ = real
    module.read_private_text = read_private_text


class _PrivateFilesPatcher(importlib.abc.MetaPathFinder):
    # Patch utils.private_files as it loads, before integration_ownership and
    # prowlarr_settings bind read_private_text with `from ... import`.
    def find_spec(self, fullname, path=None, target=None):
        if fullname != "utils.private_files":
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return spec
        real_exec = spec.loader.exec_module

        def exec_module(module):
            real_exec(module)
            _patch_private_files(module)

        spec.loader.exec_module = exec_module
        return spec


sys.meta_path.insert(0, _PrivateFilesPatcher())

# This file shadows the distro's own sitecustomize (Ubuntu ships one at
# /usr/lib/python3.X/sitecustomize.py); chain-load it so its behaviour is kept.
_here = os.path.dirname(os.path.abspath(__file__))
for _entry in sys.path:
    _candidate = os.path.join(_entry or ".", "sitecustomize.py")
    if os.path.dirname(os.path.abspath(_candidate)) == _here or not os.path.isfile(_candidate):
        continue
    try:
        _spec = importlib.util.spec_from_file_location("_distro_sitecustomize", _candidate)
        _spec.loader.exec_module(importlib.util.module_from_spec(_spec))
    except Exception:
        pass
    break
