import sys
import time
import json
import os
import subprocess
import platform

from PyQt5.QtWidgets import (
    QApplication, QSystemTrayIcon, QMenu, QAction,
    QMessageBox, QWidget, QVBoxLayout, QPushButton,
    QTableWidget, QTableWidgetItem, QMainWindow, QMenuBar,
    QFileDialog, QDialog, QFormLayout, QDialogButtonBox,
    QHeaderView, QLineEdit, QAbstractItemView, QInputDialog, QLabel
)
from PyQt5.QtGui import QIcon, QColor, QPixmap, QPainter
from PyQt5.QtCore import QTimer, Qt, QThread, pyqtSignal, QSettings

# === Draggable Table Widget ===
class DraggableTableWidget(QTableWidget):
    rowDropped = pyqtSignal(int, int)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._start_row = None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            idx = self.indexAt(event.pos())
            self._start_row = idx.row()
        super().mousePressEvent(event)

    def dropEvent(self, event):
        super().dropEvent(event)
        if self._start_row is None:
            return
        idx = self.indexAt(event.pos())
        end_row = idx.row() if idx.isValid() else self.rowCount() - 1
        self.rowDropped.emit(self._start_row, end_row)
        self._start_row = None


# === Settings ===
CHECK_INTERVAL = 3
FAILURE_THRESHOLD = 5  # how many consecutive failed pings count as down (default)
APP_VERSION = "1.0"
GITHUB_URL = "https://github.com/rykovgeka/PingMonitor"
if getattr(sys, 'frozen', False):
    RESOURCE_DIR = sys._MEIPASS
    DATA_DIR     = os.path.dirname(sys.executable)
else:
    RESOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR     = RESOURCE_DIR

# Templates (read-only, bundled with the package)
TEMPLATE_HOSTS   = os.path.join(RESOURCE_DIR, "hosts.json")
TEMPLATE_SETTINGS= os.path.join(RESOURCE_DIR, "settings.ini")

# Files we actually read from / write to at runtime
HOSTS_FILE    = os.path.join(DATA_DIR, "hosts.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.ini")


def create_icon(color):
    pixmap = QPixmap(16, 16)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setBrush(QColor(color))
    painter.setPen(QColor('black'))
    painter.drawEllipse(2, 2, 12, 12)
    painter.end()
    return QIcon(pixmap)


