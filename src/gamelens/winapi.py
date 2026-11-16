"""Win32 底层封装：DPI 感知、点击穿透窗口、窗口枚举、进程内存读写。

所有调用均通过 ctypes 直接对接 user32/kernel32/dwmapi，不依赖 pywin32，
以便打包后体积小、无额外运行库依赖。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
from dataclasses import dataclass
from typing import Iterator, List, Optional, Tuple

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
try:
    dwmapi = ctypes.WinDLL("dwmapi")
except OSError:  # pragma: no cover
    dwmapi = None

# ---------------------------------------------------------------- 常量
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOPMOST = 0x00000008

HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_NOZORDER = 0x0004
SWP_SHOWWINDOW = 0x0040

GW_OWNER = 4
GA_ROOT = 2

PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008
PROCESS_QUERY_INFORMATION = 0x0400

MEM_COMMIT = 0x1000
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100
WRITABLE_PROTECT = (0x04, 0x08, 0x40, 0x80)  # RW / WRITECOPY / EXECUTE_RW / EXECUTE_WRITECOPY

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33


class MEMORY_BASIC_INFORMATION64(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_ulonglong),
        ("AllocationBase", ctypes.c_ulonglong),
        ("AllocationProtect", wt.DWORD),
        ("__alignment1", wt.DWORD),
        ("RegionSize", ctypes.c_ulonglong),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
        ("__alignment2", wt.DWORD),
    ]


# ---------------------------------------------------------------- 函数签名
user32.GetWindowLongW.restype = ctypes.c_long
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
user32.SetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]
user32.SetWindowPos.argtypes = [
    wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT
]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
user32.IsWindow.argtypes = [wt.HWND]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.VirtualQueryEx.restype = ctypes.c_size_t
kernel32.VirtualQueryEx.argtypes = [
    wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(MEMORY_BASIC_INFORMATION64), ctypes.c_size_t
]
kernel32.ReadProcessMemory.restype = wt.BOOL
kernel32.ReadProcessMemory.argtypes = [
    wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)
]
kernel32.WriteProcessMemory.restype = wt.BOOL
kernel32.WriteProcessMemory.argtypes = [
    wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)
]
if hasattr(kernel32, "IsWow64Process"):
    kernel32.IsWow64Process.argtypes = [wt.HANDLE, ctypes.POINTER(wt.BOOL)]


def get_last_error() -> str:
    return ctypes.FormatError(ctypes.get_last_error()).strip()


# ---------------------------------------------------------------- DPI
def enable_dpi_awareness() -> str:
    """开启 Per-Monitor V2 DPI 感知，保证截图坐标与叠加层坐标一致。"""
    try:
        ctx = ctypes.c_void_p(-4)  # PER_MONITOR_AWARE_V2
        if user32.SetProcessDpiAwarenessContext(ctx):
            return "per-monitor-v2"
    except Exception:
        pass
    try:
        shcore = ctypes.WinDLL("shcore")
        shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        return "per-monitor"
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
        return "system"
    except Exception:
        return "none"


def system_dpi() -> int:
    try:
        return int(user32.GetDpiForSystem())
    except Exception:
        dc = user32.GetDC(None)
        dpi = int(ctypes.WinDLL("gdi32").GetDeviceCaps(dc, 88))
        user32.ReleaseDC(None, dc)
        return dpi or 96


def virtual_screen_rect() -> Tuple[int, int, int, int]:
    """返回 (x, y, w, h)，覆盖所有显示器的虚拟桌面。"""
    gsm = user32.GetSystemMetrics
    x, y = gsm(76), gsm(77)  # SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN
    w, h = gsm(78), gsm(79)
    if w <= 0 or h <= 0:
        w, h = gsm(0), gsm(1)
    return x, y, w, h


# ---------------------------------------------------------------- 窗口
def hwnd_of(widget) -> int:
    """把 Tk 窗口句柄映射到真正的顶层 HWND（overrideredirect 时有 wrapper）。"""
    hwnd = int(widget.winfo_id())
    root_hwnd = user32.GetAncestor(wt.HWND(hwnd), GA_ROOT)
    return int(root_hwnd or hwnd)


def make_click_through(hwnd: int) -> None:
    """叠加层关键设置：不抢焦点 + 鼠标事件穿透到游戏。"""
    style = user32.GetWindowLongW(wt.HWND(hwnd), GWL_EXSTYLE)
    style |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    user32.SetWindowLongW(wt.HWND(hwnd), GWL_EXSTYLE, style)


def set_visible(hwnd: int, visible: bool) -> None:
    """不激活窗口地显示/隐藏（抓屏前临时藏起叠加层用，避免抓到自己写的字）。"""
    user32.ShowWindow(wt.HWND(hwnd), 4 if visible else 0)   # SW_SHOWNOACTIVATE / SW_HIDE


def keep_topmost(hwnd: int, topmost: bool = True) -> None:
    user32.SetWindowPos(
        wt.HWND(hwnd),
        wt.HWND(HWND_TOPMOST if topmost else HWND_NOTOPMOST),
        0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
    )


def dark_titlebar(hwnd: int) -> None:
    if not dwmapi:
        return
    try:
        val = ctypes.c_int(1)
        dwmapi.DwmSetWindowAttribute(wt.HWND(hwnd), DWMWA_USE_IMMERSIVE_DARK_MODE,
                                     ctypes.byref(val), ctypes.sizeof(val))
        corner = ctypes.c_int(2)  # DWMWCP_ROUND
        dwmapi.DwmSetWindowAttribute(wt.HWND(hwnd), DWMWA_WINDOW_CORNER_PREFERENCE,
                                     ctypes.byref(corner), ctypes.sizeof(corner))
    except Exception:
        pass


@dataclass
class WindowInfo:
    hwnd: int
    pid: int
    title: str
    rect: Tuple[int, int, int, int]

    @property
    def label(self) -> str:
        t = self.title.strip() or "(无标题)"
        if len(t) > 42:
            t = t[:40] + "…"
        return f"{t}   [PID {self.pid}]"


def list_windows(exclude_pids: Optional[set] = None, min_size: int = 160) -> List[WindowInfo]:
    """枚举可见的顶层窗口，供"注入模式"选择目标进程。"""
    result: List[WindowInfo] = []
    own = os.getpid()
    exclude = set(exclude_pids or ())
    exclude.add(own)

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, GW_OWNER):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 2)
        user32.GetWindowTextW(hwnd, buf, length + 2)
        title = buf.value
        if not title.strip():
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in exclude:
            return True
        r = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        if w < min_size or h < min_size:
            return True
        result.append(WindowInfo(int(hwnd), int(pid.value), title, (r.left, r.top, w, h)))
        return True

    user32.EnumWindows(_cb, 0)
    return result


def client_rect_on_screen(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    r = wt.RECT()
    if not user32.GetClientRect(wt.HWND(hwnd), ctypes.byref(r)):
        return None
    pt = wt.POINT(0, 0)
    if not user32.ClientToScreen(wt.HWND(hwnd), ctypes.byref(pt)):
        return None
    return pt.x, pt.y, r.right - r.left, r.bottom - r.top


def window_rect(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    r = wt.RECT()
    if not user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(r)):
        return None
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def is_window(hwnd: int) -> bool:
    return bool(user32.IsWindow(wt.HWND(hwnd)))


def foreground_hwnd() -> int:
    return int(user32.GetForegroundWindow())


# ---------------------------------------------------------------- 进程
@dataclass
class ProcHandle:
    pid: int
    handle: int

    def close(self) -> None:
        if self.handle:
            kernel32.CloseHandle(wt.HANDLE(self.handle))
            self.handle = 0


def open_process(pid: int, write: bool = True) -> ProcHandle:
    access = PROCESS_QUERY_INFORMATION | PROCESS_VM_READ | PROCESS_VM_OPERATION
    if write:
        access |= PROCESS_VM_WRITE
    handle = kernel32.OpenProcess(access, False, pid)
    if not handle:
        raise OSError(f"无法打开进程 PID={pid}（{get_last_error()}）。"
                      f"请以管理员身份运行本工具，或确认目标架构匹配。")
    return ProcHandle(pid, int(handle))


def process_name(pid: int) -> str:
    """用 QueryFullProcessImageName 取进程可执行文件名。"""
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return f"pid{pid}"
    try:
        size = wt.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        fn = kernel32.QueryFullProcessImageNameW
        fn.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_wchar_p, ctypes.POINTER(wt.DWORD)]
        if fn(wt.HANDLE(h), 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
    finally:
        kernel32.CloseHandle(wt.HANDLE(h))
    return f"pid{pid}"


def iter_writable_regions(proc: ProcHandle) -> Iterator[Tuple[int, int]]:
    """遍历目标进程中可写的已提交内存区，产出 (base, size)。"""
    addr = 0
    mbi = MEMORY_BASIC_INFORMATION64()
    max_addr = 0x7FFFFFFF0000 if ctypes.sizeof(ctypes.c_void_p) == 8 else 0x7FFF0000
    while addr < max_addr:
        got = kernel32.VirtualQueryEx(proc.handle, ctypes.c_void_p(addr),
                                      ctypes.byref(mbi), ctypes.sizeof(mbi))
        if not got:
            break
        base = int(mbi.BaseAddress)
        size = int(mbi.RegionSize)
        if size <= 0:
            break
        if (mbi.State == MEM_COMMIT
                and not (mbi.Protect & PAGE_GUARD)
                and mbi.Protect not in (PAGE_NOACCESS,)
                and (mbi.Protect & 0xFF) in WRITABLE_PROTECT
                and size <= 64 * 1024 * 1024):
            yield base, size
        addr = base + size


def read_mem(proc: ProcHandle, addr: int, size: int) -> Optional[bytes]:
    buf = ctypes.create_string_buffer(size)
    read = ctypes.c_size_t(0)
    ok = kernel32.ReadProcessMemory(proc.handle, ctypes.c_void_p(addr), buf, size,
                                    ctypes.byref(read))
    if not ok or read.value == 0:
        return None
    return buf.raw[: read.value]


def write_mem(proc: ProcHandle, addr: int, data: bytes) -> bool:
    buf = ctypes.create_string_buffer(data, len(data))
    written = ctypes.c_size_t(0)
    return bool(kernel32.WriteProcessMemory(proc.handle, ctypes.c_void_p(addr), buf,
                                            len(data), ctypes.byref(written)))


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False
