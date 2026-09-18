from __future__ import annotations

import json
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from urllib.error import URLError
from urllib.request import Request, urlopen

DEFAULT_API = "http://127.0.0.1:5005"
DEFAULT_POLL_SECONDS = 2


class UPSflowGUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("UPSflow")
        self.geometry("560x700")
        self.minsize(500, 640)

        self.api_base, self.poll_ms = self._load_config()
        self.status_var = tk.StringVar(value="Checking UPSflow…")
        self.api_var = tk.StringVar(value=f"API: {self.api_base}")
        self.read_only_var = tk.StringVar(value="Read-only")
        self.cards: dict[str, dict[str, tk.StringVar]] = {}
        self._request_in_flight = False
        self._closed = False

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self._poll)

    def _load_config(self) -> tuple[str, int]:
        config_path = Path(__file__).resolve().parent / "config.json"
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            host = str(config.get("http_host", "0.0.0.0"))
            port = int(config.get("http_port", 5005))
            poll_seconds = max(1, int(config.get("poll_seconds", DEFAULT_POLL_SECONDS)))
            if host in {"", "0.0.0.0", "::"}:
                host = "127.0.0.1"
            return f"http://{host}:{port}", poll_seconds * 1000
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return DEFAULT_API, DEFAULT_POLL_SECONDS * 1000

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=14)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 12))
        ttk.Label(header, text="UPSflow", font=("Segoe UI", 18, "bold")).pack(side="left")
        ttk.Label(header, textvariable=self.status_var).pack(side="right")

        ttk.Label(outer, textvariable=self.api_var).pack(anchor="w")
        ttk.Label(
            outer, text=f"Read-only • Refresh: {self.poll_ms / 1000:g}s"
        ).pack(anchor="w", pady=(2, 10))

        for key, title in (("server", "SERVER"), ("network", "NETWORK")):
            self._add_device_card(outer, key, title)

        log_frame = ttk.LabelFrame(outer, text="Status log", padding=6)
        log_frame.pack(fill="both", expand=True, pady=(10, 0))
        self.log = tk.Text(log_frame, height=6, wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True)

    def _add_device_card(self, parent: ttk.Frame, key: str, title: str) -> None:
        frame = ttk.LabelFrame(parent, text=title, padding=9)
        frame.pack(fill="x", pady=5)

        fields = (
            ("BLE", "ble"),
            ("Battery", "battery"),
            ("AC input", "ac"),
            ("AC In", "ac_watts"),
            ("DC In", "dc_watts"),
            ("DC State", "dc_state"),
            ("Total Input", "total"),
            ("12V out", "dc12"),
            ("USB out", "usb"),
            ("Total Output", "output"),
            ("Net power", "net"),
            ("Telemetry", "age"),
            ("Error", "error"),
        )
        values: dict[str, tk.StringVar] = {}
        for row, (label, name) in enumerate(fields):
            ttk.Label(frame, text=f"{label}:", width=13).grid(row=row, column=0, sticky="w")
            var = tk.StringVar(value="—")
            values[name] = var
            ttk.Label(frame, textvariable=var).grid(row=row, column=1, sticky="w")
        self.cards[key] = values

    def _poll(self) -> None:
        if self._closed or self._request_in_flight:
            if not self._closed:
                self.after(self.poll_ms, self._poll)
            return
        self._request_in_flight = True
        threading.Thread(target=self._fetch, daemon=True).start()
        self.after(self.poll_ms, self._poll)

    def _fetch(self) -> None:
        try:
            request = Request(f"{self.api_base}/v1/telemetry", headers={"Accept": "application/json"})
            with urlopen(request, timeout=2.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.after(0, lambda: self._apply(payload))
        except (URLError, OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            self.after(0, lambda: self._show_error(str(exc)))
        finally:
            self._request_in_flight = False

    def _apply(self, payload: dict) -> None:
        self.status_var.set("● Connected")
        configured_poll = payload.get("poll_seconds")
        if isinstance(configured_poll, (int, float)) and configured_poll > 0:
            self.poll_ms = max(1000, int(configured_poll * 1000))
        self.read_only_var.set(f"Read-only • Refresh: {self.poll_ms / 1000:g}s")
        devices = payload.get("devices", {})
        for key, values in self.cards.items():
            d = devices.get(key)
            if not d:
                for var in values.values():
                    var.set("Not configured")
                continue
            values["ble"].set("Connected" if d.get("connected") else "Disconnected")
            values["battery"].set(f"{float(d.get('battery_percent', 0)):.1f}%")
            values["ac"].set("YES" if d.get("ac_present") else "NO")
            values["ac_watts"].set(f"{float(d.get('ac_watts', 0)):.0f} W")
            values["dc_watts"].set(f"{float(d.get('dc_in_watts', 0)):.0f} W")
            values["dc_state"].set(str(d.get("dc_state") or "UNKNOWN"))
            values["total"].set(f"{float(d.get('total_input_watts', 0)):.0f} W")
            values["dc12"].set(f"{float(d.get('dc12v_output_watts', 0)):.0f} W")
            values["usb"].set(f"{float(d.get('usb_output_watts', 0)):.0f} W")
            values["output"].set(f"{float(d.get('output_watts', 0)):.0f} W")
            net = float(d.get("net_watts", 0))
            values["net"].set(f"{net:+.0f} W")
            age = d.get("telemetry_age_seconds")
            if d.get("stale"):
                values["age"].set("STALE")
            elif age is None:
                values["age"].set("Never")
            else:
                values["age"].set(f"{float(age):.0f}s ago")
            values["error"].set(d.get("error") or "—")

    def _show_error(self, message: str) -> None:
        self.status_var.set("● API unavailable")
        self._log(f"API error: {message}")

    def _log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _close(self) -> None:
        self._closed = True
        self.destroy()


if __name__ == "__main__":
    UPSflowGUI().mainloop()
