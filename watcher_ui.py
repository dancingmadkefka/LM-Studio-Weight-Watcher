"""Look and feel for the Weight Watcher window.

Theme tokens, ttk styles, icons, tooltips, banners, the update confirmation
dialog, and small formatting helpers. Everything here is presentation only:
no alert state is read or written. Pillow is imported lazily so the
launcher's dependency preflight stays the single place that reports a broken
install.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from tkinter import font as tkfont
from tkinter import ttk


# ----- Platform -----


def enable_dpi_awareness() -> None:
    """Render crisply on scaled displays instead of being bitmap-stretched.

    System-aware (not per-monitor) on purpose: Tk 8.6 does not relayout on
    WM_DPICHANGED, so Windows keeps scaling the window correctly when it moves
    to a monitor with a different scale factor.
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def system_prefers_dark() -> bool:
    override = os.environ.get("LMSTUDIO_WATCHER_THEME", "").strip().lower()
    if override in {"dark", "light"}:
        return override == "dark"
    if os.name != "nt":
        return False
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return int(value) == 0
    except Exception:
        return False


def apply_titlebar_theme(window: tk.Misc, dark: bool) -> None:
    """Match the native title bar to the window theme (Windows 10 1809+)."""
    if os.name != "nt":
        return
    try:
        import ctypes

        window.update_idletasks()
        hwnd = int(window.wm_frame(), 16)
        value = ctypes.c_int(1 if dark else 0)
        for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE, pre-20H1 id
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)
            ) == 0:
                break
    except Exception:
        pass


def reveal_in_file_manager(path: str | os.PathLike | None) -> bool:
    """Open the file manager with ``path`` selected, or its folder."""
    if not path:
        return False
    target = Path(path)
    try:
        if os.name == "nt":
            if target.is_file():
                subprocess.Popen(["explorer", "/select,", str(target)])
                return True
            folder = target if target.is_dir() else target.parent
            if folder.is_dir():
                os.startfile(str(folder))  # type: ignore[attr-defined]
                return True
            return False
        folder = target if target.is_dir() else target.parent
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([opener, str(folder)])
        return True
    except Exception:
        return False


# ----- Theme -----


@dataclass(frozen=True)
class Palette:
    dark: bool
    bg: str
    surface: str
    surface_alt: str
    border: str
    border_strong: str
    text: str
    muted: str
    subtle: str
    accent: str
    accent_hover: str
    accent_pressed: str
    on_accent: str
    accent_text: str
    selection: str
    control: str
    control_hover: str
    control_pressed: str
    disabled_bg: str
    disabled_text: str
    danger: str
    danger_soft: str
    warn: str
    warn_soft: str
    ok: str
    ok_soft: str
    info: str
    info_soft: str
    snooze: str
    snooze_soft: str


LIGHT = Palette(
    dark=False,
    bg="#f3f3f3",
    surface="#ffffff",
    surface_alt="#fafafa",
    border="#e3e3e3",
    border_strong="#cfcfcf",
    text="#1b1b1b",
    muted="#5d5d5d",
    subtle="#8a8a8a",
    accent="#0067c0",
    accent_hover="#1975c5",
    accent_pressed="#3a87cb",
    on_accent="#ffffff",
    accent_text="#005fb8",
    selection="#dcebf9",
    control="#fdfdfd",
    control_hover="#f4f4f4",
    control_pressed="#ebebeb",
    disabled_bg="#f5f5f5",
    disabled_text="#a3a3a3",
    danger="#c42b1c",
    danger_soft="#fde7e9",
    warn="#8a5300",
    warn_soft="#fff4ce",
    ok="#0f7b0f",
    ok_soft="#dff6dd",
    info="#005fb8",
    info_soft="#e5f0fb",
    snooze="#6b4fbb",
    snooze_soft="#efeafb",
)

DARK = Palette(
    dark=True,
    bg="#1c1c1c",
    surface="#272727",
    surface_alt="#2c2c2c",
    border="#383838",
    border_strong="#4a4a4a",
    text="#f2f2f2",
    muted="#c8c8c8",
    subtle="#9b9b9b",
    accent="#4cc2ff",
    accent_hover="#47b1e8",
    accent_pressed="#42a1d2",
    on_accent="#000000",
    accent_text="#79d0ff",
    selection="#1f3b55",
    control="#2f2f2f",
    control_hover="#363636",
    control_pressed="#2a2a2a",
    disabled_bg="#2a2a2a",
    disabled_text="#6f6f6f",
    danger="#ff99a4",
    danger_soft="#43272a",
    warn="#ffd75e",
    warn_soft="#3d3220",
    ok="#6ccb5f",
    ok_soft="#25361f",
    info="#79d0ff",
    info_soft="#1c2f40",
    snooze="#c3adff",
    snooze_soft="#2e2742",
)


