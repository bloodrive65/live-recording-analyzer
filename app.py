"""Live Recording Analyzer — desktop app (formerly OBS Session Analyzer)."""
from __future__ import annotations

import json
import queue
import os
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime, timedelta

from PySide6.QtCore import Qt, QTimer, QThread, Signal, QUrl, QSize
from PySide6.QtGui import QIcon, QAction, QDesktopServices, QFont, QColor, QPalette
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QTabWidget, QComboBox, QFileDialog, QCheckBox, QLineEdit, QSpinBox, QProgressBar, QTreeWidget,
    QTreeWidgetItem, QMessageBox, QFrame, QSystemTrayIcon, QMenu, QListWidget, QListWidgetItem,
    QTextBrowser, QSizePolicy, QHeaderView, QAbstractItemView, QScrollArea, QDialog, QStackedWidget,
)

from analyzer import obs_log, analyze, report
from analyzer.monitor import Monitor
from analyzer.obsws import ObsClient, obs_ws_config, connection_status
from analyzer import system_check
from analyzer.live import LiveAdvisor, Advice
from analyzer.analyze import fmt_t
from analyzer import weblink, updater

APP_NAME = "Live Recording Analyzer"
OLD_NAME = "OBS Session Analyzer"
VERSION = "1.3.0"


def resource(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "assets", name)


def docs_dir(name=None):
    name = name or APP_NAME
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf)  # CSIDL_PERSONAL
            if buf.value:
                return os.path.join(buf.value, name)
        except Exception:
            pass
    return os.path.join(os.path.expanduser("~"), "Documents", name)


DATA = docs_dir()
CRASH_LOG = os.path.join(DATA, "crash.log")
RUN_FLAG = os.path.join(DATA, ".running")
# Upgrading from 1.1 (OBS Session Analyzer): move the old data folder so sessions, reports and settings carry over.
try:
    _old = docs_dir(OLD_NAME)
    if os.path.isdir(_old) and not os.path.exists(DATA):
        os.rename(_old, DATA)
except Exception:
    pass
SESSIONS = os.path.join(DATA, "Sessions")
REPORTS = os.path.join(DATA, "Reports")
SETTINGS_FILE = os.path.join(DATA, "settings.json")
for d in (SESSIONS, REPORTS):
    os.makedirs(d, exist_ok=True)


def load_settings():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(s):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except Exception:
        pass


STYLE = """
* { font-family: 'Segoe UI', sans-serif; font-size: 10pt; }
QMainWindow, QWidget { background: #0f1115; color: #e7e9ee; }
QTabWidget::pane { border: none; }
QTabBar::tab { background: transparent; color: #9aa3b2; padding: 10px 18px; margin-right: 4px; border-bottom: 2px solid transparent; font-weight: 600; }
QTabBar::tab:selected { color: #ffffff; border-bottom: 2px solid #d64545; }
QTabBar::tab:hover { color: #ffffff; }
QFrame#card { background: #171a21; border: 1px solid #2a2f3a; border-radius: 10px; }
QLabel#h1 { font-size: 17pt; font-weight: 700; }
QLabel#h2 { font-size: 12pt; font-weight: 650; }
QLabel#muted { color: #9aa3b2; }
QLabel#tileK { color: #9aa3b2; font-size: 8.5pt; font-weight: 600; }
QLabel#tileV { font-size: 17pt; font-weight: 650; }
QPushButton { background: #232834; border: 1px solid #343b49; border-radius: 7px; padding: 7px 14px; color: #e7e9ee; }
QPushButton:hover { background: #2b3140; }
QPushButton:disabled { color: #6b7280; background: #1a1d24; }
QPushButton#primary { background: #c23b3b; border: 1px solid #d64545; font-weight: 700; font-size: 11pt; padding: 10px 22px; }
QPushButton#primary:hover { background: #d64545; }
QPushButton#stop { background: #3a3f4b; border: 1px solid #555c6b; font-weight: 700; font-size: 11pt; padding: 10px 22px; }
QLineEdit, QComboBox, QSpinBox { background: #1d212a; border: 1px solid #343b49; border-radius: 6px; padding: 5px 8px; color: #e7e9ee; }
QSpinBox { padding-right: 26px; min-height: 22px; }
QSpinBox::up-button { subcontrol-origin: border; subcontrol-position: top right; width: 22px; background: #2e3545;
    border-left: 1px solid #4a5263; border-bottom: 1px solid #4a5263; border-top-right-radius: 6px; }
QSpinBox::down-button { subcontrol-origin: border; subcontrol-position: bottom right; width: 22px; background: #2e3545;
    border-left: 1px solid #4a5263; border-bottom-right-radius: 6px; }
QSpinBox::up-button:hover, QSpinBox::down-button:hover { background: #3d4659; }
QSpinBox::up-button:pressed, QSpinBox::down-button:pressed { background: #c23b3b; }
QSpinBox::up-arrow { image: url(UP_SVG); width: 10px; height: 10px; }
QSpinBox::down-arrow { image: url(DOWN_SVG); width: 10px; height: 10px; }
QLabel#pill { border-radius: 10px; padding: 3px 10px; font-weight: 600; font-size: 9pt; }
QLabel#chip { border-radius: 9px; padding: 2px 9px; font-weight: 700; font-size: 8.5pt; }
QComboBox QAbstractItemView { background: #1d212a; color: #e7e9ee; selection-background-color: #c23b3b; }
QTreeWidget, QListWidget, QTextBrowser { background: #171a21; border: 1px solid #2a2f3a; border-radius: 8px; color: #e7e9ee; }
QHeaderView::section { background: #1d212a; color: #9aa3b2; border: none; padding: 6px; font-weight: 600; }
QTreeWidget::item, QListWidget::item { padding: 4px; }
QTreeWidget::item:selected, QListWidget::item:selected { background: #3a2226; }
QProgressBar { background: #1d212a; border: 1px solid #343b49; border-radius: 6px; text-align: center; color: #e7e9ee; height: 18px; }
QProgressBar::chunk { background: #c23b3b; border-radius: 5px; }
QLabel, QCheckBox { background: transparent; }
QCheckBox { spacing: 8px; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #555c6b; border-radius: 4px; background: #1d212a; }
QCheckBox::indicator:checked { background: #c23b3b; border: 1px solid #d64545; image: url(CHECK_SVG); }
QCheckBox::indicator:hover { border: 1px solid #8a93a5; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #343b49; border-radius: 5px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #4a5263; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollArea#feed { border: none; background: transparent; }
QWidget#feedInner { background: transparent; }
QLabel#advT { font-size: 11pt; font-weight: 650; }
QLabel#steps { color: #cfd5df; background: #12151b; border-radius: 6px; padding: 8px 10px; }
QPushButton#link { background: transparent; border: none; color: #8fb8ff; padding: 2px 4px; }
QPushButton#link:hover { color: #c4d9ff; text-decoration: underline; }
QPushButton#act { background: #2a2030; border: 1px solid #5a3a46; padding: 5px 12px; }
QPushButton#act:hover { background: #3a2a3e; }
QStatusBar { color: #9aa3b2; }
QStatusBar QLabel { color: #9aa3b2; padding: 0 8px; }
"""


def _default_browser_cmd():
    """The default browser's command line (Windows), so very long report links aren't cut off."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice") as k:
            prog = winreg.QueryValueEx(k, "ProgId")[0]
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, prog + r"\shell\open\command") as k:
            return winreg.QueryValueEx(k, "")[0]
    except Exception:
        return None


def open_in_browser(url, scratch_dir):
    """Open a URL in the default browser. Long links go straight to the browser's command line
    (the normal Windows route can shorten them); a small local redirect page is the fallback."""
    import subprocess
    import shlex
    if len(url) < 2000 or sys.platform != "win32":
        return QDesktopServices.openUrl(QUrl(url))
    cmd = _default_browser_cmd()
    if cmd and "%1" in cmd:
        try:
            exe = shlex.split(cmd, posix=False)[0].strip('"')
            rest = cmd[cmd.find(exe) + len(exe):].lstrip('"').strip()
            args = [a.strip('"') for a in shlex.split(rest, posix=False)] if rest else ["%1"]
            args = [url if a == "%1" else a for a in args if a not in ("--single-argument",)]
            if url not in args:
                args.append(url)
            subprocess.Popen([exe] + args, close_fds=True)
            return True
        except Exception:
            pass
    try:
        os.makedirs(scratch_dir, exist_ok=True)
        p = os.path.join(scratch_dir, "_open-report.html")
        with open(p, "w", encoding="utf-8") as f:
            f.write("<!doctype html><meta charset='utf-8'><title>Opening report…</title>"
                    f"<script>location.replace({json.dumps(url)});</script>"
                    f"<p>Opening the report… <a href={json.dumps(url)}>Click here</a> if nothing happens.</p>")
        return QDesktopServices.openUrl(QUrl.fromLocalFile(p))
    except Exception:
        return False


def app_style():
    f = lambda n: resource(n).replace("\\", "/")
    return STYLE.replace("CHECK_SVG", f("check.svg")).replace("UP_SVG", f("up.svg")).replace("DOWN_SVG", f("down.svg"))


PILL = {"ok": ("#12321f", "#3ecf8e"), "warn": ("#3a2f12", "#f5b748"), "bad": ("#3a1717", "#ff6b6b"), "idle": ("#23272f", "#9aa3b2")}


def set_pill(lbl, kind, text):
    bg, fg = PILL.get(kind, PILL["idle"])
    lbl.setText(text)
    lbl.setStyleSheet(f"background:{bg}; color:{fg};")


def card():
    f = QFrame()
    f.setObjectName("card")
    return f


def label(text="", obj=None, wrap=False):
    l = QLabel(text)
    if obj:
        l.setObjectName(obj)
    if wrap:
        l.setWordWrap(True)
    return l


class Tile(QFrame):
    def __init__(self, title):
        super().__init__()
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        self.k = label(title.upper(), "tileK")
        self.v = label("—", "tileV")
        self.s = label("", "muted")
        lay.addWidget(self.k)
        lay.addWidget(self.v)
        lay.addWidget(self.s)

    def set(self, value, sub="", warn=False, bad=False):
        self.v.setText(value)
        self.s.setText(sub)
        color = "#ff5d5d" if bad else ("#f5b748" if warn else "#e7e9ee")
        self.v.setStyleSheet(f"color:{color}")


SEV = {"critical": ("#ff5d5d", "CRITICAL"), "warning": ("#f5b748", "WARNING"), "tip": ("#5fa8ff", "TIP"),
       "good": ("#3ecf8e", "GOOD"), "status": ("#9aa3b2", "STATUS"), "request": ("#c08cff", "NEEDS YOU")}
PHASE = {"before": "Before recording", "during": "During recording", "after": "After recording"}


class AdviceCard(QFrame):
    def __init__(self, a, on_action, on_dismiss):
        super().__init__()
        self.on_action, self.on_dismiss = on_action, on_dismiss
        self.setObjectName("adv")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 12, 10)
        lay.setSpacing(4)
        top = QHBoxLayout()
        self.badge = QLabel()
        self.badge.setObjectName("badge")
        top.addWidget(self.badge)
        self.meta = label("", "muted")
        top.addWidget(self.meta)
        top.addStretch(1)
        x = QPushButton("Dismiss")
        x.setObjectName("link")
        x.clicked.connect(lambda: self.on_dismiss(self.advice.key))
        top.addWidget(x)
        lay.addLayout(top)
        self.title = label("", "advT", True)
        self.body = label("", None, True)
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.title)
        lay.addWidget(self.body)
        self.steps_btn = QPushButton("Show what to do")
        self.steps_btn.setObjectName("link")
        self.steps_btn.clicked.connect(self._toggle)
        self.steps = label("", "steps", True)
        self.steps.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.steps)
        self.btns = QHBoxLayout()
        self.btns.setSpacing(6)
        self.btns.addWidget(self.steps_btn)
        lay.addLayout(self.btns)
        self.refresh(a)

    def _toggle(self):
        v = not self._open
        self._open = v
        self.steps.setVisible(v)
        self.steps_btn.setText("Hide steps" if v else "Show what to do")

    def refresh(self, a):
        self.advice = a
        col, name = SEV.get(a.severity, ("#9aa3b2", a.severity.upper()))
        self.setStyleSheet(f"QFrame#adv {{ background:#171a21; border:1px solid #2a2f3a; border-left:4px solid {col}; border-radius:8px; }}")
        self.badge.setText(name)
        self.badge.setStyleSheet(f"color:{col}; font-weight:700; font-size:8.5pt;")
        meta = [a.ts.strftime("%H:%M:%S"), PHASE.get(a.phase, a.phase)]
        if a.video_t is not None:
            meta.append(f"video {fmt_t(a.video_t)}")
        if a.count > 1:
            meta.append(f"×{a.count:,}")
        self.meta.setText("  ·  ".join(meta))
        self.title.setText(a.title)
        self.body.setText(a.body)
        self.body.setVisible(bool(a.body))
        self.steps.setText("<br>".join(f"{i + 1}. {report.E(s)}" for i, s in enumerate(a.steps)))
        auto_open = a.severity in ("critical", "warning", "request") or a.key == "analysis-done"
        if not hasattr(self, "_open"):
            self._open = bool(a.steps) and auto_open
        self.steps.setVisible(bool(a.steps) and self._open)
        self.steps_btn.setVisible(bool(a.steps))
        self.steps_btn.setText("Hide steps" if self._open else "Show what to do")
        # rebuild action buttons
        while self.btns.count() > 1:
            it = self.btns.takeAt(1)
            if it.widget():
                it.widget().deleteLater()
        for lbl, aid, data in a.actions:
            b = QPushButton(lbl)
            b.setObjectName("act")
            b.clicked.connect(lambda _=False, i=aid, d=data: self.on_action(i, d))
            self.btns.addWidget(b)
        self.btns.addStretch(1)


# ------------------------------------------------------------------------------ workers
class AnalyzeWorker(QThread):
    progress = Signal(str, float)
    done = Signal(object, str)
    failed = Signal(str)

    def __init__(self, log_path, videos, monitor_path, deep, rec_filter):
        super().__init__()
        self.args = (log_path, videos, monitor_path, deep, rec_filter)
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        log_path, videos, monitor_path, deep, rec_filter = self.args
        try:
            res = analyze.run(log_path, video_paths=videos, monitor_path=monitor_path, deep=deep,
                              progress=lambda m, p: self.progress.emit(m, p), cancel=lambda: self._cancel,
                              recordings_filter=rec_filter)
            if self._cancel:
                self.failed.emit("Cancelled.")
                return
            path = report.save(res, REPORTS)
            self.done.emit(res, path)
        except Exception:
            self.failed.emit(traceback.format_exc())


class TestObsWorker(QThread):
    result = Signal(bool, str)

    def __init__(self, port, password):
        super().__init__()
        self.port, self.password = port, password

    def run(self):
        try:
            c = ObsClient("127.0.0.1", self.port, self.password)
            c.connect()
            v = c.request("GetVersion")
            st = c.request("GetStats")
            c.close()
            self.result.emit(True, f"Connected to OBS {v.get('obsVersion', '')} — {st.get('activeFps', 0):.0f} fps")
        except PermissionError as e:
            self.result.emit(False, str(e))
        except Exception:
            self.result.emit(False, "Couldn’t reach OBS. Is OBS open, and is Tools → WebSocket Server Settings → “Enable WebSocket server” ticked?")


# ------------------------------------------------------------------------------ guided setup
class SetupOffer(QDialog):
    """First-run question: guided setup or not, with “Remember my choice”."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Welcome to " + APP_NAME)
        self.setMinimumWidth(500)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 18)
        lay.setSpacing(12)
        lay.addWidget(label("Would you like a guided setup?", "h1"))
        lay.addWidget(label("The guided setup checks this PC — OBS, the connection to OBS, your recording drive, graphics card, "
                            "Windows HDR and notifications — and walks you through anything that needs changing. It takes about a minute "
                            "and never changes anything by itself.", "muted", True))
        lay.addWidget(label("Prefer to set things up yourself? Skip it. You can start it any time from Settings → Run guided setup.", "muted", True))
        self.remember = QCheckBox("Remember my choice")
        lay.addWidget(self.remember)
        row = QHBoxLayout()
        row.addStretch(1)
        no = QPushButton("No, I’ll set it up myself")
        no.clicked.connect(self.reject)
        yes = QPushButton("Yes, guide me")
        yes.setObjectName("primary")
        yes.setDefault(True)
        yes.clicked.connect(self.accept)
        row.addWidget(no)
        row.addWidget(yes)
        lay.addLayout(row)


