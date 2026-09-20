"""Crypto Market Data Downloader - Main Application.

Run this file to start the GUI application.
"""
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from datetime import datetime, timedelta
import threading
import os
import queue
import sys
import subprocess
import time

from gui import AppTheme
from widgets import (
    ThemedTreeview, ThemedButton, ThemedEntry, ThemedCombobox,
    apply_dark_theme
)
from database import (
    get_crypto_list, create_favorite_list, get_favorite_lists,
    get_favorite_list, add_to_favorite_list, remove_from_favorite_list,
    delete_favorite_list, get_sources, update_source_status,
    save_downloaded_data
)
from config import load_settings, save_settings, DEFAULT_SETTINGS as DEFAULTS
from source_registry import (get_crypto_list as fetch_crypto_list,
                             get_candles_from_source)
from mt5_exporter import (export_to_mt5_format, get_mt5_filename,
                          read_mt5_file, merge_and_export)
from api_server import ApiServer

# UI queue drain interval (ms). Must be small enough to feel live but not so
# small that the main thread spins; worker threads post via _safe_ui() and
# this loop is the only place that touches Tcl from them.
UI_POLL_MS = 150


class CryptoMarketDownloaderApp:
    """Main application class."""

    def __init__(self, root):
        self.root = root
        self.root.title("Crypto Market Data Downloader")
        self.root.geometry("1400x850")
        self.root.minsize(1200, 700)
        apply_dark_theme(self.root)

        self.settings = self._load_settings()
        self.current_crypto_data = []
        self.download_stopped = False

        # Auto-update: periodic 1m refresh thread + local API server.
        # auto_output_dir is snapshotted on the main thread at toggle time;
        # the worker thread must never read the Tcl entry var directly.
        self.api_server = None
        self.auto_output_dir = os.path.expanduser(
            os.path.join("~", "mt5_data"))
        self.auto_update_running = False
        self.auto_update_thread = None

        # Thread-safe queue for UI updates from worker threads. Worker threads
        # must never call Tcl directly (not thread-safe; also raises
        # RuntimeError while the main thread is inside root.update). They push
        # callbacks here; the main thread drains them via _poll_ui_queue.
        self._ui_queue = queue.Queue()

        self._setup_styles()
        self._setup_menu()
        self._setup_tabs()

        # Global status bar at the bottom of the window (persisted across tabs)
        self.status_bar = tk.Label(
            self.root, text="Ready", anchor="w",
            bg=AppTheme.BG_CARD, fg=AppTheme.TEXT_DIM,
            font=("Segoe UI", 8), relief="sunken", padx=8)
        self.status_bar.pack(side="bottom", fill="x")

        # Start the main-thread UI drain loop (arms its own recurring timer).
        self._poll_ui_queue()

    def _load_settings(self):
        from config import load_settings
        return load_settings()

    def _save_settings(self):
        from config import save_settings
        save_settings(self.settings)

    def _setup_styles(self):
        style = ttk.Style()
        style.configure("Accent.TButton",
            foreground=AppTheme.TEXT_BRIGHT, background=AppTheme.ACCENT,
            font=("Segoe UI", 9, "bold"))
        style.configure("Danger.TButton",
            foreground=AppTheme.TEXT_BRIGHT, background=AppTheme.ERROR,
            font=("Segoe UI", 9, "bold"))
        style.configure("Success.TButton",
            foreground=AppTheme.TEXT_BRIGHT, background=AppTheme.SUCCESS,
            font=("Segoe UI", 9, "bold"))

    def _setup_menu(self):
        # Menu bar removed: Settings and Proxy now live in a dedicated "Settings" tab.
        # (kept for backwards compatibility — no top menu is installed)
        pass

    def _setup_tabs(self):
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self.tab_crypto = tk.Frame(notebook, bg=AppTheme.BG_DARK)
        notebook.add(self.tab_crypto, text="\U0001F4CA Cryptocurrencies")

        self.tab_favorites = tk.Frame(notebook, bg=AppTheme.BG_DARK)
        notebook.add(self.tab_favorites, text="\u2B50 Favorite Lists")

        self.tab_downloader = tk.Frame(notebook, bg=AppTheme.BG_DARK)
        notebook.add(self.tab_downloader, text="\U0001F4E5 Data Downloader")

        self.tab_sources = tk.Frame(notebook, bg=AppTheme.BG_DARK)
        notebook.add(self.tab_sources, text="🔌 Data Sources")

        self.tab_auto = tk.Frame(notebook, bg=AppTheme.BG_DARK)
        notebook.add(self.tab_auto, text="🔁 Auto Update")

        self.tab_settings = tk.Frame(notebook, bg=AppTheme.BG_DARK)
        notebook.add(self.tab_settings, text="⚙️ Settings")

        self._setup_crypto_tab()
        self._setup_favorites_tab()
        self._setup_downloader_tab()
        self._setup_sources_tab()
        self._setup_auto_tab()
        self._setup_settings_tab()

    def _setup_crypto_tab(self):
        top_frame = tk.Frame(self.tab_crypto, bg=AppTheme.BG_DARK)
        top_frame.pack(fill="x", padx=12, pady=8)

        # Top N / Exclude Stablecoins live in the Settings tab now.
        btn_frame = tk.Frame(top_frame, bg=AppTheme.BG_DARK)
        btn_frame.pack(side="right")

        self.btn_fetch_crypto = ThemedButton(btn_frame, text="\U0001F504 Fetch List")
        self.btn_fetch_crypto.pack(side="left", padx=4)
        self.btn_add_favorite_crypto = ThemedButton(btn_frame, text="\u2B50 Add to Favorites")
        self.btn_add_favorite_crypto.pack(side="left", padx=4)

        self.lbl_status = tk.Label(self.tab_crypto, text="Ready", anchor="w",
                                   bg=AppTheme.BG_DARK, fg=AppTheme.TEXT_DIM,
                                   font=("Segoe UI", 8))
        self.lbl_status.pack(side="bottom", fill="x", padx=12, pady=2)

        tree_container = tk.Frame(self.tab_crypto, bg=AppTheme.BG_DARK)
        tree_container.pack(fill="both", expand=True, padx=12, pady=4)

        self.tree_crypto = ThemedTreeview(
            tree_container,
            columns=("rank", "symbol", "name", "price", "market_cap", "source"))
        self.tree_crypto.heading("rank", text="#")
        self.tree_crypto.heading("symbol", text="Symbol")
        self.tree_crypto.heading("name", text="Name")
        self.tree_crypto.heading("price", text="Price (USD)")
        self.tree_crypto.heading("market_cap", text="Market Cap")
        self.tree_crypto.heading("source", text="Source")
        self.tree_crypto.column("rank", width=35, anchor="center")
        self.tree_crypto.column("symbol", width=70, anchor="center")
        self.tree_crypto.column("name", width=180)
        self.tree_crypto.column("price", width=100, anchor="e")
        self.tree_crypto.column("market_cap", width=120, anchor="e")
        self.tree_crypto.column("source", width=80, anchor="center")

        vsb = ttk.Scrollbar(tree_container, orient="vertical", command=self.tree_crypto.yview)
        self.tree_crypto.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tree_crypto.pack(side="left", fill="both", expand=True)
        self.tree_crypto.selection_mode = "extended"

        self.btn_fetch_crypto.config(command=self._fetch_crypto_list)
        self.btn_add_favorite_crypto.config(command=self._add_selected_to_favorites)
        self.tree_crypto.bind("<<TreeviewSelect>>", self._on_crypto_select)

        # Load cached data on startup
        self._load_cached_crypto()

    def _load_cached_crypto(self):
        """Load cached crypto data from database."""
        data = get_crypto_list(exclude_stablecoins=self.settings.get("exclude_stablecoins", True),
                               limit=self.settings.get("top_n", 100))
        if data:
            self._populate_crypto_tree(data)

    def _fetch_crypto_list(self):
        top_n = int(self.settings.get("top_n", 100))
        exclude_sc = self.settings.get("exclude_stablecoins", True)

        self.lbl_status.config(text=f"\U0001F50D Fetching top {top_n} cryptocurrencies...")
        self.btn_fetch_crypto.config(state="disabled")

        def do_fetch():
            try:
                data = fetch_crypto_list(top_n, exclude_stablecoins=exclude_sc, settings=self.settings)
                self._safe_ui(lambda: _update_ui(data, True, None))
            except Exception as e:
                self._safe_ui(lambda e=e: _update_ui(None, False, e))

        def _update_ui(data, success, error):
            if success:
                self._populate_crypto_tree(data)
                self.lbl_status.config(text=f"✅ Loaded {len(data)} cryptocurrencies")
            else:
                self.lbl_status.config(text=f"Error: {error}")
                messagebox.showerror("Error", f"Failed to fetch data:\n{error}")
            self.btn_fetch_crypto.config(state="normal")

        threading.Thread(target=do_fetch, daemon=True).start()

    def _populate_crypto_tree(self, data):
        self.current_crypto_data = data
        for item in self.tree_crypto.get_children():
            self.tree_crypto.delete(item)
        for row in data:
            self.tree_crypto.insert("", "end", values=(
                row["rank"], row["symbol"], row["name"],
                f"{row['price']:,.2f}",
                f"{row['market_cap']:,.0f}",
                row["source"]))

    def _on_crypto_select(self, event):
        selected = self.tree_crypto.selection()
        if selected:
            values = self.tree_crypto.item(selected[0])["values"]
            self.lbl_status.config(text=f"Selected: {values[2]} ({values[1]})")
        else:
            self.lbl_status.config(text="Ready")

    def _add_selected_to_favorites(self):
        selected = self.tree_crypto.selection()
        if not selected:
            messagebox.showwarning("Warning", "Please select at least one cryptocurrency")
            return
        symbols = [self.tree_crypto.item(s)["values"][1] for s in selected]
        dlg = FavoriteSelectDialog(self.root, self._create_fav_callback, symbols, self)
        dlg.show()

    def _create_fav_callback(self, list_name, symbols):
        existing = get_favorite_lists()
        list_id = None
        for lst in existing:
            if lst["name"] == list_name:
                list_id = lst["id"]
                break
        if list_id is None:
            list_id = create_favorite_list(list_name)
        for sym in symbols:
            add_to_favorite_list(list_id, sym)
        messagebox.showinfo("Success", f"Added {len(symbols)} symbols to '{list_name}'")
        self._refresh_favorites_tab()
        self._populate_download_lists()

    def _refresh_favorites_tab(self):
        """Refresh favorites tab data."""
        lists = get_favorite_lists()
        for item in self.tree_favorites_lists.get_children():
            self.tree_favorites_lists.delete(item)
        for lst in lists:
            self.tree_favorites_lists.insert("", "end", values=(
                lst["id"], lst["name"], lst.get("item_count", 0), lst["created_at"]))

        # Also refresh the download tab's list combobox
        if hasattr(self, 'cmbb_download_list'):
            self._populate_download_lists()

    # ==================== Favorites Tab ====================

    def _setup_favorites_tab(self):
        # PanedWindow lets the user drag the divider; both panes resize with
        # the window instead of being fixed-width (which hid columns).
        pane = tk.PanedWindow(self.tab_favorites, orient="horizontal",
                              bg=AppTheme.BG_DARK, sashwidth=6,
                              sashrelief="flat")
        pane.pack(fill="both", expand=True, padx=12, pady=8)

        left_panel = tk.Frame(pane, bg=AppTheme.BG_DARK)
        right_panel = tk.Frame(pane, bg=AppTheme.BG_DARK)
        pane.add(left_panel, minsize=240, stretch="always")
        pane.add(right_panel, minsize=240, stretch="always")

        tk.Label(left_panel, text="Your Lists", fg=AppTheme.TEXT_BRIGHT,
                 bg=AppTheme.BG_DARK, font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=4)

        list_frame = tk.Frame(left_panel, bg=AppTheme.BG_DARK)
        list_frame.pack(fill="both", expand=True, pady=8)

        self.tree_favorites_lists = ThemedTreeview(
            list_frame, columns=("id", "name", "items", "created"))
        self.tree_favorites_lists.heading("id", text="#")
        self.tree_favorites_lists.heading("name", text="List Name")
        self.tree_favorites_lists.heading("items", text="Items")
        self.tree_favorites_lists.heading("created", text="Created")
        self.tree_favorites_lists.column("#0", width=0, stretch=False)
        self.tree_favorites_lists.column("id", width=30, anchor="center")
        self.tree_favorites_lists.column("name", width=120, stretch=True)
        self.tree_favorites_lists.column("items", width=50, anchor="center")
        self.tree_favorites_lists.column("created", width=120)

        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree_favorites_lists.yview)
        self.tree_favorites_lists.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tree_favorites_lists.pack(side="left", fill="both", expand=True)
        self.tree_favorites_lists.selection_mode = "browse"
        self.tree_favorites_lists.bind("<<TreeviewSelect>>", self._on_favorite_list_select)

        btn_list_add = ThemedButton(left_panel, text="\u2795 New List")
        btn_list_add.pack(side="left", padx=4, pady=4)
        btn_list_delete = ThemedButton(left_panel, text="\U0001F5D1 Delete List")
        btn_list_delete.pack(side="left", padx=4, pady=4)

        tk.Label(right_panel, text="List Items", fg=AppTheme.TEXT_BRIGHT,
                 bg=AppTheme.BG_DARK, font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=4)

        # Remove button must sit flush directly under the items tree. It is
        # packed side="bottom" BEFORE the tree so the tree (expand=True) fills
        # the space between the heading and the button, leaving no gap and
        # keeping the button pinned to the bottom edge of the pane — it moves
        # with the pane when the divider is dragged.
        btn_remove = ThemedButton(right_panel, text="🗑 Remove Selected")
        btn_remove.pack(side="bottom", fill="x", pady=(4, 0))

        self.tree_favorites_items = ThemedTreeview(
            right_panel, columns=("symbol", "name", "price", "market_cap"))
        self.tree_favorites_items.heading("symbol", text="Symbol")
        self.tree_favorites_items.heading("name", text="Name")
        self.tree_favorites_items.heading("price", text="Price")
        self.tree_favorites_items.heading("market_cap", text="Market Cap")
        self.tree_favorites_items.column("#0", width=0, stretch=False)
        self.tree_favorites_items.column("symbol", width=70, anchor="center")
        self.tree_favorites_items.column("name", width=100, stretch=True)
        self.tree_favorites_items.column("price", width=80, anchor="e")
        self.tree_favorites_items.column("market_cap", width=90, anchor="e")

        vsb2 = ttk.Scrollbar(right_panel, orient="vertical", command=self.tree_favorites_items.yview)
        self.tree_favorites_items.configure(yscrollcommand=vsb2.set)
        vsb2.pack(side="right", fill="y")
        self.tree_favorites_items.pack(side="left", fill="both", expand=True)

        btn_list_add.config(command=self._create_new_list_dialog)
        btn_list_delete.config(command=self._delete_favorite_list)
        btn_remove.config(command=self._remove_from_favorite_list)

        self._refresh_favorites_tab()

    def _on_favorite_list_select(self, event):
        selected = self.tree_favorites_lists.selection()
        if selected:
            values = self.tree_favorites_lists.item(selected[0])["values"]
            list_id = values[0]
            self._load_favorite_list_items(list_id)

    def _load_favorite_list_items(self, list_id):
        for item in self.tree_favorites_items.get_children():
            self.tree_favorites_items.delete(item)
        items = get_favorite_list(list_id)
        for item in items:
            self.tree_favorites_items.insert("", "end", values=(
                item["symbol"],
                item.get("name", item["symbol"]),
                f"{item.get('price', 0):,.2f}",
                f"{item.get('market_cap', 0):,.0f}"))

    def _create_new_list_dialog(self):
        dlg = SimpleDialog(self.root, "New List", "Enter list name:")
        result = dlg.show()
        if result:
            try:
                list_id = create_favorite_list(result)
                self._populate_download_lists()
                self._refresh_favorites_tab()
                messagebox.showinfo("Success", f"List '{result}' created")
            except Exception as e:
                messagebox.showerror("Error", f"Could not create list:\n{e}")

    def _delete_favorite_list(self):
        selected = self.tree_favorites_lists.selection()
        if not selected:
            messagebox.showwarning("Warning", "Please select a list to delete")
            return
        values = self.tree_favorites_lists.item(selected[0])["values"]
        list_id = values[0]
        list_name = values[1]
        if messagebox.askyesno("Confirm", f"Delete list '{list_name}'?"):
            delete_favorite_list(list_id)
            for item in self.tree_favorites_items.get_children():
                self.tree_favorites_items.delete(item)
            self._refresh_favorites_tab()
            self._populate_download_lists()

    def _remove_from_favorite_list(self):
        selected_list = self.tree_favorites_lists.selection()
        if not selected_list:
            messagebox.showwarning("Warning", "Please select a list first")
            return
        list_id = self.tree_favorites_lists.item(selected_list[0])["values"][0]
        selected_items = self.tree_favorites_items.selection()
        if not selected_items:
            messagebox.showwarning("Warning", "Please select items to remove")
            return
        for item in selected_items:
            symbol = self.tree_favorites_items.item(item)["values"][0]
            remove_from_favorite_list(list_id, symbol)
        self._load_favorite_list_items(list_id)
        self._refresh_favorites_tab()

    # ==================== Downloader Tab ====================

    def _setup_downloader_tab(self):
        top_row = tk.Frame(self.tab_downloader, bg=AppTheme.BG_DARK)
        top_row.pack(fill="x", padx=12, pady=8)
        tk.Label(top_row, text="Data Downloader", fg=AppTheme.TEXT_BRIGHT,
                 bg=AppTheme.BG_DARK, font=("Segoe UI", 12, "bold")).pack(anchor="w")

        list_row = tk.Frame(self.tab_downloader, bg=AppTheme.BG_DARK)
        list_row.pack(fill="x", padx=12, pady=4)
        tk.Label(list_row, text="Favorite List:", fg=AppTheme.TEXT,
                 bg=AppTheme.BG_DARK, font=("Segoe UI", 9)).pack(side="left")
        self.cmbb_download_list = ThemedCombobox(list_row, state="readonly", width=25)
        self.cmbb_download_list.pack(side="left", padx=(8, 12))

        tk.Label(list_row, text="Timeframe:", fg=AppTheme.TEXT,
                 bg=AppTheme.BG_DARK, font=("Segoe UI", 9)).pack(side="left")
        self.cmbb_timeframe = ThemedCombobox(
            list_row, state="readonly", width=10,
            values=["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d", "1w"])
        # Default to 1m — the main use case. Restored value (if any) is
        # applied in _restore_download_form, which runs after this.
        self.cmbb_timeframe.current(0)
        self.cmbb_timeframe.pack(side="left", padx=(8, 12))

        # Exchange selection. Incremental downloads keep each exchange's data
        # in its own file (SYMBOL_TF_EXCHANGE.csv) because one venue's history
        # cannot extend another's — so the user must pick a single venue here
        # instead of relying on automatic failover.
        tk.Label(list_row, text="Exchange:", fg=AppTheme.TEXT,
                 bg=AppTheme.BG_DARK, font=("Segoe UI", 9)).pack(side="left")
        self.cmbb_exchange = ThemedCombobox(
            list_row, state="readonly", width=12, values=[])
        self.cmbb_exchange.pack(side="left", padx=(8, 12))
        self._populate_exchange_combobox()

        date_row = tk.Frame(self.tab_downloader, bg=AppTheme.BG_DARK)
        date_row.pack(fill="x", padx=12, pady=4)
        tk.Label(date_row, text="Start Date:", fg=AppTheme.TEXT,
                 bg=AppTheme.BG_DARK, font=("Segoe UI", 9)).pack(side="left")
        self.ent_start_date = ThemedEntry(date_row, width=12)
        self.ent_start_date.insert(0, (datetime.now() - timedelta(days=30)).strftime("%Y.%m.%d"))
        self.ent_start_date.pack(side="left", padx=(4, 12))
        tk.Label(date_row, text="End Date:", fg=AppTheme.TEXT,
                 bg=AppTheme.BG_DARK, font=("Segoe UI", 9)).pack(side="left")
        self.ent_end_date = ThemedEntry(date_row, width=12)
        self.ent_end_date.insert(0, datetime.now().strftime("%Y.%m.%d"))
        self.ent_end_date.pack(side="left", padx=(4, 12))

        # NOW: end date = current date AND time (HH:MM of right now).
        # Without this, a today end date means midnight 00:00 of today.
        self.var_now = tk.BooleanVar(value=False)
        self.cb_now = tk.Checkbutton(
            date_row, text="NOW",
            variable=self.var_now,
            bg=AppTheme.BG_DARK, fg=AppTheme.TEXT,
            selectcolor=AppTheme.BG_CARD,
            activebackground=AppTheme.BG_DARK,
            activeforeground=AppTheme.TEXT,
            command=self._on_now_toggled)
        self.cb_now.pack(side="left", padx=(0, 12))

        tk.Label(date_row, text="Format:", fg=AppTheme.TEXT,
                 bg=AppTheme.BG_DARK, font=("Segoe UI", 9)).pack(side="left", padx=(12, 4))
        self.cmbb_format = ThemedCombobox(date_row, state="readonly", width=6,
                                           values=["csv", "txt"])
        self.cmbb_format.current(self.settings.get("mt5_export_format", "csv") == "txt" and 1 or 0)
        self.cmbb_format.pack(side="left")

        output_row = tk.Frame(self.tab_downloader, bg=AppTheme.BG_DARK)
        output_row.pack(fill="x", padx=12, pady=4)
        tk.Label(output_row, text="Output Dir:", fg=AppTheme.TEXT,
                 bg=AppTheme.BG_DARK, font=("Segoe UI", 9)).pack(side="left")
        self.ent_output_dir = ThemedEntry(output_row, width=40)
        self.ent_output_dir.insert(0, os.path.join(os.path.expanduser("~"), "mt5_data"))
        self.ent_output_dir.pack(side="left", padx=(4, 4))
        btn_browse = ThemedButton(output_row, text="\U0001F4C5", width=3)
        btn_browse.pack(side="left")
        self.btn_open_output = ThemedButton(output_row, text="📂 Open Output", width=14)
        self.btn_open_output.pack(side="left", padx=(6, 0))

        btn_row = tk.Frame(self.tab_downloader, bg=AppTheme.BG_DARK)
        btn_row.pack(fill="x", pady=12)
        self.btn_download = ThemedButton(btn_row, text="⬇ Download Data")
        self.btn_download.pack(side="left")
        self.btn_stop_download = ThemedButton(btn_row, text="⏹ Stop")
        self.btn_stop_download.pack(side="left", padx=(8, 0))
        self.btn_stop_download.config(state="disabled")

        # Progress label showing percentage + candle count
        self.lbl_progress = tk.Label(btn_row, text="0%",
                                     anchor="w", bg=AppTheme.BG_DARK,
                                     fg=AppTheme.TEXT_BRIGHT, font=("Segoe UI", 10, "bold"),
                                     width=20)
        self.lbl_progress.pack(side="left", padx=(20, 0))

        self.lbl_download_status = tk.Label(self.tab_downloader, text="Ready to download",
                                            anchor="w", bg=AppTheme.BG_DARK,
                                            fg=AppTheme.TEXT_DIM, font=("Segoe UI", 8))
        self.lbl_download_status.pack(side="bottom", fill="x", padx=12, pady=2)

        self.progress_download = ttk.Progressbar(self.tab_downloader, mode="determinate")
        self.progress_download.pack(side="bottom", fill="x", padx=12, pady=4)

        # Download log (shows each symbol step in real-time)
        log_frame = tk.Frame(self.tab_downloader, bg=AppTheme.BG_CARD,
                             relief="sunken", height=140)
        log_frame.pack(fill="both", expand=True, padx=12, pady=4)
        log_frame.pack_propagate(False)
        tk.Label(log_frame, text="Download Log",
                 fg=AppTheme.TEXT_BRIGHT, bg=AppTheme.BG_CARD,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=4, pady=2)
        self.txt_download_log = tk.Text(log_frame, height=10, wrap="word",
                                        bg=AppTheme.BG_DARK, fg=AppTheme.TEXT_DIM,
                                        font=("Segoe UI", 8), state="disabled")
        self.txt_download_log.pack(fill="both", expand=True, padx=4, pady=2)
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical",
                                   command=self.txt_download_log.yview)
        self.txt_download_log.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y", padx=(0, 4))

        btn_browse.config(command=self._browse_output_dir)
        self.btn_open_output.config(command=self._open_output_dir)
        self.btn_download.config(command=self._start_download)
        self.btn_stop_download.config(command=self._stop_download)
        self._populate_download_lists()
        self._restore_download_form()

    def _setup_auto_tab(self):
        """Auto-update tab: refresh 1m data every N minutes + local API server."""
        ar = tk.Frame(self.tab_auto, bg=AppTheme.BG_DARK)
        ar.pack(fill="x", padx=12, pady=8)
        tk.Label(ar, text="Auto Update (1m)", fg=AppTheme.TEXT_BRIGHT,
                 bg=AppTheme.BG_DARK, font=("Segoe UI", 12, "bold")).pack(anchor="w")

        row1 = tk.Frame(self.tab_auto, bg=AppTheme.BG_DARK)
        row1.pack(fill="x", padx=12, pady=4)
        tk.Label(row1, text="Favorite List:", fg=AppTheme.TEXT,
                 bg=AppTheme.BG_DARK, font=("Segoe UI", 9)).pack(side="left")
        self.cmbb_auto_list = ThemedCombobox(row1, state="readonly", width=20)
        self.cmbb_auto_list.pack(side="left", padx=(8, 12))

        tk.Label(row1, text="Interval (min):", fg=AppTheme.TEXT,
                 bg=AppTheme.BG_DARK, font=("Segoe UI", 9)).pack(side="left")
        self.spin_auto_interval = tk.Spinbox(
            row1, from_=1, to=1440, width=5, bg=AppTheme.BG_CARD, fg=AppTheme.TEXT,
            buttonbackground=AppTheme.ACCENT, relief="flat")
        self.spin_auto_interval.delete(0, tk.END)
        self.spin_auto_interval.insert(0, str(self.settings.get("auto_interval_min", 5)))
        self.spin_auto_interval.pack(side="left", padx=(8, 12))

        tk.Label(row1, text="Exchange:", fg=AppTheme.TEXT,
                 bg=AppTheme.BG_DARK, font=("Segoe UI", 9)).pack(side="left")
        self.cmbb_auto_exchange = ThemedCombobox(row1, state="readonly", width=12)
        self.cmbb_auto_exchange.pack(side="left", padx=(8, 12))

        row2 = tk.Frame(self.tab_auto, bg=AppTheme.BG_DARK)
        row2.pack(fill="x", padx=12, pady=4)
        tk.Label(row2, text="API Port:", fg=AppTheme.TEXT,
                 bg=AppTheme.BG_DARK, font=("Segoe UI", 9)).pack(side="left")
        self.ent_api_port = ThemedEntry(row2, width=8)
        self.ent_api_port.delete(0, tk.END)
        self.ent_api_port.insert(0, str(self.settings.get("api_port", 8900)))
        self.ent_api_port.pack(side="left", padx=(8, 12))

        self.btn_api_toggle = ThemedButton(row2, text="▶ Start API Server")
        self.btn_api_toggle.pack(side="left", padx=4)
        self.btn_auto_toggle = ThemedButton(row2, text="▶ Start Auto Update")
        self.btn_auto_toggle.pack(side="left", padx=4)

        # Live status of served symbols + last refresh
        self.lbl_auto_status = tk.Label(
            self.tab_auto, text="API server: stopped | Auto update: off",
            anchor="w", bg=AppTheme.BG_DARK, fg=AppTheme.TEXT_DIM,
            font=("Segoe UI", 8))
        self.lbl_auto_status.pack(fill="x", padx=12, pady=2)

        log_frame2 = tk.Frame(self.tab_auto, bg=AppTheme.BG_CARD, relief="sunken",
                              height=120)
        log_frame2.pack(fill="both", expand=True, padx=12, pady=4)
        log_frame2.pack_propagate(False)
        tk.Label(log_frame2, text="Auto Update Log",
                 fg=AppTheme.TEXT_BRIGHT, bg=AppTheme.BG_CARD,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=4, pady=2)
        self.txt_auto_log = tk.Text(log_frame2, height=8, wrap="word",
                                    bg=AppTheme.BG_DARK, fg=AppTheme.TEXT_DIM,
                                    font=("Segoe UI", 8), state="disabled")
        self.txt_auto_log.pack(fill="both", expand=True, padx=4, pady=2)

        self.btn_api_toggle.config(command=self._toggle_api_server)
        self.btn_auto_toggle.config(command=self._toggle_auto_update)

        # Populate dropdowns
        self._populate_auto_lists()
        self._populate_auto_exchange()

        self.btn_stop_download.config(command=self._stop_download)
        self._populate_download_lists()
        self._restore_download_form()

    def _populate_auto_lists(self):
        """Favorite lists available for auto-update."""
        try:
            names = [l["name"] for l in get_favorite_lists()]
        except Exception:
            names = []
        self.cmbb_auto_list.config(values=names)
        if names:
            last = self.settings.get("auto_list")
            if last in names:
                self.cmbb_auto_list.set(last)
            else:
                self.cmbb_auto_list.current(0)

    def _populate_auto_exchange(self):
        """Exchanges available in the auto-update tab (enabled sources)."""
        try:
            names = [s["name"] for s in get_sources() if s.get("enabled")]
        except Exception:
            names = []
        self.cmbb_auto_exchange.config(values=names)
        if names:
            cur = self.cmbb_auto_exchange.get()
            if cur not in names:
                self.cmbb_auto_exchange.current(0)

    # ---------------- local API server ----------------
    def _toggle_api_server(self):
        """Start/stop the local Binance-compatible API server."""
        if self.api_server and self.api_server.running:
            self.api_server.stop()
            self.api_server = None
            self.btn_api_toggle.config(text="▶ Start API Server")
            self._auto_log("API server stopped")
            self._refresh_auto_status()
            return

        try:
            port = int(self.ent_api_port.get())
        except ValueError:
            messagebox.showerror("Error", "API port must be a number")
            return
        output_dir = os.path.expanduser(self.ent_output_dir.get()
                                        or os.path.join("~", "mt5_data"))
        self.api_server = ApiServer(data_dir=output_dir, port=port)
        if not self.api_server.start():
            messagebox.showerror("Error", f"Port {port} is already in use")
            self.api_server = None
            return
        self.settings["api_port"] = port
        self._save_settings()
        self.btn_api_toggle.config(text="⏹ Stop API Server")
        syms = self.api_server.store.available_symbols()
        self._auto_log(f"API server started on 127.0.0.1:{port} "
                       f"— serving {len(syms)} symbols")
        self._refresh_auto_status()

    # ---------------- periodic auto-update ----------------
    def _toggle_auto_update(self):
        """Start/stop the every-N-minutes refresh of the selected list."""
        if self.auto_update_running:
            self.auto_update_running = False
            self.btn_auto_toggle.config(text="▶ Start Auto Update")
            self._auto_log("Auto update stopped")
            self._refresh_auto_status()
            return

        list_name = self.cmbb_auto_list.get()
        if not list_name:
            messagebox.showwarning("Warning", "Select a favorite list first")
            return
        exchange = self.cmbb_auto_exchange.get()
        if not exchange:
            messagebox.showwarning("Warning", "Select an exchange first")
            return
        try:
            interval = max(1, int(self.spin_auto_interval.get()))
        except ValueError:
            messagebox.showerror("Error", "Interval must be a number")
            return

        self.settings["auto_interval_min"] = interval
        self.settings["auto_list"] = list_name
        self._save_settings()

        # Snapshot the output dir NOW (main thread): reading the Tcl entry
        # var from the worker thread is not allowed.
        self.auto_output_dir = os.path.expanduser(
            self.ent_output_dir.get() or os.path.join("~", "mt5_data"))

        self.auto_update_running = True
        self.btn_auto_toggle.config(text="⏹ Stop Auto Update")
        self._auto_log(f"Auto update every {interval} min | list='{list_name}' "
                       f"| exchange={exchange}")
        # Start the API server too if it isn't running — the served data is
        # what the refresh keeps current.
        if not (self.api_server and self.api_server.running):
            self._toggle_api_server()
        self._refresh_auto_status()

        self.auto_update_thread = threading.Thread(
            target=self._auto_update_loop,
            args=(list_name, exchange, interval),
            daemon=True)
        self.auto_update_thread.start()

    def _auto_update_loop(self, list_name, exchange, interval):
        """Worker: every N minutes, fetch only the newest 1m candles.

        Uses the same incremental logic as a manual download: read the
        existing file, fetch from its last candle to now, merge. So a refresh
        costs a few pages, not a full re-download.
        """
        from database import get_favorite_list, get_favorite_lists

        def _still_running():
            # NOTE: must NOT call self.root.winfo_exists() here — Tcl from a
            # worker thread raises RuntimeError: main thread is not in main
            # loop. The flag alone is the shutdown signal; the daemon thread
            # dies with the app anyway.
            return self.auto_update_running

        while _still_running():
            # Resolve symbols each cycle in case the list was edited.
            syms = []
            try:
                lists = get_favorite_lists()
                lid = next((l["id"] for l in lists if l["name"] == list_name), None)
                if lid:
                    syms = [i["symbol"] for i in get_favorite_list(lid)]
            except Exception as e:
                self._auto_log(f"could not read list: {e}")

            now = datetime.now()
            self._auto_log(f"refreshing {len(syms)} symbols "
                           f"({now:%H:%M:%S})...")
            new_total = 0
            for sym in syms:
                if not _still_running():
                    break
                try:
                    new_total += self._refresh_one_symbol(sym, exchange, now)
                except Exception as e:
                    self._auto_log(f"{sym}: refresh failed — {e}")
            self._auto_log(f"cycle done: +{new_total:,} new candles "
                           f"| next in {interval} min")
            self._refresh_auto_status()

            # Sleep in small slices so Stop responds quickly.
            for _ in range(interval * 60):
                if not _still_running():
                    return
                time.sleep(1)

    def _refresh_one_symbol(self, symbol, exchange, now):
        """Fetch only what the file is missing since its last candle."""
        output_dir = self.auto_output_dir
        filepath = os.path.join(
            output_dir, get_mt5_filename(symbol, "1m", now, now, "csv",
                                         source_name=exchange))
        existing = read_mt5_file(filepath)
        fetch_start = now - timedelta(hours=3)
        if existing:
            have_last = max(c["timestamp"] for c in existing)
            if have_last >= now - timedelta(minutes=1):
                return 0  # already current
            fetch_start = have_last

        candles = get_candles_from_source(exchange, symbol, "1m",
                                          fetch_start, now)
        if not candles:
            return 0
        if existing:
            n = merge_and_export(existing, candles, filepath, format_type="csv")
        else:
            n = len(candles)
            export_to_mt5_format(candles, symbol, "1m", filepath)
        self._auto_log(f"{symbol}: +{len(candles)} new (file now {n:,})")
        return len(candles)

    # ---------------- helpers ----------------
    def _auto_log(self, msg):
        """Append to the auto-update log (thread-safe via the UI queue)."""
        stamp = time.strftime("%H:%M:%S")
        def _append():
            try:
                self.txt_auto_log.config(state="normal")
                self.txt_auto_log.insert(tk.END, f"[{stamp}] {msg}\n")
                self.txt_auto_log.see(tk.END)
                self.txt_auto_log.config(state="disabled")
            except Exception:
                pass
        self._safe_ui(_append)

    def _refresh_auto_status(self):
        """One-line status: API state + served symbol counts."""
        def _update():
            try:
                parts = []
                if self.api_server and self.api_server.running:
                    syms = self.api_server.store.available_symbols()
                    port = self.settings.get("api_port", 8900)
                    parts.append(f"API: 127.0.0.1:{port} | {len(syms)} symbols")
                else:
                    parts.append("API: stopped")
                parts.append("Auto update: " + ("on" if self.auto_update_running
                                                else "off"))
                self.lbl_auto_status.config(text=" | ".join(parts))
            except Exception:
                pass
        self._safe_ui(_update)

    def _populate_download_lists(self):
        lists = get_favorite_lists()
        list_names = [lst["name"] for lst in lists]
        self.cmbb_download_list.config(values=list_names)
        if list_names:
            self.cmbb_download_list.current(0)

    def _restore_download_form(self):
        """Restore last-used download form values from settings."""
        s = self.settings
        try:
            start = s.get("last_dl_start")
            end = s.get("last_dl_end")
            if start:
                self.ent_start_date.delete(0, tk.END)
                self.ent_start_date.insert(0, start)
            if end:
                self.ent_end_date.delete(0, tk.END)
                self.ent_end_date.insert(0, end)
            out = s.get("last_dl_output")
            if out:
                self.ent_output_dir.delete(0, tk.END)
                self.ent_output_dir.insert(0, out)
            tf = s.get("last_dl_timeframe")
            if tf:
                vals = list(self.cmbb_timeframe.cget("values"))
                if tf in vals:
                    self.cmbb_timeframe.current(vals.index(tf))
            ex = s.get("last_dl_exchange")
            if ex:
                self._populate_exchange_combobox()
                vals = list(self.cmbb_exchange.cget("values"))
                if ex in vals:
                    self.cmbb_exchange.current(vals.index(ex))
            lst = s.get("last_dl_list")
            if lst:
                vals = list(self.cmbb_download_list.cget("values"))
                if lst in vals:
                    self.cmbb_download_list.current(vals.index(lst))
        except Exception:
            pass

    def _browse_output_dir(self):
        current = self.ent_output_dir.get()
        selected = filedialog.askdirectory(initialdir=current, title="Select Output Directory")
        if selected:
            self.ent_output_dir.delete(0, tk.END)
            self.ent_output_dir.insert(0, selected)

    def _open_output_dir(self):
        """Open the output folder in the OS file explorer."""
        out = self.ent_output_dir.get().strip()
        if not out:
            return
        os.makedirs(out, exist_ok=True)
        try:
            if sys.platform.startswith("win"):
                os.startfile(out)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", out])
            else:
                subprocess.Popen(["xdg-open", out])
        except Exception as e:
            messagebox.showerror("Error", f"Could not open folder:\n{e}")

    def _populate_exchange_combobox(self):
        """Fill the exchange dropdown with ENABLED candle sources only."""
        try:
            sources = [s for s in get_sources() if s.get("enabled")]
        except Exception:
            sources = []
        names = [s["name"] for s in sources]
        self.cmbb_exchange.config(values=names)
        # Keep current selection if still valid, else first entry
        cur = self.cmbb_exchange.get()
        if cur not in names:
            self.cmbb_exchange.current(0 if names else -1)

    def _on_now_toggled(self):
        """NOW checkbox: lock end date to the current date+time.

        Without NOW, an end date of today parses to midnight 00:00, silently
        dropping the current day. With NOW, the exact current hour/minute is
        used and the entry is greyed out so it can't drift out of sync.
        """
        if self.var_now.get():
            self.ent_end_date.delete(0, tk.END)
            self.ent_end_date.insert(0, datetime.now().strftime("%Y.%m.%d"))
            self.ent_end_date.config(state="disabled")
        else:
            self.ent_end_date.config(state="normal")

    def _start_download(self):
        # Initialize stop flag
        self.download_stopped = False
        self.btn_stop_download.config(state="normal")
        self.lbl_progress.config(text="0%")

        list_name = self.cmbb_download_list.get()
        if not list_name:
            messagebox.showwarning("Warning", "Please select a favorite list")
            return

        timeframe = self.cmbb_timeframe.get()
        exchange = self.cmbb_exchange.get()
        if not exchange:
            messagebox.showwarning("Warning", "Please select an exchange")
            return
        start_str = self.ent_start_date.get()
        end_str = self.ent_end_date.get()
        fmt = self.cmbb_format.get()
        output_dir = self.ent_output_dir.get()

        try:
            start_date = datetime.strptime(start_str, "%Y.%m.%d")
            # NOW: use current hour/minute instead of 00:00 midnight.
            if self.var_now.get():
                end_date = datetime.now()
            else:
                end_date = datetime.strptime(end_str, "%Y.%m.%d")
        except ValueError:
            messagebox.showerror("Error", "Invalid date format. Use YYYY.MM.DD")
            return

        os.makedirs(output_dir, exist_ok=True)

        lists = get_favorite_lists()
        list_id = None
        for lst in lists:
            if lst["name"] == list_name:
                list_id = lst["id"]
                break
        if not list_id:
            messagebox.showerror("Error", "Selected list not found")
            return

        items = get_favorite_list(list_id)
        if not items:
            messagebox.showwarning("Warning", "List is empty")
            return

        symbols = [item["symbol"] for item in items]
        total = len(symbols)
        self.lbl_download_status.config(text=f"Downloading {total} symbols...")
        self.btn_download.config(state="disabled")
        self.progress_download["mode"] = "determinate"
        self.progress_download["maximum"] = total
        self.progress_download["value"] = 0

        # Save format preference
        self.settings["mt5_export_format"] = fmt
        # Persist last-used download form values
        self.settings["last_dl_list"] = list_name
        self.settings["last_dl_timeframe"] = timeframe
        self.settings["last_dl_exchange"] = exchange
        self.settings["last_dl_start"] = start_str
        self.settings["last_dl_end"] = end_str
        self.settings["last_dl_output"] = output_dir
        self._save_settings()

        # Reset stop flag for this new download
        self.download_stopped = False

        # Wall-clock start for ETA estimation (set before the worker starts)
        _download_t0 = time.time()

        def do_download():
            import traceback
            from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
            import time

            total_symbols = len(symbols)
            total_candles = [0]  # mutable list so inner functions can update
            completed = [0]      # mutable counter for thread-safe progress

            def _log(msg):
                """Log to both debug file and GUI text widget (thread-safe)."""
                try:
                    log_path = os.path.join(os.path.expanduser("~"),
                                            ".crypto_market_downloader", "debug.log")
                    with open(log_path, "a") as lf:
                        lf.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
                except:
                    pass
                # Marshal onto the main thread via the UI queue. Never let a
                # GUI-update failure kill the download thread.
                self._safe_ui(lambda m=msg: _append_log(m))

            def _clear_log():
                try:
                    self.txt_download_log.config(state="normal")
                    self.txt_download_log.delete("1.0", tk.END)
                    self.txt_download_log.config(state="disabled")
                except Exception:
                    pass

            def _append_log(msg):
                try:
                    self.txt_download_log.config(state="normal")
                    self.txt_download_log.insert(tk.END, msg + "\n")
                    self.txt_download_log.see(tk.END)
                    self.txt_download_log.config(state="disabled")
                except Exception:
                    pass

            def _update_progress(done, candles_count, sub_pct=None, sub_label=None):
                """Thread-safe: update progress bar + percentage label + ETA.

                done: number of completed symbols
                candles_count: total candles so far
                sub_pct: optional per-symbol page progress 0-100 (current symbol)
                sub_label: optional descriptive text for the current activity
                """
                def _do_update():
                    try:
                        if sub_pct is not None:
                            # Fraction inside current symbol, blended with done count
                            frac = done + min(sub_pct, 100) / 100.0
                            pct = int(frac / total_symbols * 100)
                        else:
                            pct = int(done / total_symbols * 100) if total_symbols else 0
                        elapsed = time.time() - _download_t0
                        eta_txt = ""
                        if (done > 0 or sub_pct) and elapsed > 0:
                            eff = done + (min(sub_pct or 0, 100) / 100.0)
                            if eff > 0:
                                remaining = (elapsed / eff) * (total_symbols - eff)
                                if remaining >= 3600:
                                    eta_txt = f" | ETA {remaining/3600:.1f}h"
                                elif remaining >= 60:
                                    eta_txt = f" | ETA {remaining/60:.0f}m"
                                else:
                                    eta_txt = f" | ETA {remaining:.0f}s"
                        if sub_label:
                            eta_txt = f"{eta_txt} | {sub_label}"
                        self.progress_download.config(value=done)
                        self.lbl_progress.config(
                            text=f"{pct}% ({done}/{total_symbols}) | {candles_count:,} candles{eta_txt}")
                    except Exception:
                        pass
                self._safe_ui(_do_update)

            # Clear previous log
            self._safe_ui(_clear_log)

            _log(f"do_download started | {total_symbols} symbols: {symbols}")
            _log(f"timeframe={timeframe}, start={start_date.strftime('%Y.%m.%d')}, end={end_date.strftime('%Y.%m.%d')}")
            _log(f"proxy: {self.settings.get('proxy_type')} @ {self.settings.get('default_proxy')}")
            _log("")

            successful = 0
            failed = 0

            def download_symbol(symbol):
                nonlocal total_candles
                try:
                    _log(f"Fetching {symbol} ({timeframe}) from {exchange}...")

                    # Sub-symbol progress: pages within a symbol's fetch.
                    # Long ranges (e.g. 1m since Jan) take minutes per symbol;
                    # without this the GUI shows 0% the whole time.
                    def _on_page(sym, source_name, page, total_pages, so_far):
                        try:
                            pct = int(page / max(total_pages, 1) * 100)
                            _log(f"{sym} [{source_name}]: page {page}/"
                                 f"{total_pages or '?'} ({pct}%) — {so_far:,} candles")
                            _update_progress(
                                completed[0], total_candles[0] + so_far,
                                sub_pct=pct,
                                sub_label=f"{sym} [{source_name}] {pct}%")
                        except Exception:
                            pass

                    # Filename pins the exchange: SYMBOL_TF_EXCHANGE.csv.
                    # Each venue keeps its own file so histories never mix.
                    filename = get_mt5_filename(symbol, timeframe,
                                                start_date, end_date,
                                                fmt, source_name=exchange)
                    filepath = os.path.join(output_dir, filename)

                    # Incremental: reuse what we already have and only fetch
                    # the parts of [start, end] the file does not cover.
                    # The gap can be on BOTH sides at once (e.g. earlier start
                    # + NOW end), so back and forward gaps are fetched
                    # separately and merged.
                    existing = read_mt5_file(filepath)
                    fetches = []  # list of (start, end) windows to download
                    if existing:
                        have_first = min(c["timestamp"] for c in existing)
                        have_last = max(c["timestamp"] for c in existing)
                        if have_first <= start_date and have_last >= end_date:
                            _log(f"{symbol}: {len(existing):,} candles already "
                                 f"cover the range — skipping download")
                            total_candles[0] += len(existing)
                            return symbol, True, None, len(existing)
                        if have_first > start_date:
                            fetches.append((start_date, have_first))  # back gap
                        if have_last < end_date:
                            fetches.append((have_last, end_date))      # forward gap
                        gaps = "; ".join(
                            f"{a:%Y.%m.%d %H:%M}–{b:%Y.%m.%d %H:%M}"
                            for a, b in fetches) or "none"
                        _log(f"{symbol}: have {len(existing):,} candles "
                              f"({have_first:%Y.%m.%d %H:%M}–{have_last:%Y.%m.%d %H:%M}); "
                              f"fetching gaps: {gaps}")
                    else:
                        fetches.append((start_date, end_date))

                    candles = []
                    for fs, fe in fetches:
                        part = get_candles_from_source(
                            exchange, symbol, timeframe, fs, fe,
                            on_progress=_on_page)
                        if part:
                            candles.extend(part)

                    if not candles:
                        if existing:
                            _log(f"{symbol}: no new candles from {exchange}; "
                                 f"keeping existing {len(existing):,}")
                            return symbol, True, None, len(existing)
                        _log(f"{symbol}: no candles returned")
                        return symbol, False, "No candles returned"

                    # Check stop flag AFTER network call, BEFORE export
                    if getattr(self, 'download_stopped', False):
                        _log(f"{symbol}: stop requested, skipping export")
                        return symbol, False, "Stopped", 0

                    if existing:
                        n = merge_and_export(existing, candles, filepath,
                                             format_type=fmt)
                    else:
                        n = len(candles)
                        export_to_mt5_format(candles, symbol, timeframe,
                                             filepath, format_type=fmt)
                    total_candles[0] += len(candles)
                    _log(f"{symbol}: +{len(candles):,} new from {exchange}, "
                         f"file now {n:,} candles")
                    save_downloaded_data(list_name, symbol, timeframe,
                                         start_str, end_str, exchange, filepath)
                    return symbol, True, None, n
                except Exception as e:
                    _log(f"{symbol} FAILED: {e}")
                    return symbol, False, str(e), 0

            try:
                # Use ThreadPoolExecutor but check stop flag between each completed future
                max_workers = self.settings.get("max_workers", 10)
                executor = ThreadPoolExecutor(max_workers=max_workers)
                # Submit all tasks
                futures = {}
                for sym in symbols:
                    futures[executor.submit(download_symbol, sym)] = sym

                # Poll for completed futures, checking stop flag frequently
                pending = set(futures.keys())
                while pending and not getattr(self, 'download_stopped', False):
                    done_set, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)

                    if getattr(self, 'download_stopped', False):
                        _log("Download stopped by user")
                        break

                    for future in done_set:
                        sym = futures[future]
                        try:
                            result = future.result()
                            symbol, success, error = result[0], result[1], result[2]
                            candles_count = result[3] if len(result) > 3 else 0
                        except Exception as e:
                            symbol = "UNKNOWN"
                            success = False
                            error = str(e)
                            candles_count = 0
                            _log(f"Future exception for {sym}: {e}")

                        completed[0] += 1
                        if success:
                            successful += 1
                        else:
                            failed += 1
                            _log(f"  ✗ {symbol}: {str(error)[:80]}")

                        _update_progress(completed[0], total_candles[0])

                # Cancel pending futures — don't wait for running ones
                executor.shutdown(wait=False, cancel_futures=True)

            except Exception:
                _log(f"do_download outer exception: {traceback.format_exc()}")

            stopped = getattr(self, 'download_stopped', False)
            done_count = completed[0]

            def _finish():
                try:
                    if stopped:
                        pct = int(done_count / total_symbols * 100) if total_symbols else 0
                        txt = f"⏹ Stopped. {successful} done, {failed} failed, {total_candles[0]} candles"
                        self.lbl_download_status.config(text=txt)
                        self.lbl_progress.config(
                            text=f"{pct}% ({done_count}/{total_symbols}) | stopped")
                    else:
                        txt = f"✅ Complete! {successful} succeeded, {failed} failed, {total_candles[0]} candles"
                        self.lbl_download_status.config(text=txt)
                        self.lbl_progress.config(
                            text=f"100% ({total_symbols}/{total_symbols}) | {total_candles[0]} candles")
                    self.btn_download.config(state="normal")
                    self.btn_stop_download.config(state="disabled")
                    # Persist summary + update global status bar
                    try:
                        self.settings["last_download_summary"] = txt
                        self._save_settings()
                        self.set_status(f"{txt}  |  Output: {output_dir}")
                    except Exception:
                        pass
                except Exception:
                    pass
                _log(f"\n{txt}")

            self._safe_ui(_finish)

        threading.Thread(target=do_download, daemon=True).start()

    def _stop_download(self):
        """Stop the current download."""
        self.download_stopped = True
        self.btn_stop_download.config(state="disabled")
        self.btn_download.config(state="disabled")
        self.lbl_download_status.config(text="Stopping...")
        self.lbl_progress.config(text="Stopping...")

    # ==================== Sources Tab ====================

    def _setup_sources_tab(self):
        top_row = tk.Frame(self.tab_sources, bg=AppTheme.BG_DARK)
        top_row.pack(fill="x", padx=12, pady=8)
        tk.Label(top_row, text="Data Sources", fg=AppTheme.TEXT_BRIGHT,
                 bg=AppTheme.BG_DARK, font=("Segoe UI", 12, "bold")).pack(anchor="w")

        tree_frame = tk.Frame(self.tab_sources, bg=AppTheme.BG_DARK)
        tree_frame.pack(fill="both", expand=True, padx=12, pady=4)

        self.tree_sources = ThemedTreeview(
            tree_frame,
            columns=("name", "type", "enabled", "proxy", "limit", "status"))
        self.tree_sources.heading("name", text="Source Name")
        self.tree_sources.heading("type", text="Type")
        self.tree_sources.heading("enabled", text="Enabled")
        self.tree_sources.heading("proxy", text="Use Proxy")
        self.tree_sources.heading("limit", text="History Depth")
        self.tree_sources.heading("status", text="Last Status")
        self.tree_sources.column("#0", width=0, stretch=False)
        self.tree_sources.column("name", width=110)
        self.tree_sources.column("type", width=60, anchor="center")
        self.tree_sources.column("enabled", width=56, anchor="center")
        self.tree_sources.column("proxy", width=64, anchor="center")
        self.tree_sources.column("limit", width=130, anchor="center")
        self.tree_sources.column("status", width=180)
        self.tree_sources.pack(side="left", fill="both", expand=True)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree_sources.yview)
        self.tree_sources.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")

        # Click column headings to sort
        for col in ("name", "type", "enabled", "proxy", "limit", "status"):
            self.tree_sources.heading(col,
                command=lambda c=col: self._sort_treeview(self.tree_sources, c))
        self._source_sort = {"col": "name", "reverse": False}

        btn_row = tk.Frame(self.tab_sources, bg=AppTheme.BG_DARK)
        btn_row.pack(fill="x", padx=12, pady=4)
        self.btn_test_source = ThemedButton(btn_row, text="🧪 Test Selected")
        self.btn_test_source.pack(side="left", padx=4)
        self.btn_test_all_sources = ThemedButton(btn_row, text="🧪 Test All Sources")
        self.btn_test_all_sources.pack(side="left", padx=4)
        self.btn_toggle_source = ThemedButton(btn_row, text="🔄 Toggle Enabled")
        self.btn_toggle_source.pack(side="left", padx=4)
        self.btn_toggle_proxy = ThemedButton(btn_row, text="🔌 Toggle Proxy")
        self.btn_toggle_proxy.pack(side="left", padx=4)

        self.lbl_source_status = tk.Label(self.tab_sources, text="Ready",
                                          anchor="w", bg=AppTheme.BG_DARK,
                                          fg=AppTheme.TEXT_DIM, font=("Segoe UI", 8))
        self.lbl_source_status.pack(fill="x", padx=12, pady=2)

        self.btn_test_source.config(command=self._test_selected_source)
        self.btn_toggle_source.config(command=self._toggle_selected_source)
        self.btn_toggle_proxy.config(command=self._toggle_selected_proxy)
        self.btn_test_all_sources.config(command=self._test_all_sources)

        # Toggle enabled/disabled on double-click
        self.tree_sources.bind("<Double-Button-1>", self._toggle_source_enabled)

        self._load_sources()

    def _load_sources(self):
        sources = get_sources()
        # History depth per source is a property of each exchange's API,
        # verified by the live 3-day 1m test — shown instead of the old
        # priority column, which no selection path reads anymore.
        from source_registry import get_source_capability
        caps = {}
        try:
            for s in sources:
                caps[s["name"]] = get_source_capability(s["name"])
        except Exception:
            pass
        for item in self.tree_sources.get_children():
            self.tree_sources.delete(item)
        for s in sources:
            limit, page = caps.get(s["name"], ("?", "?"))
            # "full" reads nicer than "unlimited" in the narrow column.
            limit_text = "Full history" if limit == "full" else str(limit)
            self.tree_sources.insert("", "end", values=(
                s["name"], s["type"],
                "Yes" if s.get("enabled") else "No",
                "Yes" if s.get("use_proxy") else "No",
                limit_text,
                s.get("last_status", "N/A") or "N/A"))
        n_on = sum(1 for s in sources if s.get("enabled"))
        self.set_status(
            f"Sources: {n_on}/{len(sources)} enabled  |  "
            f"Last download: {self.settings.get('last_download_summary', '—')}")

    def _test_selected_source(self):
        selected = self.tree_sources.selection()
        if not selected:
            messagebox.showwarning("Warning", "Select a source to test")
            return
        values = self.tree_sources.item(selected[0])["values"]
        source_name = values[0]
        self.lbl_source_status.config(text=f"Testing {source_name}...")

        def do_test():
            def _update_tree_item(name, status_text):
                """Thread-safe: update source status in the tree."""
                self._safe_ui(lambda: _update_tree_item_sync(name, status_text))

            def _update_tree_item_sync(name, status_text):
                for item in self.tree_sources.get_children():
                    ivals = self.tree_sources.item(item)["values"]
                    if ivals[0] == name:
                        ivals[5] = status_text
                        self.tree_sources.item(item, values=ivals)
                        break

            try:
                from source_registry import get_list_source_instance
                instance = get_list_source_instance(source_name)
                data = instance.fetch_list(3, True)
                if data:
                    update_source_status(source_name, "success")
                    _update_tree_item(source_name, "✅ success")
                    self._safe_ui(lambda n=source_name:
                        self.lbl_source_status.config(text=f"\u2705 {n} working"))
                else:
                    update_source_status(source_name, "no data")
                    _update_tree_item(source_name, "\u26a0 no data")
                    self._safe_ui(lambda n=source_name:
                        self.lbl_source_status.config(text=f"\u26A0 {n} returned no data"))
            except Exception as e:
                err = str(e)[:60]
                update_source_status(source_name, f"failed: {err}")
                _update_tree_item(source_name, f"\u274C {err}")
                self._safe_ui(lambda n=source_name, e=err:
                    self.lbl_source_status.config(text=f"\u274C {n} failed: {e}"))

        threading.Thread(target=do_test, daemon=True).start()

    def _toggle_source_enabled(self, event):
        """Double-click on a source row to toggle its enabled state (auto-persists)."""
        item = self.tree_sources.identify_row(event.y)
        if not item:
            return
        from database import update_source
        values = list(self.tree_sources.item(item, "values"))
        values[2] = "No" if values[2] == "Yes" else "Yes"
        self.tree_sources.item(item, values=values)
        enabled = values[2] == "Yes"
        try:
            update_source(values[0], enabled=enabled)
        except Exception:
            pass
        self._sync_settings_after_toggle()

    def _sync_settings_after_toggle(self):
        """Sync settings.json with current tree state so failover/proxy_manager see toggles immediately."""
        try:
            self._populate_exchange_combobox()
        except Exception:
            pass
        db_map = {}
        try:
            db_map = {s["name"]: s for s in get_sources()}
        except Exception:
            db_map = {}
        priorities = []
        for item in self.tree_sources.get_children():
            values = self.tree_sources.item(item, "values")
            prev = db_map.get(values[0], {})
            priorities.append({
                "name": values[0], "type": values[1],
                "priority": prev.get("priority", 1),
                "enabled": values[2] == "Yes",
                "proxy": values[3] == "Yes"
            })
        settings = self.settings.copy()
        settings["source_priorities"] = priorities
        save_settings(settings)

    def _toggle_selected_source(self):
        """Button: toggle enabled state of all selected sources (auto-persists)."""
        selected = self.tree_sources.selection()
        if not selected:
            messagebox.showwarning("Warning", "Select a source to toggle")
            return
        from database import update_source
        for item in selected:
            values = list(self.tree_sources.item(item, "values"))
            values[2] = "No" if values[2] == "Yes" else "Yes"
            self.tree_sources.item(item, values=values)
            enabled = values[2] == "Yes"
            try:
                update_source(values[0], enabled=enabled)
            except Exception:
                pass
        self._sync_settings_after_toggle()

    def _toggle_selected_proxy(self):
        """Button: toggle use-proxy state of all selected sources (auto-persists)."""
        selected = self.tree_sources.selection()
        if not selected:
            messagebox.showwarning("Warning", "Select a source to toggle")
            return
        from database import update_source
        for item in selected:
            values = list(self.tree_sources.item(item, "values"))
            values[3] = "No" if values[3] == "Yes" else "Yes"
            self.tree_sources.item(item, values=values)
            use_proxy = values[3] == "Yes"
            try:
                update_source(values[0], use_proxy=use_proxy)
            except Exception:
                pass
        self._sync_settings_after_toggle()

    # ==================== Settings ====================

    # ==================== Settings Tab ====================

    def _setup_settings_tab(self):
        """Unified Settings tab: general settings + proxy configuration in one place."""
        # ---- General settings section ----
        gen_frame = tk.Frame(self.tab_settings, bg=AppTheme.BG_DARK)
        gen_frame.pack(fill="x", padx=12, pady=(8, 4))
        tk.Label(gen_frame, text="⚙️ General Settings", fg=AppTheme.TEXT_BRIGHT,
                 bg=AppTheme.BG_DARK, font=("Segoe UI", 12, "bold")).pack(anchor="w")

        fields = [
            ("top_n", "Top N Cryptocurrencies", 10, 500),
            ("max_workers", "Max Parallel Downloads", 1, 50),
            ("request_timeout", "Request Timeout (sec)", 5, 120),
            ("retry_attempts", "Retry Attempts", 0, 10),
            ("retry_delay", "Retry Delay (sec)", 0, 30),
        ]
        self.settings_entries = {}
        for key, label, min_val, max_val in fields:
            row = tk.Frame(gen_frame, bg=AppTheme.BG_DARK)
            row.pack(fill="x", pady=3)
            tk.Label(row, text=label, fg=AppTheme.TEXT,
                     bg=AppTheme.BG_DARK, font=("Segoe UI", 9),
                     width=24, anchor="w").pack(side="left")
            spin = tk.Spinbox(row, from_=min_val, to=max_val, width=8,
                              bg=AppTheme.BG_CARD, fg=AppTheme.TEXT,
                              buttonbackground=AppTheme.ACCENT, relief="flat")
            spin.delete(0, tk.END)
            spin.insert(0, str(self.settings.get(key, DEFAULTS.get(key, 10))))
            spin.pack(side="left", padx=(4, 0))
            self.settings_entries[key] = spin

        cb_row = tk.Frame(gen_frame, bg=AppTheme.BG_DARK)
        cb_row.pack(fill="x", pady=4)
        self.var_exclude = tk.BooleanVar(value=self.settings.get("exclude_stablecoins", True))
        tk.Checkbutton(cb_row, text="Exclude Stablecoins", variable=self.var_exclude,
                       bg=AppTheme.BG_DARK, fg=AppTheme.TEXT,
                       selectcolor=AppTheme.BG_CARD,
                       activebackground=AppTheme.BG_DARK,
                       activeforeground=AppTheme.TEXT).pack(anchor="w")
        self.var_progress = tk.BooleanVar(value=self.settings.get("show_progress", True))
        tk.Checkbutton(cb_row, text="Show Progress Bar", variable=self.var_progress,
                       bg=AppTheme.BG_DARK, fg=AppTheme.TEXT,
                       selectcolor=AppTheme.BG_CARD,
                       activebackground=AppTheme.BG_DARK,
                       activeforeground=AppTheme.TEXT).pack(anchor="w")

        btn_gen_row = tk.Frame(gen_frame, bg=AppTheme.BG_DARK)
        btn_gen_row.pack(pady=(8, 4))
        self.btn_save_general = ThemedButton(btn_gen_row, text="💾 Save General Settings")
        self.btn_save_general.pack(side="left", padx=4)
        self.btn_save_general.config(command=self._save_general_settings)

        # ---- Proxy section ----
        tk.Frame(self.tab_settings, height=1, bg=AppTheme.BG_CARD).pack(
            fill="x", padx=24, pady=8)

        proxy_frame = tk.Frame(self.tab_settings, bg=AppTheme.BG_DARK)
        proxy_frame.pack(fill="x", padx=12, pady=(0, 8))
        tk.Label(proxy_frame, text="🌐 Proxy Configuration", fg=AppTheme.TEXT_BRIGHT,
                 bg=AppTheme.BG_DARK, font=("Segoe UI", 12, "bold")).pack(anchor="w")

        tk.Label(proxy_frame, text="Proxy Type:", fg=AppTheme.TEXT,
                 bg=AppTheme.BG_DARK, font=("Segoe UI", 9)).pack(anchor="w")
        self.var_proxy_type = tk.StringVar(value=self.settings.get("proxy_type", "none"))
        for val, lbl in [("none", "None"), ("http", "HTTP"),
                         ("socks5", "SOCKS5"), ("socks4", "SOCKS4")]:
            ttk.Radiobutton(proxy_frame, text=lbl, variable=self.var_proxy_type,
                            value=val).pack(anchor="w", padx=20, pady=1)

        tk.Label(proxy_frame, text="Proxy Address (host:port):", fg=AppTheme.TEXT,
                 bg=AppTheme.BG_DARK, font=("Segoe UI", 9)).pack(anchor="w", pady=(8, 0))
        self.ent_proxy_addr = tk.Entry(proxy_frame, font=("Segoe UI", 9),
                                       bg=AppTheme.BG_CARD, fg=AppTheme.TEXT, width=30)
        self.ent_proxy_addr.insert(0, self.settings.get("default_proxy", ""))
        self.ent_proxy_addr.pack(anchor="w", pady=2)

        btn_proxy_row = tk.Frame(proxy_frame, bg=AppTheme.BG_DARK)
        btn_proxy_row.pack(pady=6)
        self.btn_test_proxy = ThemedButton(btn_proxy_row, text="🧪 Test Proxy")
        self.btn_test_proxy.pack(side="left", padx=4)
        self.btn_save_proxy = ThemedButton(btn_proxy_row, text="💾 Save Proxy")
        self.btn_save_proxy.pack(side="left", padx=4)
        self.btn_test_proxy.config(command=self._test_proxy_in_tab)
        self.btn_save_proxy.config(command=self._save_proxy_in_tab)

        # ---- Status line ----
        self.lbl_settings_status = tk.Label(
            self.tab_settings, text="Ready", anchor="w",
            bg=AppTheme.BG_DARK, fg=AppTheme.TEXT_DIM, font=("Segoe UI", 8))
        self.lbl_settings_status.pack(side="bottom", fill="x", padx=12, pady=2)

    def _save_general_settings(self):
        """Save general settings from the Settings tab spinboxes/checkboxes."""
        try:
            for key, widget in self.settings_entries.items():
                self.settings[key] = int(widget.get())
            self.settings["exclude_stablecoins"] = self.var_exclude.get()
            self.settings["show_progress"] = self.var_progress.get()
            save_settings(self.settings)
            self.lbl_settings_status.config(text="General settings saved ✓")
        except ValueError:
            messagebox.showerror("Error", "All numeric settings must be numbers")
        except Exception as e:
            messagebox.showerror("Error", f"Could not save settings:\n{e}")

    def _save_proxy_in_tab(self):
        """Save proxy configuration from the Settings tab."""
        self.settings["proxy_type"] = self.var_proxy_type.get()
        self.settings["default_proxy"] = self.ent_proxy_addr.get().strip()
        save_settings(self.settings)
        self.lbl_settings_status.config(text="Proxy settings saved ✓")

    def _test_proxy_in_tab(self):
        """Test proxy connection from the Settings tab (non-blocking)."""
        proxy_type = self.var_proxy_type.get()
        proxy_addr = self.ent_proxy_addr.get().strip()
        if not proxy_addr or proxy_type == "none":
            messagebox.showwarning("Warning", "Enter a proxy address and select a type first")
            return
        from proxy_manager import test_proxy_connection
        self.lbl_settings_status.config(text="Testing proxy…")
        self.btn_test_proxy.config(state="disabled")

        def do_test():
            try:
                success, msg = test_proxy_connection(proxy_type, proxy_addr)
            except Exception as e:
                success, msg = False, str(e)

            def _done():
                self.btn_test_proxy.config(state="normal")
                self.lbl_settings_status.config(
                    text=("Proxy OK ✓ " if success else "Proxy failed ✗ ") + msg[:80])
                if success:
                    messagebox.showinfo("Success", msg)
                else:
                    messagebox.showerror("Failed", msg)
            self._safe_ui(_done)

        threading.Thread(target=do_test, daemon=True).start()

    # ==================== Misc ====================

    def set_status(self, text):
        """Update the global bottom status bar (call from main thread)."""
        try:
            self.status_bar.config(text=text)
        except Exception:
            pass

    def _sort_treeview(self, tree, col):
        """Sort a Treeview by a column; click again to reverse direction."""
        items = [(tree.set(k, col), k) for k in tree.get_children("")]
        # Numeric columns sort by value, text columns case-insensitively
        def _key(v):
            s = str(v[0]).strip()
            if s.replace(".", "", 1).lstrip("-").isdigit():
                return (0, float(s))
            return (1, s.lower())
        items.sort(key=_key, reverse=self._source_sort["reverse"])
        for idx, (val, k) in enumerate(items):
            tree.move(k, "", idx)
        self._source_sort["reverse"] = not self._source_sort["reverse"]
        tree.heading(col, text=col.title() +
                     (" ▲" if not self._source_sort["reverse"] else " ▼"))

    def _safe_ui(self, func):
        """Queue a UI update from a worker thread, thread-safely.

        Worker threads must never call into Tcl directly (root.after raises
        RuntimeError when the interpreter is inside root.update and is not
        thread-safe anyway). Instead we enqueue the callback and the main
        thread's _poll_ui_queue loop drains it.
        """
        self._ui_queue.put(func)

    def _poll_ui_queue(self):
        """Main-thread loop: drain UI updates queued by worker threads."""
        try:
            while True:
                func = self._ui_queue.get_nowait()
                try:
                    func()
                except Exception:
                    pass
        except queue.Empty:
            pass
        # Re-arm; safe under both root.mainloop() and root.update()
        try:
            self.root.after(UI_POLL_MS, self._poll_ui_queue)
        except RuntimeError:
            pass

    def _test_all_sources(self):
        """Test connectivity of every enabled source, updating the status column live."""
        sources = [s for s in get_sources() if s.get("enabled")]
        if not sources:
            messagebox.showwarning("Warning", "No enabled sources to test")
            return
        self.lbl_source_status.config(text=f"Testing {len(sources)} sources…")
        self.btn_test_all_sources.config(state="disabled")

        def do_test_all():
            from source_registry import get_list_source_instance
            for s in sources:
                try:
                    instance = get_list_source_instance(s["name"])
                    data = instance.fetch_list(1, True)
                    update_source_status(s["name"], "success" if data else "no data")
                except Exception as e:
                    update_source_status(s["name"], f"failed: {str(e)[:80]}")
                # Refresh the tree live after each source
                self._safe_ui(self._load_sources)

            def _done():
                self.btn_test_all_sources.config(state="normal")
                self.lbl_source_status.config(text="All sources tested ✓")
            self._safe_ui(_done)

        threading.Thread(target=do_test_all, daemon=True).start()


