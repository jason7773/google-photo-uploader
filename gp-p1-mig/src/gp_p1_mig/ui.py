from __future__ import annotations

import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .workflow import (
    cmd_ingest, cmd_init, cmd_reconcile, cmd_patch,
    cmd_make_batch, cmd_export_verify, cmd_push,
    cmd_import_verify, cmd_purge, cmd_retry_failed,
)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("gp-p1-mig manager")
        self.geometry("900x600")

        self.root_var = tk.StringVar(value=str(Path.cwd()))
        self.db_var = tk.StringVar(value=str(Path.cwd() / "data" / "state" / "state.db"))
        self.zip_var = tk.StringVar()
        self.batch_var = tk.StringVar(value="B0001")

        frm = ttk.Frame(self, padding=10)
        frm.pack(fill=tk.BOTH, expand=True)

        # ── Path inputs ──
        ttk.Label(frm, text="Repo root").grid(row=0, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.root_var, width=80).grid(row=0, column=1, sticky="ew")

        ttk.Label(frm, text="DB path").grid(row=1, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.db_var, width=80).grid(row=1, column=1, sticky="ew")

        ttk.Label(frm, text="Takeout zip").grid(row=2, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.zip_var, width=60).grid(row=2, column=1, sticky="w")
        ttk.Button(frm, text="Browse", command=self.pick_zip).grid(row=2, column=2)

        ttk.Label(frm, text="Batch ID").grid(row=3, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.batch_var, width=20).grid(row=3, column=1, sticky="w")

        # ── Pipeline buttons ──
        btns = ttk.Frame(frm)
        btns.grid(row=4, column=0, columnspan=3, pady=8, sticky="w")
        for label, handler in [
            ("Init", self.do_init),
            ("Ingest", self.do_ingest),
            ("Reconcile", self.do_reconcile),
            ("Patch", self.do_patch),
            ("Retry failed", self.do_retry_failed),
            ("Make batch", self.do_make_batch),
            ("Push", self.do_push),
            ("Export verify", self.do_export_verify),
            ("Import verify", self.do_import_verify),
            ("Purge", self.do_purge),
        ]:
            ttk.Button(btns, text=label, command=lambda n=label, f=handler: self.run_bg(n, f)).pack(side=tk.LEFT, padx=3)

        # ── Log area ──
        self.log = tk.Text(frm, height=25)
        self.log.grid(row=5, column=0, columnspan=3, sticky="nsew")
        frm.columnconfigure(1, weight=1)
        frm.rowconfigure(5, weight=1)

    # ── Helpers ──

    def pick_zip(self):
        p = filedialog.askopenfilename(filetypes=[("zip", "*.zip")])
        if p:
            self.zip_var.set(p)

    def _paths(self):
        return Path(self.root_var.get()).resolve(), Path(self.db_var.get()).resolve()

    def _log(self, msg: str) -> None:
        """Thread-safe log append — always dispatched on the main thread."""
        self.after(0, lambda: self.log.insert(tk.END, msg))

    def run_bg(self, name, fn):
        self._log(f"\n== {name} ==\n")

        def _t():
            try:
                out = fn()
                self._log(f"OK: {out}\n")
            except Exception as e:
                self._log(f"ERROR: {e}\n")
                self.after(0, lambda: messagebox.showerror("Error", str(e)))

        threading.Thread(target=_t, daemon=True).start()

    # ── Workflow handlers ──

    def do_init(self):
        r, d = self._paths()
        return cmd_init(r, d)

    def do_ingest(self):
        r, d = self._paths()
        return cmd_ingest(r, d, Path(self.zip_var.get()).resolve())

    def do_reconcile(self):
        _, d = self._paths()
        return cmd_reconcile(d)

    def do_patch(self):
        r, d = self._paths()
        return cmd_patch(r, d)

    def do_make_batch(self):
        r, d = self._paths()
        return cmd_make_batch(r, d, max_bytes=2 * 1024 * 1024 * 1024, max_files=500)

    def do_push(self):
        r, d = self._paths()
        return cmd_push(r, d, batch_id=self.batch_var.get(), device_path="/sdcard/DCIM/Camera")

    def do_export_verify(self):
        r, d = self._paths()
        return cmd_export_verify(r, d, batch_id=self.batch_var.get())

    def do_import_verify(self):
        _, d = self._paths()
        csv_path = filedialog.askopenfilename(filetypes=[("CSV", "*.csv")])
        if not csv_path:
            return "cancelled"
        return cmd_import_verify(d, batch_id=self.batch_var.get(), result_csv=Path(csv_path))

    def do_purge(self):
        r, d = self._paths()
        return cmd_purge(r, d, batch_id=self.batch_var.get(), purge_patched=True)

    def do_retry_failed(self):
        r, d = self._paths()
        return cmd_retry_failed(r, d)


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