STATE_CHIP = {"ok": ("ok", "OK"), "attention": ("warn", "Needs attention"), "unknown": ("idle", "Can’t check yet")}


class CheckCard(QFrame):
    def __init__(self, c):
        super().__init__()
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(6)
        top = QHBoxLayout()
        top.addWidget(label(c["title"], "advT"))
        chip = QLabel()
        chip.setObjectName("chip")
        kind, text = STATE_CHIP.get(c["state"], STATE_CHIP["unknown"])
        set_pill(chip, kind, text)
        top.addWidget(chip)
        top.addStretch(1)
        act = c.get("action")
        if act:
            b = QPushButton(act[0])
            b.setObjectName("link")
            target = act[1][4:] if act[1].startswith("url:") else act[1]
            b.clicked.connect(lambda _=False, t=target: QDesktopServices.openUrl(QUrl(t)))
            top.addWidget(b)
        lay.addLayout(top)
        lay.addWidget(label(c["summary"], "muted", True))
        if c.get("steps"):
            st = label("\n".join(("• " if len(c["steps"]) > 1 else "") + x for x in c["steps"]), "steps", True)
            st.setTextInteractionFlags(Qt.TextSelectableByMouse)
            lay.addWidget(st)


class SetupWizard(QDialog):
    """Step-by-step setup based on what is actually on this PC."""

    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.completed = False
        self.setWindowTitle(APP_NAME + " — guided setup")
        self.resize(760, 640)
        self.setAttribute(Qt.WA_DeleteOnClose)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 16)
        root.setSpacing(10)
        self.step_lbl = label("", "muted")
        root.addWidget(self.step_lbl)
        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)

        # page 1: intro
        p1 = QWidget()
        l1 = QVBoxLayout(p1)
        l1.setContentsMargins(0, 0, 0, 0)
        l1.addWidget(label("Let’s check this PC", "h1"))
        l1.addWidget(label("The next step looks at what’s installed and how it’s set up. Each item shows <b>OK</b>, <b>Needs attention</b> "
                           "(with the exact steps for your setup) or <b>Can’t check yet</b> (usually because OBS isn’t open).", "muted", True))
        l1.addWidget(label("Before you start: open OBS Studio if it’s installed. It lets more checks run.", "muted", True))
        l1.addStretch(1)
        self.stack.addWidget(p1)

        # page 2: checks
        p2 = QWidget()
        l2 = QVBoxLayout(p2)
        l2.setContentsMargins(0, 0, 0, 0)
        hdr = QHBoxLayout()
        hdr.addWidget(label("What this PC has", "h1"))
        hdr.addStretch(1)
        self.again = QPushButton("Check again")
        self.again.clicked.connect(self.run_checks)
        hdr.addWidget(self.again)
        l2.addLayout(hdr)
        self.check_status = label("", "muted", True)
        l2.addWidget(self.check_status)
        self.prog = QProgressBar()
        self.prog.setRange(0, 0)
        self.prog.setFixedHeight(6)
        self.prog.setTextVisible(False)
        l2.addWidget(self.prog)
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setObjectName("feed")
        inner = QWidget()
        inner.setObjectName("feedInner")
        self.checks_lay = QVBoxLayout(inner)
        self.checks_lay.setContentsMargins(0, 0, 6, 0)
        self.checks_lay.setSpacing(8)
        self.checks_lay.addStretch(1)
        sa.setWidget(inner)
        l2.addWidget(sa, 1)
        self.stack.addWidget(p2)

        # page 3: alerts and finish
        p3 = QWidget()
        l3 = QVBoxLayout(p3)
        l3.setContentsMargins(0, 0, 0, 0)
        l3.setSpacing(10)
        l3.addWidget(label("Alerts and reports", "h1"))
        l3.addWidget(label("Alerts appear as Windows pop-ups in the corner of the screen, so you don’t have to look at the app.", "muted", True))
        g = QGridLayout()
        g.addWidget(label("Windows pop-ups"), 0, 0)
        self.toasts = QComboBox()
        self.toasts.addItems([main.toast_combo.itemText(i) for i in range(main.toast_combo.count())])
        self.toasts.setCurrentIndex(main.toast_combo.currentIndex())
        g.addWidget(self.toasts, 0, 1)
        g.addWidget(label("Resource use"), 1, 0)
        self.res = QComboBox()
        self.res.addItems([main.res_combo.itemText(i) for i in range(main.res_combo.count())])
        self.res.setCurrentIndex(main.res_combo.currentIndex())
        g.addWidget(self.res, 1, 1)
        g.addWidget(label("After each recording"), 2, 0)
        self.after = QComboBox()
        self.after.addItems(["Analyze it automatically (recommended)", "Let me click Analyze myself"])
        self.after.setCurrentIndex(0 if main.cb_autoanalyze.isChecked() else 1)
        self.after.setToolTip("Automatic: a report is ready a minute after you stop recording, with a Windows pop-up. "
                              "Manual: go to the Analyzer tab and click “Analyze my last recording” when you want one.")
        g.addWidget(self.after, 2, 1)
        g.setColumnStretch(1, 1)
        l3.addLayout(g)
        self.sound = QCheckBox("Play a sound with warnings")
        self.sound.setChecked(main.cb_sound.isChecked())
        self.auto = QCheckBox("Start watching automatically when this app opens")
        self.auto.setChecked(main.cb_autostart.isChecked())
        self.check_now = QCheckBox("Check my OBS settings when I click Finish")
        self.check_now.setChecked(True)
        for c in (self.sound, self.auto, self.check_now):
            l3.addWidget(c)
        l3.addWidget(label("Before each recording: click “Start watching”, then record in OBS like normal. "
                           "After you stop recording, the report appears on the Analyzer tab (and on the Live tab), and opens in your browser when you click Open report.", "muted", True))
        l3.addStretch(1)
        self.stack.addWidget(p3)

        nav = QHBoxLayout()
        self.cancel = QPushButton("Close")
        self.cancel.clicked.connect(self.reject)
        nav.addWidget(self.cancel)
        nav.addStretch(1)
        self.back = QPushButton("Back")
        self.back.clicked.connect(lambda: self.go(self.stack.currentIndex() - 1))
        self.next = QPushButton("Start checks")
        self.next.setObjectName("primary")
        self.next.clicked.connect(self.on_next)
        nav.addWidget(self.back)
        nav.addWidget(self.next)
        root.addLayout(nav)
        self.go(0)

    def go(self, i):
        i = max(0, min(2, i))
        self.stack.setCurrentIndex(i)
        self.step_lbl.setText(f"Step {i + 1} of 3")
        self.back.setVisible(i > 0)
        self.next.setText(("Start checks", "Next", "Finish")[i])
        if i == 1 and self.checks_lay.count() <= 1:
            self.run_checks()

    def on_next(self):
        i = self.stack.currentIndex()
        if i < 2:
            self.go(i + 1)
            return
        m = self.main
        m.toast_combo.setCurrentIndex(self.toasts.currentIndex())
        m.res_combo.setCurrentIndex(self.res.currentIndex())
        m.cb_sound.setChecked(self.sound.isChecked())
        m.cb_autostart.setChecked(self.auto.isChecked())
        m.cb_autoanalyze.setChecked(self.after.currentIndex() == 0)
        m._save_opts()
        self.completed = True
        if self.check_now.isChecked():
            m.tabs.setCurrentIndex(0)
            m.run_preflight()
        self.accept()

    def run_checks(self):
        self.again.setEnabled(False)
        self.prog.setVisible(True)
        self.check_status.setText("Checking OBS, the connection, the recording drive, graphics card, HDR and notifications…")
        while self.checks_lay.count() > 1:
            w = self.checks_lay.takeAt(0).widget()
            if w:
                w.deleteLater()
        self.main.run_setup_checks()

    def show_results(self, res):
        self.prog.setVisible(False)
        self.again.setEnabled(True)
        n = sum(1 for c in res if c["state"] == "attention")
        u = sum(1 for c in res if c["state"] == "unknown")
        self.check_status.setText(("Everything checked looks good." if not n else f"{n} item(s) need attention — the steps are below each one.")
                                  + (f" {u} item(s) couldn’t be checked yet." if u else "")
                                  + " Fix what you want, then click Check again, or click Next.")
        for i, c in enumerate(res):
            self.checks_lay.insertWidget(i, CheckCard(c))


class DownloadWorker(QThread):
    progress = Signal(int, int)
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, url, name):
        super().__init__()
        self.url, self.name = url, name
        self._cancel = False

    def run(self):
        try:
            p = updater.download(self.url, self.name, progress=lambda g, t: self.progress.emit(g, t), cancel=lambda: self._cancel)
            self.done.emit(p)
        except Exception as e:
            self.failed.emit(str(e) or repr(e))