# ==================== Dialogs ====================

class SimpleDialog:
    """Simple text input dialog."""

    def __init__(self, parent, title, prompt):
        self.parent = parent
        self.title = title
        self.prompt = prompt
        self.result = None
        self.dialog = None

    def show(self):
        self.dialog = tk.Toplevel(self.parent)
        self.dialog.title(self.title)
        self.dialog.configure(bg=AppTheme.BG_DARK)
        self.dialog.transient(self.parent)
        self.dialog.grab_set()
        self.dialog.geometry("350x130")

        tk.Label(self.dialog, text=self.prompt, fg=AppTheme.TEXT,
                 bg=AppTheme.BG_DARK, font=("Segoe UI", 10)).pack(pady=12)
        self.entry = tk.Entry(self.dialog, font=("Segoe UI", 10),
                              bg=AppTheme.BG_CARD, fg=AppTheme.TEXT, width=30)
        self.entry.pack(pady=4)
        self.entry.focus()
        self.entry.bind("<Return>", lambda e: self._ok())

        btn_row = tk.Frame(self.dialog, bg=AppTheme.BG_DARK)
        btn_row.pack(pady=8)
        tk.Button(btn_row, text="OK", width=8, command=self._ok,
                  bg=AppTheme.SUCCESS, fg=AppTheme.TEXT_BRIGHT,
                  font=("Segoe UI", 9, "bold"),
                  activebackground=AppTheme.ACCENT_HOVER,
                  activeforeground=AppTheme.TEXT_BRIGHT).pack(side="left", padx=4)
        tk.Button(btn_row, text="Cancel", width=8, command=self._cancel,
                  bg=AppTheme.BG_CARD, fg=AppTheme.TEXT,
                  font=("Segoe UI", 9),
                  activebackground=AppTheme.BG_DARK,
                  activeforeground=AppTheme.TEXT_BRIGHT).pack(side="left", padx=4)
        self.dialog.wait_window()
        return self.result

    def _ok(self):
        self.result = self.entry.get()
        self.dialog.destroy()

    def _cancel(self):
        self.result = None
        self.dialog.destroy()


