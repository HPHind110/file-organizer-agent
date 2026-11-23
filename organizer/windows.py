"""Windows handle operations. Keep the exact file open across verify and rename."""
from contextlib import ExitStack, contextmanager
import ctypes as ct
from ctypes import wintypes as wt
import hashlib
import msvcrt
import os
from pathlib import Path
import stat

kernel = ct.WinDLL("kernel32", use_last_error=True)

def api(name, result, *args):
    fn = getattr(kernel, name)
    fn.restype, fn.argtypes = result, args
    return fn

open_handle = api("CreateFileW", wt.HANDLE, wt.LPCWSTR, wt.DWORD, wt.DWORD,
                  wt.LPVOID, wt.DWORD, wt.DWORD, wt.HANDLE)
close_handle = api("CloseHandle", wt.BOOL, wt.HANDLE)
drive_type = api("GetDriveTypeW", wt.UINT, wt.LPCWSTR)
set_info = api("SetFileInformationByHandle", wt.BOOL, wt.HANDLE, ct.c_int,
               wt.LPVOID, wt.DWORD)
READ, DELETE, ATTRIBUTES = 0x80000000, 0x10000, 0x80
SHARE_READ, SHARE_WRITE = 1, 2
OPEN_EXISTING, OPEN_REPARSE, DIRECTORY = 3, 0x00200000, 0x02000000
INVALID = ct.c_void_p(-1).value

class SafetyError(ValueError):
    pass

def acquire(path, access, share, flags=OPEN_REPARSE):
    handle = open_handle(str(path), access, share, None, OPEN_EXISTING, flags, None)
    if handle == INVALID:
        raise ct.WinError(ct.get_last_error())
    return handle

def component(name):
    if (not name or name in (".", "..") or os.path.isreserved(name)
            or any(c in name for c in "/\\:") or name.endswith((" ", "."))):
        raise SafetyError(f"Unsafe path component {name!r}")
    return name

def plain(path, *, directory):
    info = path.lstat()
    if info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise SafetyError(f"Link or reparse point: {path}")
    wanted = stat.S_ISDIR if directory else stat.S_ISREG
    if not wanted(info.st_mode):
        raise SafetyError(f"Unexpected file type: {path}")
    return info

def select_root(value):
    path = Path(os.path.abspath(value))
    if len(path.drive) != 2 or path == Path(path.anchor):
        raise SafetyError("Select a local folder below a drive root.")
    if drive_type(path.anchor) not in (2, 3, 6):  # removable, fixed, RAM disk
        raise SafetyError("Select a local writable drive. Network drives are unsupported.")
    for part in path.parts[1:]:
        component(part)
    return path

@contextmanager
def pin_directory(path):
    # Denying delete sharing keeps the opened directory from being moved.
    handle = acquire(path, ATTRIBUTES, SHARE_READ | SHARE_WRITE, OPEN_REPARSE | DIRECTORY)
    try:
        plain(path, directory=True)
        yield
    finally:
        close_handle(handle)

@contextmanager
def pin_tree(path):
    with ExitStack() as stack:
        for folder in (*reversed(path.parents), path):
            stack.enter_context(pin_directory(folder))
        yield

@contextmanager
def reader(path, *, moving=False, exclusive=False):
    handle = acquire(path, READ | (DELETE if moving else 0), 0 if exclusive else SHARE_READ)
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close_handle(handle)
        raise
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise SafetyError(f"Expected an ordinary file: {path}")
        yield stream

def fingerprint(stream):
    info = os.fstat(stream.fileno())
    stream.seek(0)
    head = stream.read(32)
    stream.seek(0)
    digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"device": info.st_dev, "inode": info.st_ino, "size": info.st_size,
            "mtime_ns": info.st_mtime_ns, "sha256": digest}, head

class RenameInfo(ct.Structure):
    _fields_ = [("replace", wt.DWORD), ("root", wt.HANDLE), ("length", wt.DWORD),
                ("name", wt.WCHAR * 1)]

def rename_open(stream, dest):
    # FileRenameInfo, with replacement disabled. Windows performs the final
    # collision check atomically, even if the name appeared after planning.
    name = str(dest).encode("utf-16-le")
    raw = ct.create_string_buffer(ct.sizeof(RenameInfo) + len(name))
    header = RenameInfo.from_buffer(raw)
    header.length = len(name)
    ct.memmove(ct.addressof(raw) + RenameInfo.name.offset, name, len(name))
    handle = msvcrt.get_osfhandle(stream.fileno())
    if not set_info(handle, 3, raw, len(raw)):
        raise ct.WinError(ct.get_last_error())
