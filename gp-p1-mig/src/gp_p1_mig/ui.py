from __future__ import annotations

import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .workflow import cmd_ingest, cmd_init, cmd_reconcile, cmd_patch, cmd_make_batch, cmd_export_verify


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("gp-p1-mig manager")
        self.geometry("900x600")

        self.root_var = tk.StringVar(value=str(Path.cwd()))
        self.db_var = tk.StringVar(value=str(Path.cwd() / "data" / "state" / "state.db"))
        self.zip_var = tk.StringVar()

        frm = ttk.Frame(self, padding=10)
        frm.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frm, text="Repo root").grid(row=0, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.root_var, width=80).grid(row=0, column=1, sticky="ew")

        ttk.Label(frm, text="DB path").grid(row=1, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.db_var, width=80).grid(row=1, column=1, sticky="ew")

        ttk.Label(frm, text="Takeout zip").grid(row=2, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.zip_var, width=60).grid(row=2, column=1, sticky="w")
        ttk.Button(frm, text="Browse", command=self.pick_zip).grid(row=2, column=2)

        btns = ttk.Frame(frm)
        btns.grid(row=3, column=0, columnspan=3, pady=8, sticky="w")
        ttk.Button(btns, text="Init", command=lambda: self.run_bg("init", self.do_init)).pack(side=tk.LEFT, padx=3)
        ttk.Button(btns, text="Ingest", command=lambda: self.run_bg("ingest", self.do_ingest)).pack(side=tk.LEFT, padx=3)
        ttk.Button(btns, text="Reconcile", command=lambda: self.run_bg("reconcile", self.do_reconcile)).pack(side=tk.LEFT, padx=3)
        ttk.Button(btns, text="Patch", command=lambda: self.run_bg("patch", self.do_patch)).pack(side=tk.LEFT, padx=3)
        ttk.Button(btns, text="Make batch", command=lambda: self.run_bg("make-batch", self.do_make_batch)).pack(side=tk.LEFT, padx=3)
        ttk.Button(btns, text="Export verify", command=lambda: self.run_bg("export-verify", self.do_export_verify)).pack(side=tk.LEFT, padx=3)

        self.log = tk.Text(frm, height=25)
        self.log.grid(row=4, column=0, columnspan=3, sticky="nsew")
        frm.columnconfigure(1, weight=1)
        frm.rowconfigure(4, weight=1)

    def pick_zip(self):
        p = filedialog.askopenfilename(filetypes=[("zip", "*.zip")])
        if p:
            self.zip_var.set(p)

    def _paths(self):
        return Path(self.root_var.get()).resolve(), Path(self.db_var.get()).resolve()

    def run_bg(self, name, fn):
        self.log.insert(tk.END, f"\n== {name} ==\n")

        def _t():
            try:
                out = fn()
                self.log.insert(tk.END, f"OK: {out}\n")
            except Exception as e:
                self.log.insert(tk.END, f"ERROR: {e}\n")
                messagebox.showerror("Error", str(e))

        threading.Thread(target=_t, daemon=True).start()

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

    def do_export_verify(self):
        r, d = self._paths()
        return cmd_export_verify(r, d, batch_id="B0001")


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
