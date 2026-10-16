# DUMB 2.23.x workaround — loaded via PYTHONPATH (see dumb-release.yaml).
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
# tolerates setgid. Tracking: kubernetes-lab memory
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