# Fluent / MDL2 icon font code points, with plain-Unicode fallbacks.
GLYPHS = {
    "info": ("\ue946", "i"),
    "warning": ("\ue7ba", "!"),
    "error": ("\uea39", "×"),
    "done": ("\ue930", "✓"),
    "check": ("\ue73e", "✓"),
    "refresh": ("\ue72c", "↻"),
    "download": ("\ue896", "↓"),
    "more": ("\ue712", "⋯"),
    "close": ("\ue711", "×"),
    "clock": ("\ue823", "◷"),
    "search": ("\ue721", "⌕"),
}


@dataclass
class Theme:
    palette: Palette
    scale: float
    fonts: dict[str, tkfont.Font] = field(default_factory=dict)
    icon_family: str | None = None

    def px(self, value: float) -> int:
        return max(1, int(round(value * self.scale)))

    def glyph(self, name: str) -> str:
        fluent, fallback = GLYPHS[name]
        return fluent if self.icon_family else fallback

    def icon_font(self, size: int) -> tuple:
        if self.icon_family:
            return (self.icon_family, size)
        return (self.fonts["body"].actual("family"), size, "bold")


def build_theme(master: tk.Misc, *, dark: bool | None = None) -> Theme:
    if dark is None:
        dark = system_prefers_dark()
    palette = DARK if dark else LIGHT
    try:
        scale = float(master.winfo_fpixels("1i")) / 96.0
    except tk.TclError:
        scale = 1.0
    scale = min(max(scale, 1.0), 4.0)
    families = set(tkfont.families(master))
    default_family = tkfont.nametofont("TkDefaultFont", root=master).actual("family")

    def first(*names: str) -> str | None:
        return next((name for name in names if name in families), None)

    ui = first("Segoe UI") or default_family
    semibold = first("Segoe UI Semibold")
    mono = first("Cascadia Mono", "Consolas", "Menlo", "DejaVu Sans Mono") or "TkFixedFont"

    def strong(size: int) -> tkfont.Font:
        if semibold:
            return tkfont.Font(root=master, family=semibold, size=size)
        return tkfont.Font(root=master, family=ui, size=size, weight="bold")

    fonts = {
        "title": strong(15),
        "h2": strong(13),
        "body": tkfont.Font(root=master, family=ui, size=10),
        "body_strong": strong(10),
        "small": tkfont.Font(root=master, family=ui, size=9),
        "small_strong": strong(9),
        "caption": strong(8),
        "mono": tkfont.Font(root=master, family=mono, size=9),
    }
    return Theme(
        palette=palette,
        scale=scale,
        fonts=fonts,
        icon_family=first("Segoe Fluent Icons", "Segoe MDL2 Assets"),
    )