class UpdateDialog(QDialog):
    """Shows what's new, then downloads and runs the installer when the user agrees."""

    def __init__(self, main, info):
        super().__init__(main)
        self.main, self.info, self.worker = main, info, None
        self.setWindowTitle(APP_NAME + " — update available")
        self.resize(560, 460)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 16)
        root.setSpacing(10)
        root.addWidget(label(f"Version {info['version']} is available", "h1"))
        root.addWidget(label(f"You have version {VERSION}. Updating keeps your settings, reports and watching data.", "muted", True))
        notes = QTextBrowser()
        notes.setOpenExternalLinks(True)
        notes.setMarkdown(info.get("notes") or "No release notes.")
        root.addWidget(notes, 1)
        self.prog = QProgressBar()
        self.prog.setRange(0, 1000)
        self.prog.setTextVisible(False)
        self.prog.setFixedHeight(8)
        self.prog.setVisible(False)
        root.addWidget(self.prog)
        self.status = label("The app will close, update and open again by itself. This takes about a minute.", "muted", True)
        root.addWidget(self.status)
        row = QHBoxLayout()
        self.skip = QPushButton("Skip this version")
        self.skip.clicked.connect(self._skip)
        row.addWidget(self.skip)
        row.addStretch(1)
        self.later = QPushButton("Later")
        self.later.clicked.connect(self._later)
        self.go = QPushButton("Update now")
        self.go.setObjectName("primary")
        self.go.clicked.connect(self._start)
        row.addWidget(self.later)
        row.addWidget(self.go)
        root.addLayout(row)

    def _skip(self):
        self.main.settings["skip_version"] = self.info["version"]
        save_settings(self.main.settings)
        self.reject()

    def _later(self):
        if self.worker:
            self.worker._cancel = True
        self.reject()

    def _start(self):
        if self.main.monitor and QMessageBox.question(
                self, APP_NAME, "The app is watching right now. Updating stops watching (your recording in OBS is not affected). Update now?") != QMessageBox.Yes:
            return
        self.go.setEnabled(False)
        self.skip.setEnabled(False)
        self.later.setText("Cancel")
        self.prog.setVisible(True)
        self.status.setText("Downloading the update…")
        self.worker = DownloadWorker(self.info["url"], self.info.get("name") or f"Live-Recording-Analyzer-Setup-{self.info['version']}.exe")
        self.main._threads = getattr(self.main, "_threads", set())
        self.main._threads.add(self.worker)
        self.worker.progress.connect(self._prog)
        self.worker.done.connect(self._install)
        self.worker.failed.connect(self._fail)
        self.worker.start()

    def _prog(self, got, total):
        total = total or self.info.get("size") or 0
        if total:
            self.prog.setValue(int(1000 * got / total))
            self.status.setText(f"Downloading the update… {got / 1048576:.1f} of {total / 1048576:.1f} MB")

    def _fail(self, msg):
        self.worker = None
        if msg == "Cancelled.":
            return
        self.go.setEnabled(True)
        self.later.setText("Later")
        self.prog.setVisible(False)
        self.status.setText(f"The update couldn’t be downloaded: {msg} You can also download it from the Releases page: {self.info.get('page')}")

    def _install(self, path):
        self.worker = None
        self.prog.setValue(1000)
        if sys.platform != "win32":
            self.status.setText(f"Downloaded to {path}. Automatic install only works on Windows.")
            return
        import subprocess
        try:
            subprocess.Popen([path, "/SILENT", "/SP-", "/NOCANCEL", "/SUPPRESSMSGBOXES", "/NORESTART"], close_fds=True)
        except Exception as e:
            self.status.setText(f"Couldn’t start the installer: {e!r}. It was saved to {path}; you can run it yourself.")
            self.go.setEnabled(True)
            return
        self.status.setText("Installing… the app will open again in a moment.")
        try:
            os.remove(RUN_FLAG)    # this is a planned exit, not a crash
        except Exception:
            pass
        QTimer.singleShot(400, self.main.quit_app)


