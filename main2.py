"""BatteryOverlay: оверлей заряда батареи для Windows 10 (Python + PySide6).

Обычный режим: прозрачная панель поверх окон, которая не ловит клики и не забирает фокус.
Режим настройки (Ctrl+Shift+B или значок в трее): экран затемняется, вокруг панели
появляется пунктирная зона, как в оверлее Discord. Зону можно перетащить мышью,
а по шестерёнке открываются настройки.
"""
import os
import sys
import ctypes
from ctypes import wintypes

from PySide6.QtCore import (
    Qt, QEvent, QPoint, QPointF, QRect, QRectF, QTimer, QSettings, QAbstractNativeEventFilter,
)
from PySide6.QtGui import (
    QPainter, QPen, QColor, QFont, QFontMetricsF, QPainterPath, QPolygonF,
    QGuiApplication, QIcon, QPixmap, QAction,
)
from PySide6.QtWidgets import (
    QApplication, QWidget, QFrame, QLabel, QPushButton, QSlider, QCheckBox,
    QHBoxLayout, QVBoxLayout, QButtonGroup, QSystemTrayIcon, QMenu,
)

# Показывается на ПК без аккумулятора. На ноутбуке с батареей не используется.
TEST_PERCENT = 87

# Запас вокруг панели: за эту область можно хватать при перетаскивании.
ZONE_PAD = 24

STYLES = ["Светлый", "Тёмный", "Плашка"]

# Подсказки к кнопкам вида: показываются при наведении (рисуются сами, без системных).
STYLE_HINTS = {
    "Светлый": "Белый текст с тёмной обводкой.",
    "Тёмный": "Тёмный текст со светлой обводкой.",
    "Плашка": "Светлый текст на тёмной полупрозрачной подложке.",
}

# Режимы показа: везде или только в играх.
MODE_BUTTONS = [
    ("all", "Везде", "Оверлей виден в любом приложении"),
    ("games", "Только в играх", "Только в играх: окно на весь экран или программа из вашего списка"),
]

# Прозрачность подложки в режиме «Плашка» по умолчанию, % (0 = сплошная).
DEFAULT_PILL_TRANSPARENCY = 53

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
user32.SetWindowPos.restype = wintypes.BOOL
user32.RegisterHotKey.argtypes = [
    wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT,
]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = wintypes.BOOL

WM_HOTKEY = 0x0312
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
VK_B = 0x42
HOTKEY_ID = 1

HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010


# ---------------------------------------------------------------- батарея

class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", wintypes.DWORD),
        ("BatteryFullLifeTime", wintypes.DWORD),
    ]


def read_battery():
    """Возвращает (процент, заряжается, тестовые_данные)."""
    status = SYSTEM_POWER_STATUS()
    ok = kernel32.GetSystemPowerStatus(ctypes.byref(status))
    no_battery = (
        not ok
        or (status.BatteryFlag & 128)
        or status.BatteryLifePercent == 255
    )
    if no_battery:
        return TEST_PERCENT, False, True
    return int(status.BatteryLifePercent), status.ACLineStatus == 1, False


# ---------------------------------------------------------------- определение игры

class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.MonitorFromWindow.restype = wintypes.HANDLE
user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
user32.GetMonitorInfoW.restype = wintypes.BOOL
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

GWL_STYLE = -16
WS_CAPTION = 0x00C00000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MONITOR_DEFAULTTONEAREST = 2

# Программы, которые часто открываются на весь экран, но играми не являются.
# Если нужно, чтобы какая-то из них считалась игрой, есть кнопка «Это игра».
AUTO_BLOCKLIST = {
    "chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "brave.exe",
    "vivaldi.exe", "browser.exe",
    "vlc.exe", "mpc-hc.exe", "mpc-hc64.exe", "mpc-be.exe", "mpc-be64.exe",
    "potplayermini.exe", "potplayermini64.exe",
    "powerpnt.exe", "winword.exe", "excel.exe",
    "explorer.exe", "mstsc.exe", "teams.exe", "ms-teams.exe", "zoom.exe",
    "pycharm64.exe", "idea64.exe", "code.exe", "devenv.exe",
}

# Окна оболочки Windows: пока они в фокусе, прежнее состояние не меняем.
TRANSIENT_CLASSES = {
    "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "NotifyIconOverflowWindow",
    "TopLevelWindowForOverflowXamlIsland", "#32768", "TaskListThumbnailWnd",
    "XamlExplorerHostIslandWindow", "MultitaskingViewFrame",
}
TRANSIENT_EXES = {
    "searchhost.exe", "searchapp.exe", "startmenuexperiencehost.exe",
    "shellexperiencehost.exe", "textinputhost.exe",
}
DESKTOP_CLASSES = {"Progman", "WorkerW"}