class FavoriteSelectDialog:
    """Dialog to select or create a favorite list for adding symbols."""

    def __init__(self, parent, callback, symbols, app):
        self.parent = parent
        self.callback = callback
        self.symbols = symbols
        self.app = app
        self.dialog = None
        self.list_name = tk.StringVar()

    def show(self):
        self.dialog = tk.Toplevel(self.parent)
        self.dialog.title("Add to Favorites")
        self.dialog.configure(bg=AppTheme.BG_DARK)
        self.dialog.transient(self.parent)
        self.dialog.grab_set()
        self.dialog.geometry("380x160")

        tk.Label(self.dialog, text=f"Add {len(self.symbols)} symbols to:",
                 fg=AppTheme.TEXT_BRIGHT, bg=AppTheme.BG_DARK,
                 font=("Segoe UI", 11, "bold")).pack(pady=8)

        existing_lists = get_favorite_lists()
        list_names = [lst["name"] for lst in existing_lists]

        if list_names:
            tk.Label(self.dialog, text="Select existing list:",
                     fg=AppTheme.TEXT, bg=AppTheme.BG_DARK,
                     font=("Segoe UI", 9)).pack(anchor="w", padx=20)
            self.cmbb = ttk.Combobox(self.dialog, textvariable=self.list_name,
                                     values=list_names, state="readonly", width=30)
            self.cmbb.pack(pady=4)
        else:
            tk.Label(self.dialog, text="No existing lists. Enter new list name:",
                     fg=AppTheme.TEXT, bg=AppTheme.BG_DARK,
                     font=("Segoe UI", 9)).pack(pady=8)

        self.ent_new = tk.Entry(self.dialog, font=("Segoe UI", 10),
                                bg=AppTheme.BG_CARD, fg=AppTheme.TEXT, width=30)
        self.ent_new.pack(pady=4)
        self.ent_new.insert(0, "")
        self.ent_new.bind("<Return>", lambda e: self._ok())

        btn_row = tk.Frame(self.dialog, bg=AppTheme.BG_DARK)
        btn_row.pack(pady=8)
        tk.Button(btn_row, text="OK", width=8, command=self._ok,
                  bg=AppTheme.SUCCESS, fg=AppTheme.TEXT_BRIGHT,
                  font=("Segoe UI", 9, "bold"),
                  activebackground=AppTheme.ACCENT_HOVER).pack(side="left", padx=4)
        tk.Button(btn_row, text="Cancel", width=8, command=self._cancel,
                  bg=AppTheme.BG_CARD, fg=AppTheme.TEXT,
                  font=("Segoe UI", 9),
                  activebackground=AppTheme.BG_DARK).pack(side="left", padx=4)

        self.dialog.wait_window()
        return self.result if hasattr(self, 'result') else None

    def _ok(self):
        name = self.list_name.get() if hasattr(self, 'cmbb') and self.list_name.get() else self.ent_new.get()
        if not name:
            # Try getting from combobox selection
            if hasattr(self, 'cmbb'):
                name = self.cmbb.get()
        if not name:
            messagebox.showwarning("Warning", "Please select or enter a list name")
            return
        self.result = name
        self.dialog.destroy()
        self.callback(name, self.symbols)
        self.app._refresh_favorites_tab()

    def _cancel(self):
        self.result = None
        self.dialog.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = CryptoMarketDownloaderApp(root)
    root.mainloop()