# ------------------------------------------------------------------------------ main window
class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(resource("icon.ico")))
        self.resize(1080, 760)
        self.monitor: Monitor | None = None
        self.session_dir = None
        self.mon_started = None
        self.worker = None
        self.parsed_log = None
        self.video_overrides = {}
        self.quitting = False

        tabs = QTabWidget()
        self.tabs = tabs
        self.advisor = None
        self._ui_q = queue.Queue()
        self._live_job = False
        self.last_video = None
        settings_tab = self._settings_tab()     # build first: other tabs read its widgets
        tabs.addTab(self._live_tab(), "  Live  ")
        tabs.addTab(self._analyze_tab(), "  Analyzer  ")
        tabs.addTab(self._reports_tab(), "  OBS Reports  ")
        tabs.addTab(settings_tab, "  Settings  ")
        tabs.addTab(self._help_tab(), "  Help  ")
        self.setCentralWidget(tabs)
        self.statusBar().showMessage(f"Data folder: {DATA}")
        self.usage_lbl = QLabel("This app: measuring…")
        self.usage_lbl.setToolTip("CPU and memory used by Live Recording Analyzer itself")
        self.statusBar().addPermanentWidget(self.usage_lbl)
        self.toast_combo.setToolTip("How the app alerts you while you play")

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(1000)

        self._setup_tray()
        self.refresh_logs()
        self.refresh_sessions()
        self.refresh_reports()
        self.refresh_latest()
        tabs.currentChanged.connect(lambda i: self.refresh_latest() if i == 1 and not self.worker else None)
        if self.settings.get("autostart"):
            QTimer.singleShot(800, self.start_monitor)

    # ------------------------------------------------------------------ live tab
    def _live_tab(self):
        w = QWidget()
        root = QVBoxLayout(w)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)

        top = card()
        tl = QHBoxLayout(top)
        tl.setContentsMargins(20, 14, 20, 14)
        self.phase_dot = QLabel()
        self.phase_dot.setFixedSize(14, 14)
        self._set_dot("#6b7280")
        tl.addWidget(self.phase_dot, 0, Qt.AlignVCenter)
        tl.addSpacing(6)
        col = QVBoxLayout()
        self.mon_title = label("Not watching", "h1")
        self.mon_sub = label("Click Start before you play. The advisor watches OBS, the log, the PC and the file, and alerts you with Windows pop-ups.", "muted", True)
        trow = QHBoxLayout()
        trow.setSpacing(10)
        trow.addWidget(self.mon_title)
        self.live_pill = QLabel()
        self.live_pill.setObjectName("pill")
        self.live_pill.setCursor(Qt.PointingHandCursor)
        self.live_pill.setToolTip("Connection to OBS. Click to open Settings.")
        self.live_pill.mousePressEvent = lambda e: self.tabs.setCurrentIndex(3)
        set_pill(self.live_pill, "idle", "OBS: checking…")
        trow.addWidget(self.live_pill, 0, Qt.AlignVCenter)
        trow.addStretch(1)
        col.addLayout(trow)
        col.addWidget(self.mon_sub)
        self.pf_prog = QProgressBar()
        self.pf_prog.setRange(0, 0)          # moving bar: "working"
        self.pf_prog.setFixedHeight(6)
        self.pf_prog.setTextVisible(False)
        self.pf_prog.setVisible(False)
        col.addWidget(self.pf_prog)
        tl.addLayout(col, 1)
        self.pf_btn = QPushButton("Check OBS settings")
        self.pf_btn.setToolTip("Reads your OBS profile, scenes and latest log and lists anything that would cause a problem in your next recording.")
        self.pf_btn.clicked.connect(self.run_preflight)
        tl.addWidget(self.pf_btn, 0, Qt.AlignVCenter)
        self.mon_btn = QPushButton("Start watching")
        self.mon_btn.setObjectName("primary")
        self.mon_btn.setMinimumWidth(180)
        self.mon_btn.clicked.connect(self.toggle_monitor)
        tl.addWidget(self.mon_btn, 0, Qt.AlignVCenter)
        root.addWidget(top)

        grid = QGridLayout()
        grid.setSpacing(8)
        self.t_rec = Tile("Recording")
        self.t_skip_r = Tile("Rendering lag")
        self.t_skip_e = Tile("Encoding skips")
        self.t_fps = Tile("OBS FPS")
        self.t_gpu = Tile("GPU 3D")
        self.t_enc = Tile("GPU encoder")
        self.t_cpu = Tile("CPU")
        self.t_disk = Tile("Recording drive")
        for i, t in enumerate((self.t_rec, self.t_skip_r, self.t_skip_e, self.t_fps, self.t_gpu, self.t_enc, self.t_cpu, self.t_disk)):
            grid.addWidget(t, 0, i)
        root.addLayout(grid)

        hdr = QHBoxLayout()
        hdr.addWidget(label("Advisor", "h2"))
        self.feed_count = label("", "muted")
        hdr.addWidget(self.feed_count)
        hdr.addStretch(1)
        self.feed_filter = QComboBox()
        self.feed_filter.addItems(["Show everything", "Problems and requests only"])
        self.feed_filter.currentIndexChanged.connect(self._apply_filter)
        hdr.addWidget(self.feed_filter)
        b = QPushButton("Clear")
        b.clicked.connect(self.clear_feed)
        hdr.addWidget(b)
        root.addLayout(hdr)

        self.feed_area = QScrollArea()
        self.feed_area.setWidgetResizable(True)
        self.feed_area.setObjectName("feed")
        inner = QWidget()
        inner.setObjectName("feedInner")
        self.feed_lay = QVBoxLayout(inner)
        self.feed_lay.setContentsMargins(0, 0, 6, 0)
        self.feed_lay.setSpacing(8)
        self.feed_lay.addStretch(1)
        self.feed_area.setWidget(inner)
        root.addWidget(self.feed_area, 1)
        self.cards = {}       # key -> AdviceCard
        self._toast_last = 0
        self.post_local(Advice("hello", "status", "before", "Ready",
                               "1) Click “Check OBS settings” to look for anything that would cause a problem.  2) Click “Start watching”, then play and record like normal.  "
                               "3) When you stop recording, the file is checked and a report is posted here."))
        return w

    def _set_dot(self, color):
        self.phase_dot.setStyleSheet(f"background:{color}; border-radius:7px;")

    # ------------------------------------------------------------------ feed
    def post_local(self, a):
        """Add or update a card in the feed (UI thread)."""
        c = self.cards.get(a.key)
        if c and (a.update or c.advice.uid == a.uid):
            c.refresh(a)
            self.feed_lay.removeWidget(c)
            self.feed_lay.insertWidget(0, c)
        else:
            if c:
                c.setParent(None)
                c.deleteLater()
            c = AdviceCard(a, self._action, self._dismiss)
            self.cards[a.key] = c
            self.feed_lay.insertWidget(0, c)
        c.setVisible(self._passes(a))
        while len(self.cards) > 150:   # keep the feed light during long sessions
            oldest = self.feed_lay.itemAt(self.feed_lay.count() - 2).widget()
            self._dismiss(oldest.advice.key)
        self.feed_count.setText(f"{len(self.cards)} message(s)")
        self._maybe_toast(a)

    def _passes(self, a):
        return self.feed_filter.currentIndex() == 0 or a.severity in ("critical", "warning", "request")

    def _apply_filter(self):
        for c in self.cards.values():
            c.setVisible(self._passes(c.advice))

    def _dismiss(self, key):
        c = self.cards.pop(key, None)
        if c:
            c.setParent(None)
            c.deleteLater()
        self.feed_count.setText(f"{len(self.cards)} message(s)")

    def clear_feed(self):
        for k in list(self.cards):
            self._dismiss(k)

    def _maybe_toast(self, a):
        lvl = self.toast_combo.currentIndex() if hasattr(self, "toast_combo") else 1
        if lvl == 2 or a.phase == "before":
            return
        want = {"critical"} if lvl == 0 else {"critical", "warning", "request"}
        if a.severity not in want and not (a.key.startswith("rec-stop") or a.key == "analysis-done"):
            return
        now = time.time()
        if a.severity != "critical" and now - self._toast_last < 30:
            return
        self._toast_last = now
        self._last_toast_key = a.key
        if QSystemTrayIcon.isSystemTrayAvailable():
            icon = QSystemTrayIcon.Critical if a.severity == "critical" else (QSystemTrayIcon.Warning if a.severity in ("warning", "request") else QSystemTrayIcon.Information)
            self.tray.showMessage(a.title, (a.steps[0] if a.steps else a.body)[:200], icon, 8000)
        if self.cb_sound.isChecked() and a.severity in ("critical", "warning"):
            QApplication.beep()

    # ------------------------------------------------------------------ actions
    def _action(self, action_id, data):
        if action_id in ("pause", "resume"):
            self._obs_call("PauseRecord" if action_id == "pause" else "ResumeRecord")
        elif action_id == "browse_log":
            p, _ = QFileDialog.getOpenFileName(self, "Choose an OBS log", obs_log.default_log_dir(), "OBS log (*.txt *.log);;All files (*)")
            if p:
                self.tabs.setCurrentIndex(2)
                self._add_log(p)
        elif action_id == "locate_video":
            p, _ = QFileDialog.getOpenFileName(self, "Locate the recording", "", "Video (*.mkv *.mp4 *.mov *.flv *.ts *.m4v);;All files (*)")
            if p:
                self.last_video = p
                if self.advisor:
                    self.advisor.rec_path = p
                self.post_local(Advice("located", "status", "after", "Got it", f"The analysis will use {p}."))
        elif action_id in ("analyze_last", "deep_last"):
            self.analyze_last(deep=action_id == "deep_last")
        elif action_id == "open_report" and data:
            self.open_report_link(data)
        elif action_id == "open_url" and data:
            QDesktopServices.openUrl(QUrl(data))
        elif action_id == "open_path" and data:
            QDesktopServices.openUrl(QUrl.fromLocalFile(data))
        elif action_id == "copy_link" and data:
            self.copy_report_link(data)
        elif action_id == "open_settings":
            self.tabs.setCurrentIndex(3)

    def _obs_call(self, req):
        port, pw = self.port.value(), self.pw.text()

        def go():
            try:
                c = ObsClient("127.0.0.1", port, pw)
                c.connect()
                c.request(req)
                c.close()
                self._ui_q.put(Advice(f"obs-{req}", "status", "during", "Recording paused" if req == "PauseRecord" else "Recording resumed",
                                      "Fix the setting, then click Resume (or resume in OBS)." if req == "PauseRecord" else "",
                                      actions=[("Resume recording", "resume", None)] if req == "PauseRecord" else []))
            except Exception as e:
                self._ui_q.put(Advice(f"obs-{req}-err", "warning", "during", "Couldn’t reach OBS",
                                      f"{e}. Use OBS’s own Pause button, or check the WebSocket settings."))
        threading.Thread(target=go, daemon=True).start()

    # ------------------------------------------------------------------ OBS settings check
    def run_preflight(self):
        if not self.pf_btn.isEnabled():
            return
        self.pf_btn.setEnabled(False)
        self.pf_btn.setText("Checking…")
        self.pf_prog.setVisible(True)
        self.post_local(Advice("pf-progress", "status", "before", "Checking your OBS settings…", "Starting…", update=True))

        def stage(t):
            self._ui_q.put(("pf-stage", t))

        def go():
            from analyzer import obs_config
            from analyzer.live import preflight_rules
            from analyzer.analyze import drive_info
            t0 = time.time()
            try:
                stage("Reading your OBS profile (video, output and encoder settings)…")
                cfg = obs_config.load()
                time.sleep(0.25)
                stage(f"Reading scenes and sources ({len(cfg.capture_devices)} capture device(s), {len(cfg.screen_captures)} screen/game capture(s))…")
                time.sleep(0.25)
                stage("Reading the latest OBS log…")
                log = None
                logs = obs_log.latest_logs(1)
                if logs:
                    try:
                        log = obs_log.parse(logs[0])
                    except Exception:
                        log = None
                if cfg.rec_path:
                    stage(f"Checking the recording drive ({cfg.rec_path[:2]})…")
                    drive_info(cfg.rec_path)
                stage("Comparing everything…")
                items = preflight_rules(cfg, log)
            except Exception as e:
                items = [Advice("pf-error", "warning", "before", "The OBS settings check could not finish", repr(e))]
            left = 1.2 - (time.time() - t0)
            if left > 0:
                time.sleep(left)
            self._ui_q.put("pf-clear")
            for a in reversed(items):     # newest-first feed: keep the summary card on top
                self._ui_q.put(a)
            self._ui_q.put("pf-done")
        threading.Thread(target=go, daemon=True).start()

    # ------------------------------------------------------------------ OBS connection status
    def check_connection(self, force=False):
        """Background check: is OBS open, is WebSocket on, does the password work? Runs every few seconds."""
        if getattr(self, "_conn_busy", False):
            return
        if self.monitor and not force:
            st = self.monitor.obs_status
            if st == "connected":
                self._conn_result({"state": "connected", "label": "Connected to OBS", "detail": "Live skip tracking is on."})
                return
        self._conn_busy = True
        port, pw = self.port.value(), self.pw.text()
        last = getattr(self, "_conn_last", {}) or {}

        def go():
            import socket
            try:
                open_ = False
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                        open_ = True
                except OSError:
                    pass
                # Re-use a good result while the port stays open, so OBS isn't asked to log in every few seconds.
                if open_ and not force and last.get("state") in ("connected", "auth") and last.get("_pw") == pw and last.get("_port") == port:
                    res = last
                else:
                    res = connection_status(port, pw)
                    if res["state"] == "auth":
                        cfg = res.get("cfg") or {}
                        if cfg.get("password") and cfg["password"] != pw:   # OBS has a new password: use it
                            res2 = connection_status(port, cfg["password"])
                            res2["_newpw"] = cfg["password"]
                            res = res2
                    if res["state"] in ("obs_closed", "ws_off", "port"):
                        cfg = res.get("cfg") or {}
                        if cfg.get("found") and cfg.get("password") and not pw:
                            res["_newpw"] = cfg["password"]
                res["_pw"], res["_port"] = res.get("_newpw", pw), port
            except Exception as e:
                res = {"state": "error", "label": "Couldn’t check OBS", "detail": str(e)}
            self._ui_q.put(("conn", res))
        threading.Thread(target=go, daemon=True).start()

    def _conn_result(self, res):
        self._conn_busy = False
        prev = (getattr(self, "_conn_last", None) or {}).get("state")
        self._conn_last = res
        if res.get("_newpw") and res.get("_newpw") != self.pw.text():
            self.pw.setText(res["_newpw"])
            self._save_opts()
        st = res.get("state")
        kind = {"connected": "ok", "obs_closed": "idle", "ws_off": "warn", "port": "warn", "auth": "bad"}.get(st, "bad")
        short = {"connected": "OBS connected", "obs_closed": "OBS not running", "ws_off": "OBS WebSocket off",
                 "port": "OBS port mismatch", "auth": "OBS password rejected"}.get(st, "OBS not connected")
        set_pill(self.live_pill, kind, short)
        set_pill(self.conn_pill, kind, res.get("label", short))
        self.obs_state.setText(res.get("detail", ""))
        self.obs_state.setStyleSheet("color:#9aa3b2")
        if st == "connected":
            self.obs_hint.setText("Everything is set. The app connects to OBS automatically.")
            if prev and prev != "connected":
                self.statusBar().showMessage("Connected to OBS — the WebSocket setup worked.", 8000)
                self._save_opts()
        elif st == "ws_off":
            self.obs_hint.setText("One-time step: in OBS open Tools → WebSocket Server Settings and tick “Enable WebSocket server”, then click OK. "
                                  "This status updates by itself within a few seconds.")
        elif st == "obs_closed":
            cfg = res.get("cfg") or {}
            self.obs_hint.setText("WebSocket is turned on in OBS. Open OBS and this connects by itself." if cfg.get("enabled") else
                                  "Open OBS. If you haven’t yet: Tools → WebSocket Server Settings → tick “Enable WebSocket server” → OK.")
        elif st == "auth":
            self.obs_hint.setText("The password here doesn’t match OBS. In OBS: Tools → WebSocket Server Settings → Show Connect Info → copy the Server Password and paste it below.")
        if hasattr(self, "test_btn"):
            self.test_btn.setEnabled(True)

    # ------------------------------------------------------------------ watch start/stop
    def toggle_monitor(self):
        if self.monitor:
            self.stop_monitor()
        else:
            self.start_monitor()

    def start_monitor(self):
        if self.monitor:
            return
        self._save_opts()
        self.session_dir = os.path.join(SESSIONS, datetime.now().strftime("%Y-%m-%d %H-%M-%S"))
        try:
            import psutil
            if sys.platform == "win32":
                psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        except Exception:
            pass
        light = self.res_combo.currentIndex() == 1
        self.monitor = Monitor(self.session_dir, interval=2.0 if light else 1.0, obs_port=self.port.value(), obs_password=self.pw.text(), light=light)
        self.monitor.start()
        self.advisor = LiveAdvisor(self.monitor)
        self.advisor.start()
        self.mon_started = time.time()
        self.mon_btn.setText("Stop watching")
        self.mon_btn.setObjectName("stop")
        self.mon_btn.setStyle(self.mon_btn.style())
        self.tray_stop.setEnabled(True)
        self.tray.setToolTip(f"{APP_NAME} — watching")
        self._set_dot("#3ecf8e")
        self.post_local(Advice(f"watch-{time.time():.0f}", "status", "before", "Watching started",
                               "Checking your OBS settings now. You can minimize this window — alerts will pop up in the corner of the screen."))

    def stop_monitor(self):
        if not self.monitor:
            return
        m = self.monitor
        self.monitor = None
        m.stop()
        if self.advisor:
            self.advisor.stop()
        self.mon_btn.setText("Start watching")
        self.mon_btn.setObjectName("primary")
        self.mon_btn.setStyle(self.mon_btn.style())
        self.mon_title.setText("Not watching")
        self._set_dot("#6b7280")
        dur = time.time() - (self.mon_started or time.time())
        self.mon_sub.setText(f"Last session saved ({analyze.fmt_t(dur)}, {m.counts['samples']:,} samples).")
        self.tray_stop.setEnabled(False)
        self.tray.setToolTip(APP_NAME)
        self.refresh_sessions(select=os.path.join(self.session_dir, "monitor.jsonl"))

    # ------------------------------------------------------------------ after-recording analysis
    def _an_msg(self, text):
        if hasattr(self, "prog_lbl"):
            self.prog_lbl.setText(text)

    def analyze_last(self, deep=False, log_path=None, video=None):
        if self.worker:
            self.post_local(Advice("an-busy", "status", "after", "An analysis is already running", "The result will appear here when it’s done."))
            return
        if log_path:
            try:
                lg = obs_log.parse(log_path)
            except Exception as e:
                self.post_local(Advice("log-bad", "warning", "after", "Couldn’t read the OBS log", repr(e)))
                self._an_msg("Couldn’t read the OBS log: " + repr(e))
                return
            idx = len(lg.recordings) - 1 if lg.recordings else None
        else:
            log_path, lg, idx = self._latest_recording()
        if not log_path:
            self.post_local(Advice("need-log", "request", "after", "The OBS log for this recording is needed",
                                   "OBS → Help → Log Files → Show Log Files, then pick the newest file.",
                                   actions=[("Browse for log…", "browse_log", None)]))
            self._an_msg("No recordings were found in the recent OBS logs. Use the OBS Reports tab to pick a log.")
            return
        if idx is None:
            self.post_local(Advice("no-rec", "request", "after", "That log has no recordings in it",
                                   "If you restarted OBS, pick the log from the session you recorded in.",
                                   actions=[("Browse for log…", "browse_log", None)]))
            self._an_msg("That OBS log has no recordings in it.")
            return
        o = lg.recordings[idx]
        vid = video or getattr(self, "last_video", None) or o.path
        if not vid or not os.path.isfile(vid):
            self.post_local(Advice("need-video", "request", "after", "Where is the recording file?",
                                   f"Couldn’t open {vid or 'the file'}. If you moved it, click Locate file. The log and PC data are still analyzed without it.",
                                   actions=[("Locate file…", "locate_video", None)]))
            vid = None
        mon = self._match_session(o)
        self.post_local(Advice("analysis-run", "status", "after", "Analyzing the recording…" + (" (deep scan)" if deep else ""),
                               "This reads the log, the PC data and the file. A deep scan of a long recording can take several minutes.", update=True))
        self._live_job = True
        self._start_worker(log_path, {idx: vid} if vid else {}, mon, deep, [idx])

    def _record_history(self, res):
        """Remember each analyzed recording's settings and skip counts, so the next one can be compared."""
        try:
            rec = res.recordings[-1].output
        except Exception:
            return None, None
        e = rec.encoder or {}
        dur = rec.duration_seconds - rec.paused_seconds
        entry = {"when": rec.start.isoformat(), "path": rec.path, "dur": dur,
                 "frames": rec.frames_output or rec.encode_total or 0,
                 "enc_skip": rec.encode_skipped or 0, "render_lag": rec.render_lagged or 0,
                 "settings": {k: str(v) for k, v in e.items() if k in ("codec", "rate_control", "cqp", "cq", "bitrate", "preset", "preset2",
                                                                      "tuning", "multipass", "profile", "width", "height", "lookahead", "encoder")}}
        hist_path = os.path.join(DATA, "history.jsonl")
        prev = None
        try:
            with open(hist_path, "r", encoding="utf-8") as f:
                rows = [json.loads(x) for x in f if x.strip()]
            rows = [x for x in rows if x.get("when") != entry["when"]]
            prev = rows[-1] if rows else None
        except Exception:
            rows = []
        try:
            with open(hist_path, "a", encoding="utf-8") as f:
                if not any(x.get("when") == entry["when"] for x in rows):
                    f.write(json.dumps(entry) + "\n")
        except Exception:
            pass
        return entry, prev

    @staticmethod
    def _compare(cur, prev):
        if not cur or not prev or not prev.get("frames") or not cur.get("frames"):
            return None
        names = {"preset": "Preset", "preset2": "Preset", "rate_control": "Rate control", "cqp": "CQP", "cq": "CQ", "bitrate": "Bitrate",
                 "multipass": "Multipass", "lookahead": "Look-ahead", "profile": "Profile", "codec": "Codec", "tuning": "Tuning"}
        a, b = prev.get("settings", {}), cur.get("settings", {})
        changed = [f"{names.get(k, k)}: {a.get(k, '—')} → {b.get(k, '—')}" for k in names if a.get(k) != b.get(k) and (a.get(k) or b.get(k))]
        w, h = (b.get("width"), b.get("height")), (a.get("width"), a.get("height"))
        if w != h and all(w) and all(h):
            changed.append(f"Resolution: {h[0]}x{h[1]} → {w[0]}x{w[1]}")
        pe = 100 * prev["enc_skip"] / prev["frames"]
        ce = 100 * cur["enc_skip"] / cur["frames"]
        pr = 100 * prev["render_lag"] / max(prev["frames"], 1)
        cr = 100 * cur["render_lag"] / max(cur["frames"], 1)
        lines = [f"Encoding skips: {prev['enc_skip']:,} ({pe:.2f}%) → {cur['enc_skip']:,} ({ce:.2f}%)",
                 f"Rendering lag: {prev['render_lag']:,} ({pr:.2f}%) → {cur['render_lag']:,} ({cr:.2f}%)"]
        if changed:
            if ce + 0.01 < pe:
                verdict = "The change helped: fewer encoding skips than last time."
            elif ce > pe + 0.01:
                verdict = "More encoding skips than last time, so the change didn’t help on its own."
            else:
                verdict = "No real difference in encoding skips." + (" Both recordings were clean." if ce == 0 and pe == 0 else "")
            if cur.get("dur", 0) < 600 or prev.get("dur", 0) < 600:
                verdict += " (One recording was under 10 minutes, so take this with a grain of salt.)"
        else:
            verdict = "Same encoder settings as your previous recording."
        return verdict, (["Changed: " + "; ".join(changed)] if changed else []) + lines

    def _post_analysis(self, res, path, cmp_, prev):
        bad = [f for f in res.findings if f.severity in ("critical", "warning")]
        sev = "critical" if any(f.severity == "critical" for f in bad) else ("warning" if bad else "good")
        steps = [f"{f.title}" + (f" — {f.fix}" if f.fix else "") for f in bad[:6]]
        if cmp_:
            self.post_local(Advice(f"compare-{time.time():.0f}", "status", "after", "Compared with your previous recording: " + cmp_[0],
                                   f"Previous recording: {os.path.basename(prev.get('path') or '') or prev.get('when', '')[:16]}", steps=cmp_[1]))
        self._dismiss("analysis-run")
        self.post_local(Advice("analysis-done", sev, "after",
                               "Report ready — " + (f"{len(bad)} thing(s) to look at" if bad else "no problems found"),
                               "Top findings below. The full report has the timeline with timestamps and the likely cause of each problem.",
                               steps=steps,
                               actions=[("Open report", "open_report", path), ("Copy link", "copy_link", path), ("Run deep scan", "deep_last", None)]))

    # ------------------------------------------------------------------ tick
    def _tick(self):
        while True:
            try:
                a = self._ui_q.get_nowait()
            except queue.Empty:
                break
            if a == "pf-done":
                self.pf_btn.setEnabled(True)
                self.pf_btn.setText("Check OBS settings")
                self.pf_prog.setVisible(False)
            elif a == "pf-clear":
                self._dismiss("pf-progress")
            elif isinstance(a, tuple) and a[0] == "pf-stage":
                self.post_local(Advice("pf-progress", "status", "before", "Checking your OBS settings…", a[1], update=True))
            elif isinstance(a, tuple) and a[0] == "conn":
                self._conn_result(a[1])
            elif isinstance(a, tuple) and a[0] == "upd":
                self._upd_result(a[1], a[2])
            elif isinstance(a, tuple) and a[0] == "setup":
                if getattr(self, "_wizard", None):
                    self._wizard.show_results(a[1])
            else:
                self.post_local(a)
        now = time.time()
        if now - getattr(self, "_conn_t", 0) >= (4 if self.isVisible() else 15):
            self._conn_t = now
            self.check_connection()
        if now - getattr(self, "_self_t", 0) >= 5:
            self._self_t = now
            self._update_self_usage()
        if self.advisor:
            for a in self.advisor.drain():
                self.post_local(a)
            if self.advisor.want_analysis and self.cb_autoanalyze.isChecked():
                log_path, rec = self.advisor.want_analysis
                self.advisor.want_analysis = None
                self.last_video = rec
                QTimer.singleShot(4000, lambda lp=log_path, r=rec: self.analyze_last(log_path=lp or None, video=r or None))
        m = self.monitor
        if not m or not self.isVisible():      # nothing to draw while minimized to the tray
            return
        s = m.latest or {}
        o = s.get("obs") or {}
        el = time.time() - (self.mon_started or time.time())
        if o.get("rec"):
            if o.get("rec_paused"):
                self.mon_title.setText(f"Recording paused — {(o.get('rec_tc') or '')[:8]}")
                self._set_dot("#f5b748")
            else:
                self.mon_title.setText(f"Recording — {(o.get('rec_tc') or '')[:8]}")
                self._set_dot("#ff5d5d")
        else:
            self.mon_title.setText(f"Watching — not recording yet ({analyze.fmt_t(el)})" if not self.advisor or not self.advisor.cards.get("rec-start")
                                   else f"Watching — recording stopped ({analyze.fmt_t(el)})")
            self._set_dot("#3ecf8e")
        self.mon_sub.setText(f"{'Light mode  ·  ' if m.light else ''}OBS {m.obs_status}  ·  {m.counts['samples']:,} samples  ·  watching for {analyze.fmt_t(el)}  ·  you can minimize this window, alerts will pop up")
        if o:
            if o.get("rec"):
                self.t_rec.set("PAUSED" if o.get("rec_paused") else "REC", f"{o.get('rec_mbs', 0) or 0:.0f} MB/s written")
                self.t_rec.v.setStyleSheet("color:#f5b748" if o.get("rec_paused") else "color:#ff5d5d")
            else:
                self.t_rec.set("Idle", "not recording")
            self.t_skip_r.set(f"{m.counts['render_skips']:,}", f"OBS total {o.get('render_skip', 0):,}", bad=m.counts['render_skips'] > 0)
            self.t_skip_e.set(f"{m.counts['encode_skips']:,}", f"OBS total {o.get('enc_skip', 0):,}", bad=m.counts['encode_skips'] > 0)
            self.t_fps.set(f"{o.get('fps', 0):.1f}", f"render {o.get('render_ms', 0):.1f} ms", warn=o.get("render_ms", 0) > 12)
        else:
            for t in (self.t_rec, self.t_skip_r, self.t_skip_e, self.t_fps):
                t.set("—", "OBS not connected")
        g = s.get("gpu_3d")
        self.t_gpu.set(f"{g:.0f}%" if isinstance(g, (int, float)) else "—", ", ".join(n for n, _ in (s.get("gpu_procs") or [])[:1]), warn=(g or 0) >= 90)
        e = s.get("gpu_enc")
        nv = (s.get("nv") or {})
        if nv.get("enc") is not None:
            e = max(e or 0, nv["enc"])
        self.t_enc.set(f"{e:.0f}%" if isinstance(e, (int, float)) else "—", f"GPU {nv.get('temp'):.0f} °C" if nv.get("temp") else "", warn=(e or 0) >= 90)
        c = s.get("cpu")
        self.t_cpu.set(f"{c:.0f}%" if isinstance(c, (int, float)) else "—", f"RAM {s.get('ram', 0):.0f}%", warn=(c or 0) >= 90)
        disks = s.get("disks") or {}
        letter = (self.advisor.drive.get("letter") if self.advisor and self.advisor.drive else "") or self._guess_rec_drive()
        d = disks.get(letter) if letter else None
        if d:
            self.t_disk.set(f"{d.get('busy', 0):.0f}%", f"{letter} {d.get('write_ms', 0):.0f} ms/write",
                            warn=d.get("write_ms", 0) > 100 or d.get("busy", 0) > 90, bad=d.get("write_ms", 0) > 500)
        elif disks:
            k, d = max(disks.items(), key=lambda kv: kv[1].get("busy", 0))
            self.t_disk.set(f"{d.get('busy', 0):.0f}%", f"busiest: {k}")
        self.tray.setToolTip(f"{APP_NAME} — {self.mon_title.text()}")

    def _guess_rec_drive(self):
        if self.parsed_log and self.parsed_log.recordings:
            p = self.parsed_log.recordings[-1].path
            if len(p) > 1 and p[1] == ":":
                return p[0].upper() + ":"
        return ""

    # ------------------------------------------------------------------ settings tab
    def _settings_tab(self):
        w = QWidget()
        root = QVBoxLayout(w)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)
        obs = card()
        ol = QGridLayout(obs)
        ol.setContentsMargins(18, 14, 18, 14)
        ol.addWidget(label("OBS connection", "h2"), 0, 0, 1, 2)
        self.conn_pill = QLabel()
        self.conn_pill.setObjectName("pill")
        set_pill(self.conn_pill, "idle", "Checking…")
        ol.addWidget(self.conn_pill, 0, 2, 1, 3, Qt.AlignLeft | Qt.AlignVCenter)
        cfg = obs_ws_config()
        self.obs_hint = label("", "muted", True)
        ol.addWidget(self.obs_hint, 1, 0, 1, 5)
        ol.addWidget(label("Port"), 2, 0)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(int(self.settings.get("port") or cfg["port"]))
        ol.addWidget(self.port, 2, 1)
        ol.addWidget(label("Password"), 2, 2)
        self.pw = QLineEdit(self.settings.get("password") or cfg["password"])
        self.pw.setEchoMode(QLineEdit.Password)
        self.pw.setPlaceholderText("auto-filled from OBS if found")
        ol.addWidget(self.pw, 2, 3)
        self.port.setToolTip("Use the arrows or type a number. OBS uses 4455 unless you changed it.")
        self.port.setMinimumWidth(110)
        self.test_btn = QPushButton("Check again")
        self.test_btn.setToolTip("Re-reads OBS’s WebSocket settings and tries to connect right now. The status also updates by itself every few seconds.")
        self.test_btn.clicked.connect(self.test_obs)
        ol.addWidget(self.test_btn, 2, 4)
        self.obs_state = label("", "muted", True)
        ol.addWidget(self.obs_state, 3, 0, 1, 5)
        ol.setColumnStretch(3, 1)
        self.obs_hint.setText("Checking whether OBS is open and accepting connections…")
        self.port.valueChanged.connect(lambda _v: self.check_connection(force=True))
        self.pw.editingFinished.connect(lambda: self.check_connection(force=True))
        root.addWidget(obs)

        al = card()
        all_ = QGridLayout(al)
        all_.setContentsMargins(18, 14, 18, 14)
        all_.addWidget(label("Alerts while you play", "h2"), 0, 0, 1, 2)
        all_.addWidget(label("Windows pop-ups"), 1, 0)
        self.toast_combo = QComboBox()
        self.toast_combo.addItems(["Critical problems only", "Warnings and critical (recommended)", "Off"])
        self.toast_combo.setCurrentIndex(int(self.settings.get("toasts", 1)))
        self.toast_combo.currentIndexChanged.connect(self._save_opts)
        all_.addWidget(self.toast_combo, 1, 1)
        self.cb_sound = QCheckBox("Play a sound with warnings")
        self.cb_sound.setChecked(bool(self.settings.get("sound", False)))
        all_.addWidget(self.cb_sound, 2, 0, 1, 2)
        all_.addWidget(label("At most one pop-up every 30 seconds, except critical problems. Tip: turn off Windows “Do not disturb while gaming” (Settings → System → Notifications) so pop-ups appear over games.", "muted", True), 3, 0, 1, 2)
        all_.setColumnStretch(1, 1)
        root.addWidget(al)

        opts = card()
        opl = QVBoxLayout(opts)
        opl.setContentsMargins(18, 12, 18, 12)
        opl.addWidget(label("Behavior", "h2"))
        self.cb_autostart = QCheckBox("Start watching automatically when this app opens")
        self.cb_autostart.setChecked(bool(self.settings.get("autostart")))
        self.cb_tray = QCheckBox("Keep running in the system tray when I close the window while watching")
        self.cb_tray.setChecked(self.settings.get("tray", True))
        self.cb_autoanalyze = QCheckBox("Analyze each recording automatically when it stops (turn off to click Analyze yourself on the Analyzer tab)")
        self.cb_autoanalyze.setChecked(self.settings.get("autoanalyze", True))
        rrow = QHBoxLayout()
        rrow.addWidget(label("Resource use while watching"))
        self.res_combo = QComboBox()
        self.res_combo.addItems(["Normal — checks every second", "Light — checks every 2 seconds, fewer program scans"])
        self.res_combo.setCurrentIndex(int(self.settings.get("light", 0)))
        self.res_combo.currentIndexChanged.connect(self._save_opts)
        rrow.addWidget(self.res_combo)
        rrow.addStretch(1)
        for c in (self.cb_autostart, self.cb_tray, self.cb_autoanalyze, self.cb_sound):
            c.stateChanged.connect(self._save_opts)
        for c in (self.cb_autostart, self.cb_tray, self.cb_autoanalyze):
            opl.addWidget(c)
        opl.addLayout(rrow)
        opl.addWidget(label("The app runs at low priority so the game and OBS always come first. Light mode halves its work; "
                            "use it on older PCs. Changes apply the next time you start watching. Its own CPU and memory use is shown at the bottom of the window.",
                            "muted", True))
        opl.addWidget(label("The app never changes OBS settings by itself. It only recommends changes, and pauses or resumes recording when you click the button.", "muted", True))
        root.addWidget(opts)

        su = card()
        sul = QHBoxLayout(su)
        sul.setContentsMargins(18, 12, 18, 12)
        sc = QVBoxLayout()
        sc.addWidget(label("Guided setup", "h2"))
        sc.addWidget(label("Checks this PC step by step — OBS, the connection, the recording drive, graphics card, HDR and notifications — "
                           "and shows what to change, if anything.", "muted", True))
        self.cb_ask_setup = QCheckBox("Ask about guided setup when the app opens")
        self.cb_ask_setup.setChecked(not self.settings.get("setup_remember", False))
        self.cb_ask_setup.stateChanged.connect(self._save_opts)
        sc.addWidget(self.cb_ask_setup)
        sul.addLayout(sc, 1)
        b = QPushButton("Run guided setup")
        b.clicked.connect(self.open_setup)
        sul.addWidget(b, 0, Qt.AlignVCenter)
        root.addWidget(su)

        up = card()
        upl = QHBoxLayout(up)
        upl.setContentsMargins(18, 12, 18, 12)
        uc = QVBoxLayout()
        uc.addWidget(label("Updates", "h2"))
        self.upd_lbl = label(f"You have version {VERSION}.", "muted", True)
        uc.addWidget(self.upd_lbl)
        self.cb_updates = QCheckBox("Check for updates when the app opens")
        self.cb_updates.setChecked(self.settings.get("check_updates", True))
        self.cb_updates.stateChanged.connect(self._save_opts)
        uc.addWidget(self.cb_updates)
        upl.addLayout(uc, 1)
        self.upd_btn = QPushButton("Check for updates now")
        self.upd_btn.clicked.connect(lambda: self.check_updates(manual=True))
        upl.addWidget(self.upd_btn, 0, Qt.AlignVCenter)
        root.addWidget(up)
        root.addStretch(1)
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.NoFrame)
        sa.setWidget(w)
        return sa

    def _save_opts(self):
        if not hasattr(self, "cb_autoanalyze"):
            return
        self.settings.update({"autostart": self.cb_autostart.isChecked(), "tray": self.cb_tray.isChecked(),
                              "autoanalyze": self.cb_autoanalyze.isChecked(), "port": self.port.value(), "password": self.pw.text(),
                              "toasts": self.toast_combo.currentIndex(), "sound": self.cb_sound.isChecked(),
                              "deep": self.cb_deep.isChecked() if hasattr(self, "cb_deep") else False,
                              "light": self.res_combo.currentIndex() if hasattr(self, "res_combo") else 0})
        if hasattr(self, "cb_ask_setup"):
            self.settings["setup_remember"] = not self.cb_ask_setup.isChecked()
        if hasattr(self, "cb_updates"):
            self.settings["check_updates"] = self.cb_updates.isChecked()
        save_settings(self.settings)

    def test_obs(self):
        self.test_btn.setEnabled(False)
        set_pill(self.conn_pill, "idle", "Checking…")
        self.obs_state.setText("Re-reading OBS’s WebSocket settings and connecting…")
        self._conn_busy = False
        self.check_connection(force=True)

    def _update_self_usage(self):
        try:
            import psutil
            if not hasattr(self, "_proc"):
                self._proc = psutil.Process()
                self._proc.cpu_percent(None)
                return
            cpu = self._proc.cpu_percent(None) / max(psutil.cpu_count() or 1, 1)
            mem = self._proc.memory_info().rss / 1048576
            self.usage_lbl.setText(f"This app: {cpu:.1f}% CPU · {mem:.0f} MB RAM")
        except Exception:
            pass

    # ------------------------------------------------------------------ updates
    def check_updates(self, manual=False):
        if getattr(self, "_upd_busy", False):
            return
        self._upd_busy = True
        if manual:
            self.upd_btn.setEnabled(False)
            self.upd_lbl.setText(f"You have version {VERSION}. Checking for updates…")

        def go():
            try:
                res = updater.check(VERSION)
            except Exception as e:
                res = {"error": str(e) or repr(e)}
            self._ui_q.put(("upd", res, manual))
        threading.Thread(target=go, daemon=True).start()

    def _upd_result(self, res, manual):
        self._upd_busy = False
        self.upd_btn.setEnabled(True)
        stamp = datetime.now().strftime("%I:%M %p").lstrip("0")
        if res.get("error"):
            self.upd_lbl.setText(f"You have version {VERSION}. Couldn’t check for updates ({res['error'][:120]}). Check your internet connection and try again.")
            return
        if not res.get("newer"):
            self.upd_lbl.setText(f"You have version {VERSION}, the latest version (checked at {stamp}).")
            return
        self.upd_lbl.setText(f"You have version {VERSION}. Version {res['version']} is available.")
        if not manual and self.settings.get("skip_version") == res["version"]:
            return
        if getattr(self, "_upd_dlg", None):
            return
        self._upd_dlg = UpdateDialog(self, res)
        self._upd_dlg.finished.connect(lambda _c: setattr(self, "_upd_dlg", None))
        if not self.isVisible() and QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.showMessage("Update available", f"{APP_NAME} {res['version']} is ready to install. Open the app to update.",
                                  QSystemTrayIcon.Information, 8000)
        self._upd_dlg.show()

    # ------------------------------------------------------------------ guided setup
    def maybe_offer_setup(self):
        # Ask every time the app opens, unless "Remember my choice" was ticked.
        if self.settings.get("setup_remember"):
            if self.settings.get("setup_choice") == "yes" and not self.settings.get("setup_done"):
                QTimer.singleShot(300, self.open_setup)
            return
        d = SetupOffer(self)
        yes = d.exec() == QDialog.Accepted
        self.settings["setup_choice"] = "yes" if yes else "no"
        self.settings["setup_remember"] = d.remember.isChecked()
        self.cb_ask_setup.setChecked(not d.remember.isChecked())
        save_settings(self.settings)
        if yes:
            self.open_setup()

    def open_setup(self):
        if getattr(self, "_wizard", None):
            self._wizard.raise_()
            return
        self._wizard = SetupWizard(self)
        self._wizard.finished.connect(self._setup_closed)
        self._wizard.show()

    def _setup_closed(self, _code):
        if self._wizard and self._wizard.completed:
            self.settings["setup_done"] = True
            save_settings(self.settings)
        self._wizard = None

    def run_setup_checks(self):
        port, pw = self.port.value(), self.pw.text()

        def go():
            try:
                res = system_check.run_all(port, pw)
            except Exception as e:
                res = [{"key": "err", "title": "Setup checks", "state": "unknown", "summary": repr(e), "steps": [], "action": None}]
            self._ui_q.put(("setup", res))
        threading.Thread(target=go, daemon=True).start()

    def showEvent(self, e):
        super().showEvent(e)
        if not getattr(self, "_offered", False):
            self._offered = True
            QTimer.singleShot(700, self.maybe_offer_setup)
            if self.settings.get("check_updates", True):
                QTimer.singleShot(4000, self.check_updates)


    # ------------------------------------------------------------------ analyzer tab (latest recording)
    def _analyze_tab(self):
        w = QWidget()
        root = QVBoxLayout(w)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)

        c = card()
        cl = QVBoxLayout(c)
        cl.setContentsMargins(18, 14, 18, 14)
        cl.setSpacing(8)
        hdr = QHBoxLayout()
        hdr.addWidget(label("Your latest recording", "h2"))
        hdr.addStretch(1)
        rb = QPushButton("Refresh")
        rb.setToolTip("Look for a newer recording")
        rb.clicked.connect(self.refresh_latest)
        hdr.addWidget(rb)
        cl.addLayout(hdr)
        self.latest_lbl = label("Looking for your latest recording…", None, True)
        self.latest_lbl.setTextFormat(Qt.RichText)
        cl.addWidget(self.latest_lbl)
        self.latest_note = label("", "muted", True)
        cl.addWidget(self.latest_note)
        row = QHBoxLayout()
        self.an_btn = QPushButton("Analyze my last recording")
        self.an_btn.setObjectName("primary")
        self.an_btn.clicked.connect(lambda: self.analyze_last(deep=self.cb_deep.isChecked()))
        row.addWidget(self.an_btn)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_analysis)
        row.addWidget(self.cancel_btn)
        row.addStretch(1)
        cl.addLayout(row)
        self.cb_deep = QCheckBox("Deep scan: also find repeated and frozen frames (reads every frame; about 5–20 minutes per hour of 4K video)")
        self.cb_deep.setChecked(bool(self.settings.get("deep")))
        self.cb_deep.stateChanged.connect(self._save_opts)
        cl.addWidget(self.cb_deep)
        self.prog = QProgressBar()
        self.prog.setRange(0, 1000)
        self.prog.setValue(0)
        self.prog.setTextVisible(False)
        self.prog.setFixedHeight(8)
        self.prog.setVisible(False)
        cl.addWidget(self.prog)
        self.prog_lbl = label("", "muted", True)
        cl.addWidget(self.prog_lbl)
        root.addWidget(c)

        rc = card()
        rl = QVBoxLayout(rc)
        rl.setContentsMargins(18, 14, 18, 14)
        rl.setSpacing(8)
        rl.addWidget(label("Last report", "h2"))
        self.res_title = label("No report yet. Click “Analyze my last recording” after you record something.", "muted", True)
        rl.addWidget(self.res_title)
        self.summary = QTextBrowser()
        self.summary.setOpenExternalLinks(False)
        self.summary.setMinimumHeight(110)
        self.summary.setVisible(False)
        rl.addWidget(self.summary, 1)
        br = QHBoxLayout()
        self.res_open = QPushButton("Open report")
        self.res_open.setObjectName("primary")
        self.res_open.setToolTip("Opens the report in your web browser")
        self.res_open.clicked.connect(lambda: self.open_report_link(self.settings.get("last_report", {}).get("path")))
        self.res_copy = QPushButton("Copy link")
        self.res_copy.setToolTip("Copies the report link so you can paste it anywhere or send it to someone")
        self.res_copy.clicked.connect(lambda: self.copy_report_link(self.settings.get("last_report", {}).get("path")))
        self.res_local = QPushButton("Open saved copy")
        self.res_local.setToolTip("Opens the copy saved on this PC (works offline)")
        self.res_local.clicked.connect(lambda: self.open_local_report(self.settings.get("last_report", {}).get("path")))
        for b in (self.res_open, self.res_copy, self.res_local):
            b.setVisible(False)
            br.addWidget(b)
        br.addStretch(1)
        rl.addLayout(br)
        self.res_hint = label("The report opens on the Live Recording Analyzer website. The report is stored inside the link itself, "
                              "so nothing is uploaded, and the link works on any device. Folder names are left out.", "muted", True)
        self.res_hint.setVisible(False)
        rl.addWidget(self.res_hint)
        root.addWidget(rc, 1)
        QTimer.singleShot(0, self._restore_last_report)
        return w

    # ---- latest recording helpers
    def _latest_recording(self):
        """Newest recording across the newest OBS logs: (log_path, parsed_log, index) or (None, None, None)."""
        for p in obs_log.latest_logs(4):
            try:
                lg = obs_log.parse(p)
            except Exception:
                continue
            if lg.recordings:
                return p, lg, len(lg.recordings) - 1
        return None, None, None

    def _list_sessions(self):
        out = []
        if os.path.isdir(SESSIONS):
            for d in sorted(os.listdir(SESSIONS), reverse=True)[:60]:
                p = os.path.join(SESSIONS, d, "monitor.jsonl")
                if os.path.isfile(p):
                    rng = self._session_range(p)
                    if rng:
                        out.append((p, rng[0], rng[1]))
        return out

    def _match_session(self, o, sessions=None):
        """The app's own watching data that overlaps this recording, if any."""
        stop = o.stop or datetime.now()
        best, best_ov = None, 0
        for p, a, b in (sessions if sessions is not None else self._list_sessions()):
            ov = (min(b, stop) - max(a, o.start)).total_seconds()
            if ov > best_ov:
                best, best_ov = p, ov
        if self.session_dir and not best:
            p = os.path.join(self.session_dir, "monitor.jsonl")
            if os.path.isfile(p):
                best = p
        return best

    def refresh_latest(self):
        if not hasattr(self, "latest_lbl"):
            return
        log_path, lg, idx = self._latest_recording()
        if not lg:
            self.latest_lbl.setText("No recordings found in the recent OBS logs yet.")
            self.latest_note.setText("Record something in OBS, then come back here. If OBS keeps its logs somewhere unusual, "
                                     "use the OBS Reports tab to pick a log.")
            return
        o = lg.recordings[idx]
        vid = getattr(self, "last_video", None) or o.path
        exists = bool(vid) and os.path.isfile(vid)
        bits = [f"<b>{report.E(os.path.basename(vid or '') or 'Recording')}</b>"]
        bits.append(f"Started {o.start:%b %d, %I:%M %p}")
        if o.stop:
            bits.append("Length " + analyze.fmt_t(o.duration_seconds - (o.paused_seconds or 0)))
        else:
            bits.append("Still recording")
        if exists:
            try:
                bits.append(f"{os.path.getsize(vid) / 1073741824:.2f} GB")
            except OSError:
                pass
        if o.encode_total:
            bits.append(f"OBS encoding skips: {o.encode_skipped:,} ({100 * o.encode_skipped / o.encode_total:.2f}%)")
        self.latest_lbl.setText(" &nbsp;·&nbsp; ".join(bits))
        notes = [f"OBS log: {os.path.basename(log_path)}."]
        if not exists:
            notes.append("The video file wasn’t found (moved or deleted?), so the analysis uses the OBS log and the app’s watching data only.")
        mon = self._match_session(o)
        if mon:
            notes.append("This app was watching during this recording, so the report includes what the PC was doing at each problem.")
        else:
            notes.append("This app wasn’t watching during this recording. It can still check the OBS log and the file; "
                         "click “Start watching” on the Live tab before your next recording to also see what the PC was doing.")
        if not o.stop:
            notes.append("Stop the recording in OBS first for a complete result.")
        self.latest_note.setText(" ".join(notes))

    # ---- last report card
    @staticmethod
    def _link_file(path):
        return (path[:-5] if path and path.endswith(".html") else (path or "")) + ".json"

    def _report_meta(self, path):
        try:
            with open(self._link_file(path), "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError, TypeError):
            return {}

    def _report_link(self, path):
        return self._report_meta(path).get("link")

    def _restore_last_report(self):
        lr = self.settings.get("last_report") or {}
        if lr.get("path") and os.path.isfile(lr["path"]):
            self._show_result(lr)

    def _show_result(self, lr):
        col = {"critical": "#ff5d5d", "warning": "#f5b748", "good": "#3ecf8e"}.get(lr.get("sev"), "#e7e9ee")
        self.res_title.setObjectName("h2")
        self.res_title.setStyleSheet(f"color:{col}")
        self.res_title.setText(lr.get("title", "Report ready"))
        self.summary.setHtml(lr.get("html", ""))
        self.summary.setVisible(True)
        has_link = bool(self._report_link(lr.get("path")))
        self.res_open.setVisible(True)
        self.res_open.setEnabled(True)
        self.res_copy.setVisible(has_link)
        self.res_local.setVisible(True)
        self.res_hint.setVisible(has_link)

    # ---- opening reports
    def open_report_link(self, path):
        url = self._report_link(path)
        if not url:
            return self.open_local_report(path)
        if not open_in_browser(url, REPORTS):
            QApplication.clipboard().setText(url)
            QMessageBox.information(self, APP_NAME, "Couldn’t open your browser, so the report link was copied. Paste it into your browser’s address bar.")

    def copy_report_link(self, path):
        url = self._report_link(path)
        if url:
            QApplication.clipboard().setText(url)
            self.statusBar().showMessage("Report link copied. Paste it into a browser, a message or an email.", 6000)

    def open_local_report(self, path):
        if path and os.path.isfile(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    # ------------------------------------------------------------------ OBS reports tab
    def _reports_tab(self):
        w = QWidget()
        root = QVBoxLayout(w)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)

        rc = card()
        rl = QVBoxLayout(rc)
        rl.setContentsMargins(18, 14, 18, 14)
        rl.addWidget(label("Saved reports", "h2"))
        rl.addWidget(label("Every analysis is kept here until you delete it. Double-click to open.", "muted", True))
        self.rep_list = QListWidget()
        self.rep_list.itemDoubleClicked.connect(lambda it: self.open_report_link(it.data(Qt.UserRole)))
        self.rep_list.setMinimumHeight(110)
        rl.addWidget(self.rep_list, 1)
        row = QHBoxLayout()
        for text, fn in (("Open report", lambda: self.open_report_link(self._sel_report())),
                         ("Copy link", lambda: self.copy_report_link(self._sel_report())),
                         ("Open saved copy", lambda: self.open_local_report(self._sel_report())),
                         ("Reports folder", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(REPORTS))),
                         ("Delete", self._del_report)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        rl.addLayout(row)
        root.addWidget(rc, 1)

        c1 = card()
        g = QGridLayout(c1)
        g.setContentsMargins(18, 14, 18, 14)
        g.addWidget(label("Analyze an older recording from an OBS log", "h2"), 0, 0, 1, 3)
        g.addWidget(label("OBS writes a new log each time it opens. Pick a log, tick the recordings to check, then click Analyze selected. "
                          "Double-click a row to point to a file you moved.", "muted", True), 1, 0, 1, 3)
        self.log_combo = QComboBox()
        self.log_combo.currentIndexChanged.connect(self._log_changed)
        g.addWidget(self.log_combo, 2, 0, 1, 2)
        b = QPushButton("Browse…")
        b.clicked.connect(self.browse_log)
        g.addWidget(b, 2, 2)
        self.rec_tree = QTreeWidget()
        self.rec_tree.setHeaderLabels(["Recording", "Length", "OBS skips", "File"])
        self.rec_tree.setRootIsDecorated(False)
        self.rec_tree.itemDoubleClicked.connect(self.locate_file)
        for k in range(3):
            self.rec_tree.header().setSectionResizeMode(k, QHeaderView.ResizeToContents)
        self.rec_tree.header().setSectionResizeMode(3, QHeaderView.Stretch)
        self.rec_tree.setMinimumHeight(110)
        g.addWidget(self.rec_tree, 3, 0, 1, 3)
        g.addWidget(label("Watching data"), 4, 0)
        self.sess_combo = QComboBox()
        self.sess_combo.setToolTip("What this app recorded about the PC during that time. Picked automatically when one matches.")
        g.addWidget(self.sess_combo, 4, 1, 1, 2)
        row2 = QHBoxLayout()
        self.an_sel_btn = QPushButton("Analyze selected")
        self.an_sel_btn.clicked.connect(self.run_analysis)
        self.vid_btn = QPushButton("Check a video file only…")
        self.vid_btn.clicked.connect(self.analyze_video_only)
        row2.addWidget(self.an_sel_btn)
        row2.addWidget(self.vid_btn)
        row2.addStretch(1)
        g.addLayout(row2, 5, 0, 1, 3)
        g.setColumnStretch(1, 1)
        root.addWidget(c1, 1)
        return w

    def _sel_report(self):
        it = self.rep_list.currentItem()
        return it.data(Qt.UserRole) if it else None

    def refresh_logs(self):
        cur = self.log_combo.currentData()
        self.log_combo.blockSignals(True)
        self.log_combo.clear()
        logs = obs_log.latest_logs(20)
        extra = self.settings.get("extra_logs", [])
        for p in extra:
            if os.path.isfile(p) and p not in logs:
                logs.insert(0, p)
        for p in logs:
            try:
                mt = datetime.fromtimestamp(os.path.getmtime(p))
                self.log_combo.addItem(f"{os.path.basename(p)}   (last written {mt:%b %d, %I:%M %p})", p)
            except OSError:
                pass
        if not logs:
            self.log_combo.addItem("No OBS logs found — use Browse…", None)
        self.log_combo.blockSignals(False)
        if cur:
            i = self.log_combo.findData(cur)
            if i >= 0:
                self.log_combo.setCurrentIndex(i)
        self._log_changed()

    def browse_log(self):
        p, _ = QFileDialog.getOpenFileName(self, "Choose an OBS log", obs_log.default_log_dir() or "", "OBS logs (*.txt *.log);;All files (*)")
        if p:
            self._add_log(p)

    def _add_log(self, p):
        if True:
            extra = self.settings.get("extra_logs", [])
            if p not in extra:
                extra.insert(0, p)
            self.settings["extra_logs"] = extra[:10]
            save_settings(self.settings)
            self.refresh_logs()
            self.log_combo.setCurrentIndex(max(0, self.log_combo.findData(p)))

    def _log_changed(self):
        p = self.log_combo.currentData()
        self.rec_tree.clear()
        self.video_overrides = {}
        self.parsed_log = None
        if not p:
            return
        try:
            self.parsed_log = obs_log.parse(p)
        except Exception as e:
            self.rec_tree.addTopLevelItem(QTreeWidgetItem([f"Could not read log: {e}"]))
            return
        recs = self.parsed_log.recordings
        if not recs:
            it = QTreeWidgetItem(["No recordings in this log", "", "", ""])
            self.rec_tree.addTopLevelItem(it)
        for i, o in enumerate(recs):
            exists = os.path.isfile(o.path)
            skips = "—"
            if o.encode_total:
                skips = f"{o.encode_skipped:,} ({100 * o.encode_skipped / o.encode_total:.2f}%)"
            elif o.stop is None:
                skips = "still recording"
            length = analyze.fmt_t(o.duration_seconds) if o.stop else "…"
            it = QTreeWidgetItem([f"{o.start:%b %d  %I:%M %p}", length, skips, (o.path if exists else f"✖ not found — double-click to locate   ({o.path})")])
            it.setData(0, Qt.UserRole, i)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            recent = o.duration_seconds >= 20 or o.stop is None
            it.setCheckState(0, Qt.Checked if (i >= len(recs) - 3 and recent) else Qt.Unchecked)
            if not exists:
                it.setForeground(3, QColor("#f5b748"))
            self.rec_tree.addTopLevelItem(it)
        self._auto_pick_session()

    def locate_file(self, item, col):
        i = item.data(0, Qt.UserRole)
        if i is None:
            return
        o = self.parsed_log.recordings[i]
        start_dir = os.path.dirname(o.path) if os.path.isdir(os.path.dirname(o.path)) else ""
        p, _ = QFileDialog.getOpenFileName(self, f"Locate recording from {o.start:%b %d %I:%M %p}", start_dir,
                                           "Video (*.mkv *.mp4 *.mov *.flv *.ts *.m4v);;All files (*)")
        if p:
            self.video_overrides[i] = p
            item.setText(3, p)
            item.setForeground(3, QColor("#e7e9ee"))
            item.setCheckState(0, Qt.Checked)

    def refresh_sessions(self, select=None):
        self.sess_combo.clear()
        self.sess_combo.addItem("None — use OBS log and video only", None)
        items = []
        if os.path.isdir(SESSIONS):
            for d in sorted(os.listdir(SESSIONS), reverse=True):
                p = os.path.join(SESSIONS, d, "monitor.jsonl")
                if not os.path.isfile(p):
                    continue
                rng = self._session_range(p)
                if not rng:
                    continue
                a, b = rng
                items.append((p, a, b))
                self.sess_combo.addItem(f"{a:%b %d  %I:%M %p} – {b:%I:%M %p}   ({analyze.fmt_t((b - a).total_seconds())})", p)
        self._sessions = items
        if select:
            i = self.sess_combo.findData(select)
            if i >= 0:
                self.sess_combo.setCurrentIndex(i)
        else:
            self._auto_pick_session()

    def _session_range(self, p):
        try:
            with open(p, "rb") as f:
                first = None
                for line in f:
                    d = json.loads(line)
                    if d.get("ts"):
                        first = datetime.fromisoformat(d["ts"])
                        break
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 65536))
                tail = f.read().decode("utf-8", "replace").strip().splitlines()
            last = None
            for line in reversed(tail):
                try:
                    d = json.loads(line)
                    last = datetime.fromisoformat(d["ts"])
                    break
                except Exception:
                    continue
            return (first, last) if first and last else None
        except Exception:
            return None

    def _auto_pick_session(self):
        if not getattr(self, "_sessions", None) or not self.parsed_log:
            return
        recs = self.parsed_log.recordings
        if not recs:
            return
        for p, a, b in self._sessions:
            for o in recs[-3:]:
                stop = o.stop or datetime.now()
                if a <= stop and b >= o.start:
                    i = self.sess_combo.findData(p)
                    if i >= 0:
                        self.sess_combo.setCurrentIndex(i)
                    return

    def run_analysis(self):
        if self.worker:
            return
        p = self.log_combo.currentData()
        if not p:
            QMessageBox.information(self, APP_NAME, "Choose an OBS log first (or use “Check a video file only…”).")
            return
        sel = []
        for k in range(self.rec_tree.topLevelItemCount()):
            it = self.rec_tree.topLevelItem(k)
            if it.data(0, Qt.UserRole) is not None and it.checkState(0) == Qt.Checked:
                sel.append(it.data(0, Qt.UserRole))
        if self.parsed_log and self.parsed_log.recordings and not sel:
            QMessageBox.information(self, APP_NAME, "Tick at least one recording to analyze.")
            return
        self.tabs.setCurrentIndex(1)
        self._start_worker(p, dict(self.video_overrides), self.sess_combo.currentData(), self.cb_deep.isChecked(), sel)

    def analyze_video_only(self):
        if self.worker:
            return
        p, _ = QFileDialog.getOpenFileName(self, "Choose a recording", "", "Video (*.mkv *.mp4 *.mov *.flv *.ts *.m4v);;All files (*)")
        if p:
            self.tabs.setCurrentIndex(1)
            self._start_worker(None, {0: p}, None, self.cb_deep.isChecked(), None)

    def _start_worker(self, log, vids, mon, deep, sel):
        self.worker = AnalyzeWorker(log, vids, mon, deep, sel)
        if not hasattr(self, "_threads"):
            self._threads = set()
        self._threads.add(self.worker)
        self.worker.finished.connect(lambda w=self.worker: self._threads.discard(w))
        self.worker.progress.connect(self._prog)
        self.worker.done.connect(self._done)
        self.worker.failed.connect(self._failed)
        for b in (self.an_btn, self.an_sel_btn, self.vid_btn):
            b.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.prog.setValue(0)
        self.prog.setVisible(True)
        self.prog_lbl.setText("Starting…")
        self.worker.start()

    def cancel_analysis(self):
        if self.worker:
            self.worker.cancel()
            self.prog_lbl.setText("Cancelling…")

    def _prog(self, msg, p):
        self.prog.setValue(int(p * 1000))
        self.prog_lbl.setText(msg)

    def _finish(self):
        self.worker = None
        for b in (self.an_btn, self.an_sel_btn, self.vid_btn):
            b.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.prog.setVisible(False)

    def _done(self, res, path):
        self._finish()
        cmp_, prev = None, None
        if res.recordings:
            cur, prev = self._record_history(res)
            cmp_ = self._compare(cur, prev)
        bad = [f for f in res.findings if f.severity in ("critical", "warning")]
        sev = "critical" if any(f.severity == "critical" for f in bad) else ("warning" if bad else "good")
        fname = ""
        try:
            r = res.recordings[-1]
            fname = os.path.basename((r.video.path if r.video else "") or (r.output.path if r.output else "") or "")
        except Exception:
            pass
        title = (f"{len(bad)} thing(s) worth fixing" if bad else "No problems found")
        link = None
        try:
            link = weblink.link(weblink.to_data(res, VERSION, cmp_))
        except Exception:
            traceback.print_exc()
        try:
            with open(self._link_file(path), "w", encoding="utf-8") as f:
                json.dump({"link": link, "title": title, "file": fname, "sev": sev}, f)
        except OSError:
            pass
        sev_col = {"critical": "#ff5d5d", "warning": "#f5b748", "info": "#5fa8ff", "good": "#3ecf8e"}
        inc = [i for i in res.incidents if i.kind != "pause"]
        html = f"<p style='color:#9aa3b2'>{report.E(fname or 'Recording')} &nbsp;·&nbsp; analyzed {datetime.now():%b %d, %I:%M %p} &nbsp;·&nbsp; {len(res.findings)} findings, {len(inc)} timeline incidents</p>"
        if cmp_:
            html += f"<p><b>Compared with your previous recording:</b> {report.E(cmp_[0])}</p>"
        for f in res.findings[:12]:
            html += f"<div style='margin:3px 0'><b style='color:{sev_col.get(f.severity)}'>{f.severity.upper()}</b>&nbsp; {report.E(f.title)}</div>"
        lr = {"path": path, "title": "Report ready: " + title, "sev": sev, "html": html}
        self.settings["last_report"] = lr
        save_settings(self.settings)
        self._show_result(lr)
        self.prog_lbl.setText("Done. Click Open report to see the full report with the timeline and the likely cause of each problem.")
        self.refresh_reports()
        self.refresh_latest()
        self._live_job = False
        self._post_analysis(res, path, cmp_, prev)

    def _failed(self, msg):
        self._finish()
        if self._live_job:
            self._live_job = False
            self._dismiss("analysis-run")
            self.post_local(Advice("analysis-fail", "warning", "after", "Analysis stopped", msg.strip().splitlines()[-1][:300],
                                   actions=[("Try again", "analyze_last", None)]))
        last = msg.strip().splitlines()[-1][:300] if msg.strip() else "Unknown error"
        self.prog_lbl.setText("Analysis stopped. " + ("" if last == "Cancelled." else last) + " The previous report is still available below.")

    def refresh_reports(self):
        self.rep_list.clear()
        if not os.path.isdir(REPORTS):
            return
        files = sorted((f for f in os.listdir(REPORTS) if f.endswith(".html") and not f.startswith("_")), reverse=True)
        for f in files:
            p = os.path.join(REPORTS, f)
            try:
                when = datetime.fromtimestamp(os.path.getmtime(p)).strftime("%b %d, %Y  %I:%M %p")
            except OSError:
                when = ""
            meta = self._report_meta(p)
            title = " — ".join(x for x in (meta.get("file"), meta.get("title")) if x) or "Older report"
            it = QListWidgetItem(f"{when}    {title}" + ("" if meta.get("link") else "    (saved copy only)"))
            it.setData(Qt.UserRole, p)
            self.rep_list.addItem(it)

    def _del_report(self):
        it = self.rep_list.currentItem()
        if it and QMessageBox.question(self, APP_NAME, f"Delete “{it.text()}”?") == QMessageBox.Yes:
            p = it.data(Qt.UserRole)
            for f in (p, self._link_file(p)):
                try:
                    os.remove(f)
                except OSError:
                    pass
            if (self.settings.get("last_report") or {}).get("path") == p:
                self.settings.pop("last_report", None)
                save_settings(self.settings)
                self.res_title.setStyleSheet("")
                self.res_title.setText("No report yet. Click “Analyze my last recording” after you record something.")
                for b in (self.summary, self.res_open, self.res_copy, self.res_local, self.res_hint):
                    b.setVisible(False)
            self.refresh_reports()

    # ------------------------------------------------------------------ help tab
    def _help_tab(self):
        w = QTextBrowser()
        w.setOpenExternalLinks(True)
        w.setHtml(f"""
<div style='padding:14px;line-height:150%'>
<h2>How to use</h2>
<h3>First time</h3>
<p>When the app opens it asks whether you want a <b>guided setup</b>. Choose <b>Yes, guide me</b> to have it check this PC step by step, or
<b>No, I’ll set it up myself</b>. Tick <b>Remember my choice</b> to stop the question. You can run it any time from <b>Settings → Run guided setup</b>.</p>
<h3>Before you record</h3>
<ol>
<li>Open OBS, then this app. Click <b>Check OBS settings</b>. A moving bar and a “Checking your OBS settings…” message show each step while it reads your OBS profile, scenes and sources, the latest OBS log and the recording drive.
Then the Advisor lists only what would cause a problem in your next recording — for example a frame-rate mismatch between a capture device and OBS, an HDR/SDR conflict,
a missing or nearly full recording drive, or encoder settings that already caused skips. Each message gives the exact steps for your OBS setup. Lower-priority items are marked as tips.</li>
<li>Click <b>Start watching</b>, minimize the window, and play.</li>
</ol>
<h3>While you record</h3>
<p>The Advisor checks OBS (skipped frames, FPS, render time), the OBS log, the graphics card, CPU, memory, the recording drive, and whether the file is still growing.
About 25 seconds in, it opens the file to confirm the resolution, frame rate, color settings and audio match what you set.
When something goes wrong it posts a message with the time, the position in the video, the likely cause, and what to do. You also get a Windows pop-up, so you don't have to look at the app.
Some messages have a <b>Pause recording</b> button. Use it, fix the setting, then click <b>Resume</b>.</p>
<h3>After you record</h3>
<p>The <b>Analyzer</b> tab always shows your latest recording. If automatic analysis is on (Settings → Behavior, or the last step of the guided setup), the app analyzes it
about a minute after you stop recording and shows a Windows pop-up. Otherwise click <b>Analyze my last recording</b>. Nothing opens by itself.</p>
<p>The result stays on the Analyzer tab, even after you close and reopen the app, until you analyze again. Click <b>Open report</b> to see the full report in your browser:
a timeline with timestamps, what the PC was doing, and the likely cause of each problem. The report is stored inside the link itself, so nothing is uploaded to a server,
and <b>Copy link</b> lets you open it on your phone or send it to someone. Folder names are left out. <b>Open saved copy</b> opens the copy saved on this PC (works offline).</p>
<p>The <b>OBS Reports</b> tab lists every saved report and lets you analyze older recordings from any OBS log.
If the app can't find the file or log, it asks for it with a <b>Locate file</b> or <b>Browse for log</b> button.</p>
<h3>Updates</h3>
<p>The app checks for a new version when it opens (you can turn this off in Settings → Updates, or click <b>Check for updates now</b>). If one is available, it shows what's new and asks first.
Click <b>Update now</b> and it downloads the installer, closes, updates and opens again by itself. Your settings and reports are kept.</p>
<h3>Connecting to OBS (one time)</h3>
<p>In OBS open <b>Tools → WebSocket Server Settings</b>, tick <b>Enable WebSocket server</b> and click <b>OK</b>. The status pill on the Live tab and in Settings turns green
(“Connected to OBS”) within a few seconds — you don't need to restart this app. The password is read from OBS automatically. If the pill says
<b>WebSocket off</b>, <b>Password rejected</b> or <b>port mismatch</b>, Settings shows exactly what to do; <b>Check again</b> retries right away.
Without the connection, the app still reads the log, the PC data and the file, but it can't see skips the second they happen or pause recording for you.</p>
<h3>Light on your PC</h3>
<p>The app and the tools it starts run at low priority, so your game and OBS always come first. In <b>Settings → Resource use</b>, <b>Light</b> mode checks every 2 seconds and scans programs less often — use it on older PCs.
The app's own CPU and memory use is shown at the bottom right of the window. While it's minimized to the tray it stops drawing the dashboard.</p>
<p style='color:#9aa3b2'>The app never changes OBS settings by itself. Likely causes are educated guesses from what the PC was doing at that moment.</p>
<h3>What the report checks</h3>
<ul>
<li>Rendering-lag and encoding-lag skips, with clock time <i>and</i> position in the video</li>
<li>Missing frames (timestamp gaps) and variable frame timing in the file</li>
<li>Deep scan: repeated and frozen frames, including the regular stutter caused by frame-rate mismatches (for example 59.94 vs 60)</li>
<li>Audio gaps, audio/video drift, and audio that arrives late (with the sample rate, when it’s part of the cause)</li>
<li>Resolution, frame rate, color format, color space, color range and bit depth — OBS settings vs. what the file actually contains</li>
<li>Capture-device settings, HDR/SDR conflicts, encoder settings that are too heavy</li>
<li>What the PC was doing at each problem: GPU 3D and encoder load, CPU, RAM, recording drive busy/latency, GPU temperature, busiest programs</li>
<li>Windows events: GPU driver resets, disk/USB errors, crashes</li>
</ul>
<p style='color:#9aa3b2'>Data folder: {DATA}<br>Version {VERSION}</p>
</div>""")
        return w

    # ------------------------------------------------------------------ tray / close
    def _toast_clicked(self):
        """Clicking a pop-up brings the app back; for a finished report, show the Analyzer tab."""
        self.showNormal()
        self.raise_()
        self.activateWindow()
        if getattr(self, "_last_toast_key", "") == "analysis-done":
            self.tabs.setCurrentIndex(1)

    def _setup_tray(self):
        self.tray = QSystemTrayIcon(QIcon(resource("icon.ico")), self)
        self.tray.setToolTip(APP_NAME)
        menu = QMenu()
        a_show = QAction("Show window", self)
        a_show.triggered.connect(self.showNormal)
        self.tray_stop = QAction("Stop watching", self)
        self.tray_stop.triggered.connect(self.stop_monitor)
        self.tray_stop.setEnabled(False)
        a_quit = QAction("Quit", self)
        a_quit.triggered.connect(self.quit_app)
        menu.addAction(a_show)
        menu.addAction(self.tray_stop)
        menu.addSeparator()
        menu.addAction(a_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda r: self.showNormal() if r in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) else None)
        self.tray.messageClicked.connect(self._toast_clicked)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    def quit_app(self):
        self.quitting = True
        if self.advisor:
            self.advisor.stop()
        if self.monitor:
            self.monitor.stop()
            self.monitor = None
        QApplication.quit()

    def closeEvent(self, e):
        if self.monitor and not self.quitting:
            if self.cb_tray.isChecked() and QSystemTrayIcon.isSystemTrayAvailable():
                self.hide()
                self.tray.showMessage(APP_NAME, "Still watching in the background. Alerts will pop up here. Right-click the tray icon to stop.", QSystemTrayIcon.Information, 4000)
                e.ignore()
                return
            if QMessageBox.question(self, APP_NAME, "Watching is running. Stop it and quit?") != QMessageBox.Yes:
                e.ignore()
                return
            if self.advisor:
                self.advisor.stop()
            self.monitor.stop()
            self.monitor = None
        if self.worker:
            self.worker.cancel()
            self.worker.wait(3000)
        self.tray.hide()
        e.accept()
        QApplication.quit()


