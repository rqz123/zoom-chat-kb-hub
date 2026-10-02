from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


class DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    return DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _crypt(data: bytes, protect: bool) -> bytes:
    if os.name != "nt":
        raise RuntimeError("The local token vault currently requires Windows DPAPI.")

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    input_blob, input_buffer = _blob(data)
    entropy_blob, entropy_buffer = _blob(b"zoom-chat-knowledge-hub-v1")
    output_blob = DataBlob()
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData

    if protect:
        ok = function(
            ctypes.byref(input_blob), None, ctypes.byref(entropy_blob), None, None,
            flags, ctypes.byref(output_blob),
        )
    else:
        ok = function(
            ctypes.byref(input_blob), None, ctypes.byref(entropy_blob), None, None,
            flags, ctypes.byref(output_blob),
        )
    _ = input_buffer, entropy_buffer
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def protect(data: bytes) -> bytes:
    return _crypt(data, True)


def unprotect(data: bytes) -> bytes:
    return _crypt(data, False)

