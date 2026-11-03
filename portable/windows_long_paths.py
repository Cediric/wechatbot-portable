"""Process-local extended paths for this package's two managed Python venvs.

Installed under a private module name by portable_bootstrap, invoked from its
private .pth. This never adds a directory or changes Windows/Python settings.
"""
from __future__ import annotations

import ntpath
import os
import sys
from importlib.machinery import PathFinder

VENV_NAMES = {"bridge-venv", "astrbot-venv"}


def normal_absolute(value):
    """Return an ordinary absolute drive/UNC path; reject device/relative paths."""
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        return None
    path = value.replace("/", "\\")
    if path.lower().startswith("\\\\?\\unc\\"):
        path = "\\\\" + path[8:]
    elif path.startswith("\\\\?\\"):
        path = path[4:]
    elif path.startswith("\\\\.\\"):
        return None
    drive, tail = ntpath.splitdrive(path)
    drive_root = len(drive) == 2 and drive[0] in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz" and drive[1] == ":"
    unc_root = drive.startswith("\\\\") and len(drive[2:].split("\\")) == 2 and all(drive[2:].split("\\"))
    if not (drive_root or unc_root) or not (tail.startswith("\\") or unc_root and not tail):
        return None
    return ntpath.normpath(path)


def extended_path(value):
    path = normal_absolute(value)
    if path is None:
        return value
    if path.startswith("\\\\"):
        native = "\\\\?\\UNC\\" + path[2:]
    else:
        native = "\\\\?\\" + path
    # Keep valid normalized prefixes intact. Extended paths bypass Windows'
    # normal slash/dot cleanup, so normalize those before attaching the prefix.
    if value.startswith("\\\\?\\") and value.lower() == native.lower():
        return value
    return native


def is_within(value, root):
    path, parent = normal_absolute(value), normal_absolute(root)
    if path is None or parent is None:
        return False
    try:
        return ntpath.normcase(ntpath.commonpath([path, parent])) == ntpath.normcase(parent)
    except ValueError:
        return False


def rewrite_paths(paths, prefix, base_prefix):
    """Preserve order and all unrelated entries, including relative/empty ones."""
    return _rewrite(paths, (prefix, base_prefix))


def _rewrite(paths, roots):
    return [extended_path(value) if any(is_within(value, root) for root in roots) else value for value in paths]


def _owned_crypto_file(value, site_packages, filename):
    expected = ntpath.join(site_packages, "Crypto", "Util", filename)
    path = normal_absolute(value)
    if path is None or ntpath.normcase(path) != ntpath.normcase(expected):
        return False
    try:
        physical = normal_absolute(os.path.realpath(extended_path(path)))
        return physical is not None and ntpath.normcase(physical) == ntpath.normcase(expected)
    except (OSError, ValueError):
        return False


def _adapt_crypto_filesystem(module, site_packages):
    if not _owned_crypto_file(getattr(module, "__file__", None), site_packages, "_file_system.py"):
        return False
    original = getattr(module, "pycryptodome_filename", None)
    if not callable(original):
        return False
    key = ntpath.normcase(site_packages)
    if getattr(original, "_wechatbot_crypto_site", None) == key:
        return True
    if getattr(original, "_wechatbot_crypto_site", None) is not None:
        return False
    crypto_root = ntpath.join(site_packages, "Crypto")

    def normalized_filename(dir_comps, filename):
        value = original(dir_comps, filename)
        return extended_path(value) if is_within(value, crypto_root) else value

    normalized_filename._wechatbot_crypto_site = key
    module.pycryptodome_filename = normalized_filename
    raw_api = sys.modules.get("Crypto.Util._raw_api")
    if (raw_api is not None
            and _owned_crypto_file(getattr(raw_api, "__file__", None), site_packages, "_raw_api.py")
            and getattr(raw_api, "pycryptodome_filename", None) is original):
        raw_api.pycryptodome_filename = normalized_filename
    return True


