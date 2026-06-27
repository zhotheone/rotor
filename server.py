"""Rotor server — run on the machine with the physical keyboard, mouse, and audio.

Standalone: python server.py <client-ip>
As module:  import server; server.start(config, on_status=fn); server.stop()

config keys:
  client_ip        str   target machine IP
  audio_device     int   sounddevice device index (None = audio disabled)
  direction        str   'right'|'left'|'top'|'bottom' — where client screen is
  block_fullscreen bool  don't trigger KVM when a fullscreen window is focused
"""
import ctypes, ctypes.wintypes as wt, socket, threading, json, sys, queue
import sounddevice as sd
import numpy as np

u32 = ctypes.windll.user32
k32 = ctypes.windll.kernel32

# ── Windows constants ────────────────────────────────────────────────────────
WH_KEYBOARD_LL, WH_MOUSE_LL  = 13, 14
WM_KEYDOWN,     WM_KEYUP      = 0x100, 0x101
WM_SYSKEYDOWN,  WM_SYSKEYUP   = 0x104, 0x105
WM_MOUSEMOVE                  = 0x200
WM_LBUTTONDOWN, WM_LBUTTONUP  = 0x201, 0x202
WM_RBUTTONDOWN, WM_RBUTTONUP  = 0x204, 0x205
WM_MBUTTONDOWN, WM_MBUTTONUP  = 0x207, 0x208
WM_MOUSEWHEEL                 = 0x20A
WM_QUIT                       = 0x012
LLKHF_INJECTED                = 0x10
LLMHF_INJECTED                = 0x01
MOUSEEVENTF_MOVE              = 0x0001
MOUSEEVENTF_ABSOLUTE          = 0x8000

# ── ctypes structs ───────────────────────────────────────────────────────────
class POINT(ctypes.Structure):
    _fields_ = [('x', ctypes.c_long), ('y', ctypes.c_long)]

class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [('vkCode', wt.DWORD), ('scanCode', wt.DWORD),
                ('flags',  wt.DWORD), ('time',     wt.DWORD),
                ('dwExtraInfo', ctypes.c_void_p)]

class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [('pt', POINT), ('mouseData', wt.DWORD),
                ('flags', wt.DWORD), ('time', wt.DWORD),
                ('dwExtraInfo', ctypes.c_void_p)]

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [('dx', ctypes.c_long), ('dy', ctypes.c_long),
                ('mouseData', wt.DWORD), ('dwFlags', wt.DWORD),
                ('time', wt.DWORD), ('dwExtraInfo', ctypes.c_void_p)]

class INPUT(ctypes.Structure):
    class _I(ctypes.Union):
        _fields_ = [('mi', MOUSEINPUT)]
    _anonymous_ = ('_i',)
    _fields_    = [('type', wt.DWORD), ('_i', _I)]

HOOKPROC = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_int, wt.WPARAM, wt.LPARAM)

# ── Module state ─────────────────────────────────────────────────────────────
_cfg       = {}
_active    = False          # KVM pass-through on/off
_conn      = None           # current TCP client socket
_kvm_q     = queue.SimpleQueue()
_stop      = threading.Event()
_hook_tid  = None
_hook_ready= threading.Event()
_kb_fn     = None           # keep HOOKPROC refs alive (ctypes requirement)
_ms_fn     = None
_status_cb = None

def _notify(msg: str):
    print(msg)
    if _status_cb:
        _status_cb(msg)

# ── Screen / edge helpers ────────────────────────────────────────────────────
def _screen():
    # ponytail: primary monitor only; for multi-monitor servers use SM_CXVIRTUALSCREEN
    return u32.GetSystemMetrics(0), u32.GetSystemMetrics(1)

def _is_fullscreen() -> bool:
    hwnd = u32.GetForegroundWindow()
    if not hwnd:
        return False
    r = wt.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(r))
    sw, sh = _screen()
    return r.left <= 0 and r.top <= 0 and r.right >= sw and r.bottom >= sh

def _at_trigger_edge(x: int, y: int) -> bool:
    sw, sh = _screen()
    d = _cfg.get('direction', 'right')
    return ((d == 'right'  and x >= sw - 1) or
            (d == 'left'   and x <= 0)       or
            (d == 'top'    and y <= 0)        or
            (d == 'bottom' and y >= sh - 1))

def _lock_xy():
    """Cursor position to hold while in KVM mode (one pixel inside trigger edge)."""
    sw, sh = _screen()
    d = _cfg.get('direction', 'right')
    if d == 'right':  return sw - 2, sh // 2
    if d == 'left':   return 1,       sh // 2
    if d == 'top':    return sw // 2, 1
    return                   sw // 2, sh - 2    # bottom

def _warp(x: int, y: int):
    sw, sh = _screen()
    inp = INPUT(type=0)
    inp.mi = MOUSEINPUT(
        dx=x * 65535 // max(sw - 1, 1),
        dy=y * 65535 // max(sh - 1, 1),
        mouseData=0, dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
        time=0, dwExtraInfo=None)
    u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))

# ── KVM state machine ────────────────────────────────────────────────────────
def _set_active(val: bool):
    global _active
    _active = val
    ip = _cfg.get('client_ip', '?')
    _notify(f'KVM → {ip}' if val else 'KVM ← local')
    if not val:
        _warp(*_lock_xy())   # return cursor to edge when releasing

def _send(ev: dict):
    _kvm_q.put(ev)           # never blocks in hook callback

# ── Keyboard hook ────────────────────────────────────────────────────────────
def _kb_hook(nCode, wParam, lParam):
    if nCode >= 0:
        s = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
        if not (s.flags & LLKHF_INJECTED) and _active:
            down = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
            _send({'t': 'kd' if down else 'ku', 'vk': s.vkCode, 'sc': s.scanCode})
            return 1     # suppress locally while in KVM mode
    return u32.CallNextHookEx(None, nCode, wParam, lParam)

