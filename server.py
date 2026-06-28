"""Rotor server — run on the machine with the physical keyboard, mouse, and audio.

Standalone: python server.py <client-ip>
As module:  import server; server.start(config, on_status=fn); server.stop()

config keys:
  client_ip        str   target machine IP
  audio_device     int   sounddevice device index (None = audio disabled)
  direction        str   'right'|'left'|'top'|'bottom' — where client screen is
  block_fullscreen bool  don't trigger KVM when a fullscreen window is focused
"""
import ctypes, ctypes.wintypes as wt, socket, threading, json, sys, queue, time, logging
import sounddevice as sd
import numpy as np

from protocol import (
    AUDIO_BLOCK_FRAMES,
    AUDIO_CHANNELS,
    AUDIO_DTYPE,
    AUDIO_PACKET_BYTES,
    AUDIO_SAMPLERATE,
    DEFAULT_AUDIO_PORT,
    DEFAULT_KVM_PORT,
    Direction,
    EventType,
    MouseButton,
)

_log = logging.getLogger('server')

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
_last_edge_log = 0.0        # throttle edge-trigger debug spam

def _notify(msg: str):
    _log.info(msg)
    if _status_cb:
        _status_cb(msg)

# ── Screen / edge helpers ────────────────────────────────────────────────────
def _screen():
    # ponytail: primary monitor only; for multi-monitor servers use SM_CXVIRTUALSCREEN
    return u32.GetSystemMetrics(0), u32.GetSystemMetrics(1)

_GWL_STYLE  = -16
_WS_CAPTION = 0x00C00000

def _is_fullscreen() -> bool:
    hwnd = u32.GetForegroundWindow()
    # shell/desktop window covers the full screen but isn't a real fullscreen app
    if not hwnd or hwnd == u32.GetShellWindow():
        return False
    # maximized normal windows have WS_CAPTION; real fullscreen apps (games) don't
    if u32.GetWindowLongW(hwnd, _GWL_STYLE) & _WS_CAPTION:
        return False
    r = wt.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(r))
    sw, sh = _screen()
    return r.left <= 0 and r.top <= 0 and r.right >= sw and r.bottom >= sh

def _at_trigger_edge(x: int, y: int) -> bool:
    sw, sh = _screen()
    d = Direction.from_value(_cfg.get('direction'))
    return ((d is Direction.RIGHT and x >= sw - 1) or
            (d is Direction.LEFT and x <= 0) or
            (d is Direction.TOP and y <= 0) or
            (d is Direction.BOTTOM and y >= sh - 1))

def _lock_xy():
    """Cursor position to hold while in KVM mode (one pixel inside trigger edge)."""
    sw, sh = _screen()
    d = Direction.from_value(_cfg.get('direction'))
    if d is Direction.RIGHT:
        return sw - 2, sh // 2
    if d is Direction.LEFT:
        return 1, sh // 2
    if d is Direction.TOP:
        return sw // 2, 1
    return sw // 2, sh - 2

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
            _send({
                't': EventType.KEY_DOWN.value if down else EventType.KEY_UP.value,
                'vk': s.vkCode,
                'sc': s.scanCode,
            })
            return 1     # suppress locally while in KVM mode
    return u32.CallNextHookEx(None, nCode, wParam, lParam)

# ── Mouse hook ───────────────────────────────────────────────────────────────
_BTN = {
    WM_LBUTTONDOWN: MouseButton.LEFT_DOWN.value,
    WM_LBUTTONUP: MouseButton.LEFT_UP.value,
    WM_RBUTTONDOWN: MouseButton.RIGHT_DOWN.value,
    WM_RBUTTONUP: MouseButton.RIGHT_UP.value,
    WM_MBUTTONDOWN: MouseButton.MIDDLE_DOWN.value,
    WM_MBUTTONUP: MouseButton.MIDDLE_UP.value,
}

