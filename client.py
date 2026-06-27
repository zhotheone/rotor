"""Rotor client — run on the secondary machine.

Standalone: python client.py <server-ip>
As module:  import client; client.start(config, on_status=fn); client.stop()

config keys:
  server_ip     str   server machine IP
  audio_device  int   sounddevice output device index (None = system default)
  direction     str   'right'|'left'|'top'|'bottom' — same as server setting
"""
import ctypes, ctypes.wintypes as wt, socket, threading, json, sys
import sounddevice as sd
import numpy as np

u32 = ctypes.windll.user32

# ── ctypes structs for SendInput ─────────────────────────────────────────────
KEYEVENTF_KEYUP        = 0x0002
MOUSEEVENTF_MOVE       = 0x0001
MOUSEEVENTF_LEFTDOWN   = 0x0002; MOUSEEVENTF_LEFTUP    = 0x0004
MOUSEEVENTF_RIGHTDOWN  = 0x0008; MOUSEEVENTF_RIGHTUP   = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020; MOUSEEVENTF_MIDDLEUP  = 0x0040
MOUSEEVENTF_WHEEL      = 0x0800

class POINT(ctypes.Structure):
    _fields_ = [('x', ctypes.c_long), ('y', ctypes.c_long)]

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [('wVk', ctypes.c_ushort), ('wScan', ctypes.c_ushort),
                ('dwFlags', wt.DWORD), ('time', wt.DWORD),
                ('dwExtraInfo', ctypes.c_void_p)]

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [('dx', ctypes.c_long), ('dy', ctypes.c_long),
                ('mouseData', wt.DWORD), ('dwFlags', wt.DWORD),
                ('time', wt.DWORD), ('dwExtraInfo', ctypes.c_void_p)]

class INPUT(ctypes.Structure):
    class _I(ctypes.Union):
        _fields_ = [('ki', KEYBDINPUT), ('mi', MOUSEINPUT)]
    _anonymous_ = ('_i',)
    _fields_    = [('type', wt.DWORD), ('_i', _I)]

_BTN = {'ld': MOUSEEVENTF_LEFTDOWN,   'lu': MOUSEEVENTF_LEFTUP,
        'rd': MOUSEEVENTF_RIGHTDOWN,  'ru': MOUSEEVENTF_RIGHTUP,
        'md': MOUSEEVENTF_MIDDLEDOWN, 'mu': MOUSEEVENTF_MIDDLEUP}

# ── Module state ─────────────────────────────────────────────────────────────
_cfg       = {}
_sock      = None
_stop      = threading.Event()
_status_cb = None

def _notify(msg: str):
    print(msg)
    if _status_cb:
        _status_cb(msg)

# ── Input injection ──────────────────────────────────────────────────────────
def _send(inp: INPUT):
    u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

def handle(ev: dict):
    t = ev.get('t')
    if t in ('kd', 'ku'):
        i = INPUT(type=1)
        i.ki = KEYBDINPUT(wVk=ev['vk'], wScan=ev['sc'],
                          dwFlags=KEYEVENTF_KEYUP if t == 'ku' else 0,
                          time=0, dwExtraInfo=None)
        _send(i)
    elif t == 'mm':
        i = INPUT(type=0)
        i.mi = MOUSEINPUT(dx=ev['dx'], dy=ev['dy'], mouseData=0,
                          dwFlags=MOUSEEVENTF_MOVE, time=0, dwExtraInfo=None)
        _send(i)
    elif t == 'mc':
        i = INPUT(type=0)
        i.mi = MOUSEINPUT(dx=0, dy=0, mouseData=0,
                          dwFlags=_BTN.get(ev['b'], 0), time=0, dwExtraInfo=None)
        _send(i)
    elif t == 'ms':
        i = INPUT(type=0)
        # ponytail: & 0xFFFFFFFF converts negative wheel delta to unsigned DWORD
        i.mi = MOUSEINPUT(dx=0, dy=0,
                          mouseData=(ev['dy'] * 120) & 0xFFFFFFFF,
                          dwFlags=MOUSEEVENTF_WHEEL, time=0, dwExtraInfo=None)
        _send(i)

