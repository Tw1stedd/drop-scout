"""
Entertainment Earth Checkout Bot  —  Desktop GUI
Run with:  pythonw ee_gui.py   (no console window)
       or:  python   ee_gui.py
"""

import sys
import json
import queue
import threading
import asyncio
import time
from pathlib import Path
from datetime import datetime
from tkinter import messagebox, filedialog
import tkinter as tk

try:
    import customtkinter as ctk
except ImportError:
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "customtkinter"], check=True)
    import customtkinter as ctk

# ── Paths ──────────────────────────────────────────────────────────────────────
_ROOT        = Path(__file__).parent
CONFIG_FILE  = _ROOT / "ee_config.json"
SESSION_FILE = _ROOT / "ee_session.json"
sys.path.insert(0, str(_ROOT))

# ── Theme ──────────────────────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

BG       = "#16172a"
CARD     = "#1e2040"
CARD2    = "#252750"
BORDER   = "#2d3060"
ACCENT   = "#5b6bff"
TEXT     = "#e2e8f0"
DIM      = "#7880a0"
GREEN    = "#4ade80"
YELLOW   = "#fbbf24"
RED      = "#f87171"
BLUE     = "#60a5fa"
PURPLE   = "#a78bfa"

STATUS_COLOR = {
    "in_stock":    GREEN,
    "pre_order":   YELLOW,
    "out_of_stock": RED,
    "confirmed":   PURPLE,
    "unknown":     DIM,
    "error":       RED,
}
STATUS_ICON = {
    "in_stock":    "●",
    "pre_order":   "●",
    "out_of_stock":"●",
    "confirmed":   "✔",
    "unknown":     "○",
    "error":       "✗",
}
LOG_COLOR = {
    "OK":    GREEN,
    "WARN":  YELLOW,
    "ERROR": RED,
    "STEP":  PURPLE,
    "MON":   BLUE,
    "BOT":   BLUE,
    "INFO":  TEXT,
}


# ── Config helpers ─────────────────────────────────────────────────────────────
def load_cfg() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {
        "account": {"email": "", "password": ""},
        "targets": [],
        "checkout": {
            "shipping": {"first_name":"","last_name":"","address1":"","address2":"",
                         "city":"","state":"CA","zip":"","country":"US","phone":""},
            "payment":  {"card_number":"","expiry_month":"12","expiry_year":"2027",
                         "cvv":"","name_on_card":""},
            "use_saved_payment": False,
            "shipping_method": "standard",
        },
        "bot": {
            "checkout_mode": "account",
            "monitor_interval_seconds": 10,
            "max_retries": 3,
            "headless": False,
            "slow_mo_ms": 0,
            "timeout_ms": 15000,
            "discord_webhook_url": "",
            "notify_on_success": True,
            "notify_on_failure": True,
            "auto_checkout": True,
            "save_session": True,
            "proxies": [],
        },
    }


