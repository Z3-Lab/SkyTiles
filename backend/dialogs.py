"""Native Windows dialogs for the browser-based desktop application."""
import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .library import LibraryError

logger = logging.getLogger(__name__)


def choose_directory(initial_directory=''):
    return _choose(initial_directory, False)


def choose_files(initial_directory=''):
    return _choose(initial_directory, True)


def _choose(initial_directory, files):
    if os.name != 'nt':
        raise LibraryError('当前系统请直接填写目标文件夹的完整路径', 422)
    try:
        # HTTP worker threads can be reused with a different COM apartment.
        # Each dialog owns its STA thread, HWND, COM lifetime and message loop.
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix='folder-dialog') as executor:
            return executor.submit(_choose_windows_directory, initial_directory, files).result()
    except OSError as error:
        logger.exception('Could not open the folder dialog')
        raise LibraryError(f'无法打开系统文件夹窗口：{error}。可直接填写完整路径。', 422) from error


def _choose_windows_directory(initial_directory, files=False):
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [('data1', ctypes.c_uint32), ('data2', ctypes.c_uint16),
                    ('data3', ctypes.c_uint16), ('data4', ctypes.c_ubyte * 8)]

        def __init__(self, value):
            super().__init__()
            ctypes.memmove(ctypes.byref(self), uuid.UUID(value).bytes_le, 16)

    pointer = ctypes.c_void_p
    hresult = ctypes.c_long
    ole = ctypes.WinDLL('ole32')
    shell = ctypes.WinDLL('shell32')
    user = ctypes.WinDLL('user32', use_last_error=True)
    ole.CoInitializeEx.argtypes = [pointer, wintypes.DWORD]
    ole.CoInitializeEx.restype = hresult
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    ole.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), pointer, wintypes.DWORD,
                                    ctypes.POINTER(GUID), ctypes.POINTER(pointer)]
    ole.CoCreateInstance.restype = hresult
    ole.CoTaskMemFree.argtypes = [pointer]
    ole.CoTaskMemFree.restype = None
    shell.SHCreateItemFromParsingName.argtypes = [wintypes.LPCWSTR, pointer,
                                                ctypes.POINTER(GUID), ctypes.POINTER(pointer)]
    shell.SHCreateItemFromParsingName.restype = hresult
    user.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                    wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                    wintypes.HINSTANCE, pointer]
    user.CreateWindowExW.restype = wintypes.HWND
    user.DestroyWindow.argtypes = [wintypes.HWND]
    user.DestroyWindow.restype = wintypes.BOOL
    user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user.ShowWindow.restype = wintypes.BOOL
    user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user.SetWindowPos.restype = wintypes.BOOL
    user.SetForegroundWindow.argtypes = [wintypes.HWND]
    user.SetForegroundWindow.restype = wintypes.BOOL
    user.GetSystemMetrics.argtypes = [ctypes.c_int]
    user.GetSystemMetrics.restype = ctypes.c_int

    def check(result):
        if result < 0:
            raise ctypes.WinError(result & 0xffffffff)

    def method(interface, index, *argument_types, result_type=hresult):
        table = ctypes.cast(interface, ctypes.POINTER(ctypes.POINTER(pointer))).contents
        return ctypes.WINFUNCTYPE(result_type, pointer, *argument_types)(table[index])

    def release(interface):
        if interface.value:
            method(interface, 2, result_type=wintypes.ULONG)(interface)

    check(ole.CoInitializeEx(None, 2))  # COINIT_APARTMENTTHREADED
    owner, dialog, item, text, items = None, pointer(), pointer(), pointer(), pointer()
    try:
        # A real, explicitly shown owner prevents a hidden STARTUPINFO (e.g.
        # background EXE launch) from hiding the application's first dialog.
        # Owned dialogs stay above this foreground owner instead of behind the browser.
        title = 'SkyTiles · 选择导入照片' if files else 'SkyTiles · 选择文件夹'
        owner = user.CreateWindowExW(0x00040008, 'STATIC', title,
                                     0x80000000, user.GetSystemMetrics(0) // 2,
                                     user.GetSystemMetrics(1) // 2, 1, 1, None, None, None, None)
        if not owner:
            raise ctypes.WinError(ctypes.get_last_error())
        user.ShowWindow(owner, 1)
        user.ShowWindow(owner, 1)  # The first call can be overridden by STARTF_USESHOWWINDOW.
        if not user.SetWindowPos(owner, wintypes.HWND(-1), 0, 0, 0, 0, 0x43):
            raise ctypes.WinError(ctypes.get_last_error())
        user.SetForegroundWindow(owner)
        class_id = GUID('DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7')
        dialog_id = GUID('D57C7288-D4AD-4768-BE02-9D969532D960')
        shell_item_id = GUID('43826D1E-E718-42EE-BC55-A1E261C37BFE')
        check(ole.CoCreateInstance(ctypes.byref(class_id), None, 1, ctypes.byref(dialog_id), ctypes.byref(dialog)))
        options = wintypes.DWORD()
        check(method(dialog, 10, ctypes.POINTER(wintypes.DWORD))(dialog, ctypes.byref(options)))
        # FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST | FOS_NOCHANGEDIR
        check(method(dialog, 9, wintypes.DWORD)(dialog, options.value | (0x1200 if files else 0x20) | 0x40 | 0x800 | 0x8))
        check(method(dialog, 17, wintypes.LPCWSTR)(dialog, title))
        check(method(dialog, 18, wintypes.LPCWSTR)(dialog, '选择这些照片' if files else '选择此文件夹'))
        if files:
            class Filter(ctypes.Structure):
                _fields_ = [('name', wintypes.LPCWSTR), ('pattern', wintypes.LPCWSTR)]
            filters = (Filter * 1)(Filter('照片 (JPG / JPEG / PNG / WebP)', '*.jpg;*.jpeg;*.png;*.webp'))
            check(method(dialog, 4, wintypes.UINT, ctypes.POINTER(Filter))(dialog, 1, filters))
        if initial_directory.strip() and '\x00' not in initial_directory:
            initial = Path(initial_directory).expanduser()
            if initial.is_absolute():
                while not initial.is_dir() and initial != initial.parent:
                    initial = initial.parent
                folder = pointer()
                try:
                    result = shell.SHCreateItemFromParsingName(str(initial), None, ctypes.byref(shell_item_id), ctypes.byref(folder))
                    if result >= 0:
                        check(method(dialog, 12, pointer)(dialog, folder))
                finally:
                    release(folder)
        result = method(dialog, 3, wintypes.HWND)(dialog, owner)
        if result & 0xffffffff == 0x800704C7:  # HRESULT_FROM_WIN32(ERROR_CANCELLED)
            return None
        check(result)
        if files:
            check(method(dialog, 27, ctypes.POINTER(pointer))(dialog, ctypes.byref(items)))
            count = wintypes.DWORD()
            check(method(items, 7, ctypes.POINTER(wintypes.DWORD))(items, ctypes.byref(count)))
            paths = []
            for index in range(count.value):
                entry, name = pointer(), pointer()
                try:
                    check(method(items, 8, wintypes.DWORD, ctypes.POINTER(pointer))(items, index, ctypes.byref(entry)))
                    check(method(entry, 5, wintypes.DWORD, ctypes.POINTER(pointer))(entry, 0x80058000, ctypes.byref(name)))
                    paths.append(ctypes.wstring_at(name))
                finally:
                    if name.value:
                        ole.CoTaskMemFree(name)
                    release(entry)
            return paths
        check(method(dialog, 20, ctypes.POINTER(pointer))(dialog, ctypes.byref(item)))
        # SIGDN_FILESYSPATH returns a Unicode filesystem path, including Chinese/UNC paths.
        check(method(item, 5, wintypes.DWORD, ctypes.POINTER(pointer))(item, 0x80058000, ctypes.byref(text)))
        return ctypes.wstring_at(text)
    finally:
        if text.value:
            ole.CoTaskMemFree(text)
        release(item)
        release(items)
        release(dialog)
        if owner:
            user.DestroyWindow(owner)
        ole.CoUninitialize()
