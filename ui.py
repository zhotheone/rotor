"""Rotor UI — configure and launch server or client mode.

Run: python ui.py
"""
import tkinter as tk
from tkinter import ttk
import threading
import socket
import os, sys, json, logging
import sounddevice as sd

# Resolve directory next to the exe (frozen) or script (dev)
_app_dir     = os.path.dirname(sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__))
_log_path    = os.path.join(_app_dir, 'rotor.log')
_config_path = os.path.join(_app_dir, 'rotor.json')

logging.basicConfig(
    filename=_log_path,
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)-5s %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    encoding='utf-8',
)
_log = logging.getLogger('ui')
# server and client use Windows-only ctypes.windll — import lazily at runtime
_server = _client = None

def _load_modules():
    global _server, _client
    if _server is None:
        import server as s, client as c
        _server, _client = s, c


# ── Audio device helpers ─────────────────────────────────────────────────────

def _audio_devices(mode: str):
    """Return [(label, device_index)] appropriate for the given mode."""
    try:
        wasapi = next(i for i, h in enumerate(sd.query_hostapis()) if 'WASAPI' in h['name'])
    except StopIteration:
        wasapi = None

    out = []
    for i, d in enumerate(sd.query_devices()):
        if mode == 'client':
            # Only WASAPI input devices (loopback) for capturing system audio
            if d['hostapi'] == wasapi and d['max_input_channels'] > 0:
                out.append((d['name'], i))
        else:
            # Any output device for playback received audio
            if d['max_output_channels'] > 0:
                out.append((d['name'], i))
    return out


# ── UI ────────────────────────────────────────────────────────────────────────

class RotorUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title('Rotor')
        self.root.resizable(False, False)
        self._running     = False
        self._thread      = None
        self._audio_devs  = []
        self._saved_audio = ''
        self._build()
        self._load_config()

    def _build(self):
        p = {'padx': 8, 'pady': 4}
        f = ttk.Frame(self.root, padding=14)
        f.grid()

        # ── Local IP ──────────────────────────────────────────────────────────
        local_ip = socket.gethostbyname(socket.gethostname())
        ttk.Label(f, text='This machine:').grid(row=0, column=0, sticky='w', **p)
        ttk.Label(f, text=local_ip, foreground='#2d7d46').grid(row=0, column=1, sticky='w', **p)

        # ── Mode ──────────────────────────────────────────────────────────────
        ttk.Label(f, text='Mode:').grid(row=1, column=0, sticky='w', **p)
        self.mode = tk.StringVar(value='server')
        mf = ttk.Frame(f)
        mf.grid(row=1, column=1, sticky='w', **p)
        for label, val in (('Server', 'server'), ('Client', 'client')):
            ttk.Radiobutton(mf, text=label, variable=self.mode, value=val,
                            command=self._on_mode_change).pack(side='left', padx=(0, 8))

        # ── IP ────────────────────────────────────────────────────────────────
        self._ip_label = ttk.Label(f, text='Server IP:')
        self._ip_label.grid(row=2, column=0, sticky='w', **p)
        self.ip = tk.StringVar()
        self._ip_entry = ttk.Entry(f, textvariable=self.ip, width=22)
        self._ip_entry.grid(row=2, column=1, sticky='w', **p)

        # ── Audio device (server only — client auto-detects loopback) ────────
        self._audio_label = ttk.Label(f, text='Audio:')
        self._audio_label.grid(row=3, column=0, sticky='w', **p)
        self.audio_var   = tk.StringVar()
        self._audio_combo = ttk.Combobox(f, textvariable=self.audio_var,
                                         width=32, state='readonly')
        self._audio_combo.grid(row=3, column=1, sticky='w', **p)

        # ── Layout direction ──────────────────────────────────────────────────
        ttk.Label(f, text='Client is to the:').grid(row=4, column=0, sticky='w', **p)
        self.direction = tk.StringVar(value='right')
        ttk.Combobox(f, textvariable=self.direction, width=10, state='readonly',
                     values=['right', 'left', 'top', 'bottom']).grid(
                     row=4, column=1, sticky='w', **p)

        # ── Fullscreen guard ──────────────────────────────────────────────────
        self.block_fs = tk.BooleanVar(value=True)
        self._fs_check = ttk.Checkbutton(f, text='Block KVM switch in fullscreen',
                                         variable=self.block_fs)
        self._fs_check.grid(row=5, column=0, columnspan=2, sticky='w', **p)

        # ── Separator ─────────────────────────────────────────────────────────
        ttk.Separator(f, orient='horizontal').grid(
            row=6, column=0, columnspan=2, sticky='ew', pady=6)

        # ── Start / Stop ──────────────────────────────────────────────────────
        self._btn = ttk.Button(f, text='Start', command=self._toggle, width=12)
        self._btn.grid(row=7, column=0, columnspan=2, pady=(2, 6))

        # ── Status ────────────────────────────────────────────────────────────
        self._status = tk.StringVar(value='Offline')
        self._status_lbl = ttk.Label(f, textvariable=self._status, foreground='gray',
                                     wraplength=280)
        self._status_lbl.grid(row=8, column=0, columnspan=2, sticky='w', **p)

        self._on_mode_change()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _on_mode_change(self):
        m = self.mode.get()
        if m == 'server':
            self._ip_label.grid_remove()
            self._ip_entry.grid_remove()
            self._audio_label.grid()
            self._audio_combo.grid()
        else:
            self._ip_label.grid()
            self._ip_entry.grid()
            self._audio_label.grid_remove()
            self._audio_combo.grid_remove()
        # fullscreen guard only meaningful on server
        self._fs_check.state(['!disabled'] if m == 'server' else ['disabled'])
        self._refresh_audio()

    def _refresh_audio(self):
        self._audio_devs = _audio_devices(self.mode.get())
        names = [name for name, _ in self._audio_devs]
        self._audio_combo['values'] = names
        if names:
            if self._saved_audio in names:
                self._audio_combo.current(names.index(self._saved_audio))
            else:
                self._audio_combo.current(0)
        else:
            self.audio_var.set('(none found)')

    def _set_status(self, msg: str):
        _log.info(msg)
        ml = msg.lower()
        if 'connected' in ml:
            color = '#2d7d46'
        elif 'waiting' in ml or 'running' in ml:
            color = '#d97706'
        elif any(w in ml for w in ('error', 'windows only', 'enter')):
            color = '#dc2626'
        else:
            color = 'gray'
        self.root.after(0, self._status.set, msg)
        self.root.after(0, self._status_lbl.config, {'foreground': color})

    def _load_config(self):
        try:
            with open(_config_path, encoding='utf-8') as f:
                c = json.load(f)
            self.mode.set(c.get('mode', 'server'))
            self.ip.set(c.get('ip', ''))
            self.direction.set(c.get('direction', 'right'))
            self.block_fs.set(c.get('block_fs', True))
            self._saved_audio = c.get('audio', '')
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            self._saved_audio = ''
        self._on_mode_change()   # re-apply mode (hides/shows IP row, refreshes audio)

    def _save_config(self):
        try:
            with open(_config_path, 'w', encoding='utf-8') as f:
                json.dump({
                    'mode':      self.mode.get(),
                    'ip':        self.ip.get(),
                    'audio':     self.audio_var.get(),
                    'direction': self.direction.get(),
                    'block_fs':  self.block_fs.get(),
                }, f, indent=2)
        except OSError:
            pass

    def _selected_audio_index(self):
        sel = self._audio_combo.current()
        if sel >= 0 and sel < len(self._audio_devs):
            return self._audio_devs[sel][1]
        return None

    # ── Start / Stop ──────────────────────────────────────────────────────────

    def _toggle(self):
        if self._running:
            self._do_stop()
        else:
            self._do_start()

    def _do_start(self):
        m   = self.mode.get()
        ip  = self.ip.get().strip()
        if not ip and m != 'server':
            self._set_status('Enter an IP address.')
            return
        dev = self._selected_audio_index()

        try:
            _load_modules()
        except (AttributeError, OSError) as e:
            self._set_status(f'Windows only: {e}')
            return

        if m == 'server':
            config = {
                'client_ip':        ip,
                'audio_device':     dev,
                'direction':        self.direction.get(),
                'block_fullscreen': self.block_fs.get(),
            }
            target = lambda: _server.start(config, on_status=self._set_status)
        else:
            config = {
                'server_ip': ip,
                'direction': self.direction.get(),
            }
            target = lambda: _client.start(config, on_status=self._set_status)

        self._save_config()
        self._running = True
        self._btn.config(text='Stop')
        self._thread = threading.Thread(target=target, daemon=True)
        self._thread.start()

    def _do_stop(self):
        if self.mode.get() == 'server':
            _server.stop()
        else:
            _client.stop()
        self._running = False
        self._btn.config(text='Start')
        self._set_status('Offline.')

    # ── Run ───────────────────────────────────────────────────────────────────

    def run(self):
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)
        self.root.mainloop()

    def _on_close(self):
        if self._running:
            self._do_stop()
        self.root.destroy()


if __name__ == '__main__':
    RotorUI().run()