# ── Return-edge watcher ───────────────────────────────────────────────────────
_OPPOSITE = {'right': 'left', 'left': 'right', 'top': 'bottom', 'bottom': 'top'}

def _at_return_edge(x: int, y: int) -> bool:
    sw = u32.GetSystemMetrics(0)
    sh = u32.GetSystemMetrics(1)
    e  = _OPPOSITE.get(_cfg.get('direction', 'right'), 'left')
    return ((e == 'left'   and x <= 0)        or
            (e == 'right'  and x >= sw - 1)   or
            (e == 'top'    and y <= 0)         or
            (e == 'bottom' and y >= sh - 1))

def _edge_watcher():
    """Poll cursor position; send 'release' to server when at the return edge."""
    p = POINT()
    while not _stop.is_set():
        u32.GetCursorPos(ctypes.byref(p))
        if _at_return_edge(p.x, p.y):
            s = _sock
            if s:
                try:
                    s.sendall(json.dumps({'t': 'release'}).encode() + b'\n')
                except OSError:
                    pass
            _stop.wait(0.4)   # debounce: don't spam releases
        else:
            _stop.wait(0.016) # ~60 Hz

# ── KVM receiver ─────────────────────────────────────────────────────────────
def _kvm_client():
    global _sock
    ip   = _cfg['server_ip']
    port = _cfg.get('kvm_port', 9000)
    _notify(f'Connecting to {ip}:{port}…')
    while not _stop.is_set():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2.0)
            s.connect((ip, port))
            s.settimeout(None)
            _sock = s
            _notify('KVM ready.')
            break
        except (ConnectionRefusedError, socket.timeout, OSError):
            _stop.wait(1)
    if _stop.is_set():
        return
    buf = b''
    while not _stop.is_set():
        try:
            data = _sock.recv(4096)
        except OSError:
            break
        if not data:
            _notify('Server disconnected.')
            break
        buf += data
        while b'\n' in buf:
            line, buf = buf.split(b'\n', 1)
            if line:
                try:
                    handle(json.loads(line))
                except (json.JSONDecodeError, KeyError):
                    pass

# ── Audio receiver ────────────────────────────────────────────────────────────
def _audio_thread():
    port = _cfg.get('audio_port', 9001)
    dev  = _cfg.get('audio_device')   # None = sounddevice picks default output
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('', port))
    sock.settimeout(1.0)
    stream = sd.OutputStream(device=dev, channels=2, samplerate=48000,
                              dtype='int16', blocksize=960)
    stream.start()
    _notify(f'Audio listening on :{port}')
    while not _stop.is_set():
        try:
            data, _ = sock.recvfrom(960 * 4)   # 960 frames × 2 ch × 2 bytes
            stream.write(np.frombuffer(data, dtype='int16').reshape(-1, 2))
        except socket.timeout:
            pass
        except Exception:
            pass
    stream.stop()
    sock.close()

# ── Public API ───────────────────────────────────────────────────────────────
def start(config: dict, on_status=None):
    """Start the client. Blocks until stop() is called."""
    global _cfg, _status_cb, _sock
    _cfg       = config
    _status_cb = on_status
    _sock      = None
    _stop.clear()
    for fn in (_kvm_client, _edge_watcher, _audio_thread):
        threading.Thread(target=fn, daemon=True).start()
    _stop.wait()

def stop():
    _stop.set()
    s = _sock
    if s:
        try:
            s.close()
        except OSError:
            pass

if __name__ == '__main__':
    ip = sys.argv[1] if len(sys.argv) > 1 else input('Server IP: ').strip()
    start({'server_ip': ip, 'direction': 'right'})