def selftest(out):
    """Used by the build pipeline: import everything, build the window offscreen, probe a file."""
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    lines = []
    try:
        from analyzer import video_probe, monitor, winevents, obs_config, live  # noqa: F401
        lines.append("ffprobe: " + str(video_probe._tool("ffprobe")))
        lines.append("ffmpeg: " + str(video_probe._tool("ffmpeg")))
        app = QApplication(sys.argv)
        app.setStyleSheet(app_style())
        w = Main()
        w.run_preflight()
        system_check.run_all()
        m = Monitor(os.path.join(SESSIONS, "_selftest"), interval=1.0)
        m.start()
        time.sleep(3)
        m.stop()
        lines.append(f"monitor samples: {m.counts['samples']}")
        lines.append(f"pdh: {bool((m.latest or {}).get('disks'))}")
        lines.append("OK")
        code = 0
    except Exception:
        lines.append(traceback.format_exc())
        code = 1
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    os._exit(code)


def install_crash_log():
    """Write any error to Documents\\Live Recording Analyzer\\crash.log instead of closing silently."""
    import faulthandler
    try:
        os.makedirs(DATA, exist_ok=True)
        f = open(CRASH_LOG, "a", encoding="utf-8")
        f.write(f"\n===== {APP_NAME} {VERSION} started {datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
        f.flush()
        faulthandler.enable(f)            # hard crashes (native) end up here too
    except Exception:
        return

    def hook(et, ev, tb):
        try:
            f.write(f"[{datetime.now():%H:%M:%S}] " + "".join(traceback.format_exception(et, ev, tb)))
            f.flush()
        except Exception:
            pass
    sys.excepthook = hook                  # keep running after an error in the window code
    threading.excepthook = lambda a: hook(a.exc_type, a.exc_value, a.exc_traceback)
    install_crash_log.file = f


def main():
    if "--selftest" in sys.argv:
        selftest(sys.argv[sys.argv.index("--selftest") + 1])
    crashed_last_time = os.path.exists(RUN_FLAG) and "--updated" not in sys.argv
    install_crash_log()
    try:
        open(RUN_FLAG, "w").close()
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("bloodrive.liverecordinganalyzer")
        except Exception:
            pass
    try:   # stay out of the game's and OBS's way
        import psutil
        if sys.platform == "win32":
            psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:
        pass
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor("#0f1115"))
    pal.setColor(QPalette.WindowText, QColor("#e7e9ee"))
    pal.setColor(QPalette.Base, QColor("#171a21"))
    pal.setColor(QPalette.Text, QColor("#e7e9ee"))
    pal.setColor(QPalette.Button, QColor("#232834"))
    pal.setColor(QPalette.ButtonText, QColor("#e7e9ee"))
    pal.setColor(QPalette.Highlight, QColor("#c23b3b"))
    app.setPalette(pal)
    app.setStyleSheet(app_style())
    app.setQuitOnLastWindowClosed(False)
    w = Main()
    if crashed_last_time:
        w.post_local(Advice("crashed", "warning", "before", "The app closed unexpectedly last time",
                            "Sorry about that. Details were saved to a crash log. If it happens again, please attach this file when you report it — "
                            "it shows exactly what went wrong.",
                            steps=[CRASH_LOG],
                            actions=[("Open crash log", "open_path", CRASH_LOG), ("Open data folder", "open_path", DATA)]))
    if "--updated" in sys.argv:
        w.post_local(Advice("updated", "good", "before", f"Updated to version {VERSION}",
                            "Your settings, reports and watching data were kept. See what’s new on the Releases page.",
                            actions=[("What’s new", "open_url", "https://bloodrive65.github.io/live-recording-analyzer/releases.html")]))
    if "--minimized" in sys.argv and QSystemTrayIcon.isSystemTrayAvailable():
        w.tray.showMessage(APP_NAME, "Running in the tray. Click the icon to open.", QSystemTrayIcon.Information, 3000)
    else:
        w.show()
    code = app.exec()
    try:
        os.remove(RUN_FLAG)               # clean exit
    except Exception:
        pass
    sys.exit(code)


if __name__ == "__main__":
    main()