def configure_styles(style: ttk.Style, theme: Theme) -> None:
    p = theme.palette
    f = theme.fonts
    px = theme.px
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    style.configure(".", background=p.bg, foreground=p.text, font=f["body"],
                    bordercolor=p.border, lightcolor=p.bg, darkcolor=p.bg,
                    troughcolor=p.bg, focuscolor=p.accent, selectbackground=p.selection,
                    selectforeground=p.text, insertcolor=p.text)
    style.configure("TFrame", background=p.bg)
    style.configure("Surface.TFrame", background=p.surface)
    style.configure("Card.TFrame", background=p.surface, bordercolor=p.border,
                    relief="solid", borderwidth=1)
    style.configure("TLabel", background=p.bg, foreground=p.text, font=f["body"])
    style.configure("Title.TLabel", font=f["title"])
    style.configure("Muted.TLabel", foreground=p.muted, font=f["small"])
    style.configure("Status.TLabel", foreground=p.muted, font=f["small"])
    style.configure("Surface.TLabel", background=p.surface)
    style.configure("SurfaceMuted.TLabel", background=p.surface, foreground=p.muted,
                    font=f["small"])
    style.configure("SurfaceTitle.TLabel", background=p.surface, font=f["h2"])
    style.configure("SurfaceStrong.TLabel", background=p.surface, font=f["body_strong"])

    # Buttons: flat Fluent-style controls with a visible keyboard focus ring.
    button_padding = (px(14), px(6))
    style.configure("TButton", background=p.control, foreground=p.text,
                    bordercolor=p.border_strong, lightcolor=p.control, darkcolor=p.control,
                    relief="flat", padding=button_padding, font=f["body"],
                    focusthickness=0, focuscolor=p.control, anchor="center")
    style.map("TButton",
              background=[("disabled", p.disabled_bg), ("pressed", p.control_pressed),
                          ("active", p.control_hover)],
              lightcolor=[("disabled", p.disabled_bg), ("pressed", p.control_pressed),
                          ("active", p.control_hover)],
              darkcolor=[("disabled", p.disabled_bg), ("pressed", p.control_pressed),
                         ("active", p.control_hover)],
              foreground=[("disabled", p.disabled_text)],
              bordercolor=[("disabled", p.border), ("focus", p.text)])
    style.configure("Accent.TButton", background=p.accent, foreground=p.on_accent,
                    bordercolor=p.accent, lightcolor=p.accent, darkcolor=p.accent,
                    font=f["body_strong"], focuscolor=p.accent, focusthickness=0)
    style.map("Accent.TButton",
              background=[("disabled", p.disabled_bg), ("pressed", p.accent_pressed),
                          ("active", p.accent_hover)],
              lightcolor=[("disabled", p.disabled_bg), ("pressed", p.accent_pressed),
                          ("active", p.accent_hover)],
              darkcolor=[("disabled", p.disabled_bg), ("pressed", p.accent_pressed),
                         ("active", p.accent_hover)],
              bordercolor=[("disabled", p.border), ("focus", p.text),
                           ("pressed", p.accent_pressed), ("active", p.accent_hover)],
              foreground=[("disabled", p.disabled_text)])
    style.configure("Icon.TButton", padding=(px(8), px(6)), font=theme.icon_font(10))
    style.configure("Link.TButton", background=p.bg, foreground=p.accent_text,
                    bordercolor=p.bg, lightcolor=p.bg, darkcolor=p.bg,
                    padding=(px(4), 0), font=f["small_strong"], focusthickness=0)
    style.map("Link.TButton",
              background=[("pressed", p.bg), ("active", p.bg)],
              lightcolor=[("pressed", p.bg), ("active", p.bg)],
              darkcolor=[("pressed", p.bg), ("active", p.bg)],
              foreground=[("active", p.accent_hover)],
              bordercolor=[("focus", p.text)])

    for kind, (soft, strong_color) in {
        "info": (p.info_soft, p.info),
        "warn": (p.warn_soft, p.warn),
        "danger": (p.danger_soft, p.danger),
        "ok": (p.ok_soft, p.ok),
    }.items():
        name = f"{kind}.Banner.TButton"
        style.configure(name, background=p.control, foreground=p.text,
                        bordercolor=p.border_strong, lightcolor=p.control, darkcolor=p.control,
                        padding=(px(12), px(4)), font=f["small_strong"], focusthickness=0)
        style.map(name,
                  background=[("disabled", soft), ("pressed", p.control_pressed),
                              ("active", p.control_hover)],
                  lightcolor=[("disabled", soft), ("pressed", p.control_pressed),
                              ("active", p.control_hover)],
                  darkcolor=[("disabled", soft), ("pressed", p.control_pressed),
                             ("active", p.control_hover)],
                  foreground=[("disabled", p.disabled_text)],
                  bordercolor=[("focus", p.text)])
        close = f"{kind}.BannerClose.TButton"
        style.configure(close, background=soft, foreground=p.muted, bordercolor=soft,
                        lightcolor=soft, darkcolor=soft, padding=(px(6), px(2)),
                        font=theme.icon_font(9), focusthickness=0)
        style.map(close,
                  background=[("pressed", p.control_pressed), ("active", p.control_hover)],
                  lightcolor=[("pressed", p.control_pressed), ("active", p.control_hover)],
                  darkcolor=[("pressed", p.control_pressed), ("active", p.control_hover)],
                  bordercolor=[("focus", p.text)],
                  foreground=[("active", p.text)])
        style.configure(f"{kind}.Banner.Horizontal.TProgressbar", troughcolor=p.border,
                        background=strong_color, bordercolor=p.border,
                        lightcolor=strong_color, darkcolor=strong_color,
                        thickness=px(6))

    # Filter chips.
    style.configure("Chip.Toolbutton", background=p.bg, foreground=p.muted,
                    bordercolor=p.bg, lightcolor=p.bg, darkcolor=p.bg,
                    padding=(px(12), px(5)), font=f["small_strong"], relief="flat",
                    focuscolor=p.bg, focusthickness=0, anchor="center")
    style.map("Chip.Toolbutton",
              background=[("selected", p.surface), ("pressed", p.control_pressed),
                          ("active", p.control_hover)],
              lightcolor=[("selected", p.surface), ("pressed", p.control_pressed),
                          ("active", p.control_hover)],
              darkcolor=[("selected", p.surface), ("pressed", p.control_pressed),
                         ("active", p.control_hover)],
              bordercolor=[("focus", p.text), ("selected", p.border_strong)],
              foreground=[("selected", p.text), ("active", p.text)])

    # Model list.
    style.configure("Models.Treeview", background=p.surface, fieldbackground=p.surface,
                    foreground=p.text, rowheight=px(34), borderwidth=0, relief="flat",
                    font=f["body"], bordercolor=p.surface, lightcolor=p.surface,
                    darkcolor=p.surface)
    style.map("Models.Treeview",
              background=[("selected", "focus", p.selection), ("selected", p.selection)],
              foreground=[("selected", p.text)])
    style.configure("Models.Treeview.Heading", background=p.surface, foreground=p.muted,
                    font=f["small_strong"], relief="flat", padding=(px(10), px(7)),
                    bordercolor=p.border, lightcolor=p.surface, darkcolor=p.border)
    style.map("Models.Treeview.Heading",
              background=[("active", p.surface_alt)],
              lightcolor=[("active", p.surface_alt)])
    # Drop the dotted per-item focus box; selection already shows the cursor row.
    style.layout("Treeview.Item", [
        ("Treeitem.padding", {"sticky": "nswe", "children": [
            ("Treeitem.indicator", {"side": "left", "sticky": ""}),
            ("Treeitem.image", {"side": "left", "sticky": ""}),
            ("Treeitem.text", {"side": "left", "sticky": ""}),
        ]}),
    ])
    style.layout("Models.Treeview", [
        ("Treeview.field", {"sticky": "nswe", "border": "0", "children": [
            ("Treeview.padding", {"sticky": "nswe", "children": [
                ("Treeview.treearea", {"sticky": "nswe"}),
            ]}),
        ]}),
    ])

    # Slim scrollbars without arrow buttons.
    for orient, sticky in (("Vertical", "ns"), ("Horizontal", "ew")):
        name = f"Slim.{orient}.TScrollbar"
        style.layout(name, [
            (f"{orient}.Scrollbar.trough", {"sticky": sticky, "children": [
                (f"{orient}.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"}),
            ]}),
        ])
        style.configure(name, troughcolor=p.surface, background=p.border_strong,
                        bordercolor=p.surface, lightcolor=p.border_strong,
                        darkcolor=p.border_strong, arrowsize=px(8), gripcount=0,
                        relief="flat")
        style.map(name, background=[("active", p.subtle)],
                  lightcolor=[("active", p.subtle)], darkcolor=[("active", p.subtle)])

    style.configure("Busy.Horizontal.TProgressbar", troughcolor=p.bg, background=p.accent,
                    bordercolor=p.bg, lightcolor=p.accent, darkcolor=p.accent,
                    thickness=px(3))
    style.configure("TPanedwindow", background=p.bg)
    style.configure("Sash", sashthickness=px(10), gripcount=0, background=p.bg,
                    lightcolor=p.bg, bordercolor=p.bg)
    style.configure("TSeparator", background=p.border)