def process_name(pid):
    """Имя exe процесса (в нижнем регистре) или пустая строка."""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1].lower()
        return ""
    finally:
        kernel32.CloseHandle(handle)


def is_fullscreen(hwnd):
    """Окно без рамки, закрывающее весь монитор: так выглядят игры
    в полноэкранном режиме и в «Полный экран в окне» (Borderless)."""
    style = user32.GetWindowLongW(hwnd, GWL_STYLE)
    if (style & WS_CAPTION) == WS_CAPTION:
        return False
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    monitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if not monitor or not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return False
    m = info.rcMonitor
    return (
        rect.left <= m.left and rect.top <= m.top
        and rect.right >= m.right and rect.bottom >= m.bottom
    )


def foreground_info():
    """(pid, exe, класс окна, полный_экран) активного окна или None."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd or user32.IsIconic(hwnd):
        return None
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls, 256)
    return pid.value, process_name(pid.value), cls.value, is_fullscreen(hwnd)


def load_set(settings, key):
    raw = settings.value(key, "")
    if not isinstance(raw, str):
        raw = ""
    return {item for item in raw.split(";") if item}


class GameWatcher:
    """Следит за активным окном и решает, находится ли пользователь в игре."""

    def __init__(self, settings):
        self.settings = settings
        mode = settings.value("mode", "all")
        self.mode = mode if mode in ("all", "games") else "all"
        self.games = load_set(settings, "games")          # «Это игра»
        self.not_games = load_set(settings, "not_games")  # «Не игра»
        self.active = False      # сейчас активна игра
        self.last_exe = ""       # последнее приложение (не наше и не оболочка)
        self.last_fullscreen = False

    def set_mode(self, mode):
        self.mode = mode
        self.settings.setValue("mode", mode)

    def evaluate(self, exe, fullscreen):
        if exe in self.not_games:
            return False
        if exe in self.games:
            return True
        if exe in AUTO_BLOCKLIST:
            return False
        return fullscreen

    def poll(self):
        info = foreground_info()
        if info is None:
            return
        pid, exe, cls, fullscreen = info
        if pid == os.getpid():
            return  # наше окно (слой настройки, меню в трее)
        if cls in TRANSIENT_CLASSES or exe in TRANSIENT_EXES:
            return
        if cls in DESKTOP_CLASSES:
            self.active = False
            return
        self.last_exe = exe
        self.last_fullscreen = fullscreen
        self.active = self.evaluate(exe, fullscreen)

    def mark(self, is_game):
        """Запомнить последнее приложение как игру или не игру."""
        if not self.last_exe:
            return
        self.games.discard(self.last_exe)
        self.not_games.discard(self.last_exe)
        (self.games if is_game else self.not_games).add(self.last_exe)
        self.settings.setValue("games", ";".join(sorted(self.games)))
        self.settings.setValue("not_games", ";".join(sorted(self.not_games)))
        self.active = self.evaluate(self.last_exe, self.last_fullscreen)

    def describe(self):
        exe = self.last_exe
        if not exe:
            return "Последнее приложение пока не определено. Переключись в игру и открой настройки сочетанием клавиш."
        if exe in self.not_games:
            status = "не игра (выбрано вручную)"
        elif exe in self.games:
            status = "игра (выбрано вручную)"
        elif exe in AUTO_BLOCKLIST:
            status = "не игра (в списке исключений)"
        elif self.last_fullscreen:
            status = "игра (окно на весь экран)"
        else:
            status = "не игра (оконный режим)"
        return f"Последнее приложение: {exe}\nСейчас считается: {status}"


# ---------------------------------------------------------------- горячая клавиша

class HotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def nativeEventFilter(self, event_type, message):
        try:
            msg = wintypes.MSG.from_address(int(message))
        except Exception:
            return False, 0
        if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
            QTimer.singleShot(0, self.callback)
            return True, 0
        return False, 0


# ---------------------------------------------------------------- сам оверлей

class BatteryWidget(QWidget):
    """Панель с зарядом. Не принимает мышь и фокус, всё рисует сама."""

    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self.font_px = int(settings.value("font_px", 26))
        self.style_name = settings.value("style", "Светлый")
        if self.style_name not in STYLES:
            self.style_name = "Светлый"
        self.user_visible = int(settings.value("visible", 1)) == 1
        self.pill_transparency = int(
            settings.value("pill_transparency", DEFAULT_PILL_TRANSPARENCY)
        )
        self.show_icon = int(settings.value("show_icon", 1)) == 1

        self.percent = 0
        self.charging = False
        self.is_test = False

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        self.relayout()
        self.restore_position()

    # --- геометрия и сохранение

    def restore_position(self):
        x = self.settings.value("x", None)
        y = self.settings.value("y", None)
        if x is not None and y is not None:
            rect = QRect(int(x), int(y), self.width(), self.height())
            for screen in QGuiApplication.screens():
                if screen.availableGeometry().intersects(rect):
                    self.move(rect.topLeft())
                    return
        self.reset_position()

    def reset_position(self):
        area = QGuiApplication.primaryScreen().availableGeometry()
        self.move(area.right() - self.width() - 40, area.top() + 40)
        self.save_position()

    def save_position(self):
        self.settings.setValue("x", self.x())
        self.settings.setValue("y", self.y())

    # --- настройки вида

    def set_font_px(self, px):
        self.font_px = px
        self.settings.setValue("font_px", px)
        self.relayout()

    def set_style_name(self, name):
        self.style_name = name
        self.settings.setValue("style", name)
        self.update()

    def set_show_icon(self, show):
        self.show_icon = show
        self.settings.setValue("show_icon", 1 if show else 0)
        self.relayout()

    def set_pill_transparency(self, percent):
        self.pill_transparency = percent
        self.settings.setValue("pill_transparency", percent)
        self.update()

    def pill_alpha(self):
        return round(255 * (100 - self.pill_transparency) / 100)

    def set_user_visible(self, visible):
        self.user_visible = visible
        self.settings.setValue("visible", 1 if visible else 0)

    def set_ghost(self, ghost):
        self.setWindowOpacity(0.35 if ghost else 1.0)

    def set_data(self, percent, charging, is_test):
        changed = (
            percent != self.percent
            or charging != self.charging
            or is_test != self.is_test
        )
        self.percent, self.charging, self.is_test = percent, charging, is_test
        if changed:
            self.relayout()

    def raise_topmost(self):
        """Некоторые игры сбрасывают «поверх всех», поэтому подтверждаем это."""
        if self.isVisible():
            user32.SetWindowPos(
                int(self.winId()), HWND_TOPMOST, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
            )

    def relayout(self):
        px = self.font_px
        self.text_font = QFont("Segoe UI")
        self.text_font.setPixelSize(px)
        self.text_font.setBold(True)
        self.test_font = QFont("Segoe UI")
        self.test_font.setPixelSize(max(8, round(px * 0.38)))
        self.test_font.setBold(True)

        self.fm = QFontMetricsF(self.text_font)
        tfm = QFontMetricsF(self.test_font)

        # Значок стал меньше (0.85 вместо 0.95 от размера текста),
        # а отступы плашки шире: по бокам 0.6, сверху и снизу 0.35.
        self.icon_h = round(px * 0.85)
        self.icon_w = round(self.icon_h * 1.9)
        self.pad_x = px * 0.6
        self.pad_y = px * 0.35
        self.gap = px * 0.35
        # Ширина считается по самому длинному значению («100%»),
        # поэтому размер панели не прыгает при смене процента.
        self.text_w = self.fm.horizontalAdvance("100%")
        self.test_w = (
            tfm.horizontalAdvance("TEST") + self.gap * 0.6 if self.is_test else 0
        )

        # Место под значок занято только если значок включён.
        self.icon_space = (self.icon_w + self.gap) if self.show_icon else 0
        width = self.pad_x * 2 + self.icon_space + self.text_w + self.test_w
        height = self.fm.height() + self.pad_y * 2
        self.setFixedSize(round(width), round(height))
        self.update()

    # --- отрисовка

    @staticmethod
    def draw_path(p, path, fill, outline, width):
        if outline is not None:
            pen = QPen(outline)
            pen.setWidthF(width)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(fill)
        p.drawPath(path)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        outline = None
        if self.style_name == "Плашка":
            p.setPen(QPen(QColor(255, 255, 255, 30), 1))
            p.setBrush(QColor(24, 25, 28, self.pill_alpha()))
            radius = self.height() * 0.3
            p.drawRoundedRect(
                QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), radius, radius
            )
            fg = QColor("#F2F3F5")
        elif self.style_name == "Тёмный":
            fg = QColor("#14161A")
            outline = QColor(255, 255, 255, 235)
        else:
            fg = QColor("#FFFFFF")
            outline = QColor(0, 0, 0, 235)

        # Значок.
        ix = self.pad_x
        iy = (self.height() - self.icon_h) / 2
        if self.show_icon:
            self.draw_icon(p, ix, iy, self.icon_w, self.icon_h, fg, outline)

        # Процент: центрируется внутри блока шириной «100%».
        text = f"{self.percent}%"
        adv = self.fm.horizontalAdvance(text)
        box_x = ix + self.icon_space
        tx = box_x + (self.text_w - adv) / 2
        ty = self.pad_y + self.fm.ascent()
        text_color = (
            QColor("#FF5D65") if (self.percent <= 15 and not self.charging) else fg
        )
        path = QPainterPath()
        path.addText(tx, ty, self.text_font, text)
        self.draw_path(p, path, text_color, outline, max(2.4, self.font_px * 0.13))

        if self.is_test:
            tpath = QPainterPath()
            tpath.addText(
                box_x + self.text_w + self.gap * 0.6, ty, self.test_font, "TEST"
            )
            self.draw_path(p, tpath, QColor("#FFC857"), outline, 2.0)

        p.end()

    def draw_icon(self, p, x, y, w, h, fg, edge):
        cap_w = max(2.0, h * 0.14)
        body_w = w - cap_w - 1
        pen_w = max(1.6, h * 0.09)
        body = QRectF(x + pen_w / 2, y + pen_w / 2, body_w - pen_w, h - pen_w)

        # Тонкая подсветка контура для читаемости (кроме режима «Плашка»).
        if edge is not None:
            p.setPen(QPen(edge, pen_w + 1.6))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(body, h * 0.2, h * 0.2)

        p.setPen(QPen(fg, pen_w))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(body, h * 0.2, h * 0.2)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(fg)
        p.drawRoundedRect(
            QRectF(x + body_w, y + h * 0.32, cap_w, h * 0.36), cap_w / 2, cap_w / 2
        )

        if self.percent <= 15:
            fill = QColor("#FF5D65")
        elif self.percent <= 35:
            fill = QColor("#FFC857")
        else:
            fill = QColor("#43D9A3")
        inset = pen_w * 1.9
        inner = QRectF(x + inset, y + inset, body_w - 2 * inset, h - 2 * inset)
        fill_w = inner.width() * max(0, min(100, self.percent)) / 100
        if fill_w > 0:
            p.setBrush(fill)
            p.drawRoundedRect(
                QRectF(inner.x(), inner.y(), fill_w, inner.height()),
                h * 0.1, h * 0.1,
            )

        if self.charging:
            cx, cy, s = x + body_w / 2, y + h / 2, h * 0.28
            bolt = QPolygonF([
                QPointF(cx + s * 0.2, cy - s * 1.1),
                QPointF(cx - s * 0.7, cy + s * 0.15),
                QPointF(cx - s * 0.05, cy + s * 0.15),
                QPointF(cx - s * 0.2, cy + s * 1.1),
                QPointF(cx + s * 0.7, cy - s * 0.15),
                QPointF(cx + s * 0.05, cy - s * 0.15),
            ])
            p.setPen(QPen(QColor(0, 0, 0, 160), 1))
            p.setBrush(QColor("#FFFFFF"))
            p.drawPolygon(bolt)


# ---------------------------------------------------------------- режим настройки

LAYER_STYLE = """
#topbar, #chip, #panel {
    background-color: #1E1F22;
    border: 1px solid #3A3C43;
    border-radius: 12px;
}
QLabel { color: #DBDEE1; background: transparent; font-family: "Segoe UI"; font-size: 13px; }
QLabel#title { color: #FFFFFF; font-weight: bold; font-size: 14px; }
QPushButton {
    outline: none;
    background-color: #2B2D31; color: #F2F3F5; border: none;
    border-radius: 8px; padding: 6px 12px;
    font-family: "Segoe UI"; font-size: 13px;
}
QPushButton:hover { background-color: #383A40; }
QCheckBox { color: #DBDEE1; font-family: "Segoe UI"; font-size: 13px; spacing: 8px; outline: none; }
#tip { background-color: #111214; border: 1px solid #3A3C43; border-radius: 10px; }
#tip QLabel { color: #F2F3F5; font-family: "Segoe UI"; font-size: 12px; }
QPushButton:checked { background-color: #5865F2; color: #FFFFFF; }
QPushButton#done { background-color: #5865F2; color: #FFFFFF; font-weight: bold; padding: 7px 18px; }
QPushButton#done:hover { background-color: #4752C4; }
QPushButton#gear { font-family: "Segoe UI Symbol"; font-size: 15px; padding: 3px 9px; }
QSlider::groove:horizontal { height: 4px; background: #4E5058; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #5865F2; border-radius: 2px; }
QSlider::handle:horizontal { background: #FFFFFF; width: 14px; margin: -6px 0; border-radius: 7px; }
"""


class HoverTip(QFrame):
    """Своя подсказка: обычный дочерний виджет слоя, поэтому появляется мгновенно
    и не мигает (системные подсказки Qt на прозрачных окнах ведут себя нестабильно)."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("tip")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)  # отступы от текста до края
        self.label = QLabel()
        layout.addWidget(self.label)
        self.hide()

    def show_for(self, widget, text, container):
        self.label.setText(text)
        self.adjustSize()
        top_left = widget.mapTo(container, QPoint(0, 0))
        x = top_left.x() + (widget.width() - self.width()) // 2
        y = top_left.y() - self.height() - 8
        if y < 8:  # не помещается сверху — показываем снизу
            y = top_left.y() + widget.height() + 8
        x = min(max(x, 8), container.width() - self.width() - 8)
        self.move(x, y)
        self.show()
        self.raise_()


class EditLayer(QWidget):
    """Затемнённый слой на весь экран с пунктирной зоной вокруг панели."""

    def __init__(self, battery, on_done, watcher):
        super().__init__()
        self.battery = battery
        self.on_done = on_done
        self.watcher = watcher
        self._tips = {}  # кнопка -> текст всплывающей подсказки
        self.dragging = False
        self.drag_offset = QPoint()

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setStyleSheet(LAYER_STYLE)

        self.build_topbar()
        self.build_chip()
        self.build_panel()
        for control in self.findChildren(QPushButton) + self.findChildren(QCheckBox):
            control.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self.tip = HoverTip(self)

    # --- элементы интерфейса

    def build_topbar(self):
        self.topbar = QFrame(self)
        self.topbar.setObjectName("topbar")
        row = QHBoxLayout(self.topbar)
        row.setContentsMargins(16, 10, 10, 10)
        row.setSpacing(14)
        row.addWidget(QLabel("Режим настройки: перетащи панель мышью. Esc или «Готово» для выхода."))
        done = QPushButton("Готово")
        done.setObjectName("done")
        done.clicked.connect(self.finish)
        row.addWidget(done)

    def build_chip(self):
        self.chip = QFrame(self)
        self.chip.setObjectName("chip")
        row = QHBoxLayout(self.chip)
        row.setContentsMargins(12, 6, 8, 6)
        row.setSpacing(8)
        title = QLabel("Батарея")
        title.setObjectName("title")
        row.addWidget(title)

        self.gear = QPushButton("⚙")
        self.gear.setObjectName("gear")
        self.gear.setCheckable(True)
        self.gear.toggled.connect(self.toggle_panel)
        row.addWidget(self.gear)

        self.eye = QPushButton("Скрыть")
        self.eye.clicked.connect(self.toggle_hidden)
        row.addWidget(self.eye)

    def build_panel(self):
        self.panel = QFrame(self)
        self.panel.setObjectName("panel")
        self.panel.setFixedWidth(320)  # фиксированная ширина, чтобы подсказки переносились стабильно
        col = QVBoxLayout(self.panel)
        col.setContentsMargins(16, 14, 16, 14)
        col.setSpacing(10)

        head = QLabel("Настройки панели")
        head.setObjectName("title")
        col.addWidget(head)

        col.addWidget(QLabel("Размер"))
        size_row = QHBoxLayout()
        self.size_slider = QSlider(Qt.Orientation.Horizontal)
        self.size_slider.setRange(14, 64)
        self.size_slider.setValue(self.battery.font_px)
        self.size_slider.setMinimumWidth(200)
        self.size_label = QLabel(str(self.battery.font_px))
        self.size_label.setMinimumWidth(26)
        self.size_slider.valueChanged.connect(self.on_size)
        size_row.addWidget(self.size_slider)
        size_row.addWidget(self.size_label)
        col.addLayout(size_row)

        col.addWidget(QLabel("Вид текста"))
        style_row = QHBoxLayout()
        style_row.setSpacing(6)
        self.style_group = QButtonGroup(self)
        self.style_group.setExclusive(True)
        for name in STYLES:
            button = QPushButton(name)
            button.setCheckable(True)
            button.setChecked(name == self.battery.style_name)
            button.clicked.connect(lambda _checked, n=name: self.on_style(n))
            self.style_group.addButton(button)
            self.add_tip(button, STYLE_HINTS[name])
            style_row.addWidget(button)
        col.addLayout(style_row)

        # Прозрачность подложки: нужна только в виде «Плашка».
        self.alpha_box = QWidget()
        alpha_col = QVBoxLayout(self.alpha_box)
        alpha_col.setContentsMargins(0, 0, 0, 0)
        alpha_col.setSpacing(6)
        alpha_col.addWidget(QLabel("Прозрачность плашки"))
        alpha_row = QHBoxLayout()
        self.alpha_slider = QSlider(Qt.Orientation.Horizontal)
        self.alpha_slider.setRange(0, 95)
        self.alpha_slider.setValue(self.battery.pill_transparency)
        self.alpha_label = QLabel(f"{self.battery.pill_transparency}%")
        self.alpha_label.setMinimumWidth(38)
        self.alpha_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.alpha_slider.valueChanged.connect(self.on_alpha)
        alpha_row.addWidget(self.alpha_slider)
        alpha_row.addWidget(self.alpha_label)
        alpha_col.addLayout(alpha_row)
        self.alpha_box.setVisible(self.battery.style_name == "Плашка")
        col.addWidget(self.alpha_box)

        self.icon_cb = QCheckBox("Показывать значок батареи")
        self.icon_cb.setChecked(self.battery.show_icon)
        self.icon_cb.toggled.connect(self.on_icon)
        col.addWidget(self.icon_cb)

        col.addWidget(QLabel("Когда показывать оверлей"))
        mode_row = QHBoxLayout()
        mode_row.setSpacing(6)
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        for mode, caption, tip in MODE_BUTTONS:
            button = QPushButton(caption)
            button.setCheckable(True)
            button.setChecked(mode == self.watcher.mode)
            button.clicked.connect(lambda _checked, m=mode: self.on_mode(m))
            self.mode_group.addButton(button)
            self.add_tip(button, tip)
            mode_row.addWidget(button)
        col.addLayout(mode_row)

        # Список игр: нужен только в режиме «Только в играх».
        self.games_box = QWidget()
        games_col = QVBoxLayout(self.games_box)
        games_col.setContentsMargins(0, 0, 0, 0)
        games_col.setSpacing(6)
        self.games_info = QLabel()
        self.games_info.setWordWrap(True)
        games_col.addWidget(self.games_info)
        games_row = QHBoxLayout()
        games_row.setSpacing(6)
        is_game = QPushButton("Это игра")
        is_game.clicked.connect(lambda: self.on_mark(True))
        self.add_tip(is_game, "Всегда показывать оверлей в этом приложении")
        not_game = QPushButton("Не игра")
        not_game.clicked.connect(lambda: self.on_mark(False))
        self.add_tip(not_game, "Никогда не показывать оверлей в этом приложении")
        games_row.addWidget(is_game)
        games_row.addWidget(not_game)
        games_col.addLayout(games_row)
        self.games_box.setVisible(self.watcher.mode == "games")
        col.addWidget(self.games_box)
        self.refresh_games_info()

        reset = QPushButton("Сбросить позицию")
        reset.clicked.connect(self.on_reset)
        col.addWidget(reset)

        self.panel.hide()

    def add_tip(self, button, text):
        self._tips[button] = text
        button.installEventFilter(self)

    def eventFilter(self, obj, event):
        text = self._tips.get(obj)
        if text is not None:
            kind = event.type()
            if kind == QEvent.Type.Enter:
                self.tip.show_for(obj, text, self)
            elif kind in (
                QEvent.Type.Leave,
                QEvent.Type.MouseButtonPress,
                QEvent.Type.Hide,
            ):
                self.tip.hide()
        return super().eventFilter(obj, event)

    # --- действия

    def on_style(self, name):
        self.battery.set_style_name(name)
        self.alpha_box.setVisible(name == "Плашка")
        self.layout_children()  # строка прозрачности меняет высоту панели

    def refresh_games_info(self):
        self.games_info.setText(self.watcher.describe())

    def on_mode(self, mode):
        self.watcher.set_mode(mode)
        self.games_box.setVisible(mode == "games")
        self.refresh_games_info()
        self.layout_children()  # панель меняет высоту

    def on_mark(self, is_game):
        self.watcher.mark(is_game)
        self.refresh_games_info()
        self.layout_children()

    def on_icon(self, checked):
        self.battery.set_show_icon(checked)
        self.layout_children()  # размер оверлея изменился, зона захвата тоже
        self.update()

    def on_alpha(self, value):
        self.alpha_label.setText(f"{value}%")
        self.battery.set_pill_transparency(value)

    def on_size(self, value):
        self.size_label.setText(str(value))
        self.battery.set_font_px(value)
        self.layout_children()
        self.update()

    def on_reset(self):
        self.battery.reset_position()
        self.layout_children()
        self.update()

    def toggle_panel(self, shown):
        self.tip.hide()
        self.panel.setVisible(shown)
        self.layout_children()

    def toggle_hidden(self):
        hide = self.battery.user_visible
        self.battery.set_user_visible(not hide)
        self.battery.set_ghost(hide)
        self.eye.setText("Показать" if hide else "Скрыть")
        self.layout_children()  # ширина кнопки и подписи зависит от текста

    def finish(self):
        self.tip.hide()
        self.battery.save_position()
        self.hide()
        self.battery.set_ghost(False)
        self.on_done()

    def open(self):
        screen = (
            QGuiApplication.screenAt(self.battery.geometry().center())
            or QGuiApplication.primaryScreen()
        )
        self.setGeometry(screen.geometry())
        self.size_slider.setValue(self.battery.font_px)
        self.alpha_slider.setValue(self.battery.pill_transparency)
        self.icon_cb.setChecked(self.battery.show_icon)
        self.refresh_games_info()
        self.battery.set_ghost(not self.battery.user_visible)
        self.eye.setText("Скрыть" if self.battery.user_visible else "Показать")
        self.gear.setChecked(False)
        self.panel.hide()

        self.show()
        self.layout_children()
        self.battery.show()
        self.battery.raise_()
        self.activateWindow()
        self.setFocus()
        QTimer.singleShot(80, self.battery.raise_)

    # --- геометрия

    def zone(self):
        top_left = self.mapFromGlobal(self.battery.pos())
        return QRect(top_left, self.battery.size()).adjusted(
            -ZONE_PAD, -ZONE_PAD, ZONE_PAD, ZONE_PAD
        )

    def layout_children(self):
        w, h = self.width(), self.height()

        self.topbar.adjustSize()
        self.topbar.move((w - self.topbar.width()) // 2, 24)

        self.chip.adjustSize()
        zone = self.zone()
        gap = 8

        panel_open = self.panel.isVisible()
        self.panel.adjustSize()  # размер считается и у скрытой панели

        # Высоту панели резервируем всегда, даже когда она закрыта, и с запасом
        # под строку прозрачности. Тогда решение «над зоной или под ней»
        # не зависит от состояния настроек, и ярлычок не прыгает при их открытии.
        panel_h = self.panel.height()
        for box in (self.alpha_box, self.games_box):
            if not box.isVisibleTo(self.panel):
                panel_h += box.sizeHint().height() + self.panel.layout().spacing()
        stack_h = self.chip.height() + gap + panel_h

        # Если стопка (ярлычок + настройки) не помещается под зоной,
        # ярлычок всегда стоит над ней.
        room_below = h - 8 - (zone.bottom() + gap)
        above = stack_h > room_below

        if above:
            chip_y = zone.top() - gap - self.chip.height()
        else:
            chip_y = zone.bottom() + gap
        chip_x = min(max(zone.left(), 8), w - self.chip.width() - 8)

        if panel_open:
            if above:
                panel_y = chip_y - gap - self.panel.height()
            else:
                panel_y = chip_y + self.chip.height() + gap
            # Страховка: панель всегда целиком в пределах экрана.
            panel_y = min(max(panel_y, 8), h - 8 - self.panel.height())
            panel_x = min(max(zone.left(), 8), w - self.panel.width() - 8)
            self.panel.move(panel_x, panel_y)
            self.panel.raise_()

        chip_y = min(max(chip_y, 8), h - 8 - self.chip.height())
        self.chip.move(chip_x, chip_y)

    # --- события

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(0, 0, 0, 150))

        zone = QRectF(self.zone())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 18))
        p.drawRoundedRect(zone, 16, 16)

        pen = QPen(QColor(255, 255, 255, 175))
        pen.setWidthF(2.0)
        pen.setDashPattern([4, 4])
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(zone.adjusted(1, 1, -1, -1), 16, 16)
        p.end()

    def mousePressEvent(self, event):
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self.zone().contains(event.position().toPoint())
        ):
            self.dragging = True
            self.drag_offset = event.globalPosition().toPoint() - self.battery.pos()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()

    def mouseMoveEvent(self, event):
        if self.dragging:
            target = event.globalPosition().toPoint() - self.drag_offset
            g = self.geometry()
            x = min(max(target.x(), g.left()), g.right() - self.battery.width() + 1)
            y = min(max(target.y(), g.top()), g.bottom() - self.battery.height() + 1)
            self.battery.move(x, y)
            self.layout_children()
            self.update()
            event.accept()
        else:
            inside = self.zone().contains(event.position().toPoint())
            self.setCursor(
                Qt.CursorShape.SizeAllCursor if inside else Qt.CursorShape.ArrowCursor
            )

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.dragging:
            self.dragging = False
            self.battery.save_position()
            self.setCursor(Qt.CursorShape.SizeAllCursor)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.finish()
        else:
            super().keyPressEvent(event)


# ---------------------------------------------------------------- приложение

def make_tray_icon():
    pix = QPixmap(32, 32)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor("#F2F3F5"))
    pen.setWidthF(2.5)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(QRectF(3, 8, 23, 16), 4, 4)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#F2F3F5"))
    p.drawRoundedRect(QRectF(27, 13, 3, 6), 1, 1)
    p.setBrush(QColor("#3DDC97"))
    p.drawRoundedRect(QRectF(6, 11, 14, 10), 2, 2)
    p.end()
    return QIcon(pix)


class OverlayApp:
    def __init__(self, app):
        self.app = app
        self.settings = QSettings("BatteryOverlayProject", "BatteryOverlay")

        self.battery = BatteryWidget(self.settings)
        self.watcher = GameWatcher(self.settings)
        self.layer = EditLayer(self.battery, self.after_edit, self.watcher)

        self.tick()
        self.watcher.poll()
        self.apply_visibility()

        self.timer = QTimer()
        self.timer.timeout.connect(self.tick)
        self.timer.start(2000)

        # Активное окно проверяем чаще, чтобы оверлей появлялся без заметной задержки.
        self.watch_timer = QTimer()
        self.watch_timer.timeout.connect(self.watch)
        self.watch_timer.start(400)

        self.setup_tray()
        self.setup_hotkey()
        app.aboutToQuit.connect(self.cleanup)

    def tick(self):
        percent, charging, is_test = read_battery()
        self.battery.set_data(percent, charging, is_test)
        self.battery.raise_topmost()

    def toggle_edit(self):
        if self.layer.isVisible():
            self.layer.finish()
        else:
            self.layer.open()

    def watch(self):
        self.watcher.poll()
        self.apply_visibility()

    def apply_visibility(self):
        """Оверлей виден, если он не выключен вручную и выполняется условие режима."""
        if self.layer.isVisible():
            return  # в режиме настройки панель всегда на экране
        show = self.battery.user_visible and (
            self.watcher.mode == "all" or self.watcher.active
        )
        if show != self.battery.isVisible():
            self.battery.setVisible(show)
            if show:
                self.battery.raise_topmost()

    def after_edit(self):
        self.apply_visibility()
        self.visible_action.setChecked(self.battery.user_visible)

    def toggle_visible(self, checked):
        self.battery.set_user_visible(checked)
        self.apply_visibility()

    def setup_tray(self):
        self.tray = QSystemTrayIcon(make_tray_icon())
        self.tray.setToolTip("BatteryOverlay")
        self.menu = QMenu()

        edit_action = QAction("Настроить оверлей (Ctrl+Shift+B)", self.menu)
        edit_action.triggered.connect(self.toggle_edit)
        self.menu.addAction(edit_action)

        self.visible_action = QAction("Показывать оверлей", self.menu)
        self.visible_action.setCheckable(True)
        self.visible_action.setChecked(self.battery.user_visible)
        self.visible_action.toggled.connect(self.toggle_visible)
        self.menu.addAction(self.visible_action)

        self.menu.addSeparator()
        quit_action = QAction("Выход", self.menu)
        quit_action.triggered.connect(self.app.quit)
        self.menu.addAction(quit_action)

        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self.on_tray_click)
        self.tray.show()
        self._tray_actions = (edit_action, quit_action)

    def on_tray_click(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.toggle_edit()

    def setup_hotkey(self):
        self.hotkey_filter = HotkeyFilter(self.toggle_edit)
        self.app.installNativeEventFilter(self.hotkey_filter)
        self.hotkey_registered = bool(user32.RegisterHotKey(
            None, HOTKEY_ID, MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, VK_B
        ))
        if self.hotkey_registered:
            print("Горячая клавиша Ctrl+Shift+B зарегистрирована.")
        else:
            message = (
                "Не удалось зарегистрировать Ctrl+Shift+B: сочетание занято "
                "другой программой или старый запуск ещё работает. "
                "Настройки доступны через значок в трее."
            )
            print(message)
            self.tray.showMessage("BatteryOverlay", message)

    def cleanup(self):
        if getattr(self, "hotkey_registered", False):
            user32.UnregisterHotKey(None, HOTKEY_ID)
            self.hotkey_registered = False
        self.battery.save_position()


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    overlay_app = OverlayApp(app)  # noqa: F841 (держим ссылку)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()