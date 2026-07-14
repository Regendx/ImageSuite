from __future__ import annotations

APP_QSS = r"""
* {
    font-family: "Segoe UI", Arial, sans-serif;
    font-size: 10pt;
}
QMainWindow, QWidget#AppRoot { background: #17191d; color: #e8ebef; }
QWidget { color: #e8ebef; }
QFrame#TopBar, QFrame#StatusBar { background: #111318; border: 0; }
QFrame#SideBar { background: #202329; border-right: 1px solid #2e323a; }
QLabel#Brand { color: #7e9aff; font-size: 20pt; font-weight: 800; letter-spacing: 1px; }
QLabel#SubBrand { color: #9097a3; font-size: 9pt; }
QLabel#SectionTitle { color: #f4f5f7; font-weight: 700; font-size: 11pt; }
QLabel#Muted { color: #aeb4bf; }
QLabel#AIStatus { background: #202a3b; color: #dce5ff; border: 1px solid #3c527e; border-radius: 7px; padding: 8px 10px; }
QLabel#Status { color: #aeb4bf; padding: 4px 8px; }
QPushButton {
    background: #2b2f37; color: #f3f5f7; border: 0; border-radius: 6px;
    padding: 7px 11px;
}
QPushButton:hover { background: #3a404b; }
QPushButton:pressed { background: #454c58; }
QPushButton:focus { border: 2px solid #9badff; padding: 5px 9px; }
QPushButton:disabled { background: #24272d; color: #777c86; }
QPushButton#Accent { background: #5b7cfa; color: white; font-weight: 700; }
QPushButton#Accent:hover { background: #6f8bfd; }
QPushButton#Danger { background: #743d48; color: white; }
QPushButton#Danger:hover { background: #8a4a57; }
QPushButton#NavButton {
    text-align: left; padding: 11px 14px; border-radius: 7px;
    background: transparent; color: #c5cad3; font-weight: 600;
}
QPushButton#NavButton:hover { background: #2b2f37; color: white; }
QPushButton#NavButton:checked { background: #5b7cfa; color: white; }
QToolButton#NavButton {
    text-align: left; padding: 11px 14px; border-radius: 7px;
    background: transparent; color: #c5cad3; font-weight: 600;
}
QToolButton#NavButton:hover, QToolButton#NavButton:pressed { background: #2b2f37; color: white; }
QToolButton {
    background: #2b2f37; color: #f3f5f7; border: 0; border-radius: 5px; padding: 6px 9px;
}
QToolButton:hover { background: #3a404b; }
QToolButton:checked { background: #5b7cfa; }
QToolButton:focus { border: 2px solid #9badff; padding: 4px 7px; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QTextEdit, QListWidget, QTreeWidget, QTableWidget {
    background: #252931; color: #eef1f4; border: 1px solid #343944; border-radius: 5px;
    padding: 5px;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus, QTextEdit:focus,
QListWidget:focus, QTreeWidget:focus, QTableWidget:focus { border: 1px solid #6f8bfd; }
QComboBox::drop-down { border: 0; width: 24px; }
QComboBox QAbstractItemView { background: #252931; color: #eef1f4; selection-background-color: #5b7cfa; }
QTabWidget::pane { border: 0; background: #202329; }
QTabBar::tab {
    background: #2b2f37; color: #c5cad3; padding: 8px 12px; margin-right: 2px;
    border-top-left-radius: 5px; border-top-right-radius: 5px;
}
QTabBar::tab:selected { background: #5b7cfa; color: white; }
QTabBar::tab:hover:!selected { background: #3a404b; }
QGroupBox {
    background: #202329; border: 1px solid #303540; border-radius: 7px; margin-top: 12px;
    padding: 10px;
}
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; color: #f4f5f7; font-weight: 700; }
QSlider { min-height: 30px; }
QSlider::groove:horizontal { height: 8px; background: #343842; border-radius: 4px; }
QSlider::handle:horizontal { width: 22px; margin: -8px 0; background: #7e9aff; border: 2px solid #c9d3ff; border-radius: 11px; }
QSlider::sub-page:horizontal { background: #5b7cfa; border-radius: 2px; }
QProgressBar { background: #252931; border: 1px solid #343944; border-radius: 5px; text-align: center; }
QProgressBar::chunk { background: #5b7cfa; border-radius: 4px; }
QHeaderView::section { background: #2b2f37; color: #e8ebef; padding: 7px; border: 0; border-right: 1px solid #3b404a; }
QTableWidget { gridline-color: #343944; selection-background-color: #3d568f; }
QListWidget::item, QTreeWidget::item { padding: 5px; }
QListWidget::item:selected, QTreeWidget::item:selected { background: #3d568f; }
QScrollBar:vertical { background: #1f2228; width: 12px; margin: 0; }
QScrollBar::handle:vertical { background: #414854; border-radius: 5px; min-height: 25px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: #1f2228; height: 12px; }
QScrollBar::handle:horizontal { background: #414854; border-radius: 5px; min-width: 25px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QSplitter::handle { background: #30343d; }
QMenuBar { background: #111318; color: #e8ebef; }
QMenuBar::item:selected { background: #2b2f37; }
QMenu { background: #252931; color: #e8ebef; border: 1px solid #3a404b; }
QMenu::item:selected { background: #5b7cfa; }
QCheckBox, QRadioButton { spacing: 7px; }
QCheckBox:focus, QRadioButton:focus { color: #ffffff; }
QCheckBox::indicator, QRadioButton::indicator { width: 16px; height: 16px; }
QCheckBox::indicator:unchecked { border: 1px solid #59606c; background: #252931; border-radius: 3px; }
QCheckBox::indicator:checked { border: 1px solid #5b7cfa; background: #5b7cfa; border-radius: 3px; }
QToolTip { background: #303540; color: white; border: 1px solid #545c69; padding: 4px; }
"""