# ── Mouse hook ───────────────────────────────────────────────────────────────
_BTN = {WM_LBUTTONDOWN: 'ld', WM_LBUTTONUP: 'lu',
        WM_RBUTTONDOWN: 'rd', WM_RBUTTONUP: 'ru',
        WM_MBUTTONDOWN: 'md', WM_MBUTTONUP: 'mu'}

def _ms_hook(nCode, wParam, lParam):
    if nCode >= 0:
        s = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
        if not (s.flags & LLMHF_INJECTED):
            x, y = s.pt.x, s.pt.y
            if not _active:
                if _at_trigger_edge(x, y) and _conn:
                    if _cfg.get('block_fullscreen', True) and _is_fullscreen():
                        pass   # don't trigger inside a fullscreen/windowed-fullscreen app
                    else:
                        _set_active(True)
                        _warp(*_lock_xy())
                        return 1
            else:
                lx, ly = _lock_xy()
                if wParam == WM_MOUSEMOVE:
                    dx, dy = x - lx, y - ly
                    if dx or dy:
                        _send({'t': 'mm', 'dx': dx, 'dy': dy})
                        _warp(lx, ly)
                elif wParam in _BTN:
                    _send({'t': 'mc', 'b': _BTN[wParam]})
                elif wParam == WM_MOUSEWHEEL:
                    dy = ctypes.c_short(s.mouseData >> 16).value // 120
                    _send({'t': 'ms', 'dy': dy})
                return 1     # suppress all mouse input in KVM mode
    return u32.CallNextHookEx(None, nCode, wParam, lParam)

# ── Background threads ───────────────────────────────────────────────────────
def _kvm_sender():
    global _conn
    while not _stop.is_set():
        try:
            ev = _kvm_q.get(timeout=0.1)
        except queue.Empty:
            continue
        c = _conn
        if c:
            try:
                c.sendall(json.dumps(ev).encode() + b'\n')
            except OSError:
                _conn = None

def _kvm_receiver():
    """Read 'release' signals sent back by the client."""
    while not _stop.is_set():
        c = _conn
        if not c:
            _stop.wait(0.3)
            continue
        buf = b''
        try:
            while not _stop.is_set():
                data = c.recv(1024)
                if not data:
                    break
                buf += data
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    if line:
                        try:
                            if json.loads(line).get('t') == 'release' and _active:
                                _set_active(False)
                        except json.JSONDecodeError:
                            pass
        except OSError:
            pass

def _kvm_server():
    global _conn
    port = _cfg.get('kvm_port', 9000)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.settimeout(1.0)
    srv.bind(('', port))
    srv.listen(1)
    while not _stop.is_set():
        try:
            conn, addr = srv.accept()
            _cfg['client_ip'] = addr[0]
            _notify(f'Connected to {addr[0]}')
            _conn = conn
        except socket.timeout:
            pass
    srv.close()

def _audio_thread():
    dev  = _cfg.get('audio_device')
    port = _cfg.get('audio_port', 9001)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('', port))
    sock.settimeout(1.0)
    stream = sd.OutputStream(device=dev, channels=2, samplerate=48000,
                              dtype='int16', blocksize=960)
    stream.start()
    while not _stop.is_set():
        try:
            data, _ = sock.recvfrom(960 * 4)
            stream.write(np.frombuffer(data, dtype='int16').reshape(-1, 2))
        except socket.timeout:
            pass
        except Exception:
            pass
    stream.stop()
    sock.close()

# ── Hook message loop (must run on the thread that installs the hooks) ────────
def _hook_loop():
    global _hook_tid, _kb_fn, _ms_fn
    _hook_tid = k32.GetCurrentThreadId()
    _kb_fn    = HOOKPROC(_kb_hook)
    _ms_fn    = HOOKPROC(_ms_hook)
    hmod = k32.GetModuleHandleW(None)
    hkb  = u32.SetWindowsHookExW(WH_KEYBOARD_LL, _kb_fn, hmod, 0)
    hms  = u32.SetWindowsHookExW(WH_MOUSE_LL,    _ms_fn, hmod, 0)
    _hook_ready.set()
    msg = wt.MSG()
    while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        u32.TranslateMessage(ctypes.byref(msg))
        u32.DispatchMessageW(ctypes.byref(msg))
    u32.UnhookWindowsHookEx(hkb)
    u32.UnhookWindowsHookEx(hms)

# ── Public API ───────────────────────────────────────────────────────────────
def start(config: dict, on_status=None):
    """Start the server. Blocks in the Windows message loop until stop() is called."""
    global _cfg, _status_cb, _active, _conn
    _cfg       = config
    _status_cb = on_status
    _active    = False
    _conn      = None
    _stop.clear()
    _hook_ready.clear()
    for fn in (_kvm_server, _kvm_sender, _kvm_receiver, _audio_thread):
        threading.Thread(target=fn, daemon=True).start()
    local_ip = socket.gethostbyname(socket.gethostname())
    d = config.get('direction', 'right')
    _notify(f'Server running on {local_ip} — waiting for connection…')
    _hook_loop()   # blocks until stop() posts WM_QUIT

def stop():
    _stop.set()
    _hook_ready.wait(timeout=2.0)
    if _hook_tid:
        u32.PostThreadMessageW(_hook_tid, WM_QUIT, 0, 0)

if __name__ == '__main__':
    ip = sys.argv[1] if len(sys.argv) > 1 else input('Client IP: ').strip()
    start({'client_ip': ip, 'direction': 'right', 'block_fullscreen': True})
