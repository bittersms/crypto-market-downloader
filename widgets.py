"""GUI widgets - reusable themed components."""
import tkinter as tk
from tkinter import ttk
from gui import AppTheme


class ThemedTreeview(ttk.Treeview):
    """Treeview with dark theme styling."""

    def __init__(self, parent, columns, **kwargs):
        super().__init__(parent, columns=columns, show="headings", **kwargs)
        self._apply_theme()

    def _apply_theme(self):
        style = ttk.Style()
        style.configure("Dark.Treeview",
            background=AppTheme.BG_CARD,
            foreground=AppTheme.TEXT,
            fieldbackground=AppTheme.BG_CARD,
            borderwidth=0,
            font=(AppTheme.FONT, 9))
        style.configure("Dark.Treeview.Heading",
            background=AppTheme.BG_DARK,
            foreground=AppTheme.TEXT_BRIGHT,
            font=(AppTheme.FONT, 9, "bold"))
        style.map("Dark.Treeview",
            background=[("selected", AppTheme.ACCENT)],
            foreground=[("selected", AppTheme.TEXT_BRIGHT)])
        self.configure(style="Dark.Treeview")


class ThemedButton(ttk.Button):
    """Button with accent color styling."""

    def __init__(self, parent, text="", **kwargs):
        super().__init__(parent, text=text, **kwargs)
        style = ttk.Style()
        style.configure("Accent.TButton",
            foreground=AppTheme.TEXT_BRIGHT,
            background=AppTheme.ACCENT,
            font=(AppTheme.FONT, 9, "bold"))
        style.map("Accent.TButton",
            background=[("active", AppTheme.ACCENT_HOVER)],
            foreground=[("disabled", AppTheme.TEXT_DIM)])
        self.configure(style="Accent.TButton")


class ThemedEntry(ttk.Entry):
    """Entry with dark theme styling."""

    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        style = ttk.Style()
        style.configure("Dark.TEntry",
            fieldbackground=AppTheme.BG_CARD,
            foreground=AppTheme.TEXT,
            bordercolor=AppTheme.ACCENT,
            font=(AppTheme.FONT, 9))
        # Greyed-out variant used for disabled/locked fields (e.g. NOW end date)
        style.map("Dark.TEntry",
            foreground=[("disabled", AppTheme.TEXT_DIM)],
            fieldbackground=[("disabled", AppTheme.BG_DARK)])
        self.configure(style="Dark.TEntry")


class ThemedCombobox(ttk.Combobox):
    """Combobox with dark theme styling."""

    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        style = ttk.Style()
        style.configure("Dark.TCombobox",
            fieldbackground=AppTheme.BG_CARD,
            foreground=AppTheme.TEXT,
            borderwidth=1,
            font=(AppTheme.FONT, 9))
        style.map("Dark.TCombobox",
            fieldbackground=[("readonly", AppTheme.BG_CARD),
                             ("disabled", AppTheme.BG_DARK)],
            foreground=[("readonly", AppTheme.TEXT),
                        ("disabled", AppTheme.TEXT_DIM)],
            bordercolor=[("focus", AppTheme.ACCENT)])
        self.configure(style="Dark.TCombobox")


class HoverFrame(tk.Frame):
    """Frame that changes background on hover."""

    def __init__(self, parent, bg=None, hover_bg=None, **kwargs):
        super().__init__(parent, **kwargs)
        self.bg = bg or AppTheme.BG_CARD
        self.hover_bg = hover_bg or AppTheme.BG_DARK
        self.configure(bg=self.bg)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def _on_enter(self, event):
        self.configure(bg=self.hover_bg)

    def _on_leave(self, event):
        self.configure(bg=self.bg)


def make_scrollable_frame(parent):
    """Create a scrollable frame widget."""
    canvas = tk.Canvas(parent, bg=AppTheme.BG_CARD, highlightthickness=0)
    scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
    scrollable_frame = tk.Frame(canvas, bg=AppTheme.BG_CARD)

    scrollable_frame.bind(
        "<Configure>",
        lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
    )

    canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)

    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")

    # Mousewheel scrolling
    def _on_mousewheel(event):
        canvas.yview_scroll(int(-1*(event.delta/120 if event.delta else 0)), "unit")
    
    scrollable_frame.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _on_mousewheel))
    scrollable_frame.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

    return canvas, scrollable_frame


def apply_dark_theme(root):
    """Apply dark theme to the entire application."""
    root.configure(bg=AppTheme.BG_DARK)
    style = ttk.Style()
    style.theme_use("clam")

    # Configure common styles
    style.configure("TFrame", background=AppTheme.BG_DARK)
    style.configure("TLabel", background=AppTheme.BG_DARK, foreground=AppTheme.TEXT,
                    font=(AppTheme.FONT, 9))
    style.configure("Header.TLabel", background=AppTheme.BG_DARK, 
                    foreground=AppTheme.TEXT_BRIGHT, font=(AppTheme.FONT, 12, "bold"))
    style.configure("Title.TLabel", background=AppTheme.BG_DARK, 
                    foreground=AppTheme.TEXT_BRIGHT, font=(AppTheme.FONT, 16, "bold"))
    style.configure("TNotebook", background=AppTheme.BG_DARK, borderwidth=0)
    style.configure("TNotebook.Tab", background=AppTheme.BG_CARD, foreground=AppTheme.TEXT,
                    font=(AppTheme.FONT, 9, "bold"), padding=[12, 6])
    style.map("TNotebook.Tab",
        background=[("selected", AppTheme.ACCENT), ("active", AppTheme.ACCENT_HOVER)],
        foreground=[("selected", AppTheme.TEXT_BRIGHT), ("disabled", AppTheme.TEXT_DIM)])