def _ms_hook(nCode, wParam, lParam):
    global _last_edge_log
    if nCode >= 0:
        s = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
        if not (s.flags & LLMHF_INJECTED):
            x, y = s.pt.x, s.pt.y
            if not _active:
                if _at_trigger_edge(x, y):
                    now = time.monotonic()
                    if now - _last_edge_log > 1.0:
                        _last_edge_log = now
                        if not _conn:
                            _log.debug(f'Edge hit at ({x},{y}) — no client connected')
                        elif _cfg.get('block_fullscreen', True) and _is_fullscreen():
                            _log.debug(f'Edge hit at ({x},{y}) — blocked by fullscreen')
                    if _conn:
                        if _cfg.get('block_fullscreen', True) and _is_fullscreen():
                            pass
                        else:
                            _set_active(True)
                            _warp(*_lock_xy())
                            return 1
            else:
                lx, ly = _lock_xy()
                if wParam == WM_MOUSEMOVE:
                    dx, dy = x - lx, y - ly
                    if dx or dy:
                        _send({'t': EventType.MOUSE_MOVE.value, 'dx': dx, 'dy': dy})
                        _warp(lx, ly)
                elif wParam in _BTN:
                    _send({'t': EventType.MOUSE_CLICK.value, 'b': _BTN[wParam]})
                elif wParam == WM_MOUSEWHEEL:
                    dy = ctypes.c_short(s.mouseData >> 16).value // 120
                    _send({'t': EventType.MOUSE_SCROLL.value, 'dy': dy})
                return 1     # suppress all mouse input in KVM mode
    return u32.CallNextHookEx(None, nCode, wParam, lParam)

# ── Background threads ───────────────────────────────────────────────────────
def _clear_connection(conn):
    global _conn
    if conn is _conn:
        _conn = None
        if _active:
            _set_active(False)
        _notify('Client disconnected.')
    try:
        conn.close()
    except OSError:
        pass


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
                if ev.get('t') != EventType.MOUSE_MOVE.value:
                    _log.debug(f'Sent event: {ev}')
            except OSError as e:
                _log.warning(f'TCP send failed: {e}')
                _clear_connection(c)

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
                            ev = json.loads(line)
                            if ev.get('t') == EventType.RELEASE.value and _active:
                                _log.debug('Release received from client')
                                _set_active(False)
                        except json.JSONDecodeError:
                            pass
        except OSError:
            pass
        finally:
            _clear_connection(c)

def _kvm_server():
    global _conn
    port = _cfg.get('kvm_port', DEFAULT_KVM_PORT)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.settimeout(1.0)
    srv.bind(('', port))
    srv.listen(1)
    while not _stop.is_set():
        try:
            conn, addr = srv.accept()
            old_conn = _conn
            if old_conn:
                _clear_connection(old_conn)
            _cfg['client_ip'] = addr[0]
            _notify(f'Connected to {addr[0]}')
            _conn = conn
        except socket.timeout:
            pass
    srv.close()

def _audio_thread():
    dev  = _cfg.get('audio_device')
    if dev is None:
        _notify('Audio disabled: no playback device selected.')
        return
    port = _cfg.get('audio_port', DEFAULT_AUDIO_PORT)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    stream = None
    try:
        sock.bind(('', port))
        sock.settimeout(1.0)
        stream = sd.OutputStream(device=dev, channels=AUDIO_CHANNELS,
                                 samplerate=AUDIO_SAMPLERATE, dtype=AUDIO_DTYPE,
                                 blocksize=AUDIO_BLOCK_FRAMES)
        stream.start()
        _notify(f'Audio: listening on UDP {port}.')
        while not _stop.is_set():
            try:
                data, _ = sock.recvfrom(AUDIO_PACKET_BYTES)
                stream.write(np.frombuffer(data, dtype=AUDIO_DTYPE).reshape(-1, AUDIO_CHANNELS))
            except socket.timeout:
                pass
            except ValueError as e:
                _log.debug(f'Dropped malformed audio packet: {e}')
            except Exception as e:
                _log.debug(f'Audio receive error: {e}')
    except Exception as e:
        _notify(f'Audio error: {e}')
    finally:
        if stream:
            stream.stop()
            stream.close()
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
    _log.debug(f'Hooks installed: kb={hkb} ms={hms}')
    _hook_ready.set()
    if not hkb or not hms:
        _notify('Input hook error: keyboard or mouse hook was not installed.')
        if hkb:
            u32.UnhookWindowsHookEx(hkb)
        if hms:
            u32.UnhookWindowsHookEx(hms)
        return
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
    _notify(f'Server running on {local_ip} — waiting for connection…')
    _hook_loop()   # blocks until stop() posts WM_QUIT

def stop():
    _stop.set()
    c = _conn
    if c:
        _clear_connection(c)
    _hook_ready.wait(timeout=2.0)
    if _hook_tid:
        u32.PostThreadMessageW(_hook_tid, WM_QUIT, 0, 0)

if __name__ == '__main__':
    ip = sys.argv[1] if len(sys.argv) > 1 else input('Client IP: ').strip()
    start({'client_ip': ip, 'direction': 'right', 'block_fullscreen': True})