class _CryptoFilesystemLoader:
    def __init__(self, delegate, site_packages):
        self.delegate = delegate
        self.site_packages = site_packages

    def create_module(self, spec):
        method = getattr(self.delegate, "create_module", None)
        return method(spec) if method is not None else None

    def exec_module(self, module):
        self.delegate.exec_module(module)
        _adapt_crypto_filesystem(module, self.site_packages)


class _CryptoFilesystemFinder:
    def __init__(self, site_packages):
        self.site_packages = site_packages
        self._wechatbot_crypto_site = ntpath.normcase(site_packages)

    def find_spec(self, fullname, path=None, target=None):
        if fullname != "Crypto.Util._file_system":
            return None
        spec = PathFinder.find_spec(fullname, path, target)
        if (spec is None or not _owned_crypto_file(spec.origin, self.site_packages, "_file_system.py")
                or not callable(getattr(spec.loader, "exec_module", None))):
            return None
        spec.loader = _CryptoFilesystemLoader(spec.loader, self.site_packages)
        return spec


def _install_crypto_path_adapter(site_packages):
    # Do not import a foreign Crypto package during interpreter startup. Only
    # the filesystem helper's later, ordinary import is intercepted.
    existing = sys.modules.get("Crypto.Util._file_system")
    if existing is not None:
        _adapt_crypto_filesystem(existing, site_packages)
    key = ntpath.normcase(site_packages)
    if not any(getattr(finder, "_wechatbot_crypto_site", None) == key for finder in sys.meta_path):
        sys.meta_path.insert(0, _CryptoFilesystemFinder(site_packages))


def activate():
    if os.name != "nt":
        return False
    module_path = normal_absolute(__file__)
    prefix = normal_absolute(sys.prefix)
    base = normal_absolute(sys.base_prefix)
    if module_path is None or prefix is None or base is None:
        return False
    site_packages = ntpath.dirname(module_path)
    venv = ntpath.dirname(ntpath.dirname(site_packages))
    runtime = ntpath.dirname(venv)
    portable = ntpath.dirname(runtime)
    if (ntpath.basename(module_path) != "_wechatbot_long_paths.py"
            or ntpath.basename(site_packages).lower() != "site-packages"
            or ntpath.basename(ntpath.dirname(site_packages)).lower() != "lib"
            or ntpath.basename(venv).lower() not in VENV_NAMES
            or ntpath.normcase(venv) != ntpath.normcase(prefix)
            or ntpath.basename(runtime).lower() != "runtime"
            or ntpath.basename(portable).lower() != "portable"):
        return False
    manifest = ntpath.join(portable, "runtime-manifest.json")
    managed_python = ntpath.join(runtime, "python")
    try:
        if not os.path.isfile(extended_path(manifest)):
            return False
        physical_venv = normal_absolute(os.path.realpath(extended_path(venv)))
        physical_base = normal_absolute(os.path.realpath(extended_path(base)))
        physical_managed = normal_absolute(os.path.realpath(extended_path(managed_python)))
        # uv's cpython-3.12 alias is an allowed junction into this same package's
        # managed Python. An external base interpreter is never authorized.
        if (physical_venv is None or ntpath.normcase(physical_venv) != ntpath.normcase(prefix)
                or physical_managed is None or ntpath.normcase(physical_managed) != ntpath.normcase(managed_python)
                or not is_within(base, managed_python) or not is_within(physical_base, physical_managed)):
            return False
    except (OSError, ValueError):
        return False
    sys.path[:] = _rewrite(sys.path, (prefix, base, physical_base))
    # Discard cached normal-path finders so imports use the rewritten entries.
    for entry in list(sys.path_importer_cache):
        if any(is_within(entry, root) for root in (prefix, base, physical_base)):
            sys.path_importer_cache.pop(entry, None)
    _install_crypto_path_adapter(site_packages)
    return True
