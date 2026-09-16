"""Windows host-only security primitives. No package code imports this module.

Named objects and persistent secrets use explicit protected DACLs; chmod on
Windows is not an ownership check. APIs are from Microsoft's Win32 SDK.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import os
from pathlib import Path
import stat

if os.name != "nt":
    raise ImportError("Windows security primitives require Windows")

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
adv = ctypes.WinDLL("advapi32", use_last_error=True)
P = ctypes.c_void_p

def _api(lib, name, result, *args):
    function = getattr(lib, name)
    function.restype = result
    function.argtypes = args
    return function

close_handle = _api(k32, "CloseHandle", w.BOOL, w.HANDLE)
_free = _api(k32, "LocalFree", P, P)
_current_process = _api(k32, "GetCurrentProcess", w.HANDLE)
_open_process = _api(k32, "OpenProcess", w.HANDLE, w.DWORD, w.BOOL, w.DWORD)
_open_token = _api(adv, "OpenProcessToken", w.BOOL, w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE))
_token_info = _api(adv, "GetTokenInformation", w.BOOL, w.HANDLE, ctypes.c_int, P, w.DWORD, ctypes.POINTER(w.DWORD))
_sid_text = _api(adv, "ConvertSidToStringSidW", w.BOOL, P, ctypes.POINTER(w.LPWSTR))
_from_sddl = _api(adv, "ConvertStringSecurityDescriptorToSecurityDescriptorW", w.BOOL, w.LPCWSTR, w.DWORD, ctypes.POINTER(P), ctypes.POINTER(w.DWORD))
_set_file = _api(adv, "SetFileSecurityW", w.BOOL, w.LPCWSTR, w.DWORD, P)
_get_named = _api(adv, "GetNamedSecurityInfoW", w.DWORD, w.LPCWSTR, ctypes.c_int, w.DWORD, ctypes.POINTER(P), ctypes.POINTER(P), ctypes.POINTER(P), ctypes.POINTER(P), ctypes.POINTER(P))
_get_ace = _api(adv, "GetAce", w.BOOL, P, w.DWORD, ctypes.POINTER(P))

def checked(ok):
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    return ok

def sid_string(sid) -> str:
    text = w.LPWSTR()
    checked(_sid_text(sid, ctypes.byref(text)))
    try:
        return text.value
    finally:
        _free(text)

def process_sid(pid: int | None = None) -> str:
    process = _current_process() if pid is None else checked(_open_process(0x1000, False, pid))
    token = w.HANDLE()
    try:
        checked(_open_token(process, 8, ctypes.byref(token)))
        length = w.DWORD()
        _token_info(token, 1, None, 0, ctypes.byref(length))
        data = ctypes.create_string_buffer(length.value)
        checked(_token_info(token, 1, data, length, ctypes.byref(length)))
        return sid_string(ctypes.cast(data, ctypes.POINTER(P))[0])
    finally:
        if token:
            close_handle(token)
        if pid is not None:
            close_handle(process)

class SecurityDescriptor:
    def __init__(self, *, directory: bool = False):
        self.pointer = P()
        inheritance = "OICI" if directory else ""
        sid = process_sid()
        checked(_from_sddl(f"O:{sid}D:P(A;{inheritance};FA;;;{sid})(A;{inheritance};FA;;;SY)", 1, ctypes.byref(self.pointer), None))

    def __enter__(self):
        return self.pointer

    def __exit__(self, *_):
        _free(self.pointer)

def ordinary(path: Path, *, directory: bool | None = None):
    info = path.lstat()
    if info.st_file_attributes & 0x400 or stat.S_ISLNK(info.st_mode):
        raise PermissionError("reparse points cannot be trusted private storage")
    if directory is True and not stat.S_ISDIR(info.st_mode):
        raise PermissionError("private directory is not a directory")
    if directory is False and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
        raise PermissionError("private file is not an ordinary single-link file")
    return info

def protect(path: Path, *, directory: bool = False) -> None:
    ordinary(path, directory=directory)
    with SecurityDescriptor(directory=directory) as descriptor:
        checked(_set_file(str(path), 1 | 4 | 0x80000000, descriptor))
    verify(path, directory=directory)

def verify(path: Path, *, directory: bool | None = None, allow_administrators: bool = False) -> None:
    ordinary(path, directory=directory)
    owner, dacl, descriptor = P(), P(), P()
    error = _get_named(str(path), 1, 1 | 4, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if error:
        raise ctypes.WinError(error)
    try:
        owner_sid = process_sid()
        allowed = {owner_sid, "S-1-5-18"}
        if allow_administrators:
            allowed.add("S-1-5-32-544")
        if sid_string(owner) not in allowed or not dacl:
            raise PermissionError("storage owner or DACL is not trusted")
        # ACL header: revision/u8, padding/u8, size/u16, ace_count/u16.
        count = ctypes.c_uint16.from_address(dacl.value + 4).value
        if not count or count > 128:
            raise PermissionError("storage has an empty or unbounded DACL")
        for index in range(count):
            ace = P()
            checked(_get_ace(dacl, index, ctypes.byref(ace)))
            kind = ctypes.c_ubyte.from_address(ace.value).value
            if kind == 1:  # ACCESS_DENIED_ACE cannot grant access.
                continue
            if kind != 0 or sid_string(ace.value + 8) not in allowed:
                raise PermissionError("storage DACL grants another principal access")
    finally:
        _free(descriptor)

def main() -> None:
    import sys
    action, raw_path = sys.argv[1:]
    path = Path(raw_path)
    if action == "protect-directory":
        protect(path, directory=True)
    elif action == "protect-file":
        protect(path)
    elif action == "verify-file":
        verify(path, directory=False)
    else:
        raise ValueError("unknown private-storage operation")

if __name__ == "__main__":
    main()