# ----- Images -----


def _rgba(color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    color = color.lstrip("#")
    return (int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16), alpha)


def status_dot_image(color: str, *, size: int, ring: str | None = None):
    """Anti-aliased status dot sized for a Treeview row."""
    from PIL import Image, ImageDraw

    pad = max(2, size // 3)
    canvas = size + pad * 2
    big = canvas * 4
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    inset = pad * 4
    if ring:
        draw.ellipse((inset, inset, big - inset, big - inset), outline=_rgba(ring), width=max(4, size // 2))
    else:
        draw.ellipse((inset, inset, big - inset, big - inset), fill=_rgba(color))
    return image.resize((canvas, canvas), Image.LANCZOS)


def _load_bold_font(size: int):
    from PIL import ImageFont

    for name in ("segoeuib.ttf", "seguisb.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


TRAY_COLORS = {
    "ok": "#2f8f46",
    "pending": "#d13438",
    "removed": "#c27c0e",
    "busy": "#0f6cbd",
    "attention": "#d13438",
}


def tray_icon_image(kind: str, count: int = 0, *, size: int = 64):
    """Tray badge: colored rounded square with a count, check mark or dots.

    Drawn at 4x and downsampled so it stays legible at 16 px.
    """
    from PIL import Image, ImageDraw

    s = size * 4
    image = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    color = _rgba(TRAY_COLORS.get(kind, TRAY_COLORS["ok"]))
    margin = s // 16
    draw.rounded_rectangle((margin, margin, s - margin, s - margin), radius=s // 4, fill=color)
    white = (255, 255, 255, 255)
    if kind == "busy":
        r = s // 13
        for cx in (s * 0.3, s * 0.5, s * 0.7):
            draw.ellipse((cx - r, s / 2 - r, cx + r, s / 2 + r), fill=white)
    elif kind == "ok":
        w = s // 10
        draw.line([(s * 0.28, s * 0.52), (s * 0.44, s * 0.68), (s * 0.73, s * 0.35)],
                  fill=white, width=w, joint="curve")
        for x, y in ((s * 0.28, s * 0.52), (s * 0.73, s * 0.35)):
            draw.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=white)
    else:
        text = "!" if kind == "attention" and count <= 0 else ("9+" if count > 9 else str(max(count, 1)))
        font = _load_bold_font(int(s * (0.62 if len(text) == 1 else 0.48)))
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text(((s - tw) / 2 - bbox[0], (s - th) / 2 - bbox[1]), text, font=font, fill=white)
    return image.resize((size, size), Image.LANCZOS)


def app_icon_image(size: int = 64):
    """Window icon: a download arrow landing in a tray."""
    from PIL import Image, ImageDraw

    s = size * 4
    image = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((s * 0.06, s * 0.06, s * 0.94, s * 0.94), radius=s * 0.22,
                           fill=_rgba("#0f6cbd"))
    white = (255, 255, 255, 255)
    w = s * 0.085
    draw.line([(s * 0.5, s * 0.2), (s * 0.5, s * 0.58)], fill=white, width=int(w))
    draw.polygon([(s * 0.3, s * 0.44), (s * 0.7, s * 0.44), (s * 0.5, s * 0.66)], fill=white)
    draw.line([(s * 0.24, s * 0.62), (s * 0.24, s * 0.78), (s * 0.76, s * 0.78), (s * 0.76, s * 0.62)],
              fill=white, width=int(w), joint="curve")
    return image.resize((size, size), Image.LANCZOS)


# ----- Widgets -----


class Tooltip:
    """Small delayed tooltip that never takes focus."""

    def __init__(self, widget: tk.Widget, text, theme: Theme, *, delay_ms: int = 550) -> None:
        self.widget = widget
        self.text = text
        self.theme = theme
        self.delay_ms = delay_ms
        self._after = None
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._cancel()
        self._after = self.widget.after(self.delay_ms, self._show)

    def _cancel(self) -> None:
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None

    def _show(self) -> None:
        self._after = None
        text = self.text() if callable(self.text) else self.text
        if not text:
            return
        try:
            p = self.theme.palette
            tip = tk.Toplevel(self.widget)
            tip.wm_overrideredirect(True)
            frame = tk.Frame(tip, bg=p.surface, highlightthickness=1,
                             highlightbackground=p.border_strong)
            frame.pack()
            tk.Label(frame, text=text, bg=p.surface, fg=p.text, font=self.theme.fonts["small"],
                     justify="left", wraplength=self.theme.px(320),
                     padx=self.theme.px(8), pady=self.theme.px(5)).pack()
            tip.update_idletasks()
            x = self.widget.winfo_rootx()
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + self.theme.px(6)
            screen_w = self.widget.winfo_screenwidth()
            x = min(x, screen_w - tip.winfo_reqwidth() - 8)
            if y + tip.winfo_reqheight() > self.widget.winfo_screenheight() - 8:
                y = self.widget.winfo_rooty() - tip.winfo_reqheight() - self.theme.px(6)
            tip.wm_geometry(f"+{max(0, x)}+{max(0, y)}")
            self._tip = tip
        except tk.TclError:
            self._tip = None

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None


class AutoScrollbar(ttk.Scrollbar):
    """Scrollbar that hides itself when everything fits."""

    def set(self, first, last):  # noqa: A003 - Tk protocol name
        try:
            if float(first) <= 0.0 and float(last) >= 1.0:
                self.grid_remove()
            else:
                self.grid()
        except tk.TclError:
            pass
        super().set(first, last)


@dataclass
class BannerAction:
    label: str
    command: object
    tooltip: str | None = None


def build_banner(
    parent: tk.Misc,
    theme: Theme,
    *,
    kind: str,
    title: str,
    message: str = "",
    icon: str = "info",
    actions: list[BannerAction] | None = None,
    on_close=None,
) -> tk.Frame:
    """Inline message bar (info / warn / danger / ok) with optional actions."""
    p = theme.palette
    soft, strong = {
        "info": (p.info_soft, p.info),
        "warn": (p.warn_soft, p.warn),
        "danger": (p.danger_soft, p.danger),
        "ok": (p.ok_soft, p.ok),
    }[kind]
    outer = tk.Frame(parent, bg=soft, highlightthickness=1,
                     highlightbackground=p.border if not p.dark else p.border_strong)
    stripe = tk.Frame(outer, bg=strong, width=theme.px(3))
    stripe.pack(side=tk.LEFT, fill=tk.Y)
    body = tk.Frame(outer, bg=soft, padx=theme.px(12), pady=theme.px(9))
    body.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    body.columnconfigure(1, weight=1)
    tk.Label(body, text=theme.glyph(icon), bg=soft, fg=strong,
             font=theme.icon_font(13)).grid(row=0, column=0, rowspan=2, sticky="n",
                                            padx=(0, theme.px(10)), pady=(theme.px(1), 0))
    title_label = tk.Label(body, text=title, bg=soft, fg=p.text, font=theme.fonts["body_strong"],
                           anchor="w", justify="left")
    title_label.grid(row=0, column=1, sticky="ew")
    message_label = None
    if message:
        message_label = tk.Label(body, text=message, bg=soft, fg=p.muted if not p.dark else p.text,
                                 font=theme.fonts["small"], anchor="w", justify="left")
        message_label.grid(row=1, column=1, sticky="ew", pady=(theme.px(1), 0))

    def rewrap(event) -> None:
        width = max(theme.px(160), event.width - theme.px(8))
        title_label.configure(wraplength=width)
        if message_label is not None:
            message_label.configure(wraplength=width)

    body.grid_columnconfigure(1, weight=1)
    title_label.bind("<Configure>", rewrap)

    buttons = tk.Frame(body, bg=soft)
    buttons.grid(row=0, column=2, rowspan=2, sticky="e", padx=(theme.px(12), 0))
    for action in actions or []:
        button = ttk.Button(buttons, text=action.label, command=action.command,
                            style=f"{kind}.Banner.TButton", takefocus=True)
        button.pack(side=tk.LEFT, padx=(theme.px(6), 0))
        if action.tooltip:
            Tooltip(button, action.tooltip, theme)
    if on_close is not None:
        close = ttk.Button(buttons, text=theme.glyph("close"), command=on_close,
                           style=f"{kind}.BannerClose.TButton", width=2)
        close.pack(side=tk.LEFT, padx=(theme.px(6), 0))
        Tooltip(close, "Dismiss this message", theme)
    return outer


# ----- Update confirmation -----


@dataclass
class ConfirmModel:
    name: str
    size_bytes: int
    files: list[str]
    commit_title: str | None = None


@dataclass
class ConfirmSummary:
    models: list[ConfirmModel]
    download_bytes: int
    total_bytes: int
    available_bytes: int
    required_bytes: int


def ask_update_confirmation(parent: tk.Misc, summary: ConfirmSummary, theme: Theme | None,
                            format_bytes) -> bool:
    """Modal confirmation that lists what will be downloaded and replaced.

    Returns True only when the user explicitly chooses to update. Raises
    tk.TclError when no dialog can be shown, so the caller can roll back.
    """
    if theme is None:
        theme = build_theme(parent)
        configure_styles(ttk.Style(parent), theme)
    p = theme.palette
    px = theme.px
    count = len(summary.models)
    noun = "model" if count == 1 else "models"

    dialog = tk.Toplevel(parent)
    dialog.withdraw()
    dialog.title(f"Update {count} {noun}")
    dialog.configure(bg=p.bg)
    dialog.resizable(False, False)
    try:
        dialog.transient(parent)
    except tk.TclError:
        pass
    result = {"ok": False}

    outer = ttk.Frame(dialog, padding=(px(24), px(20), px(24), px(18)))
    outer.pack(fill=tk.BOTH, expand=True)

    head = ttk.Frame(outer)
    head.pack(fill=tk.X)
    tk.Label(head, text=theme.glyph("download"), bg=p.bg, fg=p.accent,
             font=theme.icon_font(20)).pack(side=tk.LEFT, anchor="n", padx=(0, px(14)))
    head_text = ttk.Frame(head)
    head_text.pack(side=tk.LEFT, fill=tk.X, expand=True)
    ttk.Label(head_text, text=f"Update {count} {noun}?", style="Title.TLabel").pack(anchor="w")
    ttk.Label(
        head_text,
        text=("Weight Watcher downloads the new files, checks each one against "
              "Hugging Face, then swaps them in. Your current files are kept as a "
              "backup until the swap is verified."),
        style="Muted.TLabel", wraplength=px(440), justify="left",
    ).pack(anchor="w", pady=(px(4), 0))

    card = tk.Frame(outer, bg=p.surface, highlightthickness=1, highlightbackground=p.border)
    card.pack(fill=tk.X, pady=(px(16), px(12)))
    # The model list scrolls when it would push the buttons off a short screen.
    canvas = tk.Canvas(card, bg=p.surface, highlightthickness=0, borderwidth=0)
    list_scroll = AutoScrollbar(card, orient="vertical", command=canvas.yview,
                                style="Slim.Vertical.TScrollbar")
    canvas.configure(yscrollcommand=list_scroll.set)
    canvas.grid(row=0, column=0, sticky="nsew")
    list_scroll.grid(row=0, column=1, sticky="ns")
    card.columnconfigure(0, weight=1)
    rows_frame = tk.Frame(canvas, bg=p.surface)
    canvas.create_window((0, 0), window=rows_frame, anchor="nw", tags=("rows",))
    canvas.bind("<Configure>", lambda event: canvas.itemconfigure("rows", width=event.width))
    shown = summary.models
    for index, model in enumerate(shown):
        row = tk.Frame(rows_frame, bg=p.surface, padx=px(14), pady=px(9))
        row.pack(fill=tk.X)
        row.columnconfigure(0, weight=1)
        tk.Label(row, text=model.name, bg=p.surface, fg=p.text, font=theme.fonts["body_strong"],
                 anchor="w").grid(row=0, column=0, sticky="w")
        tk.Label(row, text=format_bytes(model.size_bytes), bg=p.surface, fg=p.muted,
                 font=theme.fonts["small"]).grid(row=0, column=1, sticky="e", padx=(px(16), 0))
        files = list(model.files) or ["weights"]
        if len(files) > 3:
            files = files[:2] + [f"{len(model.files) - 2} more files"]
        detail = ", ".join(files)
        if model.commit_title:
            detail += f"  ·  “{model.commit_title}”"
        tk.Label(row, text=detail, bg=p.surface, fg=p.muted, font=theme.fonts["small"],
                 anchor="w", justify="left", wraplength=px(440)).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(px(1), 0))
        if index < len(shown) - 1:
            tk.Frame(rows_frame, bg=p.border, height=1).pack(fill=tk.X, padx=px(14))

    facts = ttk.Frame(outer)
    facts.pack(fill=tk.X)
    facts.columnconfigure(1, weight=1)
    download_text = format_bytes(summary.download_bytes)
    already = summary.total_bytes - summary.download_bytes
    if already > 0:
        download_text += f"  ({format_bytes(already)} already downloaded)"
    rows = [("Download", download_text)]
    if summary.available_bytes:
        space = f"{format_bytes(summary.available_bytes)} free"
        if summary.required_bytes:
            space += f", {format_bytes(summary.required_bytes)} needed during the swap"
        rows.append(("Disk space", space))
    for r, (label, value) in enumerate(rows):
        ttk.Label(facts, text=label, style="Muted.TLabel").grid(row=r, column=0, sticky="w",
                                                                padx=(0, px(16)), pady=px(1))
        ttk.Label(facts, text=value).grid(row=r, column=1, sticky="w", pady=px(1))

    note = tk.Frame(outer, bg=p.info_soft, padx=px(12), pady=px(8))
    note.pack(fill=tk.X, pady=(px(14), 0))
    tk.Label(note, text=theme.glyph("info"), bg=p.info_soft, fg=p.info,
             font=theme.icon_font(11)).pack(side=tk.LEFT, anchor="n", padx=(0, px(8)))
    tk.Label(note, text=("Unload these models in LM Studio before the files are swapped. "
                         "You can cancel any time until installation starts."),
             bg=p.info_soft, fg=p.text, font=theme.fonts["small"], justify="left",
             wraplength=px(430), anchor="w").pack(side=tk.LEFT, fill=tk.X, expand=True)

    buttons = ttk.Frame(outer)
    buttons.pack(fill=tk.X, pady=(px(20), 0))

    def accept(_event=None) -> None:
        result["ok"] = True
        dialog.destroy()

    def on_return(_event=None):
        # Enter activates the focused button, so Tab to Cancel + Enter cancels.
        focused = dialog.focus_lastfor()
        if isinstance(focused, ttk.Button):
            focused.invoke()
        else:
            accept()
        return "break"

    def reject(_event=None) -> None:
        result["ok"] = False
        dialog.destroy()

    confirm = ttk.Button(buttons, text="Download and update", style="Accent.TButton",
                         command=accept)
    confirm.pack(side=tk.RIGHT)
    ttk.Button(buttons, text="Cancel", command=reject).pack(side=tk.RIGHT, padx=(0, px(8)))

    dialog.protocol("WM_DELETE_WINDOW", reject)
    dialog.bind("<Escape>", reject)
    dialog._lmww_on_return = on_return
    dialog._lmww_on_escape = reject
    dialog.bind("<Return>", on_return)
    dialog.bind("<KP_Enter>", on_return)
    try:
        icon = getattr(parent, "_lmww_icon", None)
        if icon is not None:
            dialog.iconphoto(False, icon)
    except tk.TclError:
        pass

    dialog.update_idletasks()
    list_height = rows_frame.winfo_reqheight()
    chrome = dialog.winfo_reqheight() - canvas.winfo_reqheight()
    room = int(dialog.winfo_screenheight() * 0.85) - chrome - px(40)
    canvas.configure(width=rows_frame.winfo_reqwidth(),
                     height=max(px(90), min(list_height, room)))
    canvas.configure(scrollregion=(0, 0, rows_frame.winfo_reqwidth(), list_height))

    def on_wheel(event):
        if list_height > canvas.winfo_height():
            canvas.yview_scroll(int(-event.delta / 120) or (-1 if event.delta > 0 else 1), "units")

    dialog.bind("<MouseWheel>", on_wheel)
    dialog.update_idletasks()
    width, height = dialog.winfo_reqwidth(), dialog.winfo_reqheight()
    try:
        if parent.winfo_viewable():
            x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - height) // 3
        else:
            raise tk.TclError("parent hidden")
    except (tk.TclError, TypeError):
        x = (dialog.winfo_screenwidth() - width) // 2
        y = (dialog.winfo_screenheight() - height) // 3
    y = min(y, dialog.winfo_screenheight() - height - px(60))
    dialog.geometry(f"+{max(0, x)}+{max(0, y)}")
    dialog.deiconify()
    apply_titlebar_theme(dialog, p.dark)
    confirm.focus_set()
    try:
        dialog.grab_set()
    except tk.TclError:
        pass
    parent.wait_window(dialog)
    return bool(result["ok"])


# ----- Formatting -----


def to_local(value) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        if isinstance(value, datetime):
            dt = value
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()


def format_when(value) -> str:
    """'12 Nov 2026, 17:43' in local time; '—' when unknown."""
    dt = to_local(value)
    if dt is None:
        return "—" if not value else str(value)
    return f"{dt.day} {dt.strftime('%b %Y, %H:%M')}"


def format_ago(value, *, now: datetime | None = None) -> str:
    """Relative time: 'just now', '12 min ago', '3 h ago', 'yesterday', '5 days ago'."""
    dt = to_local(value)
    if dt is None:
        return "—"
    now = (now or datetime.now(timezone.utc)).astimezone()
    seconds = (now - dt).total_seconds()
    if seconds < 0:
        return format_when(dt)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    days = int(seconds // 86400)
    if days == 1:
        return "yesterday"
    if days < 45:
        return f"{days} days ago"
    if days < 365:
        return f"{days // 30} months ago"
    return format_when(dt)


def format_duration(seconds: float) -> str:
    """Compact duration for 'next check in' and ETA text."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h" if hours else f"{days} d"


def plural(count: int, singular: str, plural_form: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural_form or singular + 's')}"