class EditHostDialog(QDialog):
    def __init__(self, host=None, parent=None):
        super().__init__(parent)
        self.host = host
        self.setWindowTitle("Edit Host" if host else "Add Host")
        self.setModal(True)
        self.setStyleSheet("""
            QDialog { background-color: #1a2430; color: #ffffff; }
            QLabel { color: #cccccc; }
            QLineEdit {
                background-color: #2a3440;
                color: #ffffff;
                border: 1px solid #3a4450;
                padding: 5px;
                border-radius: 3px;
            }
            QPushButton {
                background-color: #2a3b4b;
                color: #ffffff;
                border: none;
                padding: 8px 16px;
                border-radius: 3px;
            }
            QPushButton:hover { background-color: #3a4b5b; }
            QPushButton:pressed { background-color: #1a6aa6; }
        """)
        layout = QFormLayout()
        layout.setSpacing(10)
        layout.setContentsMargins(15, 15, 15, 15)

        self.name_edit = QLineEdit()
        self.address_edit = QLineEdit()
        self.interval_edit = QLineEdit(str(CHECK_INTERVAL))

        layout.addRow("Name:", self.name_edit)
        layout.addRow("Address:", self.address_edit)
        layout.addRow("Interval (sec):", self.interval_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

        self.setLayout(layout)
        if host:
            self.name_edit.setText(host.name)
            self.address_edit.setText(host.address)
            self.interval_edit.setText(str(host.interval))

    def get_data(self):
        # Guard against a non-numeric interval so the dialog doesn't crash
        try:
            interval = int(self.interval_edit.text())
            if interval < 1:
                interval = CHECK_INTERVAL
        except ValueError:
            interval = CHECK_INTERVAL
        return {
            'name': self.name_edit.text().strip(),
            'address': self.address_edit.text().strip(),
            'interval': interval
        }


class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About Ping Monitor")
        self.setModal(True)
        self.setFixedWidth(380)
        self.setStyleSheet("""
            QDialog { background-color: #1a2430; }
            QLabel { color: #cccccc; }
            QPushButton {
                background-color: #2a3b4b;
                color: #ffffff;
                border: none;
                padding: 8px 16px;
                border-radius: 3px;
            }
            QPushButton:hover { background-color: #3a4b5b; }
            QPushButton:pressed { background-color: #1a6aa6; }
        """)

        layout = QVBoxLayout()
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(8)

        title = QLabel("Ping Monitor")
        title.setStyleSheet("color: #ffffff; font-size: 18px; font-weight: bold;")
        layout.addWidget(title)

        version = QLabel(f"Version {APP_VERSION}")
        version.setStyleSheet("color: #8aa0b4;")
        layout.addWidget(version)

        desc = QLabel(
            "Lightweight host availability monitor.\n"
            "Pings your hosts and warns from the system tray when one goes down."
        )
        desc.setWordWrap(True)
        layout.addWidget(desc)

        layout.addSpacing(6)

        link = QLabel(f'GitHub: <a href="{GITHUB_URL}" style="color:#4aa3ff;">{GITHUB_URL}</a>')
        link.setOpenExternalLinks(True)
        link.setTextInteractionFlags(Qt.TextBrowserInteraction)
        link.setWordWrap(True)
        layout.addWidget(link)

        meta = QLabel("License: MIT  ·  Built with Python & PyQt5")
        meta.setStyleSheet("color: #8aa0b4;")
        layout.addWidget(meta)

        layout.addSpacing(12)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

        self.setLayout(layout)


class PingWorker(QThread):
    result_ready = pyqtSignal(object, bool)

    def __init__(self, host, address):
        super().__init__()
        self.host = host
        self.address = address
        self._proc = None
        self._abort = False

    def run(self):
        is_alive = False
        try:
            system = platform.system().lower()
            if system == 'windows':
                # -n 1 = one packet, -w 2000 = reply timeout in ms
                cmd = ['ping', '-n', '1', '-w', '2000', self.address]
                creationflags = subprocess.CREATE_NO_WINDOW
            else:
                # On Linux/macOS -W is the reply timeout in seconds (not -w!)
                cmd = ['ping', '-c', '1', '-W', '2', self.address]
                creationflags = 0

            # Use Popen so that on exit we can kill the ping process
            # itself instead of forcibly terminating the thread.
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creationflags
            )

            try:
                out, err = self._proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                out, err = self._proc.communicate()

            output = (out or b'').decode('utf-8', errors='ignore').lower()
            output += (err or b'').decode('utf-8', errors='ignore').lower()

            # IMPORTANT: on Windows, ping returns code 0 even for a
            # "Destination host unreachable" reply from an intermediate router,
            # so the return code alone is not enough (the host would flicker).
            # A real echo reply ALWAYS contains "TTL=" (in any Windows locale,
            # and "ttl=" on Linux), while unreachable / timed-out replies don't.
            is_alive = (self._proc.returncode == 0 and 'ttl=' in output)

        except Exception:
            is_alive = False

        if not self._abort:
            self.result_ready.emit(self.host, is_alive)

    def stop(self):
        """Abort the check and kill the ping process. Called on exit."""
        self._abort = True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass


class Host:
    def __init__(self, name, address, interval=CHECK_INTERVAL):
        self.name = name
        self.address = address
        self.interval = interval
        self.last_check = 0
        self.alive = True
        self.notification_sent = False
        self.being_checked = False
        self.failure_count = 0  # consecutive ping failures

    def to_dict(self):
        return {'name': self.name, 'address': self.address, 'interval': self.interval}

    @classmethod
    def from_dict(cls, data):
        return cls(data['name'], data['address'], data.get('interval', CHECK_INTERVAL))


