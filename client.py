"""Rotor client — run on the secondary machine.

Standalone: python client.py <server-ip>
As module:  import client; client.start(config, on_status=fn); client.stop()

config keys:
  server_ip     str   server machine IP
  audio_device  int   sounddevice input device index (WASAPI loopback, None = disabled)
  direction     str   'right'|'left'|'top'|'bottom' — same as server setting
"""
import ctypes, ctypes.wintypes as wt, socket, threading, json, sys, logging
import sounddevice as sd
import numpy as np

from protocol import (
    AUDIO_BLOCK_FRAMES,
    AUDIO_CHANNELS,
    AUDIO_DTYPE,
    AUDIO_SAMPLERATE,
    DEFAULT_AUDIO_PORT,
    DEFAULT_KVM_PORT,
    Direction,
    EventType,
    MouseButton,
)

_log = logging.getLogger('client')

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

_BTN = {
    MouseButton.LEFT_DOWN.value: MOUSEEVENTF_LEFTDOWN,
    MouseButton.LEFT_UP.value: MOUSEEVENTF_LEFTUP,
    MouseButton.RIGHT_DOWN.value: MOUSEEVENTF_RIGHTDOWN,
    MouseButton.RIGHT_UP.value: MOUSEEVENTF_RIGHTUP,
    MouseButton.MIDDLE_DOWN.value: MOUSEEVENTF_MIDDLEDOWN,
    MouseButton.MIDDLE_UP.value: MOUSEEVENTF_MIDDLEUP,
}

# ── Module state ─────────────────────────────────────────────────────────────
_cfg       = {}
_sock      = None
_stop      = threading.Event()
_status_cb = None

def _notify(msg: str):
    _log.info(msg)
    if _status_cb:
        _status_cb(msg)

# ── Input injection ──────────────────────────────────────────────────────────
def _send(inp: INPUT):
    if u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) != 1:
        _log.warning('SendInput returned 0 — injection blocked (UAC/elevated window?)')

def _handle_key(ev: dict):
    t = ev.get('t')
    i = INPUT(type=1)
    i.ki = KEYBDINPUT(wVk=ev['vk'], wScan=ev['sc'],
                      dwFlags=KEYEVENTF_KEYUP if t == EventType.KEY_UP.value else 0,
                      time=0, dwExtraInfo=None)
    _send(i)


def _handle_mouse_move(ev: dict):
    i = INPUT(type=0)
    i.mi = MOUSEINPUT(dx=ev['dx'], dy=ev['dy'], mouseData=0,
                      dwFlags=MOUSEEVENTF_MOVE, time=0, dwExtraInfo=None)
    _send(i)


def _handle_mouse_click(ev: dict):
    i = INPUT(type=0)
    i.mi = MOUSEINPUT(dx=0, dy=0, mouseData=0,
                      dwFlags=_BTN.get(ev['b'], 0), time=0, dwExtraInfo=None)
    _send(i)


def _handle_mouse_scroll(ev: dict):
    i = INPUT(type=0)
    i.mi = MOUSEINPUT(dx=0, dy=0,
                      mouseData=(ev['dy'] * 120) & 0xFFFFFFFF,
                      dwFlags=MOUSEEVENTF_WHEEL, time=0, dwExtraInfo=None)
    _send(i)


_INPUT_HANDLERS = {
    EventType.KEY_DOWN.value: _handle_key,
    EventType.KEY_UP.value: _handle_key,
    EventType.MOUSE_MOVE.value: _handle_mouse_move,
    EventType.MOUSE_CLICK.value: _handle_mouse_click,
    EventType.MOUSE_SCROLL.value: _handle_mouse_scroll,
}


def handle(ev: dict):
    if ev.get('t') != EventType.MOUSE_MOVE.value:
        _log.debug(f'Handle event: {ev}')
    handler = _INPUT_HANDLERS.get(ev.get('t'))
    if handler:
        handler(ev)

# ── Return-edge watcher ───────────────────────────────────────────────────────
def _at_return_edge(x: int, y: int) -> bool:
    sw = u32.GetSystemMetrics(0)
    sh = u32.GetSystemMetrics(1)
    edge = Direction.from_value(_cfg.get('direction')).opposite
    return ((edge is Direction.LEFT and x <= 0) or
            (edge is Direction.RIGHT and x >= sw - 1) or
            (edge is Direction.TOP and y <= 0) or
            (edge is Direction.BOTTOM and y >= sh - 1))

