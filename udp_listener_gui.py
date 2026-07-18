#!/usr/bin/env python3
"""
FH6 UDP Listener — simple Windows GUI (tkinter, stdlib only).

Wraps the same capture/upload engine as udp_listener.py.
Does not parse Forza telemetry or implement coaching logic.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Optional

from capture_format import APPLICATION_VERSION
from udp_listener import (
    DEFAULT_HOST,
    DEFAULT_HTTP_TIMEOUT,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_PORT,
    run_listener,
)

APP_TITLE = "FH6 UDP Listener"
GUI_SETTINGS_FILE = Path(__file__).resolve().parent / "gui_settings.json"


class QueueLogHandler(logging.Handler):
    def __init__(self, log_queue: queue.Queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.log_queue.put(("log", self.format(record)))
        except Exception:
            pass


class ListenerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_TITLE} v{APPLICATION_VERSION}")
        self.minsize(720, 560)
        self.geometry("820x640")

        self._stop_event: Optional[threading.Event] = None
        self._worker: Optional[threading.Thread] = None
        self._running = False
        self._ui_queue: queue.Queue = queue.Queue()

        self._build_vars()
        self._build_ui()
        self._load_settings()
        self._attach_logging()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._drain_queue)
        self._set_running(False)
        self._append_log(f"{APP_TITLE} ready. Configure settings, then press Start.")

    def _build_vars(self) -> None:
        self.var_host = tk.StringVar(value=DEFAULT_HOST)
        self.var_port = tk.StringVar(value=str(DEFAULT_PORT))
        self.var_output = tk.StringVar(value=str(Path(DEFAULT_OUTPUT_DIR).resolve()))
        self.var_receiver = tk.StringVar(value="")
        self.var_auth_env = tk.StringVar(value="FH6_UPLOAD_TOKEN")
        self.var_token = tk.StringVar(value="")
        self.var_duration = tk.StringVar(value="")
        self.var_timeout = tk.StringVar(value=str(int(DEFAULT_HTTP_TIMEOUT)))
        self.var_no_upload = tk.BooleanVar(value=False)
        self.var_verbose = tk.BooleanVar(value=False)
        self.var_state = tk.StringVar(value="Idle")
        self.var_session = tk.StringVar(value="—")
        self.var_packets = tk.StringVar(value="0")
        self.var_bytes = tk.StringVar(value="0")
        self.var_upload = tk.StringVar(value="—")

    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 4}
        root = ttk.Frame(self, padding=10)
        root.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(root)
        header.pack(fill=tk.X)
        ttk.Label(
            header,
            text=APP_TITLE,
            font=("Segoe UI Semibold", 16),
        ).pack(side=tk.LEFT)
        ttk.Label(
            header,
            text="Windows capture + upload  ·  no telemetry parsing",
            foreground="#555",
        ).pack(side=tk.LEFT, padx=(12, 0), pady=(6, 0))

        form = ttk.LabelFrame(root, text="Settings", padding=10)
        form.pack(fill=tk.X, pady=(10, 6))
        form.columnconfigure(1, weight=1)

        def row(r: int, label: str, widget: tk.Widget) -> None:
            ttk.Label(form, text=label).grid(row=r, column=0, sticky=tk.W, **pad)
            widget.grid(row=r, column=1, sticky=tk.EW, **pad)

        row(0, "UDP host", ttk.Entry(form, textvariable=self.var_host))
        row(1, "UDP port", ttk.Entry(form, textvariable=self.var_port, width=12))

        out_row = ttk.Frame(form)
        ttk.Entry(out_row, textvariable=self.var_output).pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )
        ttk.Button(out_row, text="Browse…", command=self._browse_output, width=10).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        row(2, "Output dir", out_row)

        row(3, "Receiver URL", ttk.Entry(form, textvariable=self.var_receiver))
        row(4, "Auth env name", ttk.Entry(form, textvariable=self.var_auth_env))
        row(
            5,
            "Auth token",
            ttk.Entry(form, textvariable=self.var_token, show="•"),
        )
        ttk.Label(
            form,
            text="Token is kept in memory only (not saved). Leave blank to use existing env var.",
            foreground="#666",
        ).grid(row=6, column=1, sticky=tk.W, padx=8)

        opts = ttk.Frame(form)
        opts.grid(row=7, column=0, columnspan=2, sticky=tk.EW, pady=(4, 0))
        ttk.Label(opts, text="Duration (sec, optional)").pack(side=tk.LEFT)
        ttk.Entry(opts, textvariable=self.var_duration, width=10).pack(
            side=tk.LEFT, padx=(6, 16)
        )
        ttk.Label(opts, text="HTTP timeout").pack(side=tk.LEFT)
        ttk.Entry(opts, textvariable=self.var_timeout, width=8).pack(
            side=tk.LEFT, padx=(6, 16)
        )
        ttk.Checkbutton(opts, text="No upload", variable=self.var_no_upload).pack(
            side=tk.LEFT, padx=(0, 12)
        )
        ttk.Checkbutton(opts, text="Verbose log", variable=self.var_verbose).pack(
            side=tk.LEFT
        )

        controls = ttk.Frame(root)
        controls.pack(fill=tk.X, pady=(4, 6))
        self.btn_start = ttk.Button(controls, text="Start", command=self._start)
        self.btn_start.pack(side=tk.LEFT)
        self.btn_stop = ttk.Button(controls, text="Stop", command=self._stop)
        self.btn_stop.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(controls, text="Save settings", command=self._save_settings).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        ttk.Button(controls, text="Open output folder", command=self._open_output).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        ttk.Button(controls, text="Verify last session", command=self._verify_last).pack(
            side=tk.LEFT, padx=(8, 0)
        )

        status = ttk.LabelFrame(root, text="Status", padding=10)
        status.pack(fill=tk.X, pady=(0, 6))
        for c in range(4):
            status.columnconfigure(c, weight=1)

        self._status_cell(status, 0, "State", self.var_state)
        self._status_cell(status, 1, "Session", self.var_session)
        self._status_cell(status, 2, "Packets", self.var_packets)
        self._status_cell(status, 3, "Payload bytes", self.var_bytes)
        ttk.Label(status, text="Upload").grid(row=2, column=0, sticky=tk.W, padx=4)
        ttk.Label(status, textvariable=self.var_upload).grid(
            row=3, column=0, columnspan=4, sticky=tk.W, padx=4
        )

        log_frame = ttk.LabelFrame(root, text="Log", padding=6)
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.txt_log = tk.Text(
            log_frame,
            height=16,
            wrap=tk.WORD,
            font=("Consolas", 10),
            state=tk.DISABLED,
        )
        scroll = ttk.Scrollbar(log_frame, command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=scroll.set)
        self.txt_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

    def _status_cell(
        self, parent: ttk.LabelFrame, col: int, title: str, var: tk.StringVar
    ) -> None:
        ttk.Label(parent, text=title, foreground="#666").grid(
            row=0, column=col, sticky=tk.W, padx=4
        )
        ttk.Label(parent, textvariable=var, font=("Segoe UI Semibold", 11)).grid(
            row=1, column=col, sticky=tk.W, padx=4, pady=(0, 6)
        )

    def _attach_logging(self) -> None:
        handler = QueueLogHandler(self._ui_queue)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s [%(name)s] %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG)
        # Avoid duplicate handlers on reload
        for h in list(root_logger.handlers):
            if isinstance(h, QueueLogHandler):
                root_logger.removeHandler(h)
        root_logger.addHandler(handler)
        # Also keep basic console quiet when GUI owns logging
        logging.getLogger("fh6").setLevel(logging.DEBUG)

    def _browse_output(self) -> None:
        path = filedialog.askdirectory(initialdir=self.var_output.get() or ".")
        if path:
            self.var_output.set(path)

    def _open_output(self) -> None:
        path = Path(self.var_output.get()).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Cannot open folder:\n{e}")

    def _settings_dict(self) -> dict[str, Any]:
        return {
            "host": self.var_host.get().strip(),
            "port": self.var_port.get().strip(),
            "output_dir": self.var_output.get().strip(),
            "receiver_url": self.var_receiver.get().strip(),
            "auth_token_env": self.var_auth_env.get().strip(),
            "duration": self.var_duration.get().strip(),
            "http_timeout": self.var_timeout.get().strip(),
            "no_upload": bool(self.var_no_upload.get()),
            "verbose": bool(self.var_verbose.get()),
        }

    def _save_settings(self) -> None:
        data = self._settings_dict()
        # Never persist the token itself
        try:
            GUI_SETTINGS_FILE.write_text(
                json.dumps(data, indent=2) + "\n", encoding="utf-8"
            )
            self._append_log(f"Settings saved to {GUI_SETTINGS_FILE.name}")
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Save failed:\n{e}")

    def _load_settings(self) -> None:
        path = GUI_SETTINGS_FILE
        if not path.is_file():
            example = Path(__file__).resolve().parent / "config.example.json"
            if example.is_file():
                try:
                    data = json.loads(example.read_text(encoding="utf-8"))
                    self._apply_settings(data)
                except Exception:
                    pass
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._apply_settings(data)
        except Exception as e:
            self._append_log(f"Could not load gui_settings.json: {e}")

    def _apply_settings(self, data: dict[str, Any]) -> None:
        if data.get("host") is not None:
            self.var_host.set(str(data["host"]))
        if data.get("port") is not None:
            self.var_port.set(str(data["port"]))
        if data.get("output_dir") is not None:
            self.var_output.set(str(data["output_dir"]))
        if data.get("receiver_url") is not None:
            self.var_receiver.set(str(data["receiver_url"] or ""))
        if data.get("auth_token_env") is not None:
            self.var_auth_env.set(str(data["auth_token_env"] or ""))
        dur = data.get("duration")
        self.var_duration.set("" if dur in (None, "") else str(dur))
        if data.get("http_timeout") is not None:
            self.var_timeout.set(str(data["http_timeout"]))
        self.var_no_upload.set(bool(data.get("no_upload", False)))
        self.var_verbose.set(bool(data.get("verbose", False)))

    def _parse_args_from_form(self) -> argparse.Namespace:
        host = self.var_host.get().strip() or DEFAULT_HOST
        try:
            port = int(self.var_port.get().strip())
        except ValueError as e:
            raise ValueError("UDP port must be an integer") from e
        if port < 1 or port > 65535:
            raise ValueError("UDP port must be 1–65535")

        output = Path(self.var_output.get().strip() or DEFAULT_OUTPUT_DIR).expanduser()
        receiver = self.var_receiver.get().strip() or None
        auth_env = self.var_auth_env.get().strip() or None
        no_upload = bool(self.var_no_upload.get())

        duration_raw = self.var_duration.get().strip()
        duration = float(duration_raw) if duration_raw else None
        if duration is not None and duration <= 0:
            raise ValueError("Duration must be positive")

        try:
            timeout = float(self.var_timeout.get().strip() or DEFAULT_HTTP_TIMEOUT)
        except ValueError as e:
            raise ValueError("HTTP timeout must be a number") from e

        token = self.var_token.get()
        if token and auth_env:
            os.environ[auth_env] = token

        return argparse.Namespace(
            host=host,
            port=port,
            output_dir=output,
            receiver_url=receiver,
            auth_token_env=auth_env,
            duration=duration,
            no_upload=no_upload,
            http_timeout=timeout,
            verbose=bool(self.var_verbose.get()),
            config=None,
        )

    def _set_running(self, running: bool) -> None:
        self._running = running
        self.btn_start.configure(state=tk.DISABLED if running else tk.NORMAL)
        self.btn_stop.configure(state=tk.NORMAL if running else tk.DISABLED)

    def _start(self) -> None:
        if self._running:
            return
        try:
            args = self._parse_args_from_form()
        except ValueError as e:
            messagebox.showerror(APP_TITLE, str(e))
            return

        if not args.no_upload and not args.receiver_url:
            if not messagebox.askyesno(
                APP_TITLE,
                "No Receiver URL set.\n\nContinue with local-only capture?",
            ):
                return
            args.no_upload = True

        self._save_settings()
        self._stop_event = threading.Event()
        self.var_state.set("Starting…")
        self.var_session.set("—")
        self.var_packets.set("0")
        self.var_bytes.set("0")
        self.var_upload.set("—")
        self._set_running(True)
        self._append_log(
            f"Starting listener on {args.host}:{args.port} → {args.output_dir}"
        )

        def worker() -> None:
            try:
                code = run_listener(
                    args,
                    stop_event=self._stop_event,
                    on_status=lambda st: self._ui_queue.put(("status", st)),
                    install_signal_handlers=False,
                )
                self._ui_queue.put(("done", code))
            except Exception as e:
                self._ui_queue.put(("done_error", str(e)))

        self._worker = threading.Thread(target=worker, name="fh6-gui-listener", daemon=True)
        self._worker.start()

    def _stop(self) -> None:
        if not self._running:
            return
        self.var_state.set("Stopping…")
        self._append_log("Stop requested — flushing capture…")
        if self._stop_event is not None:
            self._stop_event.set()

    def _on_close(self) -> None:
        if self._running:
            if not messagebox.askyesno(
                APP_TITLE,
                "Listener is still running.\nStop and exit?",
            ):
                return
            if self._stop_event is not None:
                self._stop_event.set()
            if self._worker is not None:
                self._worker.join(timeout=8.0)
        self.destroy()

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, payload = self._ui_queue.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "status":
                    self._apply_status(payload)
                elif kind == "done":
                    self._set_running(False)
                    self.var_state.set("Idle" if payload == 0 else f"Exited ({payload})")
                    self._append_log(f"Listener finished (code={payload})")
                elif kind == "done_error":
                    self._set_running(False)
                    self.var_state.set("Error")
                    self._append_log(f"Listener error: {payload}")
                    messagebox.showerror(APP_TITLE, f"Listener error:\n{payload}")
        except queue.Empty:
            pass
        self.after(100, self._drain_queue)

    def _apply_status(self, st: dict[str, Any]) -> None:
        state = st.get("state")
        mapping = {
            "listening": "Listening",
            "closing": "Closing…",
            "uploading": "Uploading…",
            "stopped": "Stopped",
            "error": "Error",
        }
        if state in mapping:
            self.var_state.set(mapping[state])
        if st.get("session_id"):
            self.var_session.set(str(st["session_id"]))
        if "packet_count" in st:
            self.var_packets.set(str(st["packet_count"]))
        if "total_payload_bytes" in st:
            self.var_bytes.set(str(st["total_payload_bytes"]))
        if st.get("upload_status"):
            self.var_upload.set(str(st["upload_status"]))
        if state == "error" and st.get("message"):
            self._append_log(st["message"])

    def _append_log(self, line: str) -> None:
        self.txt_log.configure(state=tk.NORMAL)
        self.txt_log.insert(tk.END, line.rstrip() + "\n")
        self.txt_log.see(tk.END)
        self.txt_log.configure(state=tk.DISABLED)

    def _verify_last(self) -> None:
        out = Path(self.var_output.get()).expanduser()
        if not out.is_dir():
            messagebox.showinfo(APP_TITLE, "Output folder does not exist yet.")
            return
        sessions = sorted(
            [p for p in out.iterdir() if p.is_dir()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not sessions:
            messagebox.showinfo(APP_TITLE, "No capture sessions found.")
            return
        session = sessions[0]
        try:
            proc = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parent / "verify_capture.py"), str(session)],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(Path(__file__).resolve().parent),
            )
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Verify failed to run:\n{e}")
            return
        text = (proc.stdout or "") + (proc.stderr or "")
        self._append_log(text.strip())
        if proc.returncode == 0:
            messagebox.showinfo(APP_TITLE, f"VERIFY OK\n\n{session.name}")
        else:
            messagebox.showerror(APP_TITLE, f"VERIFY FAILED\n\n{text}")


def main() -> int:
    app = ListenerApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