class PingMonitor(QMainWindow):
    def __init__(self):
        super().__init__()
        self.hosts = []
        self.notifications_enabled = True
        self.start_minimized = False
        self.failure_threshold = FAILURE_THRESHOLD
        self.ping_workers = []
        self._cleaned_up = False
        self.GREEN_ICON = create_icon('green')
        self.RED_ICON = create_icon('red')
        self.setWindowTitle("Ping Monitor")
        self.resize(800, 500)

        self.settings = QSettings(SETTINGS_FILE, QSettings.IniFormat)
        self.load_settings()

        icon_path = os.path.join(RESOURCE_DIR, "PingMonitor.ico")
        self.setWindowIcon(QIcon(icon_path))

        # Restore saved window geometry (size & position)
        if self.settings.contains("geometry"):
            self.restoreGeometry(self.settings.value("geometry"))

        self.setup_ui()
        self.setup_style()
        self.load_hosts()
        self.setup_tray()
        self.setup_timer()

        if self.start_minimized:
            self.hide()

    def setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.create_menu()
        self.create_hosts_table()
        layout.addWidget(self.hosts_table)
        self.add_btn = QPushButton("Add Host")
        self.add_btn.clicked.connect(self.add_host)
        self.add_btn.setMinimumHeight(35)
        layout.addWidget(self.add_btn)

    def setup_style(self):
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #1a2430;
                color: #ffffff;
                border: none;
            }
            QTableWidget {
                background-color: #141e28;
                gridline-color: #2a3b4b;
                border: none;
                alternate-background-color: #16202a;
            }
            QTableWidget::item {
                padding: 8px;
                border-bottom: 1px solid #2a3b4b;
            }
            QTableWidget::item:selected {
                background-color: #4169e1;
                color: #ffffff;
            }
            QHeaderView::section {
                background-color: #1e2a36;
                color: #cccccc;
                padding: 8px;
                border: none;
                border-bottom: 2px solid #2a3b4b;
                font-weight: bold;
            }
            QPushButton {
                background-color: #2a3b4b;
                color: #ffffff;
                border: none;
                padding: 8px;
                font-weight: bold;
                margin: 5px;
            }
            QPushButton:hover { background-color: #3a4b5b; }
            QPushButton:pressed { background-color: #1a6aa6; }
            QMenuBar {
                background-color: #2a3b4b;
                color: #cccccc;
                border: none;
            }
            QMenuBar::item {
                background-color: transparent;
                padding: 8px 12px;
                border-radius: 3px;
            }
            QMenuBar::item:selected, QMenuBar::item:hover {
                background-color: #3a4b5b;
                color: #ffffff;
            }
            QMenu {
                background-color: #2a3b4b;
                color: #cccccc;
                border: 1px solid #3a4b5b;
            }
            QMenu::item {
                padding: 8px 20px;
            }
            QMenu::item:selected {
                background-color: #4169e1;
                color: #ffffff;
            }
        """)

    def create_menu(self):
        menubar = QMenuBar(self)
        self.setMenuBar(menubar)
        file_menu = menubar.addMenu('File')
        import_action = QAction('Import', self)
        import_action.triggered.connect(self.import_hosts)
        file_menu.addAction(import_action)
        export_action = QAction('Export', self)
        export_action.triggered.connect(self.export_hosts)
        file_menu.addAction(export_action)
        file_menu.addSeparator()
        exit_action = QAction('Exit', self)
        exit_action.triggered.connect(self.quit_application)
        file_menu.addAction(exit_action)

        settings_menu = menubar.addMenu('Settings')
        toggle_notify = QAction('Notifications', self, checkable=True, checked=self.notifications_enabled)
        toggle_notify.triggered.connect(self.toggle_notifications)
        settings_menu.addAction(toggle_notify)
        start_min = QAction('Start minimized', self, checkable=True, checked=self.start_minimized)
        start_min.triggered.connect(self.toggle_start_minimized)
        settings_menu.addAction(start_min)
        settings_menu.addSeparator()
        threshold_action = QAction('Failures before dead...', self)
        threshold_action.triggered.connect(self.set_failure_threshold)
        settings_menu.addAction(threshold_action)

        # Small About button in the top-right corner of the menu bar
        about_btn = QPushButton("About")
        about_btn.setCursor(Qt.PointingHandCursor)
        about_btn.setFlat(True)
        about_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                color: #cccccc;
                border: none;
                padding: 8px 14px;
                margin: 0;
                font-weight: normal;
            }
            QPushButton:hover { color: #ffffff; }
        """)
        about_btn.clicked.connect(self.show_about)
        menubar.setCornerWidget(about_btn, Qt.TopRightCorner)

    def create_hosts_table(self):
        self.hosts_table = DraggableTableWidget()
        self.hosts_table.setColumnCount(4)
        self.hosts_table.setHorizontalHeaderLabels(["Host", "Address", "Status", "Interval"])
        header = self.hosts_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)

        self.hosts_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.hosts_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.hosts_table.setAlternatingRowColors(True)
        self.hosts_table.verticalHeader().setVisible(False)

        # Drag & Drop
        self.hosts_table.setDragEnabled(True)
        self.hosts_table.setAcceptDrops(True)
        self.hosts_table.setDropIndicatorShown(True)
        self.hosts_table.setDragDropMode(QAbstractItemView.InternalMove)

        self.hosts_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.hosts_table.customContextMenuRequested.connect(self.show_host_context_menu)
        self.hosts_table.doubleClicked.connect(self.edit_host)
        self.hosts_table.rowDropped.connect(self.on_rows_reordered)

    def on_rows_reordered(self, src, dst):
        if src == dst:
            return
        host = self.hosts.pop(src)
        if dst > src:
            dst -= 1
        self.hosts.insert(dst, host)
        self.save_hosts()
        self.update_hosts_table()

    def setup_tray(self):
        self.tray = QSystemTrayIcon(self.GREEN_ICON, self)
        self.tray.setToolTip("Ping Monitor")
        tray_menu = QMenu()
        show_action = QAction("Show", self)
        show_action.triggered.connect(self.show_window)
        tray_menu.addAction(show_action)
        toggle_notify = QAction("Notifications", self, checkable=True, checked=self.notifications_enabled)
        toggle_notify.triggered.connect(self.toggle_notifications)
        tray_menu.addAction(toggle_notify)
        tray_menu.addSeparator()
        quit_action = QAction("Exit", self)
        quit_action.triggered.connect(self.quit_application)
        tray_menu.addAction(quit_action)
        self.tray.setContextMenu(tray_menu)
        self.tray.activated.connect(self.tray_activated)
        self.tray.show()

    def setup_timer(self):
        self.timer = QTimer()
        self.timer.timeout.connect(self.check_hosts)
        self.timer.start(1000)

    def load_settings(self):
        self.notifications_enabled = self.settings.value("notifications", True, type=bool)
        self.start_minimized = self.settings.value("start_minimized", False, type=bool)
        threshold = self.settings.value("failure_threshold", FAILURE_THRESHOLD, type=int)
        # Guard against junk in the ini: the threshold must be at least 1
        self.failure_threshold = threshold if threshold >= 1 else FAILURE_THRESHOLD

    def save_settings(self):
        self.settings.setValue("notifications", self.notifications_enabled)
        self.settings.setValue("start_minimized", self.start_minimized)
        self.settings.setValue("failure_threshold", self.failure_threshold)
        # Persist window geometry (size & position)
        self.settings.setValue("geometry", self.saveGeometry())

    def cleanup(self):
        # Runs exactly once, no matter how the exit was triggered:
        # the menu item, the window's X button, or a Windows session end
        # (shutdown / restart) via app.aboutToQuit.
        if self._cleaned_up:
            return
        self._cleaned_up = True

        # Stop scheduling new pings
        try:
            self.timer.stop()
        except Exception:
            pass

        self.save_settings()
        self.save_hosts()

        # Kill the ping processes first so the threads finish quickly,
        # then wait for them. We do NOT use QThread.terminate() — it is unsafe
        # and was the cause of errors/crashes on Windows restart.
        for worker in list(self.ping_workers):
            try:
                worker.stop()
            except Exception:
                pass
        for worker in list(self.ping_workers):
            try:
                if worker.isRunning():
                    worker.wait(2000)  # wait at most 2 seconds
            except Exception:
                pass
        self.ping_workers = []

        try:
            self.tray.hide()
        except Exception:
            pass

    def quit_application(self):
        self.cleanup()
        QApplication.quit()

    def tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.show_window()

    def show_window(self):
        self.show()
        self.raise_()
        self.activateWindow()
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized | Qt.WindowActive)

    def closeEvent(self, event):
        # Clicking "X" will fully quit the application
        self.quit_application()

    def changeEvent(self, event):
        if event.type() == event.WindowStateChange and self.windowState() & Qt.WindowMinimized:
            self.hide()
        super().changeEvent(event)

    def toggle_notifications(self, state):
        self.notifications_enabled = state
        self.save_settings()

    def toggle_start_minimized(self, state):
        self.start_minimized = state
        self.save_settings()

    def set_failure_threshold(self):
        dlg = QInputDialog(self)
        # Dark theme so the dialog matches the rest of the app
        dlg.setStyleSheet("""
            QDialog { background-color: #1a2430; color: #ffffff; }
            QLabel { color: #cccccc; }
            QSpinBox {
                background-color: #2a3440;
                color: #ffffff;
                border: 1px solid #3a4450;
                padding: 5px;
                border-radius: 3px;
            }
            QPushButton {
                background-color: #2a3b4b;
                color: #ffffff;
                border: none;
                padding: 8px 16px;
                border-radius: 3px;
            }
            QPushButton:hover { background-color: #3a4b5b; }
            QPushButton:pressed { background-color: #1a6aa6; }
        """)
        dlg.setWindowTitle("Failures before dead")
        dlg.setLabelText("Mark host dead after N failed pings in a row:")
        dlg.setInputMode(QInputDialog.IntInput)
        dlg.setIntRange(1, 100)
        dlg.setIntValue(self.failure_threshold)
        if dlg.exec_() == QDialog.Accepted:
            self.failure_threshold = dlg.intValue()
            self.save_settings()

    def show_about(self):
        AboutDialog(self).exec_()

    def show_host_context_menu(self, position):
        item = self.hosts_table.itemAt(position)
        if not item:
            return
        row = item.row()
        menu = QMenu()
        edit_action = QAction("Edit", self)
        edit_action.triggered.connect(lambda: self.edit_host_by_index(row))
        menu.addAction(edit_action)
        delete_action = QAction("Delete", self)
        delete_action.triggered.connect(lambda: self.delete_host(row))
        menu.addAction(delete_action)
        menu.exec_(self.hosts_table.mapToGlobal(position))

    def edit_host_by_index(self, index):
        if 0 <= index < len(self.hosts):
            self.edit_host_dialog(self.hosts[index], index)

    def edit_host(self, item):
        self.edit_host_by_index(item.row())

    def edit_host_dialog(self, host, index):
        dialog = EditHostDialog(host, self)
        if dialog.exec_() == QDialog.Accepted:
            data = dialog.get_data()
            if data['name'] and data['address']:
                host.name, host.address, host.interval = data['name'], data['address'], data['interval']
                self.update_hosts_table()
                self.save_hosts()

    def delete_host(self, row):
        if 0 <= row < len(self.hosts):
            reply = QMessageBox.question(
                self, 'Confirmation',
                f'Delete host "{self.hosts[row].name}"?',
                QMessageBox.Yes | QMessageBox.No
            )
            if reply == QMessageBox.Yes:
                self.hosts.pop(row)
                self.update_hosts_table()
                self.save_hosts()

    def add_host(self):
        dialog = EditHostDialog()
        if dialog.exec_() == QDialog.Accepted:
            data = dialog.get_data()
            if data['name'] and data['address']:
                self.hosts.append(Host(data['name'], data['address'], data['interval']))
                self.update_hosts_table()
                self.save_hosts()

    def update_hosts_table(self):
        self.hosts_table.setRowCount(len(self.hosts))
        for row, host in enumerate(self.hosts):
            color = QColor('#90EE90') if host.alive else QColor('#FF6B6B')
            cols = [
                host.name,
                host.address,
                "Alive" if host.alive else "Dead",
                f"{host.interval} sec"
            ]
            for col_idx, text in enumerate(cols):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                item.setForeground(color)
                self.hosts_table.setItem(row, col_idx, item)

    def check_hosts(self):
        if self._cleaned_up or not self.hosts:
            return
        current_time, any_down = time.time(), False
        self.ping_workers = [w for w in self.ping_workers if w.isRunning()]
        for host in self.hosts:
            if (current_time - host.last_check >= host.interval
                    and not host.being_checked
                    and len(self.ping_workers) < 10):
                host.being_checked = True
                worker = PingWorker(host, host.address)
                worker.result_ready.connect(self.on_ping_result)
                worker.start()
                self.ping_workers.append(worker)
            if not host.alive:
                any_down = True
        self.tray.setIcon(self.RED_ICON if any_down else self.GREEN_ICON)

    def on_ping_result(self, host, is_alive):
        if self._cleaned_up:
            return
        host.being_checked = False
        host.last_check = time.time()
        was_alive = host.alive

        if is_alive:
            host.failure_count = 0
            host.alive = True
        else:
            host.failure_count += 1
            # Mark dead only after N consecutive failures (configurable)
            if host.failure_count >= self.failure_threshold:
                host.alive = False

        # Notify on transition to dead
        if not host.alive and was_alive and self.notifications_enabled:
            host.notification_sent = True
            self.tray.showMessage(
                "Ping Monitor",
                f"Host {host.name.upper()} appears DOWN after {self.failure_threshold} failures.",
                QSystemTrayIcon.Critical
            )

        # Reset notification flag when host recovers
        if was_alive != host.alive and host.alive:
            host.notification_sent = False

        self.update_hosts_table()

    def load_hosts(self):
        try:
            if os.path.exists(HOSTS_FILE):
                with open(HOSTS_FILE, 'r', encoding='utf-8') as f:
                    self.hosts = [Host.from_dict(h) for h in json.load(f)]
                self.update_hosts_table()
            else:
                self.save_hosts()
        except Exception:
            try:
                with open(HOSTS_FILE, 'w', encoding='utf-8') as f:
                    json.dump([], f)
            except Exception:
                pass

    def save_hosts(self):
        try:
            with open(HOSTS_FILE, 'w', encoding='utf-8') as f:
                json.dump([h.to_dict() for h in self.hosts],
                          f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def import_hosts(self):
        try:
            filename, _ = QFileDialog.getOpenFileName(
                self, "Import Hosts", "", "JSON Files (*.json);;All Files (*)"
            )
            if filename:
                with open(filename, 'r', encoding='utf-8') as f:
                    self.hosts = [Host.from_dict(h) for h in json.load(f)]
                self.update_hosts_table()
                self.save_hosts()
                QMessageBox.information(self, "Success", "Hosts imported successfully!")
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Import error: {str(e)}")

    def export_hosts(self):
        try:
            filename, _ = QFileDialog.getSaveFileName(
                self, "Export Hosts", "hosts.json", "JSON Files (*.json);;All Files (*)"
            )
            if filename:
                with open(filename, 'w', encoding='utf-8') as f:
                    json.dump([h.to_dict() for h in self.hosts],
                              f, ensure_ascii=False, indent=2)
                QMessageBox.information(self, "Success", "Hosts exported successfully!")
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Export error: {str(e)}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(None, "Error", "System tray not supported!")
        sys.exit(1)

    monitor = PingMonitor()
    # Guaranteed cleanup on Windows session end (restart / shutdown)
    app.aboutToQuit.connect(monitor.cleanup)

    if not monitor.start_minimized:
        monitor.show()

    sys.exit(app.exec_())