def _edge_watcher():
    """Poll cursor position; send 'release' to server when at the return edge."""
    p = POINT()
    while not _stop.is_set():
        u32.GetCursorPos(ctypes.byref(p))
        if _at_return_edge(p.x, p.y):
            _log.debug(f'Return edge at ({p.x},{p.y}), sending release')
            s = _sock
            if s:
                try:
                    s.sendall(json.dumps({'t': EventType.RELEASE.value}).encode() + b'\n')
                except OSError:
                    pass
            _stop.wait(0.4)   # debounce: don't spam releases
        else:
            _stop.wait(0.016) # ~60 Hz

# ── KVM receiver ─────────────────────────────────────────────────────────────
def _close_socket(sock):
    try:
        sock.close()
    except OSError:
        pass


def _connect_kvm(ip: str, port: int):
    while not _stop.is_set():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2.0)
            s.connect((ip, port))
            s.settimeout(None)
            _log.debug(f'TCP connected to {ip}:{port}')
            return s
        except (ConnectionRefusedError, socket.timeout, OSError) as e:
            _log.debug(f'Connect attempt failed: {e}')
            _stop.wait(1)
    return None


def _receive_kvm(sock):
    buf = b''
    while not _stop.is_set():
        try:
            data = sock.recv(4096)
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


def _kvm_client():
    global _sock
    ip   = _cfg['server_ip']
    port = _cfg.get('kvm_port', DEFAULT_KVM_PORT)
    while not _stop.is_set():
        _notify(f'Connecting to {ip}:{port}…')
        sock = _connect_kvm(ip, port)
        if sock is None:
            return
        _sock = sock
        _notify('KVM ready.')
        _receive_kvm(sock)
        if sock is _sock:
            _sock = None
        _close_socket(sock)
        if not _stop.is_set():
            _stop.wait(1)

# ── Audio capture → server ────────────────────────────────────────────────────
def _wasapi_host_index():
    try:
        return next(i for i, h in enumerate(sd.query_hostapis()) if 'WASAPI' in h['name'])
    except StopIteration:
        return None


def _default_output_device():
    default = sd.default.device
    if isinstance(default, (list, tuple)):
        return default[1]
    return default


def _is_wasapi_output(device_index: int, wasapi: int) -> bool:
    device = sd.query_devices(device_index)
    return device['hostapi'] == wasapi and device['max_output_channels'] > 0


def _find_loopback_source():
    """Return (device_index, name, channels) for a WASAPI output loopback source."""
    wasapi = _wasapi_host_index()
    if wasapi is None:
        return None, 'no WASAPI host', 0

    configured = _cfg.get('audio_device')
    if configured is not None and _is_wasapi_output(configured, wasapi):
        device = sd.query_devices(configured)
        return configured, device['name'], min(device['max_output_channels'], AUDIO_CHANNELS)

    default_output = _default_output_device()
    if default_output is not None and default_output >= 0 and _is_wasapi_output(default_output, wasapi):
        device = sd.query_devices(default_output)
        return default_output, device['name'], min(device['max_output_channels'], AUDIO_CHANNELS)

    for i, d in enumerate(sd.query_devices()):
        if d['hostapi'] == wasapi and d['max_output_channels'] > 0:
            return i, d['name'], min(d['max_output_channels'], AUDIO_CHANNELS)
    return None, 'no WASAPI output device found', 0


def _stereo_audio(indata, src_ch: int):
    if src_ch == AUDIO_CHANNELS:
        return indata
    return np.repeat(indata, AUDIO_CHANNELS, axis=1)

def _audio_thread():
    dev, dev_name, src_ch = _find_loopback_source()
    if dev is None:
        _notify(f'Audio disabled: {dev_name}')
        return
    ip         = _cfg['server_ip']
    port       = _cfg.get('audio_port', DEFAULT_AUDIO_PORT)
    settings   = sd.WasapiSettings(loopback=True)
    _log.debug(f'Audio loopback: "{dev_name}" idx={dev} ch={src_ch} rate={AUDIO_SAMPLERATE}')
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        def cb(indata, frames, t, status):
            if status:
                _log.debug(f'Audio stream status: {status}')
            out = _stereo_audio(indata, src_ch)
            try:
                sock.sendto(out.tobytes(), (ip, port))
            except OSError:
                pass
        _notify(f'Audio: streaming "{dev_name}" → {ip}:{port}')
        with sd.InputStream(device=dev, channels=src_ch, samplerate=AUDIO_SAMPLERATE,
                            dtype=AUDIO_DTYPE, blocksize=AUDIO_BLOCK_FRAMES,
                            callback=cb, extra_settings=settings):
            _stop.wait()
    except Exception as e:
        _notify(f'Audio error: {e}')
    finally:
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
        _close_socket(s)

if __name__ == '__main__':
    ip = sys.argv[1] if len(sys.argv) > 1 else input('Server IP: ').strip()
    start({'server_ip': ip, 'direction': 'right'})
