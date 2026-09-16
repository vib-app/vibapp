"""Owner-authenticated, local-only named pipes with bounded byte frames."""
from __future__ import annotations
import ctypes
from ctypes import wintypes as w
import hashlib
import time
from .windows_security import P, k32, _api, checked, close_handle, process_sid, SecurityDescriptor

_create = _api(k32, "CreateNamedPipeW", w.HANDLE, w.LPCWSTR, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, P)
_connect = _api(k32, "ConnectNamedPipe", w.BOOL, w.HANDLE, P)
_disconnect = _api(k32, "DisconnectNamedPipe", w.BOOL, w.HANDLE)
_open = _api(k32, "CreateFileW", w.HANDLE, w.LPCWSTR, w.DWORD, w.DWORD, P, w.DWORD, w.DWORD, w.HANDLE)
_peek = _api(k32, "PeekNamedPipe", w.BOOL, w.HANDLE, P, w.DWORD, P, ctypes.POINTER(w.DWORD), P)
_read = _api(k32, "ReadFile", w.BOOL, w.HANDLE, P, w.DWORD, ctypes.POINTER(w.DWORD), P)
_write = _api(k32, "WriteFile", w.BOOL, w.HANDLE, P, w.DWORD, ctypes.POINTER(w.DWORD), P)
_client_pid = _api(k32, "GetNamedPipeClientProcessId", w.BOOL, w.HANDLE, ctypes.POINTER(w.DWORD))
_server_pid = _api(k32, "GetNamedPipeServerProcessId", w.BOOL, w.HANDLE, ctypes.POINTER(w.DWORD))
_state = _api(k32, "SetNamedPipeHandleState", w.BOOL, w.HANDLE, ctypes.POINTER(w.DWORD), P, P)
INVALID_HANDLE = ctypes.c_void_p(-1).value

class SecurityAttributes(ctypes.Structure):
    _fields_ = [("length", w.DWORD), ("descriptor", P), ("inherit", w.BOOL)]

def pipe_name(endpoint: str) -> str:
    # Endpoint is a root-derived logical pathname, never an arbitrary pipe name.
    digest = hashlib.sha256((process_sid() + "\0" + endpoint.casefold()).encode()).hexdigest()
    return "\\\\.\\pipe\\vibappd-owner-v1-" + digest

class Connection:
    def __init__(self, handle, timeout=5.0):
        self.handle = handle
        self.deadline = time.monotonic() + timeout

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self.handle is not None:
            close_handle(self.handle)
            self.handle = None

    def recv(self, maximum):
        while time.monotonic() < self.deadline:
            available = w.DWORD()
            if not _peek(self.handle, None, 0, None, ctypes.byref(available), None):
                if ctypes.get_last_error() in (109, 232, 233):
                    return b""
                checked(False)
            if available.value:
                buffer = ctypes.create_string_buffer(min(maximum, available.value))
                read = w.DWORD()
                checked(_read(self.handle, buffer, len(buffer), ctypes.byref(read), None))
                return buffer.raw[:read.value]
            time.sleep(0.005)
        raise TimeoutError("named-pipe frame deadline exceeded")

    def sendall(self, data):
        while data and time.monotonic() < self.deadline:
            chunk = data[:4096]
            written = w.DWORD()
            if not _write(self.handle, chunk, len(chunk), ctypes.byref(written), None):
                if ctypes.get_last_error() in (109, 232, 233):
                    raise BrokenPipeError("named-pipe peer disconnected")
                checked(False)
            data = data[written.value:]
            if not written.value:
                time.sleep(0.005)
        if data:
            raise TimeoutError("named-pipe output deadline exceeded")

def _verify_peer(handle, server: bool):
    pid = w.DWORD()
    checked((_server_pid if server else _client_pid)(handle, ctypes.byref(pid)))
    if process_sid(pid.value) != process_sid():
        raise PermissionError("named-pipe peer is not the daemon owner")

def connect(endpoint: str, timeout=5.0) -> Connection:
    deadline = time.monotonic() + timeout
    while True:
        handle = _open(pipe_name(endpoint), 0xC0000000, 0, None, 3, 0x00100000, None)
        if handle != INVALID_HANDLE:
            try:
                _verify_peer(handle, True)
                state = w.DWORD(1)  # nonblocking byte mode, no remote/network path
                checked(_state(handle, ctypes.byref(state), None, None))
                return Connection(handle, timeout)
            except BaseException:
                close_handle(handle)
                raise
        if ctypes.get_last_error() not in (2, 231) or time.monotonic() >= deadline:
            checked(False)
        time.sleep(0.01)

class Listener:
    def __init__(self, endpoint: str):
        self.name = pipe_name(endpoint)
        self.handle = None
        self._new()

    def _new(self):
        with SecurityDescriptor() as descriptor:
            attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
            # FIRST_PIPE_INSTANCE rejects a pre-existing/racing server. REJECT_REMOTE_CLIENTS
            # and an owner-only DACL are independent barriers, then peer SID is rechecked.
            handle = _create(self.name, 3 | 0x00080000, 1 | 8, 1, 65536, 65536, 0, ctypes.byref(attributes))
        if handle == INVALID_HANDLE:
            checked(False)
        self.handle = handle

    def accept(self):
        if self.handle is None:
            self._new()
        if not _connect(self.handle, None):
            error = ctypes.get_last_error()
            if error in (232, 536):
                raise TimeoutError("no pipe client")
            if error != 535:
                checked(False)
        _verify_peer(self.handle, False)
        connection = Connection(self.handle)
        self.handle = None
        return connection, None

    def close(self):
        if self.handle is not None:
            close_handle(self.handle)
            self.handle = None