def save_cfg(cfg: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


# ── Target Dialog ──────────────────────────────────────────────────────────────
class TargetDialog(ctk.CTkToplevel):
    def __init__(self, parent, target: dict = None, on_save=None):
        super().__init__(parent)
        self.title("Add Target" if target is None else "Edit Target")
        self.geometry("500x420")
        self.resizable(False, False)
        self.configure(fg_color=BG)
        self.grab_set()
        self.on_save = on_save
        self._build(target or {})

    def _row(self, label, row):
        ctk.CTkLabel(self, text=label, text_color=DIM,
                     font=("Segoe UI", 11)).grid(row=row, column=0, padx=(20,8),
                                                  pady=4, sticky="e")

    def _entry(self, row, default="", width=300, show=""):
        e = ctk.CTkEntry(self, width=width, fg_color=CARD, border_color=BORDER,
                         text_color=TEXT, font=("Segoe UI", 12), show=show)
        e.grid(row=row, column=1, padx=(0, 20), pady=4, sticky="w")
        if default:
            e.insert(0, str(default))
        return e

    def _build(self, t):
        self.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(self, text="TARGET DETAILS",
                     font=("Segoe UI", 13, "bold"), text_color=ACCENT
                     ).grid(row=0, column=0, columnspan=2, pady=(16, 10))

        self._row("Name",        1); self.e_name   = self._entry(1, t.get("name", ""))
        self._row("Product URL", 2); self.e_url    = self._entry(2, t.get("url", ""), width=300)
        self._row("Item Number", 3); self.e_item   = self._entry(3, t.get("item_number", ""))
        self._row("Max Price $", 4); self.e_price  = self._entry(4, t.get("max_price", 0))
        self._row("Quantity",    5); self.e_qty    = self._entry(5, t.get("quantity", 1))

        self.enabled_var = ctk.BooleanVar(value=t.get("enabled", True))
        ctk.CTkCheckBox(self, text="Enabled", variable=self.enabled_var,
                        text_color=TEXT, fg_color=ACCENT,
                        font=("Segoe UI", 12)
                        ).grid(row=6, column=1, pady=8, sticky="w")

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.grid(row=7, column=0, columnspan=2, pady=(10, 20))

        ctk.CTkButton(btn_frame, text="Save", width=120,
                      fg_color=ACCENT, hover_color="#4454cc",
                      font=("Segoe UI", 12, "bold"),
                      command=self._save).pack(side="left", padx=8)
        ctk.CTkButton(btn_frame, text="Cancel", width=100,
                      fg_color=CARD2, hover_color=BORDER,
                      font=("Segoe UI", 12),
                      command=self.destroy).pack(side="left", padx=8)

    def _save(self):
        try:
            t = {
                "name":        self.e_name.get().strip(),
                "url":         self.e_url.get().strip(),
                "item_number": self.e_item.get().strip().upper(),
                "max_price":   float(self.e_price.get() or 0),
                "quantity":    int(self.e_qty.get() or 1),
                "enabled":     self.enabled_var.get(),
            }
            if not t["name"] or (not t["url"] and not t["item_number"]):
                messagebox.showerror("Missing Info", "Name + URL or Item Number required.")
                return
            if self.on_save:
                self.on_save(t)
            self.destroy()
        except ValueError as e:
            messagebox.showerror("Invalid Input", str(e))


# ── Target Card ────────────────────────────────────────────────────────────────
class TargetCard(ctk.CTkFrame):
    def __init__(self, parent, target: dict, **kwargs):
        super().__init__(parent, fg_color=CARD, corner_radius=10,
                         border_width=1, border_color=BORDER, **kwargs)
        self.target = target
        self._build()

    def _build(self):
        self.columnconfigure(1, weight=1)
        status = self.target.get("_status", "unknown")
        dot_color = STATUS_COLOR.get(status, DIM)
        icon = STATUS_ICON.get(status, "○")

        self.dot_lbl = ctk.CTkLabel(self, text=icon, text_color=dot_color,
                                    font=("Segoe UI", 18), width=28)
        self.dot_lbl.grid(row=0, column=0, rowspan=2, padx=(10, 4), pady=8)

        name = self.target.get("name", "Unknown")
        self.name_lbl = ctk.CTkLabel(self, text=name, text_color=TEXT,
                                     font=("Segoe UI", 12, "bold"),
                                     anchor="w", wraplength=165)
        self.name_lbl.grid(row=0, column=1, sticky="w", padx=(0, 8))

        status_text = status.replace("_", " ").title()
        self.status_lbl = ctk.CTkLabel(self, text=status_text,
                                       text_color=dot_color,
                                       font=("Segoe UI", 10), anchor="w")
        self.status_lbl.grid(row=1, column=1, sticky="w", padx=(0, 8))

        checks = self.target.get("_checks", 0)
        price  = self.target.get("_price", "")
        info   = f"Checks: {checks}"
        if price:
            info += f"  ·  ${price}"
        self.info_lbl = ctk.CTkLabel(self, text=info, text_color=DIM,
                                     font=("Segoe UI", 9), anchor="w")
        self.info_lbl.grid(row=2, column=0, columnspan=2,
                           padx=12, pady=(0, 6), sticky="w")

    def update_status(self, status: str, checks: int, price: str = ""):
        self.target["_status"]  = status
        self.target["_checks"]  = checks
        self.target["_price"]   = price
        dot_color = STATUS_COLOR.get(status, DIM)
        icon      = STATUS_ICON.get(status, "○")
        self.dot_lbl.configure(text=icon, text_color=dot_color)
        self.status_lbl.configure(
            text=status.replace("_", " ").title(), text_color=dot_color)
        info = f"Checks: {checks}"
        if price:
            info += f"  ·  ${price}"
        self.info_lbl.configure(text=info)


# ── Bot Thread ─────────────────────────────────────────────────────────────────
class BotThread(threading.Thread):
    def __init__(self, cfg: dict, log_q: queue.Queue, status_q: queue.Queue):
        super().__init__(daemon=True)
        self.cfg      = cfg
        self.log_q    = log_q
        self.status_q = status_q
        self._loop: asyncio.AbstractEventLoop = None

    def run(self):
        import ee_bot

        def log_cb(ts, level, prefix, msg):
            self.log_q.put((ts, level, prefix, msg))

        def status_cb(name, status, check_num, elapsed):
            self.status_q.put((name, status, check_num, elapsed))

        ee_bot.set_callbacks(log_fn=log_cb, status_fn=status_cb)

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(ee_bot.run_bot(self.cfg))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.log_q.put((
                datetime.now().strftime("%H:%M:%S.%f")[:-3],
                "ERROR", "", f"Bot crashed: {e}"
            ))
        finally:
            # Cancel any remaining tasks (e.g. checkout browser) and wait max 8s
            try:
                pending = asyncio.all_tasks(self._loop)
                if pending:
                    for t in pending:
                        t.cancel()
                    self._loop.run_until_complete(
                        asyncio.wait_for(
                            asyncio.gather(*pending, return_exceptions=True),
                            timeout=8.0
                        )
                    )
            except Exception:
                pass
            try:
                self._loop.close()
            except Exception:
                pass
            self.status_q.put(("__done__", "", 0, 0))

    def stop(self):
        """Signal cooperative shutdown via asyncio.Event (fastest, cleanest)."""
        if self._loop and not self._loop.is_closed():
            import ee_bot
            self._loop.call_soon_threadsafe(ee_bot._set_stop)


# ── Main Application ───────────────────────────────────────────────────────────
class EEBotApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("EE Checkout Bot  v3")
        self.geometry("1150×740".replace("×", "x"))
        self.minsize(900, 600)
        self.configure(fg_color=BG)

        self.cfg         = load_cfg()
        self.log_q       = queue.Queue()
        self.status_q    = queue.Queue()
        self.bot_thread: BotThread = None
        self.target_cards: dict    = {}   # name → TargetCard
        self._log_lines  = 0
        self._max_lines  = 1000

        self._build_ui()
        self._refresh_targets()
        self._poll()   # start queue polling

    # ── UI Construction ────────────────────────────────────────────────────────

    def _build_ui(self):
        self._build_header()

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=12, pady=(0, 10))
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        self._build_sidebar(body)
        self._build_main(body)

    def _build_header(self):
        hdr = ctk.CTkFrame(self, fg_color=CARD, corner_radius=0, height=54)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        ctk.CTkLabel(hdr, text="🛒  EE Checkout Bot",
                     font=("Segoe UI", 16, "bold"), text_color=TEXT
                     ).pack(side="left", padx=20)

        # Right controls
        right = ctk.CTkFrame(hdr, fg_color="transparent")
        right.pack(side="right", padx=16)

        self.stop_btn = ctk.CTkButton(
            right, text="■  Stop", width=100, height=32,
            fg_color="#3a1c1c", hover_color="#5a2020", text_color=RED,
            font=("Segoe UI", 12, "bold"), state="disabled",
            command=self._stop_bot)
        self.stop_btn.pack(side="right", padx=(6, 0))

        self.start_btn = ctk.CTkButton(
            right, text="▶  Start", width=110, height=32,
            fg_color=ACCENT, hover_color="#4454cc",
            font=("Segoe UI", 12, "bold"),
            command=self._start_bot)
        self.start_btn.pack(side="right", padx=6)

        # Status pill
        self.status_pill = ctk.CTkLabel(
            right, text="● IDLE",
            text_color=DIM, font=("Segoe UI", 11, "bold"))
        self.status_pill.pack(side="right", padx=14)

    def _build_sidebar(self, parent):
        side = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=12,
                            width=240, border_width=1, border_color=BORDER)
        side.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        side.grid_propagate(False)
        side.rowconfigure(1, weight=1)

        # Title + add button
        top = ctk.CTkFrame(side, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=(10, 4))
        ctk.CTkLabel(top, text="TARGETS",
                     font=("Segoe UI", 11, "bold"), text_color=DIM
                     ).pack(side="left")
        ctk.CTkButton(top, text="＋", width=28, height=28,
                      fg_color=ACCENT, hover_color="#4454cc",
                      font=("Segoe UI", 14, "bold"),
                      command=self._add_target).pack(side="right")

        # Scrollable card list
        self.card_scroll = ctk.CTkScrollableFrame(
            side, fg_color="transparent", scrollbar_button_color=BORDER)
        self.card_scroll.pack(fill="both", expand=True, padx=6, pady=4)

        # Edit / Remove buttons
        bot_row = ctk.CTkFrame(side, fg_color="transparent")
        bot_row.pack(fill="x", padx=10, pady=(4, 10))
        ctk.CTkButton(bot_row, text="Edit", width=90, height=28,
                      fg_color=CARD2, hover_color=BORDER,
                      font=("Segoe UI", 11),
                      command=self._edit_selected).pack(side="left")
        ctk.CTkButton(bot_row, text="Remove", width=90, height=28,
                      fg_color="#3a1c1c", hover_color="#5a2020",
                      text_color=RED, font=("Segoe UI", 11),
                      command=self._remove_selected).pack(side="right")

        self._side = side
        self._selected_target: str = None

    def _build_main(self, parent):
        main = ctk.CTkFrame(parent, fg_color="transparent")
        main.grid(row=0, column=1, sticky="nsew")

        self.tabs = ctk.CTkTabview(main, fg_color=CARD,
                                   segmented_button_fg_color=CARD2,
                                   segmented_button_selected_color=ACCENT,
                                   segmented_button_selected_hover_color="#4454cc",
                                   text_color=TEXT,
                                   corner_radius=12)
        self.tabs.pack(fill="both", expand=True)

        self.tabs.add("Live Logs")
        self.tabs.add("Config")
        self.tabs.add("Settings")

        self._build_log_tab(self.tabs.tab("Live Logs"))
        self._build_config_tab(self.tabs.tab("Config"))
        self._build_settings_tab(self.tabs.tab("Settings"))

    # ── Log Tab ────────────────────────────────────────────────────────────────

    def _build_log_tab(self, tab):
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)

        self.log_box = tk.Text(
            tab,
            bg="#0e0f1e", fg=TEXT, insertbackground=TEXT,
            font=("Consolas", 10),
            wrap="word", bd=0, padx=10, pady=8,
            state="disabled",
            selectbackground=ACCENT, selectforeground="white",
        )
        self.log_box.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=(8, 0))

        sb = ctk.CTkScrollbar(tab, command=self.log_box.yview,
                               button_color=BORDER, button_hover_color=ACCENT)
        sb.grid(row=0, column=1, sticky="ns", pady=(8, 0))
        self.log_box.configure(yscrollcommand=sb.set)

        # Tag colors
        for level, color in LOG_COLOR.items():
            self.log_box.tag_configure(f"lvl_{level}", foreground=color)
        self.log_box.tag_configure("ts",     foreground=DIM)
        self.log_box.tag_configure("prefix", foreground=BLUE)
        self.log_box.tag_configure("dim",    foreground=DIM)

        btn_row = ctk.CTkFrame(tab, fg_color="transparent")
        btn_row.grid(row=1, column=0, columnspan=2,
                     sticky="ew", padx=8, pady=(4, 8))

        self.autoscroll_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(btn_row, text="Auto-scroll",
                        variable=self.autoscroll_var,
                        text_color=DIM, fg_color=ACCENT,
                        font=("Segoe UI", 10)
                        ).pack(side="left", padx=4)

        ctk.CTkButton(btn_row, text="Save Log", width=90, height=26,
                      fg_color=CARD2, hover_color=BORDER,
                      font=("Segoe UI", 10),
                      command=self._save_log).pack(side="right", padx=4)
        ctk.CTkButton(btn_row, text="Clear", width=70, height=26,
                      fg_color=CARD2, hover_color=BORDER,
                      font=("Segoe UI", 10),
                      command=self._clear_log).pack(side="right", padx=4)

    def _append_log(self, ts, level, prefix, msg):
        self.log_box.configure(state="normal")

        # Trim old lines
        self._log_lines += 1
        if self._log_lines > self._max_lines:
            self.log_box.delete("1.0", "200.0")
            self._log_lines -= 200

        lvl_col = f"lvl_{level}"
        self.log_box.insert("end", f"[{ts}]", "ts")
        self.log_box.insert("end", f"[{level:5s}]", lvl_col)
        if prefix:
            self.log_box.insert("end", f" {prefix}", "prefix")
        self.log_box.insert("end", f" {msg}\n", lvl_col)

        self.log_box.configure(state="disabled")
        if self.autoscroll_var.get():
            self.log_box.see("end")

    def _clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        self._log_lines = 0

    def _save_log(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            initialfile=f"ee_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
        )
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.log_box.get("1.0", "end"))

    # ── Config Tab ─────────────────────────────────────────────────────────────

    def _build_config_tab(self, tab):
        scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent",
                                        scrollbar_button_color=BORDER)
        scroll.pack(fill="both", expand=True, padx=4, pady=4)
        scroll.columnconfigure(0, weight=1)
        scroll.columnconfigure(1, weight=1)

        # ── Account ────────────────────────────────────────────────
        self._section(scroll, "ACCOUNT", row=0)
        self._lbl(scroll, "Email", 1, 0)
        self._lbl(scroll, "Password", 1, 2)
        self.e_email = self._ent(scroll, self.cfg["account"].get("email",""), 1, 1)
        self.e_pass  = self._ent(scroll, self.cfg["account"].get("password",""), 1, 3, show="●")

        # ── Shipping ───────────────────────────────────────────────
        self._section(scroll, "SHIPPING", row=2)
        s = self.cfg["checkout"]["shipping"]
        pairs = [
            ("First Name", "first_name", 3, 0), ("Last Name",  "last_name", 3, 2),
            ("Company",    "company",    4, 0),
            ("Address",    "address1",   5, 0), ("Address 2",  "address2",  5, 2),
            ("City",       "city",       6, 0), ("State",      "state",     6, 2),
            ("ZIP",        "zip",        7, 0), ("Phone",      "phone",     7, 2),
        ]
        self.shipping_fields = {}
        for label, key, row, col in pairs:
            self._lbl(scroll, label, row, col)
            self.shipping_fields[key] = self._ent(scroll, s.get(key,""), row, col+1)

        # Guest Email (row 8) — used as contact email for guest checkout
        self._lbl(scroll, "Guest Email", 8, 0)
        self.e_guest_email = self._ent(
            scroll, self.cfg["checkout"].get("guest_email",""), 8, 1)
        ctk.CTkLabel(scroll, text="Order confirmation email (guest mode)",
                     text_color=DIM, font=("Segoe UI", 9), anchor="w"
                     ).grid(row=8, column=2, columnspan=2, padx=4, sticky="w")

        # ── Payment ────────────────────────────────────────────────
        self._section(scroll, "PAYMENT", row=9)
        p = self.cfg["checkout"]["payment"]
        pay_pairs = [
            ("Card Number", "card_number",  10,0), ("Name on Card","name_on_card", 10,2),
            ("Exp Month",   "expiry_month", 11,0), ("Exp Year",    "expiry_year",  11,2),
            ("CVV",         "cvv",          12,0),
        ]
        self.payment_fields = {}
        for label, key, row, col in pay_pairs:
            self._lbl(scroll, label, row, col)
            show = "●" if key in ("card_number", "cvv") else ""
            self.payment_fields[key] = self._ent(scroll, p.get(key,""), row, col+1, show=show)

        self.use_saved_var = ctk.BooleanVar(
            value=self.cfg["checkout"].get("use_saved_payment", False))
        ctk.CTkCheckBox(scroll, text="Use saved payment method on EE",
                        variable=self.use_saved_var,
                        text_color=TEXT, fg_color=ACCENT,
                        font=("Segoe UI", 11)
                        ).grid(row=13, column=0, columnspan=2,
                               padx=16, pady=6, sticky="w")

        self._lbl(scroll, "Shipping Method", 14, 0)
        self.ship_method_var = ctk.StringVar(
            value=self.cfg["checkout"].get("shipping_method","standard"))
        ctk.CTkOptionMenu(
            scroll, variable=self.ship_method_var,
            values=["standard", "ground", "expedited"],
            fg_color=CARD2, button_color=ACCENT,
            font=("Segoe UI", 11), text_color=TEXT,
        ).grid(row=14, column=1, padx=8, pady=4, sticky="w")

        ctk.CTkButton(scroll, text="💾  Save Config", width=160, height=36,
                      fg_color=ACCENT, hover_color="#4454cc",
                      font=("Segoe UI", 12, "bold"),
                      command=self._save_config
                      ).grid(row=15, column=0, columnspan=4,
                             pady=(14, 4))

    def _section(self, parent, text, row):
        # Divider line on row `row`, label on row `row` col 1+
        # (Use column spanning so the line is separate from the text label)
        ctk.CTkFrame(parent, fg_color=BORDER, height=2, corner_radius=0
                     ).grid(row=row, column=0, columnspan=4, sticky="ew",
                            padx=8, pady=(14, 0))
        ctk.CTkLabel(parent, text=f"  {text}",
                     font=("Segoe UI", 11, "bold"), text_color=ACCENT,
                     fg_color=CARD, padx=6, pady=2,
                     ).grid(row=row, column=0, columnspan=2,
                            padx=20, pady=(14, 4), sticky="w")

    def _lbl(self, parent, text, row, col):
        ctk.CTkLabel(parent, text=text, text_color=DIM,
                     font=("Segoe UI", 10), anchor="e"
                     ).grid(row=row, column=col, padx=(12, 4), pady=3, sticky="e")

    def _ent(self, parent, value, row, col, show=""):
        e = ctk.CTkEntry(parent, fg_color=CARD2, border_color=BORDER,
                         text_color=TEXT, font=("Segoe UI", 11),
                         show=show, width=160)
        e.grid(row=row, column=col, padx=(0, 12), pady=3, sticky="ew")
        e.insert(0, str(value))
        return e

    def _save_config(self):
        self.cfg["account"]["email"]    = self.e_email.get().strip()
        self.cfg["account"]["password"] = self.e_pass.get()
        s = self.cfg["checkout"]["shipping"]
        for key, w in self.shipping_fields.items():
            s[key] = w.get().strip()
        p = self.cfg["checkout"]["payment"]
        for key, w in self.payment_fields.items():
            p[key] = w.get().strip()
        self.cfg["checkout"]["guest_email"]       = self.e_guest_email.get().strip()
        self.cfg["checkout"]["use_saved_payment"] = self.use_saved_var.get()
        self.cfg["checkout"]["shipping_method"]   = self.ship_method_var.get()
        save_cfg(self.cfg)
        self._toast("Config saved ✓")

    # ── Settings Tab ───────────────────────────────────────────────────────────

    def _build_settings_tab(self, tab):
        scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent",
                                        scrollbar_button_color=BORDER)
        scroll.pack(fill="both", expand=True, padx=4, pady=4)
        scroll.columnconfigure(1, weight=1)

        self._section(scroll, "BOT MODE", row=0)

        self._lbl(scroll, "Checkout Mode", 1, 0)
        self.mode_var = ctk.StringVar(
            value=self.cfg["bot"].get("checkout_mode","account"))
        ctk.CTkOptionMenu(scroll, variable=self.mode_var,
                          values=["account", "guest"],
                          fg_color=CARD2, button_color=ACCENT,
                          font=("Segoe UI", 11), text_color=TEXT, width=140,
                          ).grid(row=1, column=1, padx=8, pady=4, sticky="w")

        self._lbl(scroll, "Monitor Interval", 2, 0)
        self.interval_var = ctk.StringVar(
            value=str(self.cfg["bot"].get("monitor_interval_seconds", 10)))
        ctk.CTkEntry(scroll, textvariable=self.interval_var,
                     fg_color=CARD2, border_color=BORDER,
                     text_color=TEXT, font=("Segoe UI", 11), width=80
                     ).grid(row=2, column=1, padx=8, pady=4, sticky="w")
        ctk.CTkLabel(scroll, text="seconds", text_color=DIM, font=("Segoe UI", 10)
                     ).grid(row=2, column=2, padx=0, pady=4, sticky="w")

        self._lbl(scroll, "Interval Jitter", 3, 0)
        self.jitter_var = ctk.StringVar(
            value=str(self.cfg["bot"].get("interval_jitter_pct", 20)))
        ctk.CTkEntry(scroll, textvariable=self.jitter_var,
                     fg_color=CARD2, border_color=BORDER,
                     text_color=TEXT, font=("Segoe UI", 11), width=80
                     ).grid(row=3, column=1, padx=8, pady=4, sticky="w")
        ctk.CTkLabel(scroll, text="% ±  (randomises each sleep to avoid patterns)",
                     text_color=DIM, font=("Segoe UI", 9)
                     ).grid(row=3, column=2, columnspan=2, padx=4, pady=4, sticky="w")

        self._lbl(scroll, "Proxy Rotate Every", 4, 0)
        self.rotate_var = ctk.StringVar(
            value=str(self.cfg["bot"].get("proxy_rotate_interval", 15)))
        ctk.CTkEntry(scroll, textvariable=self.rotate_var,
                     fg_color=CARD2, border_color=BORDER,
                     text_color=TEXT, font=("Segoe UI", 11), width=80
                     ).grid(row=4, column=1, padx=8, pady=4, sticky="w")
        ctk.CTkLabel(scroll, text="checks  (0 = never rotate)", text_color=DIM,
                     font=("Segoe UI", 9)
                     ).grid(row=4, column=2, columnspan=2, padx=4, pady=4, sticky="w")

        self._lbl(scroll, "Max Retries", 5, 0)
        self.retries_var = ctk.StringVar(
            value=str(self.cfg["bot"].get("max_retries", 3)))
        ctk.CTkEntry(scroll, textvariable=self.retries_var,
                     fg_color=CARD2, border_color=BORDER,
                     text_color=TEXT, font=("Segoe UI", 11), width=80
                     ).grid(row=5, column=1, padx=8, pady=4, sticky="w")

        self._section(scroll, "BROWSER", row=6)

        self.headless_var = ctk.BooleanVar(
            value=self.cfg["bot"].get("headless", False))
        ctk.CTkCheckBox(scroll, text="Headless (invisible browser)",
                        variable=self.headless_var,
                        text_color=TEXT, fg_color=ACCENT,
                        font=("Segoe UI", 11)
                        ).grid(row=7, column=0, columnspan=2,
                               padx=16, pady=4, sticky="w")

        self.auto_var = ctk.BooleanVar(
            value=self.cfg["bot"].get("auto_checkout", True))
        ctk.CTkCheckBox(scroll, text="Auto-checkout (place order automatically)",
                        variable=self.auto_var,
                        text_color=TEXT, fg_color=ACCENT,
                        font=("Segoe UI", 11)
                        ).grid(row=8, column=0, columnspan=2,
                               padx=16, pady=4, sticky="w")

        self.session_var = ctk.BooleanVar(
            value=self.cfg["bot"].get("save_session", True))
        ctk.CTkCheckBox(scroll, text="Save login session (faster restarts)",
                        variable=self.session_var,
                        text_color=TEXT, fg_color=ACCENT,
                        font=("Segoe UI", 11)
                        ).grid(row=9, column=0, columnspan=2,
                               padx=16, pady=4, sticky="w")

        self.notify_ok_var = ctk.BooleanVar(
            value=self.cfg["bot"].get("notify_on_success", True))
        ctk.CTkCheckBox(scroll, text="Discord notify on success",
                        variable=self.notify_ok_var,
                        text_color=TEXT, fg_color=ACCENT,
                        font=("Segoe UI", 11)
                        ).grid(row=10, column=0, columnspan=2,
                               padx=16, pady=4, sticky="w")

        self.notify_err_var = ctk.BooleanVar(
            value=self.cfg["bot"].get("notify_on_failure", True))
        ctk.CTkCheckBox(scroll, text="Discord notify on failure",
                        variable=self.notify_err_var,
                        text_color=TEXT, fg_color=ACCENT,
                        font=("Segoe UI", 11)
                        ).grid(row=11, column=0, columnspan=2,
                               padx=16, pady=4, sticky="w")

        self._section(scroll, "PROXIES  (one per line)", row=12)
        proxy_list = self.cfg["bot"].get("proxies", [])
        if isinstance(proxy_list, str):
            proxy_list = [proxy_list] if proxy_list else []
        self.proxy_box = ctk.CTkTextbox(
            scroll, height=110, fg_color=CARD2, border_color=BORDER,
            text_color=TEXT, font=("Consolas", 10), border_width=1)
        self.proxy_box.grid(row=13, column=0, columnspan=3,
                            padx=12, pady=(2, 0), sticky="ew")
        self.proxy_box.insert("1.0", "\n".join(proxy_list))

        ctk.CTkLabel(scroll,
                     text="Formats accepted:  ip:port:user:pass  ·  http://user:pass@host:port  ·  ip:port",
                     text_color=DIM, font=("Segoe UI", 9), anchor="w",
                     ).grid(row=14, column=0, columnspan=3,
                            padx=14, pady=(2, 6), sticky="w")

        self._section(scroll, "DISCORD", row=15)
        self._lbl(scroll, "Webhook URL", 16, 0)
        self.webhook_ent = ctk.CTkEntry(
            scroll, fg_color=CARD2, border_color=BORDER,
            text_color=TEXT, font=("Segoe UI", 11), width=400)
        self.webhook_ent.grid(row=16, column=1, columnspan=2,
                              padx=8, pady=4, sticky="ew")
        self.webhook_ent.insert(0, self.cfg["bot"].get("discord_webhook_url", ""))

        ctk.CTkButton(scroll, text="💾  Save Settings", width=160, height=36,
                      fg_color=ACCENT, hover_color="#4454cc",
                      font=("Segoe UI", 12, "bold"),
                      command=self._save_settings
                      ).grid(row=17, column=0, columnspan=3, pady=(14, 4))

        session_row = ctk.CTkFrame(scroll, fg_color="transparent")
        session_row.grid(row=18, column=0, columnspan=3, pady=(0, 12))
        ctk.CTkButton(session_row, text="Clear Saved Session",
                      width=160, height=28,
                      fg_color="#3a1c1c", hover_color="#5a2020",
                      text_color=RED, font=("Segoe UI", 10),
                      command=self._clear_session).pack(side="left", padx=6)

    def _save_settings(self):
        b = self.cfg["bot"]
        b["checkout_mode"]            = self.mode_var.get()
        b["monitor_interval_seconds"] = int(self.interval_var.get() or 10)
        b["interval_jitter_pct"]      = int(self.jitter_var.get() or 20)
        b["proxy_rotate_interval"]    = int(self.rotate_var.get() or 15)
        b["max_retries"]              = int(self.retries_var.get() or 3)
        b["headless"]                 = self.headless_var.get()
        b["auto_checkout"]            = self.auto_var.get()
        b["save_session"]             = self.session_var.get()
        b["notify_on_success"]        = self.notify_ok_var.get()
        b["notify_on_failure"]        = self.notify_err_var.get()
        b["discord_webhook_url"]      = self.webhook_ent.get().strip()

        raw_proxies = self.proxy_box.get("1.0", "end").strip()
        b["proxies"] = [p.strip() for p in raw_proxies.splitlines() if p.strip()]

        save_cfg(self.cfg)
        self._toast("Settings saved ✓")

    def _clear_session(self):
        if SESSION_FILE.exists():
            SESSION_FILE.unlink()
            self._toast("Session cleared — will re-login next run")
        else:
            self._toast("No session file found")

    # ── Target Management ──────────────────────────────────────────────────────

    def _refresh_targets(self):
        for w in self.card_scroll.winfo_children():
            w.destroy()
        self.target_cards.clear()

        for t in self.cfg.get("targets", []):
            card = TargetCard(self.card_scroll, t)
            card.pack(fill="x", padx=4, pady=4)
            card.bind("<Button-1>", lambda e, n=t["name"]: self._select(n))
            card.bind("<Button-3>", lambda e, n=t["name"]: self._show_qty_menu(e, n))
            for child in card.winfo_children():
                child.bind("<Button-1>", lambda e, n=t["name"]: self._select(n))
                child.bind("<Button-3>", lambda e, n=t["name"]: self._show_qty_menu(e, n))
            self.target_cards[t["name"]] = card

    def _show_qty_menu(self, event, name: str):
        target = next((t for t in self.cfg["targets"] if t["name"] == name), None)
        if target is None:
            return
        current_qty = target.get("quantity", 1)

        menu = tk.Menu(self, tearoff=0,
                       bg="#1e2130", fg="#e0e0e0",
                       activebackground="#4a90d9", activeforeground="#ffffff",
                       relief="flat", bd=0, font=("Segoe UI", 10))
        menu.add_command(
            label=f"  {name[:30]}{'…' if len(name)>30 else ''}",
            state="disabled",
            font=("Segoe UI", 9, "bold")
        )
        menu.add_separator()
        menu.add_command(
            label=f"  Quantity to buy  (currently {current_qty})",
            state="disabled",
            font=("Segoe UI", 9)
        )
        for qty in range(1, 11):
            check = "✓  " if qty == current_qty else "     "
            menu.add_command(
                label=f"  {check}Buy {qty}",
                command=lambda q=qty: self._set_quantity(name, q)
            )
        menu.add_separator()
        menu.add_command(
            label="  ✏  Edit target…",
            command=lambda: (self._select(name), self._edit_selected())
        )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _set_quantity(self, name: str, qty: int):
        for t in self.cfg["targets"]:
            if t["name"] == name:
                t["quantity"] = qty
                save_cfg(self.cfg)
                self._refresh_targets()
                self._select(name)
                self._toast(f"Quantity → {qty}  ({name[:28]})")
                return

    def _select(self, name: str):
        self._selected_target = name
        for n, card in self.target_cards.items():
            card.configure(border_color=ACCENT if n == name else BORDER)

    def _add_target(self):
        def on_save(t):
            self.cfg["targets"].append(t)
            save_cfg(self.cfg)
            self._refresh_targets()
        TargetDialog(self, on_save=on_save)

    def _edit_selected(self):
        if not self._selected_target:
            self._toast("Select a target first"); return
        idx, t = next(
            ((i, t) for i, t in enumerate(self.cfg["targets"])
             if t["name"] == self._selected_target),
            (None, None)
        )
        if t is None:
            return
        def on_save(new_t):
            self.cfg["targets"][idx] = {**t, **new_t}
            save_cfg(self.cfg)
            self._refresh_targets()
        TargetDialog(self, target=t, on_save=on_save)

    def _remove_selected(self):
        if not self._selected_target:
            self._toast("Select a target first"); return
        if messagebox.askyesno("Remove", f"Remove '{self._selected_target}'?"):
            self.cfg["targets"] = [
                t for t in self.cfg["targets"]
                if t["name"] != self._selected_target]
            save_cfg(self.cfg)
            self._selected_target = None
            self._refresh_targets()

    # ── Bot Control ────────────────────────────────────────────────────────────

    def _start_bot(self):
        # Guard: thread still alive (stop signal sent but cleanup in progress)
        if self.bot_thread and self.bot_thread.is_alive():
            self._toast("Bot is still shutting down — please wait a moment.")
            return

        # Reload config from disk, then overlay the current GUI mode selection so
        # the user doesn't need to hit Save Settings before starting.
        self.cfg = load_cfg()
        self.cfg["bot"]["checkout_mode"] = self.mode_var.get()

        enabled = [t for t in self.cfg["targets"] if t.get("enabled", True)]
        if not enabled:
            messagebox.showwarning("No Targets",
                "Add at least one enabled target before starting.")
            return

        if (not self.cfg["account"]["email"] and
                self.cfg["bot"].get("checkout_mode") == "account"):
            messagebox.showwarning("No Credentials",
                "Set your account email/password in the Config tab, "
                "or switch to Guest mode in Settings.")
            return

        self._clear_log()
        self.bot_thread = BotThread(self.cfg, self.log_q, self.status_q)
        self.bot_thread.start()

        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_pill.configure(text="● RUNNING", text_color=GREEN)
        self._append_log(
            datetime.now().strftime("%H:%M:%S.%f")[:-3],
            "BOT", "", f"Started — {len(enabled)} target(s)"
        )

    def _stop_bot(self):
        if self.bot_thread and self.bot_thread.is_alive():
            self.bot_thread.stop()
            self.stop_btn.configure(state="disabled")
            self.status_pill.configure(text="● STOPPING…", text_color=YELLOW)
            self._append_log(
                datetime.now().strftime("%H:%M:%S.%f")[:-3],
                "WARN", "", "Stop requested — closing browsers…"
            )
            # Safety net: if __done__ never arrives within 12s, force-idle the UI
            self.after(12000, self._force_idle)
        else:
            self._set_idle()

    def _force_idle(self):
        """Fallback: reset UI if the bot thread never signalled __done__."""
        if self.status_pill.cget("text") == "● STOPPING…":
            self._set_idle()
            self._append_log(
                datetime.now().strftime("%H:%M:%S.%f")[:-3],
                "WARN", "", "Force-stopped (browser took too long to close)"
            )

    def _set_idle(self):
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.status_pill.configure(text="● IDLE", text_color=DIM)

    # ── Queue Polling ──────────────────────────────────────────────────────────

    def _poll(self):
        # Drain log queue
        try:
            while True:
                ts, level, prefix, msg = self.log_q.get_nowait()
                self._append_log(ts, level, prefix, msg)
        except queue.Empty:
            pass

        # Drain status queue
        try:
            while True:
                item = self.status_q.get_nowait()
                name = item[0]
                if name == "__done__":
                    self._set_idle()
                    self._append_log(
                        datetime.now().strftime("%H:%M:%S.%f")[:-3],
                        "BOT", "", "Bot finished all tasks"
                    )
                else:
                    _, status, check_num, elapsed = item
                    if name in self.target_cards:
                        self.target_cards[name].update_status(
                            status, check_num)
        except queue.Empty:
            pass

        self.after(80, self._poll)   # poll every 80ms — smooth but light

    # ── Toast Notification ─────────────────────────────────────────────────────

    def _toast(self, msg: str):
        t = ctk.CTkToplevel(self)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        t.configure(fg_color=CARD2)

        ctk.CTkLabel(t, text=msg, font=("Segoe UI", 11),
                     text_color=TEXT, padx=16, pady=10).pack()

        # Position bottom-right of main window
        self.update_idletasks()
        wx = self.winfo_x() + self.winfo_width() - 260
        wy = self.winfo_y() + self.winfo_height() - 60
        t.geometry(f"+{wx}+{wy}")
        t.after(2200, t.destroy)

    # ── Close ──────────────────────────────────────────────────────────────────

    def on_close(self):
        if self.bot_thread and self.bot_thread.is_alive():
            if not messagebox.askyesno("Bot Running",
                "The bot is running. Stop it and exit?"):
                return
            self.bot_thread.stop()
        self.destroy()


# ── Entry ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = EEBotApp()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()
