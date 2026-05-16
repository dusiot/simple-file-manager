import os
import sys
import ctypes
import html
import json
import posixpath
import shutil
import zipfile
import xml.etree.ElementTree as ET
from PyQt5.QtCore import QEasingCurve, QEvent, QMimeData, QPoint, QPropertyAnimation, QRect, QSize, Qt, QUrl
from PyQt5.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QIcon,
    QImage,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
)
from PyQt5.QtMultimedia import QMediaContent, QMediaPlayer
from PyQt5.QtMultimediaWidgets import QVideoWidget
from PyQt5.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QMenu,
    QProgressDialog,
    QPushButton,
    QShortcut,
    QSizePolicy,
    QScrollArea,
    QSlider,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QStyle,
    QTabBar,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

try:
    import fitz
except ImportError:
    fitz = None

try:
    import cv2
except ImportError:
    cv2 = None


class CropImageLabel(QLabel):
    # Image widget used by the crop dialog to select a rectangular area.

    def __init__(self, pixmap):
        super().__init__()
        self.original_pixmap = pixmap
        self.pixmap_rect = QRect()
        self.selection_rect = QRect()
        self.selecting = False
        self.setMinimumSize(640, 420)
        self.setAlignment(Qt.AlignCenter)
        self.setMouseTracking(True)
        self.update_scaled_pixmap()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_scaled_pixmap()

    def update_scaled_pixmap(self):
        if self.original_pixmap.isNull():
            return

        scaled = self.original_pixmap.scaled(
            self.size(),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self.setPixmap(scaled)
        x = (self.width() - scaled.width()) // 2
        y = (self.height() - scaled.height()) // 2
        self.pixmap_rect = QRect(x, y, scaled.width(), scaled.height())

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.pixmap_rect.contains(event.pos()):
            self.selecting = True
            point = self.clamped_point(event.pos())
            self.selection_rect = QRect(point, point)
            self.update()
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.selecting:
            self.selection_rect.setBottomRight(self.clamped_point(event.pos()))
            self.update()
            return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.selecting and event.button() == Qt.LeftButton:
            self.selection_rect.setBottomRight(self.clamped_point(event.pos()))
            self.selecting = False
            self.update()
            return

        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        selection = self.selection_rect.normalized()
        if selection.width() < 4 or selection.height() < 4:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(selection, QColor(20, 184, 166, 55))
        painter.setPen(QPen(QColor(20, 184, 166), 2))
        painter.drawRect(selection)

    def clamped_point(self, point):
        return QPoint(
            min(max(point.x(), self.pixmap_rect.left()), self.pixmap_rect.right()),
            min(max(point.y(), self.pixmap_rect.top()), self.pixmap_rect.bottom()),
        )

    def source_selection_rect(self):
        selection = self.selection_rect.normalized().intersected(self.pixmap_rect)
        if selection.width() < 4 or selection.height() < 4:
            return QRect()

        scale_x = self.original_pixmap.width() / self.pixmap_rect.width()
        scale_y = self.original_pixmap.height() / self.pixmap_rect.height()
        source_rect = QRect(
            round((selection.x() - self.pixmap_rect.x()) * scale_x),
            round((selection.y() - self.pixmap_rect.y()) * scale_y),
            round(selection.width() * scale_x),
            round(selection.height() * scale_y),
        )
        return source_rect.intersected(
            QRect(0, 0, self.original_pixmap.width(), self.original_pixmap.height())
        )


class ImageCropDialog(QDialog):
    # Dialog that lets the user crop an image and choose how to save it.

    def __init__(self, file_path, parent=None):
        super().__init__(parent)
        self.file_path = file_path
        self.save_mode = None
        self.original_pixmap = QPixmap(file_path)

        self.setWindowTitle("Crop Image")
        self.resize(820, 620)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        self.crop_label = CropImageLabel(self.original_pixmap)
        layout.addWidget(self.crop_label, 1)

        actions = QHBoxLayout()
        actions.addStretch(1)
        btn_save_as = QPushButton("Save As")
        btn_overwrite = QPushButton("Overwrite")
        btn_cancel = QPushButton("Cancel")
        for button in (btn_save_as, btn_overwrite, btn_cancel):
            button.setMinimumHeight(36)
        actions.addWidget(btn_save_as)
        actions.addWidget(btn_overwrite)
        actions.addWidget(btn_cancel)
        layout.addLayout(actions)

        btn_save_as.clicked.connect(lambda: self.finish_crop("save_as"))
        btn_overwrite.clicked.connect(lambda: self.finish_crop("overwrite"))
        btn_cancel.clicked.connect(self.reject)

    def finish_crop(self, save_mode):
        if self.crop_label.source_selection_rect().isNull():
            QMessageBox.information(self, "Crop Image", "Select an area to crop first.")
            return

        self.save_mode = save_mode
        self.accept()

    def cropped_pixmap(self):
        source_rect = self.crop_label.source_selection_rect()
        if source_rect.isNull():
            return QPixmap()

        return self.original_pixmap.copy(source_rect)


class MediaVault(QWidget):
    # Main application window: owns the library, tabs, previews, settings, and saved state.

    # Supported file groups. These tuples decide which preview/icon logic each file uses.
    STATE_FILE_NAME = "mediavault_state.json"
    APP_ICON_FILE_NAME = "MediaVault.ico"
    STATE_VERSION = 2
    IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")
    VIDEO_EXTENSIONS = (".mp4", ".avi", ".mov", ".mkv", ".wmv")
    AUDIO_EXTENSIONS = (".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a", ".wma", ".opus", ".aiff", ".aif")
    PDF_EXTENSIONS = (".pdf",)
    WORD_EXTENSIONS = (".docx",)
    POWERPOINT_EXTENSIONS = (".pptx", ".pptm", ".ppsx")
    EXCEL_EXTENSIONS = (".xlsx", ".xlsm", ".xltx")
    TEXT_EXTENSIONS = (
        ".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm",
        ".py", ".css", ".js", ".log", ".ini", ".yaml", ".yml",
    )
    EXTERNAL_DOCUMENT_EXTENSIONS = (
        ".doc", ".rtf", ".ppt", ".xls",
        ".odt", ".ods", ".odp",
    )
    ARCHIVE_EXTENSIONS = (
        ".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz",
    )

    def __init__(self):
        # Startup state: remembers the current folder, selected file, tabs, and preferences.
        super().__init__()

        self.files = []
        self.current_folder = ""
        self.current_file_path = None
        self.current_image_path = None
        self.current_pdf_path = None
        self.current_pdf_page = 0
        self.current_pdf_page_count = 0
        self.recent_files = []
        self.favorite_folders = []
        self.pinned_folders = []
        self.selected_library_folder = ""
        self.previous_tab_index = 0
        self.tab_transition_animation = None
        self.fixed_tab_count = 1
        self.loading_state = False
        self.icon_cache = {}
        self.internal_clipboard_paths = []
        self.button_animations = {}
        self.preferences = self.default_preferences()
        self.load_preferences_from_disk()
        self.pdf_zoom = self.preferences["pdf_zoom"]
        self.library_root = self.load_library_root_from_disk()
        self.ensure_library_root()

        self.setWindowTitle("MediaVault - Multimedia File Manager")
        self.setWindowIcon(self.app_window_icon())
        self.resize(1120, 680)
        self.setMinimumSize(900, 560)
        self.setAcceptDrops(True)

        self.player = QMediaPlayer(self)

        self.build_ui()
        self.apply_style()
        self.connect_signals()
        self.load_saved_state()
        self.refresh_library_tiles()
        self.select_library_folder(self.current_folder)

    def build_ui(self):
        # Main window shell: top toolbar, tab bar, home page, hidden preview page, and settings dialog.
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(10)

        top_bar = QFrame()
        top_bar.setObjectName("TopBar")
        header_layout = QHBoxLayout(top_bar)
        header_layout.setContentsMargins(14, 10, 14, 10)
        header_layout.setSpacing(10)

        brand_layout = QVBoxLayout()
        brand_layout.setContentsMargins(0, 0, 8, 0)
        brand_layout.setSpacing(0)

        self.app_title = QLabel("MediaVault")
        self.app_title.setObjectName("AppTitle")
        self.app_subtitle = QLabel("Library")
        self.app_subtitle.setObjectName("AppSubtitle")

        brand_layout.addWidget(self.app_title)
        brand_layout.addWidget(self.app_subtitle)

        self.btn_open = QPushButton("Open Folder")
        self.btn_open.setObjectName("PrimaryButton")
        self.btn_open.setIcon(self.style().standardIcon(QStyle.SP_DirOpenIcon))
        self.btn_open.setMinimumHeight(42)

        self.search_bar = QLineEdit()
        self.search_bar.setObjectName("SearchBar")
        self.search_bar.setPlaceholderText("Search files")
        self.search_bar.setMinimumHeight(42)
        self.search_bar.setClearButtonEnabled(True)

        self.sort_box = QComboBox()
        self.sort_box.addItems(["Sort by Name", "Sort by Type", "Sort by Size"])
        self.sort_box.setMinimumHeight(42)
        self.sort_box.setMinimumWidth(170)

        self.btn_settings = QPushButton("Settings")
        self.btn_settings.setObjectName("ToolbarButton")
        self.btn_settings.setIcon(self.style().standardIcon(QStyle.SP_FileDialogDetailedView))
        self.btn_settings.setMinimumHeight(42)

        header_layout.addLayout(brand_layout)
        header_layout.addWidget(self.btn_open)
        header_layout.addWidget(self.search_bar, 1)
        header_layout.addWidget(self.sort_box)
        header_layout.addWidget(self.btn_settings)
        main_layout.addWidget(top_bar)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("MainTabs")
        self.tabs.setIconSize(QSize(20, 20))
        self.tabs.setDocumentMode(True)
        self.tabs.setUsesScrollButtons(False)
        self.tabs.tabBar().setObjectName("ExplorerTabs")
        self.tabs.tabBar().setExpanding(False)
        self.tabs.tabBar().setDrawBase(False)
        self.tabs.setTabsClosable(True)
        main_layout.addWidget(self.tabs, 1)

        self.home_tab = self.create_home_tab()
        self.preview_tab = self.create_preview_tab()
        self.settings_dialog = self.create_settings_dialog()

        home_icon = self.style().standardIcon(
            getattr(QStyle, "SP_DirHomeIcon", QStyle.SP_DirIcon)
        )

        self.tabs.addTab(self.home_tab, home_icon, "Files")
        self.update_tab_close_buttons()

        self.copy_shortcut = QShortcut(QKeySequence.Copy, self)
        self.copy_shortcut.setContext(Qt.WindowShortcut)
        self.paste_shortcut = QShortcut(QKeySequence.Paste, self)
        self.paste_shortcut.setContext(Qt.WindowShortcut)

        self.btn_add_tab = QPushButton("+")
        self.btn_add_tab.setObjectName("AddTabButton")
        self.btn_add_tab.setToolTip("Open folder in a new tab")
        self.btn_add_tab.setFixedSize(38, 38)
        self.add_tab_corner = QWidget()
        self.add_tab_corner.setObjectName("AddTabCorner")
        self.add_tab_corner.setFixedWidth(64)
        add_tab_corner_layout = QHBoxLayout(self.add_tab_corner)
        add_tab_corner_layout.setContentsMargins(8, 2, 10, 2)
        add_tab_corner_layout.addWidget(self.btn_add_tab)
        self.tabs.setCornerWidget(self.add_tab_corner, Qt.TopRightCorner)
        self.install_interaction_animations(self)

    def create_home_tab(self):
        # Files tab: front page with library files, folders, and quick access shortcuts.
        panel = QFrame()
        panel.setObjectName("Panel")

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("Library")
        title.setObjectName("PanelTitle")

        self.home_count_label = QLabel("0 items")
        self.home_count_label.setObjectName("MutedLabel")
        self.home_count_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        header.addWidget(title)
        header.addWidget(self.home_count_label, 1)

        actions = QHBoxLayout()
        actions.setSpacing(8)

        self.btn_new_library_folder = QPushButton("New Folder")
        self.btn_new_library_folder.setObjectName("PrimaryButton")
        self.btn_new_library_folder.setIcon(self.style().standardIcon(QStyle.SP_DirIcon))
        self.btn_import_files = QPushButton("Import Files")
        self.btn_import_files.setObjectName("ToolbarButton")
        self.btn_import_files.setIcon(self.style().standardIcon(QStyle.SP_DialogOpenButton))
        self.btn_import_folder = QPushButton("Import Folder")
        self.btn_import_folder.setObjectName("ToolbarButton")
        self.btn_import_folder.setIcon(self.style().standardIcon(QStyle.SP_DirOpenIcon))

        for button in (
            self.btn_new_library_folder,
            self.btn_import_files,
            self.btn_import_folder,
        ):
            button.setMinimumHeight(36)

        self.btn_import_files.setEnabled(True)
        self.btn_import_folder.setEnabled(True)

        self.library_root_label = QLabel(self.library_root)
        self.library_root_label.setObjectName("MutedLabel")
        self.library_root_label.setWordWrap(True)

        actions.addWidget(self.btn_new_library_folder)
        actions.addWidget(self.btn_import_files)
        actions.addWidget(self.btn_import_folder)
        actions.addStretch(1)

        self.library_tiles = QListWidget()
        self.library_tiles.setObjectName("FolderTileView")
        self.library_tiles.setViewMode(QListView.IconMode)
        self.library_tiles.setMovement(QListView.Static)
        self.library_tiles.setResizeMode(QListView.Adjust)
        self.library_tiles.setWrapping(True)
        self.library_tiles.setSpacing(16)
        self.library_tiles.setIconSize(QSize(72, 72))
        self.library_tiles.setGridSize(QSize(198, 148))
        self.library_tiles.setUniformItemSizes(True)
        self.library_tiles.setAlternatingRowColors(False)
        self.library_tiles.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.library_tiles.setContextMenuPolicy(Qt.CustomContextMenu)

        quick_header = QHBoxLayout()
        quick_title = QLabel("Quick Access")
        quick_title.setObjectName("PanelTitle")
        self.quick_access_count_label = QLabel("0 shortcuts")
        self.quick_access_count_label.setObjectName("MutedLabel")
        self.quick_access_count_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        quick_header.addWidget(quick_title)
        quick_header.addWidget(self.quick_access_count_label, 1)

        self.quick_access_tiles = QListWidget()
        self.quick_access_tiles.setObjectName("QuickAccessView")
        self.quick_access_tiles.setViewMode(QListView.IconMode)
        self.quick_access_tiles.setFlow(QListView.LeftToRight)
        self.quick_access_tiles.setMovement(QListView.Static)
        self.quick_access_tiles.setResizeMode(QListView.Adjust)
        self.quick_access_tiles.setWrapping(False)
        self.quick_access_tiles.setSpacing(10)
        self.quick_access_tiles.setIconSize(QSize(56, 56))
        self.quick_access_tiles.setGridSize(QSize(178, 112))
        self.quick_access_tiles.setUniformItemSizes(True)
        self.quick_access_tiles.setAlternatingRowColors(False)
        self.quick_access_tiles.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.quick_access_tiles.setContextMenuPolicy(Qt.CustomContextMenu)
        self.quick_access_tiles.setMinimumHeight(128)
        self.quick_access_tiles.setMaximumHeight(150)

        self.configure_drop_target(
            self.library_tiles,
            lambda position, paths: self.home_drop_target(self.library_tiles, position, paths),
        )
        self.configure_drop_target(
            self.quick_access_tiles,
            lambda position, paths: self.home_drop_target(self.quick_access_tiles, position, paths),
        )

        layout.addLayout(header)
        layout.addLayout(actions)
        layout.addWidget(self.library_root_label)
        layout.addLayout(quick_header)
        layout.addWidget(self.quick_access_tiles)
        layout.addWidget(self.library_tiles, 1)

        return panel

    def create_preview_tab(self):
        # Main Preview tab: created at startup but inserted into the tab bar only after opening a folder.
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(8)

        left_panel = self.create_left_panel()
        right_panel = self.create_preview_panel()

        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 5)
        splitter.setSizes([440, 550])

        return splitter

    def create_settings_tab(self):
        # Settings form: theme, font size, PDF zoom, saved session behavior, and media autoplay.
        panel = QFrame()
        panel.setObjectName("Panel")

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(16)

        header = QHBoxLayout()
        title = QLabel("Settings")
        title.setObjectName("PanelTitle")
        header.addWidget(title)
        header.addStretch(1)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setFormAlignment(Qt.AlignTop)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(14)

        self.theme_box = QComboBox()
        self.theme_box.addItems(["Dark", "Light"])
        self.theme_box.setCurrentText(self.preferences["theme"])

        self.font_family_box = QComboBox()
        self.font_family_box.addItems(self.available_font_families())
        self.font_family_box.setCurrentText(self.preferences["font_family"])

        self.font_size_spin = QSpinBox()
        self.font_size_spin.setRange(11, 20)
        self.font_size_spin.setSuffix(" px")
        self.font_size_spin.setValue(self.preferences["font_size"])

        self.pdf_zoom_spin = QDoubleSpinBox()
        self.pdf_zoom_spin.setRange(0.8, 3.0)
        self.pdf_zoom_spin.setSingleStep(0.1)
        self.pdf_zoom_spin.setDecimals(1)
        self.pdf_zoom_spin.setValue(self.preferences["pdf_zoom"])

        self.restore_session_check = QCheckBox("Remember last folder")
        self.restore_session_check.setChecked(self.preferences["restore_session"])

        self.remember_selected_check = QCheckBox("Remember selected file")
        self.remember_selected_check.setChecked(self.preferences["remember_selected_file"])

        self.auto_play_check = QCheckBox("Auto-play audio and video")
        self.auto_play_check.setChecked(self.preferences["auto_play_media"])

        self.btn_reset_settings = QPushButton("Reset Settings")
        self.btn_reset_settings.setIcon(
            self.style().standardIcon(getattr(QStyle, "SP_BrowserReload", QStyle.SP_FileIcon))
        )
        self.btn_reset_settings.setMinimumHeight(36)

        form.addRow("Theme", self.theme_box)
        form.addRow("Font style", self.font_family_box)
        form.addRow("Font size", self.font_size_spin)
        form.addRow("PDF zoom", self.pdf_zoom_spin)
        form.addRow("", self.restore_session_check)
        form.addRow("", self.remember_selected_check)
        form.addRow("", self.auto_play_check)
        form.addRow("", self.btn_reset_settings)

        layout.addLayout(header)
        layout.addLayout(form)
        layout.addStretch(1)

        return panel

    def create_settings_dialog(self):
        # Settings is a separate popup so it does not behave like a normal closable file tab.
        dialog = QDialog(self)
        dialog.setWindowTitle("MediaVault Settings")
        dialog.setModal(False)
        dialog.resize(560, 420)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.create_settings_tab())

        return dialog

    def create_left_panel(self):
        # Left side of the main Preview tab: selected folder path and file list.
        panel = QFrame()
        panel.setObjectName("Panel")
        panel.setMinimumWidth(360)
        panel.setMaximumWidth(900)

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        title = QLabel("Library")
        title.setObjectName("PanelTitle")

        self.folder_label = QLabel("No folder selected")
        self.folder_label.setObjectName("MutedLabel")
        self.folder_label.setWordWrap(True)

        self.file_list = QListWidget()
        self.file_list.setIconSize(QSize(58, 58))
        self.file_list.setSpacing(8)
        self.file_list.setUniformItemSizes(True)
        self.file_list.setAlternatingRowColors(False)
        self.file_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.file_list.setContextMenuPolicy(Qt.CustomContextMenu)

        layout.addWidget(title)
        layout.addWidget(self.folder_label)
        layout.addWidget(self.file_list, 1)

        return panel

    def create_preview_panel(self):
        # Right side of the main Preview tab: preview display, controls, and file information.
        panel = QFrame()
        panel.setObjectName("Panel")

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        preview_header = QHBoxLayout()
        preview_title = QLabel("Preview")
        preview_title.setObjectName("PanelTitle")
        self.selection_label = QLabel("Select a file")
        self.selection_label.setObjectName("MutedLabel")
        self.selection_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        preview_header.addWidget(preview_title)
        preview_header.addWidget(self.selection_label, 1)

        self.preview_stack = QStackedWidget()
        self.preview_stack.setObjectName("PreviewStack")
        self.preview_stack.setMinimumSize(420, 320)
        self.preview_stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self.preview_label = QLabel("Open a folder and choose a media file")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setWordWrap(True)
        self.preview_label.setObjectName("PreviewLabel")

        self.video_widget = QVideoWidget()
        self.video_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.video_widget.mousePressEvent = lambda event: self.toggle_main_media()
        self.player.setVideoOutput(self.video_widget)

        self.document_view = QTextEdit()
        self.document_view.setObjectName("DocumentView")
        self.document_view.setReadOnly(True)
        self.document_view.setLineWrapMode(QTextEdit.WidgetWidth)

        self.pdf_scroll = QScrollArea()
        self.pdf_scroll.setObjectName("PdfScrollArea")
        self.pdf_scroll.setWidgetResizable(True)

        self.pdf_pages = QWidget()
        self.pdf_pages.setObjectName("PdfPages")
        self.pdf_pages_layout = QVBoxLayout(self.pdf_pages)
        self.pdf_pages_layout.setContentsMargins(18, 18, 18, 18)
        self.pdf_pages_layout.setSpacing(18)
        self.pdf_pages_layout.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.pdf_scroll.setWidget(self.pdf_pages)

        self.pdf_page_label = QLabel()
        self.pdf_page_label.setObjectName("PdfPage")
        self.pdf_page_label.setAlignment(Qt.AlignCenter)

        self.pdf_pages_layout.addWidget(self.pdf_page_label, 0, Qt.AlignHCenter)
        self.pdf_pages_layout.addStretch(1)

        self.preview_stack.addWidget(self.preview_label)
        self.preview_stack.addWidget(self.video_widget)
        self.preview_stack.addWidget(self.document_view)
        self.preview_stack.addWidget(self.pdf_scroll)

        controls = QHBoxLayout()
        controls.setSpacing(8)

        self.btn_open_file = QPushButton("Open File")
        self.btn_open_file.setObjectName("PrimaryButton")
        self.btn_open_file.setIcon(self.style().standardIcon(QStyle.SP_DialogOpenButton))
        self.btn_open_file.setMinimumHeight(36)
        self.btn_open_file.setEnabled(False)

        self.btn_crop_image = QPushButton("Crop")
        self.btn_crop_image.setIcon(
            self.style().standardIcon(getattr(QStyle, "SP_FileDialogDetailedView", QStyle.SP_FileIcon))
        )
        self.btn_crop_image.setMinimumHeight(36)
        self.btn_crop_image.setEnabled(False)
        self.btn_crop_image.setVisible(False)

        self.btn_pdf_prev = QPushButton("Previous")
        self.btn_pdf_prev.setIcon(
            self.style().standardIcon(getattr(QStyle, "SP_ArrowBack", QStyle.SP_FileIcon))
        )
        self.btn_pdf_next = QPushButton("Next")
        self.btn_pdf_next.setIcon(
            self.style().standardIcon(getattr(QStyle, "SP_ArrowForward", QStyle.SP_FileIcon))
        )
        self.btn_pdf_zoom_out = QPushButton("-")
        self.btn_pdf_zoom_in = QPushButton("+")

        self.pdf_page_indicator = QLabel("Page - / -")
        self.pdf_page_indicator.setObjectName("MutedLabel")
        self.pdf_page_indicator.setAlignment(Qt.AlignCenter)
        self.pdf_page_indicator.setMinimumWidth(88)

        self.btn_play = QPushButton("Play")
        self.btn_play.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.media_slider = QSlider(Qt.Horizontal)
        self.media_slider.setRange(0, 0)
        self.media_slider.setEnabled(False)
        self.media_slider.setMinimumWidth(180)
        self.media_time_label = QLabel("0:00 / 0:00")
        self.media_time_label.setObjectName("MutedLabel")
        self.media_time_label.setMinimumWidth(92)
        self.media_time_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.media_slider_pressed = False
        self.volume_slider = QSlider(Qt.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(80)
        self.volume_slider.setFixedWidth(100)
        self.volume_slider.setEnabled(False)
        self.volume_label = QLabel("80%")
        self.volume_label.setObjectName("MutedLabel")
        self.volume_label.setMinimumWidth(42)
        self.volume_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.btn_play.setMinimumHeight(36)
        self.btn_play.setEnabled(False)
        self.btn_play.setVisible(False)
        self.media_slider.setVisible(False)
        self.media_time_label.setVisible(False)
        self.volume_slider.setVisible(False)
        self.volume_label.setVisible(False)
        self.player.setVolume(self.volume_slider.value())

        for button in (
            self.btn_pdf_prev,
            self.btn_pdf_next,
            self.btn_pdf_zoom_out,
            self.btn_pdf_zoom_in,
        ):
            button.setMinimumHeight(36)
            button.setEnabled(False)

        self.btn_pdf_zoom_out.setFixedWidth(40)
        self.btn_pdf_zoom_in.setFixedWidth(40)

        controls.addWidget(self.btn_open_file)
        controls.addWidget(self.btn_crop_image)
        controls.addWidget(self.btn_pdf_prev)
        controls.addWidget(self.pdf_page_indicator)
        controls.addWidget(self.btn_pdf_next)
        controls.addWidget(self.btn_pdf_zoom_out)
        controls.addWidget(self.btn_pdf_zoom_in)
        controls.addStretch(1)
        controls.addWidget(self.btn_play)
        controls.addWidget(self.media_slider, 1)
        controls.addWidget(self.media_time_label)
        controls.addWidget(self.volume_slider)
        controls.addWidget(self.volume_label)

        self.file_info = QLabel("File Info: No file selected")
        self.file_info.setObjectName("InfoBox")
        self.file_info.setWordWrap(True)
        self.file_info.setMinimumHeight(78)
        self.file_info.setTextInteractionFlags(Qt.TextSelectableByMouse)

        layout.addLayout(preview_header)
        layout.addWidget(self.preview_stack, 1)
        layout.addLayout(controls)
        layout.addWidget(self.file_info)

        return panel

    def connect_signals(self):
        # Event wiring: connects buttons, tabs, file lists, menus, and settings controls to methods.
        self.btn_open.clicked.connect(self.open_folder)
        self.file_list.itemClicked.connect(self.preview_file)
        self.btn_new_library_folder.clicked.connect(self.create_library_folder)
        self.btn_import_files.clicked.connect(self.import_files_to_library)
        self.btn_import_folder.clicked.connect(self.import_folder_to_library)
        self.library_tiles.itemSelectionChanged.connect(self.update_library_selection)
        self.library_tiles.itemClicked.connect(self.preview_library_file_tile)
        self.library_tiles.itemDoubleClicked.connect(self.open_library_tile)
        self.library_tiles.customContextMenuRequested.connect(self.show_library_context_menu)
        self.quick_access_tiles.itemSelectionChanged.connect(self.update_quick_access_selection)
        self.quick_access_tiles.itemDoubleClicked.connect(self.open_library_tile)
        self.quick_access_tiles.customContextMenuRequested.connect(self.show_quick_access_context_menu)
        self.file_list.customContextMenuRequested.connect(self.show_file_context_menu)
        self.search_bar.textChanged.connect(self.refresh_list)
        self.search_bar.textChanged.connect(self.refresh_library_tiles)
        self.sort_box.currentIndexChanged.connect(self.refresh_list)
        self.sort_box.currentIndexChanged.connect(self.refresh_library_tiles)
        self.sort_box.currentIndexChanged.connect(self.refresh_folder_tabs)
        self.sort_box.currentIndexChanged.connect(self.save_state)
        self.btn_play.clicked.connect(self.toggle_main_media)
        self.media_slider.sliderPressed.connect(self.start_main_media_scrub)
        self.media_slider.sliderReleased.connect(self.finish_main_media_scrub)
        self.media_slider.sliderMoved.connect(self.preview_main_media_scrub)
        self.player.positionChanged.connect(self.update_main_media_position)
        self.player.durationChanged.connect(self.update_main_media_duration)
        self.player.stateChanged.connect(self.update_main_media_button)
        self.btn_open_file.clicked.connect(self.open_current_file)
        self.btn_crop_image.clicked.connect(lambda: self.crop_image_file(self.current_file_path))
        self.volume_slider.valueChanged.connect(self.update_main_volume)
        self.btn_pdf_prev.clicked.connect(self.show_previous_pdf_page)
        self.btn_pdf_next.clicked.connect(self.show_next_pdf_page)
        self.btn_pdf_zoom_out.clicked.connect(self.zoom_pdf_out)
        self.btn_pdf_zoom_in.clicked.connect(self.zoom_pdf_in)
        self.btn_add_tab.clicked.connect(self.add_folder_tab)
        self.btn_settings.clicked.connect(self.show_settings_dialog)
        self.tabs.tabCloseRequested.connect(self.close_file_tab)
        self.theme_box.currentTextChanged.connect(self.update_preferences)
        self.font_family_box.currentTextChanged.connect(self.update_preferences)
        self.font_size_spin.valueChanged.connect(self.update_preferences)
        self.pdf_zoom_spin.valueChanged.connect(self.update_preferences)
        self.restore_session_check.toggled.connect(self.update_preferences)
        self.remember_selected_check.toggled.connect(self.update_preferences)
        self.auto_play_check.toggled.connect(self.update_preferences)
        self.btn_reset_settings.clicked.connect(self.reset_settings)
        self.tabs.currentChanged.connect(self.animate_tab_change)
        self.copy_shortcut.activated.connect(self.copy_selected_paths_to_clipboard)
        self.paste_shortcut.activated.connect(self.paste_paths_from_clipboard)

    def state_file_path(self):
        # JSON state file path used for saved preferences, current folder, favorites, and pins.
        return os.path.join(self.app_folder_path(), self.STATE_FILE_NAME)

    def app_folder_path(self):
        # Folder that contains this Python file and optional app assets.
        return os.path.dirname(os.path.abspath(__file__))

    def app_window_icon(self):
        # Put MediaVault.ico beside mediavault.py to use a custom window/taskbar icon.
        icon_path = os.path.join(self.app_folder_path(), self.APP_ICON_FILE_NAME)
        if os.path.exists(icon_path):
            return QIcon(icon_path)

        return self.type_badge_icon("MV", "#4f7cff")

    def supported_file_dialog_filter(self):
        # File picker filter used when importing/opening files from outside the app.
        image_patterns = self.extension_filter_patterns(self.IMAGE_EXTENSIONS)
        video_patterns = self.extension_filter_patterns(self.VIDEO_EXTENSIONS)
        audio_patterns = self.extension_filter_patterns(self.AUDIO_EXTENSIONS)
        document_patterns = self.extension_filter_patterns(
            self.PDF_EXTENSIONS
            + self.WORD_EXTENSIONS
            + self.POWERPOINT_EXTENSIONS
            + self.EXCEL_EXTENSIONS
            + self.TEXT_EXTENSIONS
            + self.EXTERNAL_DOCUMENT_EXTENSIONS
        )
        archive_patterns = self.extension_filter_patterns(self.ARCHIVE_EXTENSIONS)
        all_supported_patterns = " ".join(
            pattern
            for pattern in (
                image_patterns,
                video_patterns,
                audio_patterns,
                document_patterns,
                archive_patterns,
            )
            if pattern
        )

        return (
            f"All Supported Files ({all_supported_patterns});;"
            f"Audio Files ({audio_patterns});;"
            f"Images ({image_patterns});;"
            f"Videos ({video_patterns});;"
            f"Documents ({document_patterns});;"
            f"Archives ({archive_patterns});;"
            "All Files (*)"
        )

    @staticmethod
    def extension_filter_patterns(extensions):
        # Converts extension tuples like ('.mp3', '.wav') into Qt file-dialog patterns.
        return " ".join(f"*{extension}" for extension in extensions)

    def show_settings_dialog(self):
        # Opens the settings popup from the top-right Settings button.
        self.settings_dialog.show()
        self.settings_dialog.raise_()
        self.settings_dialog.activateWindow()

    def update_tab_close_buttons(self):
        # Keeps fixed tabs protected; only dynamic file/folder tabs can be closed.
        tab_bar = self.tabs.tabBar()
        for index in range(min(self.fixed_tab_count, self.tabs.count())):
            tab_bar.setTabButton(index, QTabBar.RightSide, None)

    def configure_drop_target(self, widget, target_getter):
        # Makes a widget accept files/folders dragged in from Windows Explorer.
        targets = [widget]
        if hasattr(widget, "viewport"):
            targets.append(widget.viewport())

        for target in targets:
            target.setAcceptDrops(True)
            target.dragEnterEvent = lambda event: self.accept_file_drop_event(event)
            target.dragMoveEvent = lambda event: self.accept_file_drop_event(event)
            target.dropEvent = lambda event: self.drop_paths_on_widget(event, target_getter)

        if hasattr(widget, "setDropIndicatorShown"):
            widget.setDropIndicatorShown(True)

    @staticmethod
    def paths_from_drop_event(event):
        # Converts dropped URLs into local Windows file/folder paths.
        if not event.mimeData().hasUrls():
            return []

        paths = []
        for url in event.mimeData().urls():
            if url.isLocalFile():
                paths.append(url.toLocalFile())

        return paths

    def accept_file_drop_event(self, event):
        # Accepts drag/drop only when at least one local path is being dragged.
        if self.paths_from_drop_event(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragEnterEvent(self, event):
        # Main-window drag handler for drops outside a specific file list.
        self.accept_file_drop_event(event)

    def dragMoveEvent(self, event):
        # Keeps the drag accepted while moving over the main window.
        self.accept_file_drop_event(event)

    def dropEvent(self, event):
        # Main-window drop handler; imports into the active folder tab or selected library folder.
        paths = self.paths_from_drop_event(event)
        if not paths:
            event.ignore()
            return

        target_folder = self.default_drop_target(paths)
        self.import_dropped_paths(paths, target_folder)
        event.acceptProposedAction()

    def drop_paths_on_widget(self, event, target_getter):
        # Drop handler used by library tiles and folder-tab content views.
        paths = self.paths_from_drop_event(event)
        if not paths:
            event.ignore()
            return

        target_folder = target_getter(event.pos(), paths)
        self.import_dropped_paths(paths, target_folder)
        event.acceptProposedAction()

    def default_drop_target(self, paths):
        # Chooses where a generic drop should import files/folders.
        current_tab = self.tabs.currentWidget()
        if hasattr(current_tab, "folder_tab_data"):
            return current_tab.folder_tab_data["current_path"]

        selected_folder = self.selected_or_current_library_folder()
        if selected_folder:
            return selected_folder

        return self.library_root

    def home_drop_target(self, widget, position, paths):
        # Drops on a home/quick-access tile import into that folder; blank space uses a sensible default.
        item = widget.itemAt(position)
        if item:
            folder_path = item.data(Qt.UserRole)
            if folder_path and os.path.isdir(folder_path):
                return folder_path

        return self.default_drop_target(paths)

    def folder_tab_drop_target(self, tab, position, paths):
        # Drops onto a folder tile import into that folder; blank space imports into the open folder.
        file_list = tab.folder_tab_data["file_list"]
        item = file_list.itemAt(position)
        if item and item.data(Qt.UserRole + 1):
            folder_path = item.data(Qt.UserRole)
            if folder_path and os.path.isdir(folder_path):
                return folder_path

        return tab.folder_tab_data["current_path"]

    def import_dropped_paths(self, paths, target_folder, action_name="Imported"):
        # Copies dropped files/folders into the target folder and refreshes visible views.
        if not target_folder or not os.path.isdir(target_folder):
            QMessageBox.warning(self, "Import Files", "Select a valid target folder first.")
            return

        copied_files = 0
        copied_folders = 0
        failed_items = []

        for source_path in paths:
            if not os.path.exists(source_path):
                failed_items.append(os.path.basename(source_path) or source_path)
                continue

            try:
                if os.path.isfile(source_path):
                    destination_path = self.unique_destination_path(
                        target_folder,
                        os.path.basename(source_path),
                    )
                    shutil.copy2(source_path, destination_path)
                    copied_files += 1
                elif os.path.isdir(source_path):
                    source_abs = os.path.abspath(source_path)
                    target_abs = os.path.abspath(target_folder)
                    if source_abs == target_abs or self.path_is_inside(target_abs, source_abs):
                        failed_items.append(os.path.basename(source_path) or source_path)
                        continue

                    destination_folder = self.unique_destination_folder(
                        target_folder,
                        os.path.basename(source_path),
                    )
                    shutil.copytree(source_path, destination_folder)
                    copied_folders += 1
            except OSError:
                failed_items.append(os.path.basename(source_path) or source_path)

        self.refresh_views_after_drop(target_folder)

        message_parts = []
        if copied_files:
            message_parts.append(f"{copied_files} file{'s' if copied_files != 1 else ''}")
        if copied_folders:
            message_parts.append(f"{copied_folders} folder{'s' if copied_folders != 1 else ''}")

        if message_parts:
            title = "Paste Complete" if action_name == "Pasted" else "Import Complete"
            QMessageBox.information(
                self,
                title,
                f"{action_name} {' and '.join(message_parts)} into:\n{target_folder}",
            )

        if failed_items:
            QMessageBox.warning(
                self,
                "Import Warning",
                "Some items could not be imported:\n" + "\n".join(failed_items[:8]),
            )

    def refresh_views_after_drop(self, target_folder):
        # Refreshes home counts and any open folder tab showing the import target.
        self.refresh_library_tiles()
        self.select_library_folder(target_folder)

        for index in range(self.tabs.count()):
            tab = self.tabs.widget(index)
            if not hasattr(tab, "folder_tab_data"):
                continue

            current_path = tab.folder_tab_data["current_path"]
            if os.path.abspath(current_path) == os.path.abspath(target_folder):
                self.populate_folder_tab_contents(tab)

        self.save_state()

    def selected_paths_from_list(self, list_widget, fallback_path=None):
        # Reads selected file/folder paths from a QListWidget.
        selected_paths = [
            item.data(Qt.UserRole)
            for item in list_widget.selectedItems()
            if item.data(Qt.UserRole)
        ]

        if fallback_path and fallback_path not in selected_paths:
            return [fallback_path]

        return selected_paths

    def compress_paths(self, paths, destination_parent=None):
        # Creates a ZIP archive from one or more selected files/folders.
        paths = [path for path in paths if path and os.path.exists(path)]
        if not paths:
            QMessageBox.information(self, "Compress", "Select a file or folder to compress.")
            return

        destination_parent = destination_parent or os.path.dirname(paths[0])
        if not destination_parent or not os.path.isdir(destination_parent):
            destination_parent = os.path.dirname(paths[0])

        archive_name = self.archive_file_name(paths)
        archive_path = self.unique_destination_path(destination_parent, archive_name)
        progress_dialog = None

        try:
            zip_entries = self.zip_entries_for_paths(paths, archive_path)
            total_bytes = sum(max(entry["size"], 1) for entry in zip_entries)
            progress_dialog = self.create_compress_progress_dialog(total_bytes)
            completed = self.write_zip_archive(
                zip_entries,
                archive_path,
                progress_dialog,
                total_bytes,
            )
        except Exception as error:
            if progress_dialog:
                progress_dialog.close()
            self.remove_incomplete_archive(archive_path)
            QMessageBox.critical(self, "Compress", f"Could not create ZIP file:\n{error}")
            return

        if progress_dialog:
            progress_dialog.close()
        if not completed:
            self.remove_incomplete_archive(archive_path)
            QMessageBox.information(self, "Compress", "Compression cancelled.")
            return

        self.refresh_views_after_drop(destination_parent)
        QMessageBox.information(
            self,
            "Compress Complete",
            f"Created ZIP archive:\n{archive_path}",
        )

    @staticmethod
    def archive_file_name(paths):
        # Chooses the ZIP filename from the selected item name or "Archive.zip".
        if len(paths) == 1:
            source_name = os.path.basename(os.path.normpath(paths[0]))
            base_name, extension = os.path.splitext(source_name)
            if os.path.isfile(paths[0]) and base_name:
                return f"{base_name}.zip"
            return f"{source_name or 'Archive'}.zip"

        return "Archive.zip"

    def zip_entries_for_paths(self, paths, archive_path):
        # Builds the list of files that will be written into the ZIP archive.
        entries = []
        archive_abs = os.path.abspath(archive_path)

        for source_path in paths:
            source_abs = os.path.abspath(source_path)
            if source_abs == archive_abs:
                continue

            if os.path.isfile(source_path):
                entries.append({
                    "file_path": source_path,
                    "archive_name": os.path.basename(source_path),
                    "size": os.path.getsize(source_path),
                })
            elif os.path.isdir(source_path):
                base_parent = os.path.dirname(source_path)
                for root, _, file_names in os.walk(source_path):
                    for file_name in file_names:
                        file_path = os.path.join(root, file_name)
                        if os.path.abspath(file_path) == archive_abs:
                            continue

                        archive_name = os.path.relpath(file_path, base_parent)
                        entries.append({
                            "file_path": file_path,
                            "archive_name": archive_name.replace("\\", "/"),
                            "size": os.path.getsize(file_path),
                        })

        return entries

    def create_compress_progress_dialog(self, total_bytes):
        # Creates a modal progress popup for ZIP compression.
        progress_unit = self.compress_progress_unit(total_bytes)
        maximum = max(1, (max(total_bytes, 1) + progress_unit - 1) // progress_unit)

        progress_dialog = QProgressDialog(
            "Preparing ZIP archive...",
            "Cancel",
            0,
            maximum,
            self,
        )
        progress_dialog.setWindowTitle("Compressing")
        progress_dialog.setWindowModality(Qt.WindowModal)
        progress_dialog.setMinimumDuration(0)
        progress_dialog.setAutoClose(False)
        progress_dialog.setAutoReset(False)
        progress_dialog.setValue(0)
        progress_dialog.show()
        QApplication.processEvents()
        return progress_dialog

    @staticmethod
    def compress_progress_unit(total_bytes):
        # Keeps the progress range modest even for very large files.
        return max(1, max(total_bytes, 1) // 1000)

    def write_zip_archive(self, entries, archive_path, progress_dialog, total_bytes):
        # Writes ZIP entries in chunks so the progress dialog can update.
        if not entries:
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED):
                pass
            progress_dialog.setValue(progress_dialog.maximum())
            QApplication.processEvents()
            return True

        processed_bytes = 0
        progress_unit = self.compress_progress_unit(total_bytes)

        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for entry in entries:
                if progress_dialog.wasCanceled():
                    return False

                file_path = entry["file_path"]
                archive_name = entry["archive_name"]
                progress_dialog.setLabelText(f"Compressing {os.path.basename(file_path)}")
                QApplication.processEvents()

                with open(file_path, "rb") as source_file:
                    zip_info = zipfile.ZipInfo.from_file(file_path, archive_name)
                    zip_info.compress_type = zipfile.ZIP_DEFLATED
                    with archive.open(zip_info, "w") as archive_file:
                        while True:
                            chunk = source_file.read(1024 * 1024)
                            if not chunk:
                                break

                            archive_file.write(chunk)
                            processed_bytes += len(chunk)
                            self.update_compress_progress(
                                progress_dialog,
                                processed_bytes,
                                progress_unit,
                            )

                            if progress_dialog.wasCanceled():
                                return False

                if entry["size"] == 0:
                    processed_bytes += 1
                    self.update_compress_progress(
                        progress_dialog,
                        processed_bytes,
                        progress_unit,
                    )

        progress_dialog.setValue(progress_dialog.maximum())
        QApplication.processEvents()
        return True

    @staticmethod
    def update_compress_progress(progress_dialog, processed_bytes, progress_unit):
        # Moves the progress bar after a chunk is written.
        value = (processed_bytes + progress_unit - 1) // progress_unit
        progress_dialog.setValue(min(progress_dialog.maximum(), value))
        QApplication.processEvents()

    @staticmethod
    def remove_incomplete_archive(archive_path):
        # Deletes a partial ZIP if compression fails or is cancelled.
        try:
            if archive_path and os.path.exists(archive_path):
                os.remove(archive_path)
        except OSError:
            pass

    def extract_archive(self, archive_path, destination_parent=None):
        # Extracts a supported compressed archive into a new folder beside the archive.
        if not archive_path or not os.path.isfile(archive_path):
            QMessageBox.information(self, "Extract", "Select a compressed file to extract.")
            return

        if not self.is_supported_archive(archive_path):
            QMessageBox.warning(
                self,
                "Extract",
                "This archive type is not supported.\n"
                "Supported: ZIP, TAR, TAR.GZ, TAR.BZ2, TAR.XZ",
            )
            return

        destination_parent = destination_parent or os.path.dirname(archive_path)
        if not destination_parent or not os.path.isdir(destination_parent):
            destination_parent = os.path.dirname(archive_path)

        destination_folder = self.unique_destination_folder(
            destination_parent,
            self.archive_extract_folder_name(archive_path),
        )

        try:
            os.makedirs(destination_folder)
            shutil.unpack_archive(archive_path, destination_folder)
        except Exception as error:
            try:
                os.rmdir(destination_folder)
            except OSError:
                pass
            QMessageBox.critical(self, "Extract", f"Could not extract archive:\n{error}")
            return

        self.refresh_views_after_drop(destination_parent)
        QMessageBox.information(
            self,
            "Extract Complete",
            f"Extracted archive to:\n{destination_folder}",
        )

    @classmethod
    def is_supported_archive(cls, file_path):
        # Checks compressed archive extensions that Python can unpack without extra tools.
        lower_path = file_path.lower()
        return any(lower_path.endswith(extension) for extension in cls.ARCHIVE_EXTENSIONS)

    @classmethod
    def archive_extract_folder_name(cls, archive_path):
        # Turns "photos.tar.gz" or "photos.zip" into a clean destination folder name.
        name = os.path.basename(archive_path)
        lower_name = name.lower()

        for extension in sorted(cls.ARCHIVE_EXTENSIONS, key=len, reverse=True):
            if lower_name.endswith(extension):
                name = name[:-len(extension)]
                break
        else:
            name = os.path.splitext(name)[0]

        return cls.safe_folder_name(name) or "Extracted Files"

    def keyPressEvent(self, event):
        # Global copy/paste shortcuts for files and folders.
        if event.matches(QKeySequence.Copy):
            self.copy_selected_paths_to_clipboard()
            event.accept()
            return

        if event.matches(QKeySequence.Paste):
            self.paste_paths_from_clipboard()
            event.accept()
            return

        super().keyPressEvent(event)

    def clipboard_paths(self):
        # Reads copied files/folders from the system clipboard.
        mime_data = QApplication.clipboard().mimeData()
        paths = []

        if mime_data and mime_data.hasUrls():
            for url in mime_data.urls():
                if url.isLocalFile():
                    path = url.toLocalFile()
                    if os.path.exists(path):
                        paths.append(path)

        if paths:
            return paths

        return [path for path in self.internal_clipboard_paths if os.path.exists(path)]

    def set_clipboard_paths(self, paths):
        # Places selected app items on the system clipboard as file URLs.
        paths = [path for path in paths if path and os.path.exists(path)]
        if not paths:
            return False

        mime_data = QMimeData()
        mime_data.setUrls([QUrl.fromLocalFile(path) for path in paths])
        mime_data.setText("\n".join(paths))
        QApplication.clipboard().setMimeData(mime_data)
        self.internal_clipboard_paths = paths
        return True

    def selected_paths_for_copy(self):
        # Finds selected files/folders from the active MediaVault view.
        current_tab = self.tabs.currentWidget()
        if hasattr(current_tab, "folder_tab_data"):
            selected_items = current_tab.folder_tab_data["file_list"].selectedItems()
            return [item.data(Qt.UserRole) for item in selected_items]

        selected_items = []
        if self.tabs.currentWidget() == self.home_tab:
            selected_items = (
                self.library_tiles.selectedItems()
                or self.quick_access_tiles.selectedItems()
            )
        elif self.file_list.hasFocus():
            selected_items = self.file_list.selectedItems()

        return [item.data(Qt.UserRole) for item in selected_items]

    def copy_selected_paths_to_clipboard(self):
        # Copies selected app files/folders for later paste.
        focus_widget = QApplication.focusWidget()
        if isinstance(focus_widget, (QLineEdit, QTextEdit)):
            focus_widget.copy()
            return

        paths = self.selected_paths_for_copy()
        if not self.set_clipboard_paths(paths):
            QMessageBox.information(self, "Copy", "Select a file or folder to copy.")

    def current_paste_target_folder(self):
        # Chooses where Ctrl+V should paste copied files/folders.
        current_tab = self.tabs.currentWidget()
        if hasattr(current_tab, "folder_tab_data"):
            file_list = current_tab.folder_tab_data["file_list"]
            selected_items = file_list.selectedItems()
            if len(selected_items) == 1 and selected_items[0].data(Qt.UserRole + 1):
                folder_path = selected_items[0].data(Qt.UserRole)
                if folder_path and os.path.isdir(folder_path):
                    return folder_path

            return current_tab.folder_tab_data["current_path"]

        selected_folder = self.selected_or_current_library_folder()
        if selected_folder:
            return selected_folder

        return self.library_root

    def paste_paths_from_clipboard(self, target_folder=None):
        # Pastes copied files/folders into the chosen MediaVault folder.
        focus_widget = QApplication.focusWidget()
        if target_folder is None and (
            isinstance(focus_widget, QLineEdit)
            or (isinstance(focus_widget, QTextEdit) and not focus_widget.isReadOnly())
        ):
            focus_widget.paste()
            return

        paths = self.clipboard_paths()
        if not paths:
            QMessageBox.information(self, "Paste", "Copy files or folders first.")
            return

        target_folder = target_folder or self.current_paste_target_folder()
        self.import_dropped_paths(paths, target_folder, action_name="Pasted")

    def refresh_folder_tabs(self):
        # Reapplies the selected sort order to every open folder tab.
        for index in range(self.tabs.count()):
            tab = self.tabs.widget(index)
            if hasattr(tab, "folder_tab_data"):
                self.populate_folder_tab_contents(tab)

    def close_file_tab(self, index):
        # Closes only dynamic tabs and stops their private media player first.
        if index < self.fixed_tab_count:
            return

        widget = self.tabs.widget(index)
        if not widget:
            return

        player = getattr(widget, "preview_player", None)
        if player:
            player.stop()

        self.tabs.removeTab(index)
        widget.deleteLater()
        self.previous_tab_index = self.tabs.currentIndex()
        self.update_tab_close_buttons()

    def add_file_tab(self, file_path=None):
        # Opens one specific file in its own closable tab.
        if not file_path:
            start_folder = self.current_folder if self.current_folder else self.library_root
            file_path, _ = QFileDialog.getOpenFileName(
                self,
                "Open File in New Tab",
                start_folder,
                self.supported_file_dialog_filter(),
            )

        if not file_path or not os.path.exists(file_path):
            return

        tab = self.create_file_preview_tab(file_path)
        tab_title = self.short_tab_title(file_path)
        index = self.tabs.addTab(tab, self.icon_for_file(file_path), tab_title)
        self.tabs.setTabToolTip(index, file_path)
        self.tabs.setCurrentIndex(index)
        self.update_tab_close_buttons()

    def open_or_focus_file_tab(self, file_path):
        # Reuses an existing file tab instead of opening duplicates for the same file.
        if not file_path or not os.path.isfile(file_path):
            return

        target_path = os.path.abspath(file_path)
        for index in range(self.fixed_tab_count, self.tabs.count()):
            if os.path.abspath(self.tabs.tabToolTip(index)) == target_path:
                self.tabs.setCurrentIndex(index)
                return

        self.add_file_tab(file_path)

    def add_folder_tab(self, folder_path=None):
        # Opens a full folder workspace in a new closable tab.
        if not folder_path:
            start_folder = self.current_folder if self.current_folder else self.library_root
            folder_path = QFileDialog.getExistingDirectory(
                self,
                "Open Folder in New Tab",
                start_folder,
            )

        if not folder_path or not os.path.isdir(folder_path):
            return

        tab = self.create_folder_preview_tab(folder_path)
        tab_title = self.short_folder_tab_title(folder_path)
        index = self.tabs.addTab(
            tab,
            self.style().standardIcon(QStyle.SP_DirIcon),
            tab_title,
        )
        self.tabs.setTabToolTip(index, folder_path)
        self.tabs.setCurrentIndex(index)
        self.current_folder = folder_path
        self.selected_library_folder = folder_path if self.is_library_folder(folder_path) else ""
        self.select_library_folder(folder_path)
        self.update_tab_close_buttons()
        self.save_state()

    @staticmethod
    def short_tab_title(file_path):
        # Keeps file tab titles short enough to fit in the tab bar.
        name = os.path.basename(file_path)
        return name if len(name) <= 22 else f"{name[:19]}..."

    @staticmethod
    def short_folder_tab_title(folder_path):
        # Keeps folder tab titles short enough to fit in the tab bar.
        name = os.path.basename(os.path.normpath(folder_path)) or folder_path
        return name if len(name) <= 22 else f"{name[:19]}..."

    def create_file_preview_tab(self, file_path):
        # Builds the UI for a single-file tab opened from the file context menu.
        panel = QFrame()
        panel.setObjectName("Panel")

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel(os.path.basename(file_path))
        title.setObjectName("PanelTitle")
        path_label = QLabel(file_path)
        path_label.setObjectName("MutedLabel")
        path_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        path_label.setWordWrap(True)
        header.addWidget(title)
        header.addWidget(path_label, 1)

        preview_area = self.create_tab_preview_area(panel)

        layout.addLayout(header)
        layout.addWidget(preview_area, 1)

        self.render_file_tab(panel, file_path)
        return panel

    def create_folder_preview_tab(self, folder_path):
        # Builds a full folder tab: file list on the left and independent preview on the right.
        panel = QFrame()
        panel.setObjectName("Panel")

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel(os.path.basename(os.path.normpath(folder_path)) or folder_path)
        title.setObjectName("PanelTitle")
        path_label = QLabel(folder_path)
        path_label.setObjectName("MutedLabel")
        path_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        path_label.setWordWrap(True)
        header.addWidget(title)
        header.addWidget(path_label, 1)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(8)

        file_panel = QFrame()
        file_panel.setObjectName("InnerPanel")
        file_layout = QVBoxLayout(file_panel)
        file_layout.setContentsMargins(14, 14, 14, 14)
        file_layout.setSpacing(10)

        file_header = QHBoxLayout()
        file_title = QLabel("Contents")
        file_title.setObjectName("PanelTitle")
        file_count_label = QLabel("0 items")
        file_count_label.setObjectName("MutedLabel")
        file_count_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        file_header.addWidget(file_title)
        file_header.addWidget(file_count_label, 1)

        navigation_row = QHBoxLayout()
        navigation_row.setSpacing(8)

        btn_back = QPushButton("Back")
        btn_back.setIcon(
            self.style().standardIcon(getattr(QStyle, "SP_ArrowBack", QStyle.SP_FileIcon))
        )
        btn_up = QPushButton("Up")
        btn_up.setIcon(
            self.style().standardIcon(getattr(QStyle, "SP_ArrowUp", QStyle.SP_FileIcon))
        )
        btn_new_folder = QPushButton("New Folder")
        btn_new_folder.setObjectName("PrimaryButton")
        btn_new_folder.setIcon(self.style().standardIcon(QStyle.SP_DirIcon))
        btn_import_files = QPushButton("Import Files")
        btn_import_files.setIcon(self.style().standardIcon(QStyle.SP_DialogOpenButton))

        for button in (btn_back, btn_up, btn_new_folder, btn_import_files):
            button.setMinimumHeight(34)

        navigation_row.addWidget(btn_back)
        navigation_row.addWidget(btn_up)
        navigation_row.addWidget(btn_new_folder)
        navigation_row.addWidget(btn_import_files)
        navigation_row.addStretch(1)

        folder_label = QLabel(folder_path)
        folder_label.setObjectName("MutedLabel")
        folder_label.setWordWrap(True)

        file_list = QListWidget()
        file_list.setObjectName("FolderContentView")
        file_list.setViewMode(QListView.IconMode)
        file_list.setMovement(QListView.Static)
        file_list.setResizeMode(QListView.Adjust)
        file_list.setWrapping(True)
        file_list.setSpacing(14)
        file_list.setIconSize(QSize(64, 64))
        file_list.setGridSize(QSize(172, 126))
        file_list.setUniformItemSizes(True)
        file_list.setAlternatingRowColors(False)
        file_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        file_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.configure_drop_target(
            file_list,
            lambda position, paths, tab=panel: self.folder_tab_drop_target(tab, position, paths),
        )

        file_layout.addLayout(file_header)
        file_layout.addLayout(navigation_row)
        file_layout.addWidget(folder_label)
        file_layout.addWidget(file_list, 1)

        preview_panel = QFrame()
        preview_panel.setObjectName("InnerPanel")
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(14, 14, 14, 14)
        preview_layout.setSpacing(10)

        preview_header = QHBoxLayout()
        selected_label = QLabel("Select a file")
        selected_label.setObjectName("PanelTitle")
        selected_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        preview_header.addWidget(selected_label, 1)

        preview_area = self.create_tab_preview_area(panel)
        preview_layout.addLayout(preview_header)
        preview_layout.addWidget(preview_area, 1)

        splitter.addWidget(file_panel)
        splitter.addWidget(preview_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([380, 720])

        layout.addLayout(header)
        layout.addWidget(splitter, 1)

        panel.folder_tab_data = {
            "root_path": folder_path,
            "current_path": folder_path,
            "history": [],
            "file_list": file_list,
            "file_count_label": file_count_label,
            "selected_label": selected_label,
            "folder_label": folder_label,
            "btn_back": btn_back,
            "btn_up": btn_up,
        }

        file_list.itemClicked.connect(
            lambda item, tab=panel: self.preview_folder_tab_item(tab, item)
        )
        file_list.itemDoubleClicked.connect(
            lambda item, tab=panel: self.open_folder_tab_item(tab, item)
        )
        file_list.customContextMenuRequested.connect(
            lambda position, tab=panel: self.show_folder_tab_context_menu(tab, position)
        )
        btn_back.clicked.connect(lambda _, tab=panel: self.go_folder_tab_back(tab))
        btn_up.clicked.connect(lambda _, tab=panel: self.go_folder_tab_up(tab))
        btn_new_folder.clicked.connect(lambda _, tab=panel: self.create_folder_in_folder_tab(tab))
        btn_import_files.clicked.connect(lambda _, tab=panel: self.import_files_to_folder_tab(tab))

        self.populate_folder_tab_contents(panel)
        self.show_file_tab_message(panel, "Select a file to preview")
        panel.preview_data["info_label"].setText(
            f"Folder: {folder_path}\n"
            f"Items: {panel.folder_tab_data['file_list'].count()}"
        )

        self.install_interaction_animations(panel)
        return panel

    def create_tab_preview_area(self, tab):
        # Reusable preview stack used by every dynamic file/folder tab.
        preview_area = QWidget()
        layout = QVBoxLayout(preview_area)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        preview_stack = QStackedWidget()
        preview_stack.setObjectName("PreviewStack")
        preview_stack.setMinimumSize(420, 320)
        preview_stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        message_label = QLabel()
        message_label.setObjectName("PreviewLabel")
        message_label.setAlignment(Qt.AlignCenter)
        message_label.setWordWrap(True)

        video_widget = QVideoWidget()
        video_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        video_widget.mousePressEvent = lambda event, tab=tab: self.toggle_file_tab_media(tab)

        document_view = QTextEdit()
        document_view.setObjectName("DocumentView")
        document_view.setReadOnly(True)
        document_view.setLineWrapMode(QTextEdit.WidgetWidth)

        pdf_scroll = QScrollArea()
        pdf_scroll.setObjectName("PdfScrollArea")
        pdf_scroll.setWidgetResizable(True)
        pdf_pages = QWidget()
        pdf_pages.setObjectName("PdfPages")
        pdf_layout = QVBoxLayout(pdf_pages)
        pdf_layout.setContentsMargins(18, 18, 18, 18)
        pdf_layout.setSpacing(18)
        pdf_layout.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        pdf_label = QLabel()
        pdf_label.setObjectName("PdfPage")
        pdf_label.setAlignment(Qt.AlignCenter)
        pdf_layout.addWidget(pdf_label, 0, Qt.AlignHCenter)
        pdf_layout.addStretch(1)
        pdf_scroll.setWidget(pdf_pages)

        preview_stack.addWidget(message_label)
        preview_stack.addWidget(video_widget)
        preview_stack.addWidget(document_view)
        preview_stack.addWidget(pdf_scroll)

        controls = QHBoxLayout()
        controls.setSpacing(8)

        btn_open = QPushButton("Open File")
        btn_open.setObjectName("PrimaryButton")
        btn_open.setIcon(self.style().standardIcon(QStyle.SP_DialogOpenButton))

        btn_crop_image = QPushButton("Crop")
        btn_crop_image.setIcon(
            self.style().standardIcon(getattr(QStyle, "SP_FileDialogDetailedView", QStyle.SP_FileIcon))
        )
        btn_crop_image.setEnabled(False)
        btn_crop_image.setVisible(False)

        btn_pdf_prev = QPushButton("Previous")
        btn_pdf_prev.setIcon(
            self.style().standardIcon(getattr(QStyle, "SP_ArrowBack", QStyle.SP_FileIcon))
        )
        btn_pdf_next = QPushButton("Next")
        btn_pdf_next.setIcon(
            self.style().standardIcon(getattr(QStyle, "SP_ArrowForward", QStyle.SP_FileIcon))
        )
        btn_pdf_zoom_out = QPushButton("-")
        btn_pdf_zoom_in = QPushButton("+")
        page_indicator = QLabel("Page - / -")
        page_indicator.setObjectName("MutedLabel")
        page_indicator.setAlignment(Qt.AlignCenter)
        page_indicator.setMinimumWidth(88)

        btn_play = QPushButton("Play")
        btn_play.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        media_slider = QSlider(Qt.Horizontal)
        media_slider.setRange(0, 0)
        media_slider.setEnabled(False)
        media_slider.setMinimumWidth(180)
        media_time_label = QLabel("0:00 / 0:00")
        media_time_label.setObjectName("MutedLabel")
        media_time_label.setMinimumWidth(92)
        media_time_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        volume_slider = QSlider(Qt.Horizontal)
        volume_slider.setRange(0, 100)
        volume_slider.setValue(80)
        volume_slider.setFixedWidth(100)
        volume_slider.setEnabled(False)
        volume_label = QLabel("80%")
        volume_label.setObjectName("MutedLabel")
        volume_label.setMinimumWidth(42)
        volume_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        for button in (
            btn_open,
            btn_crop_image,
            btn_pdf_prev,
            btn_pdf_next,
            btn_pdf_zoom_out,
            btn_pdf_zoom_in,
            btn_play,
        ):
            button.setMinimumHeight(36)

        btn_pdf_zoom_out.setFixedWidth(40)
        btn_pdf_zoom_in.setFixedWidth(40)
        btn_play.setVisible(False)
        media_slider.setVisible(False)
        media_time_label.setVisible(False)
        volume_slider.setVisible(False)
        volume_label.setVisible(False)

        controls.addWidget(btn_open)
        controls.addWidget(btn_crop_image)
        controls.addWidget(btn_pdf_prev)
        controls.addWidget(page_indicator)
        controls.addWidget(btn_pdf_next)
        controls.addWidget(btn_pdf_zoom_out)
        controls.addWidget(btn_pdf_zoom_in)
        controls.addStretch(1)
        controls.addWidget(btn_play)
        controls.addWidget(media_slider, 1)
        controls.addWidget(media_time_label)
        controls.addWidget(volume_slider)
        controls.addWidget(volume_label)

        info_label = QLabel()
        info_label.setObjectName("InfoBox")
        info_label.setWordWrap(True)
        info_label.setMinimumHeight(78)
        info_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        layout.addWidget(preview_stack, 1)
        layout.addLayout(controls)
        layout.addWidget(info_label)

        player = QMediaPlayer(tab)
        player.setVideoOutput(video_widget)
        player.setVolume(volume_slider.value())

        tab.preview_player = player
        tab.preview_data = {
            "file_path": None,
            "preview_stack": preview_stack,
            "message_label": message_label,
            "video_widget": video_widget,
            "document_view": document_view,
            "pdf_scroll": pdf_scroll,
            "pdf_label": pdf_label,
            "page_indicator": page_indicator,
            "btn_pdf_prev": btn_pdf_prev,
            "btn_pdf_next": btn_pdf_next,
            "btn_pdf_zoom_out": btn_pdf_zoom_out,
            "btn_pdf_zoom_in": btn_pdf_zoom_in,
            "btn_play": btn_play,
            "media_slider": media_slider,
            "media_time_label": media_time_label,
            "media_slider_pressed": False,
            "btn_crop_image": btn_crop_image,
            "volume_slider": volume_slider,
            "volume_label": volume_label,
            "info_label": info_label,
            "player": player,
            "pdf_path": None,
            "pdf_page": 0,
            "pdf_page_count": 0,
            "pdf_zoom": self.pdf_zoom,
        }

        btn_open.clicked.connect(
            lambda checked=False, data=tab.preview_data: self.open_preview_data_file(data)
        )
        btn_crop_image.clicked.connect(
            lambda checked=False, tab=tab: self.crop_file_tab_image(tab)
        )
        btn_play.clicked.connect(lambda checked=False, tab=tab: self.toggle_file_tab_media(tab))
        volume_slider.valueChanged.connect(
            lambda value, tab=tab: self.update_file_tab_volume(tab, value)
        )
        media_slider.sliderPressed.connect(
            lambda tab=tab: self.start_file_tab_media_scrub(tab)
        )
        media_slider.sliderReleased.connect(
            lambda tab=tab: self.finish_file_tab_media_scrub(tab)
        )
        media_slider.sliderMoved.connect(
            lambda position, tab=tab: self.preview_file_tab_media_scrub(tab, position)
        )
        player.positionChanged.connect(
            lambda position, tab=tab: self.update_file_tab_media_position(tab, position)
        )
        player.durationChanged.connect(
            lambda duration, tab=tab: self.update_file_tab_media_duration(tab, duration)
        )
        player.stateChanged.connect(
            lambda state, tab=tab: self.update_file_tab_media_button(tab, state)
        )
        btn_pdf_prev.clicked.connect(
            lambda checked=False, tab=tab: self.show_previous_file_tab_pdf_page(tab)
        )
        btn_pdf_next.clicked.connect(
            lambda checked=False, tab=tab: self.show_next_file_tab_pdf_page(tab)
        )
        btn_pdf_zoom_out.clicked.connect(
            lambda checked=False, tab=tab: self.zoom_file_tab_pdf(tab, -0.2)
        )
        btn_pdf_zoom_in.clicked.connect(
            lambda checked=False, tab=tab: self.zoom_file_tab_pdf(tab, 0.2)
        )

        self.install_interaction_animations(preview_area)
        return preview_area

    def open_preview_data_file(self, data):
        # Opens the currently selected file from a dynamic preview tab in the default app.
        file_path = data.get("file_path")
        if file_path and os.path.exists(file_path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(file_path))

    def folder_tab_entries(self, folder_path):
        # Reads the direct children of the current folder tab path.
        entries = []
        try:
            with os.scandir(folder_path) as scan:
                for entry in scan:
                    entries.append((entry.path, entry.is_dir()))
        except OSError:
            return []

        option = self.sort_box.currentText()
        if option == "Sort by Type":
            return sorted(
                entries,
                key=lambda item: (
                    not item[1],
                    os.path.splitext(item[0])[1].lower(),
                    os.path.basename(item[0]).lower(),
                ),
            )

        if option == "Sort by Size":
            def entry_size(entry):
                try:
                    return 0 if entry[1] else os.path.getsize(entry[0])
                except OSError:
                    return 0

            return sorted(
                entries,
                key=lambda item: (
                    not item[1],
                    entry_size(item),
                    os.path.basename(item[0]).lower(),
                ),
            )

        return sorted(entries, key=lambda item: (not item[1], os.path.basename(item[0]).lower()))

    def populate_folder_tab_contents(self, tab):
        # Fills a dynamic folder tab with folders and files from the current folder.
        data = tab.folder_tab_data
        file_list = data["file_list"]
        file_list.clear()

        for item_path, is_folder in self.folder_tab_entries(data["current_path"]):
            item = QListWidgetItem(self.folder_tab_item_text(item_path, is_folder))
            item.setData(Qt.UserRole, item_path)
            item.setData(Qt.UserRole + 1, is_folder)
            item.setToolTip(item_path)
            item.setTextAlignment(Qt.AlignCenter)
            item.setIcon(
                self.style().standardIcon(QStyle.SP_DirIcon)
                if is_folder
                else self.icon_for_file(item_path)
            )
            file_list.addItem(item)

        count = file_list.count()
        data["file_count_label"].setText(f"{count} item{'s' if count != 1 else ''}")
        data["folder_label"].setText(data["current_path"])
        data["btn_back"].setEnabled(bool(data["history"]))
        data["btn_up"].setEnabled(
            os.path.abspath(data["current_path"]) != os.path.abspath(data["root_path"])
        )

    @staticmethod
    def folder_tab_item_text(item_path, is_folder):
        # Tile text for files and folders inside a folder tab.
        name = os.path.basename(item_path)
        if is_folder:
            return f"{name}\nFolder"

        try:
            size_text = MediaVault.format_size(os.path.getsize(item_path))
        except OSError:
            size_text = "File"

        return f"{name}\n{size_text}"

    def preview_folder_tab_item(self, tab, item):
        # Handles single-clicking a folder-tab tile.
        item_path = item.data(Qt.UserRole)
        is_folder = item.data(Qt.UserRole + 1)
        if not item_path or not os.path.exists(item_path):
            self.show_file_tab_message(tab, "Item not found")
            return

        tab.folder_tab_data["selected_label"].setText(os.path.basename(item_path))
        if is_folder:
            self.show_file_tab_message(tab, "Double-click to open this folder")
            tab.preview_data["info_label"].setText(
                f"Folder: {os.path.basename(item_path)}\n"
                f"Path: {item_path}\n"
                f"Items: {len(self.folder_tab_entries(item_path))}"
            )
            return

        self.render_file_tab(tab, item_path)

    def open_folder_tab_item(self, tab, item):
        # Double-click opens folders inside the same tab; files open in the default app.
        item_path = item.data(Qt.UserRole)
        if not item_path or not os.path.exists(item_path):
            return

        if item.data(Qt.UserRole + 1):
            self.navigate_folder_tab(tab, item_path)
            return

        QDesktopServices.openUrl(QUrl.fromLocalFile(item_path))

    def navigate_folder_tab(self, tab, folder_path, remember_history=True):
        # Changes a folder tab to show another folder's contents.
        if not folder_path or not os.path.isdir(folder_path):
            return

        data = tab.folder_tab_data
        if remember_history:
            data["history"].append(data["current_path"])
        data["current_path"] = folder_path
        self.current_folder = folder_path
        self.selected_library_folder = folder_path if self.is_library_folder(folder_path) else ""
        index = self.tabs.indexOf(tab)
        if index >= 0:
            self.tabs.setTabText(index, self.short_folder_tab_title(folder_path))
            self.tabs.setTabToolTip(index, folder_path)
        self.populate_folder_tab_contents(tab)
        data["selected_label"].setText(os.path.basename(folder_path) or folder_path)
        self.show_file_tab_message(tab, "Select a file to preview")
        tab.preview_data["info_label"].setText(
            f"Folder: {folder_path}\n"
            f"Items: {tab.folder_tab_data['file_list'].count()}"
        )
        self.save_state()

    def go_folder_tab_back(self, tab):
        # Back button for folder tabs.
        data = tab.folder_tab_data
        if not data["history"]:
            return

        previous_path = data["history"].pop()
        self.navigate_folder_tab(tab, previous_path, remember_history=False)

    def go_folder_tab_up(self, tab):
        # Up button for folder tabs; it stops at the tab's original root folder.
        data = tab.folder_tab_data
        current_path = os.path.abspath(data["current_path"])
        root_path = os.path.abspath(data["root_path"])
        if current_path == root_path:
            return

        parent_path = os.path.dirname(current_path)
        if self.path_is_inside(parent_path, root_path) or os.path.abspath(parent_path) == root_path:
            self.navigate_folder_tab(tab, parent_path)

    def create_folder_in_folder_tab(self, tab):
        # Creates a new folder inside the folder currently shown in this tab.
        parent_path = tab.folder_tab_data["current_path"]
        folder_name, accepted = QInputDialog.getText(
            self,
            "Create New Folder",
            "Folder name:",
        )
        if not accepted:
            return

        folder_name = self.safe_folder_name(folder_name)
        if not folder_name:
            QMessageBox.warning(self, "Create New Folder", "Enter a valid folder name.")
            return

        folder_path = os.path.join(parent_path, folder_name)
        if os.path.exists(folder_path):
            QMessageBox.warning(self, "Create New Folder", "A folder with this name already exists.")
            return

        try:
            os.makedirs(folder_path)
        except OSError as error:
            QMessageBox.critical(self, "Create New Folder", f"Could not create folder:\n{error}")
            return

        self.populate_folder_tab_contents(tab)
        self.refresh_library_tiles()
        self.save_state()

    def import_files_to_folder_tab(self, tab):
        # Copies files into the folder currently shown in this tab.
        target_folder = tab.folder_tab_data["current_path"]
        file_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Import Files",
            target_folder,
            self.supported_file_dialog_filter(),
        )
        if not file_paths:
            return

        copied_count = 0
        for source_path in file_paths:
            if not os.path.isfile(source_path):
                continue

            destination_path = self.unique_destination_path(
                target_folder,
                os.path.basename(source_path),
            )
            if os.path.abspath(source_path) == os.path.abspath(destination_path):
                continue

            try:
                shutil.copy2(source_path, destination_path)
                copied_count += 1
            except OSError as error:
                QMessageBox.warning(
                    self,
                    "Import Files",
                    f"Could not import {os.path.basename(source_path)}:\n{error}",
                )

        self.populate_folder_tab_contents(tab)
        self.refresh_library_tiles()
        self.save_state()

        if copied_count:
            QMessageBox.information(
                self,
                "Import Files",
                f"Imported {copied_count} file{'s' if copied_count != 1 else ''}.",
            )

    def show_folder_tab_context_menu(self, tab, position):
        # Right-click menu inside a dynamic folder tab.
        data = tab.folder_tab_data
        file_list = data["file_list"]
        item = file_list.itemAt(position)
        menu = QMenu(self)

        if item:
            item_path = item.data(Qt.UserRole)
            is_folder = item.data(Qt.UserRole + 1)
            open_action = menu.addAction("Open")
            open_tab_action = menu.addAction("Open in New Tab")
            copy_action = menu.addAction("Copy")
            paste_into_action = None
            if is_folder:
                paste_into_action = menu.addAction("Paste Into This Folder")
            compress_action = menu.addAction("Compress to ZIP")
            extract_action = None
            if not is_folder and self.is_supported_archive(item_path):
                extract_action = menu.addAction("Extract Here")
            rename_action = menu.addAction("Rename")
            delete_action = menu.addAction("Delete")
            menu.addSeparator()
            paste_here_action = menu.addAction("Paste Here")
            new_folder_action = menu.addAction("Create New Folder")
            import_files_action = menu.addAction("Import Files")

            selected_action = menu.exec_(file_list.mapToGlobal(position))
            if selected_action == open_action:
                if is_folder:
                    self.navigate_folder_tab(tab, item_path)
                else:
                    QDesktopServices.openUrl(QUrl.fromLocalFile(item_path))
            elif selected_action == open_tab_action:
                if is_folder:
                    self.add_folder_tab(item_path)
                else:
                    self.add_file_tab(item_path)
            elif selected_action == copy_action:
                self.set_clipboard_paths([item_path])
            elif paste_into_action is not None and selected_action == paste_into_action:
                self.paste_paths_from_clipboard(item_path)
            elif selected_action == compress_action:
                selected_paths = self.selected_paths_from_list(file_list, item_path)
                self.compress_paths(selected_paths, data["current_path"])
            elif extract_action is not None and selected_action == extract_action:
                self.extract_archive(item_path, data["current_path"])
            elif selected_action == rename_action:
                self.rename_folder_tab_item(tab, item_path, is_folder)
            elif selected_action == delete_action:
                self.delete_folder_tab_item(tab, item_path, is_folder)
            elif selected_action == paste_here_action:
                self.paste_paths_from_clipboard(data["current_path"])
            elif selected_action == new_folder_action:
                self.create_folder_in_folder_tab(tab)
            elif selected_action == import_files_action:
                self.import_files_to_folder_tab(tab)
            return

        paste_action = menu.addAction("Paste")
        compress_action = None
        selected_paths = self.selected_paths_from_list(file_list)
        if selected_paths:
            compress_action = menu.addAction("Compress Selected to ZIP")
        new_folder_action = menu.addAction("Create New Folder")
        import_files_action = menu.addAction("Import Files")
        selected_action = menu.exec_(file_list.mapToGlobal(position))
        if selected_action == paste_action:
            self.paste_paths_from_clipboard(data["current_path"])
        elif compress_action is not None and selected_action == compress_action:
            self.compress_paths(selected_paths, data["current_path"])
        elif selected_action == new_folder_action:
            self.create_folder_in_folder_tab(tab)
        elif selected_action == import_files_action:
            self.import_files_to_folder_tab(tab)

    def rename_folder_tab_item(self, tab, item_path, is_folder):
        # Renames a file or folder inside a dynamic folder tab.
        if not item_path or not os.path.exists(item_path):
            return

        current_name = os.path.basename(item_path)
        new_name, accepted = QInputDialog.getText(
            self,
            "Rename Folder" if is_folder else "Rename File",
            "Folder name:" if is_folder else "File name:",
            text=current_name,
        )
        if not accepted:
            return

        new_name = self.safe_folder_name(new_name) if is_folder else self.safe_file_name(new_name)
        if not new_name:
            QMessageBox.warning(self, "Rename", "Enter a valid name.")
            return

        new_path = os.path.join(os.path.dirname(item_path), new_name)
        if os.path.abspath(new_path) == os.path.abspath(item_path):
            return

        if os.path.exists(new_path):
            QMessageBox.warning(self, "Rename", "An item with this name already exists.")
            return

        try:
            os.rename(item_path, new_path)
        except OSError as error:
            QMessageBox.critical(self, "Rename", f"Could not rename item:\n{error}")
            return

        if is_folder:
            self.remap_current_paths(item_path, new_path)
        else:
            self.recent_files = [
                new_path if saved_path == item_path else saved_path
                for saved_path in self.recent_files
            ]
            if tab.preview_data.get("file_path") == item_path:
                self.render_file_tab(tab, new_path)

        self.populate_folder_tab_contents(tab)
        self.refresh_library_tiles()
        self.save_state()

    def delete_folder_tab_item(self, tab, item_path, is_folder):
        # Deletes a file or folder inside a dynamic folder tab.
        if not item_path or not os.path.exists(item_path):
            return

        reply = QMessageBox.question(
            self,
            "Delete Folder" if is_folder else "Delete File",
            f"Delete '{os.path.basename(item_path)}'?"
            + (" This will delete everything inside it." if is_folder else ""),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        try:
            if is_folder:
                shutil.rmtree(item_path)
            else:
                os.remove(item_path)
        except OSError as error:
            QMessageBox.critical(self, "Delete", f"Could not delete item:\n{error}")
            return

        self.recent_files = [
            saved_path
            for saved_path in self.recent_files
            if saved_path != item_path and not self.path_is_inside(saved_path, item_path)
        ]
        self.favorite_folders = [
            saved_path
            for saved_path in self.favorite_folders
            if saved_path != item_path and not self.path_is_inside(saved_path, item_path)
        ]
        self.pinned_folders = [
            saved_path
            for saved_path in self.pinned_folders
            if saved_path != item_path and not self.path_is_inside(saved_path, item_path)
        ]

        if tab.preview_data.get("file_path") == item_path:
            tab.folder_tab_data["selected_label"].setText("Select a file")
            self.show_file_tab_message(tab, "Item deleted")
            tab.preview_data["info_label"].setText(
                f"Folder: {tab.folder_tab_data['current_path']}\n"
                "Select a file to preview"
            )

        self.populate_folder_tab_contents(tab)
        self.refresh_library_tiles()
        self.save_state()

    def render_file_tab(self, tab, file_path):
        # Chooses image/video/audio/PDF/text rendering for a dynamic tab.
        data = tab.preview_data
        player = data["player"]

        player.stop()
        data["file_path"] = file_path
        data["pdf_path"] = None
        data["pdf_page"] = 0
        data["pdf_page_count"] = 0
        data["document_view"].clear()
        self.set_file_tab_media_controls(tab, False)
        self.set_file_tab_pdf_controls(tab, False)
        self.set_file_tab_image_controls(tab, False)

        lower_path = file_path.lower()

        if lower_path.endswith(self.IMAGE_EXTENSIONS):
            pixmap = QPixmap(file_path)
            if pixmap.isNull():
                self.show_file_tab_message(tab, "Unable to load image")
            else:
                available_size = data["preview_stack"].size() - QSize(32, 32)
                scaled = pixmap.scaled(
                    available_size,
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                )
                data["message_label"].setText("")
                data["message_label"].setPixmap(scaled)
                data["preview_stack"].setCurrentWidget(data["message_label"])
                self.set_file_tab_image_controls(tab, True)
        elif lower_path.endswith(self.VIDEO_EXTENSIONS):
            data["preview_stack"].setCurrentWidget(data["video_widget"])
            player.setMedia(QMediaContent(QUrl.fromLocalFile(file_path)))
            self.set_file_tab_media_controls(tab, True)
            if self.preferences["auto_play_media"]:
                player.play()
        elif lower_path.endswith(self.AUDIO_EXTENSIONS):
            self.show_file_tab_message(
                tab,
                "Playing audio" if self.preferences["auto_play_media"] else "Audio ready",
            )
            player.setMedia(QMediaContent(QUrl.fromLocalFile(file_path)))
            self.set_file_tab_media_controls(tab, True)
            if self.preferences["auto_play_media"]:
                player.play()
        elif lower_path.endswith(self.PDF_EXTENSIONS):
            self.render_file_tab_pdf(tab, file_path)
        elif lower_path.endswith(self.WORD_EXTENSIONS):
            try:
                text = self.extract_docx_text(file_path)
            except (KeyError, zipfile.BadZipFile, ET.ParseError):
                self.show_file_tab_message(tab, "Could not read this DOCX preview")
            else:
                self.show_file_tab_text(tab, text or "This DOCX does not contain previewable text")
        elif lower_path.endswith(self.POWERPOINT_EXTENSIONS):
            try:
                preview_html = self.extract_pptx_preview_html(file_path)
            except (KeyError, zipfile.BadZipFile, ET.ParseError):
                self.show_file_tab_message(tab, "Could not read this PowerPoint preview")
            else:
                self.show_file_tab_html(tab, preview_html)
        elif lower_path.endswith(self.EXCEL_EXTENSIONS):
            try:
                preview_html = self.extract_xlsx_preview_html(file_path)
            except (KeyError, zipfile.BadZipFile, ET.ParseError, ValueError):
                self.show_file_tab_message(tab, "Could not read this Excel preview")
            else:
                self.show_file_tab_html(tab, preview_html)
        elif lower_path.endswith(self.TEXT_EXTENSIONS):
            try:
                self.show_file_tab_text(tab, self.read_text_file(file_path))
            except UnicodeDecodeError:
                self.show_file_tab_message(tab, "Could not decode this text file")
        elif self.is_supported_archive(file_path):
            self.show_file_tab_message(tab, "Compressed file ready to extract")
        elif lower_path.endswith(self.EXTERNAL_DOCUMENT_EXTENSIONS):
            extension = os.path.splitext(file_path)[1].upper()
            self.show_file_tab_message(tab, f"{extension} files are best opened with their native app")
        else:
            self.show_file_tab_message(tab, "Preview not available for this file type")

        data["info_label"].setText(
            f"Name: {os.path.basename(file_path)}\n"
            f"Type: {self.file_type_label(file_path)}\n"
            f"Size: {self.format_size(os.path.getsize(file_path))}\n"
            f"Path: {file_path}"
        )

    def show_file_tab_message(self, tab, text):
        # Shows simple status text in a dynamic tab preview area.
        data = tab.preview_data
        self.set_file_tab_image_controls(tab, False)
        data["message_label"].setPixmap(QPixmap())
        data["message_label"].setText(text)
        data["preview_stack"].setCurrentWidget(data["message_label"])

    def show_file_tab_text(self, tab, text):
        # Shows extracted document/text content in a dynamic tab.
        data = tab.preview_data
        data["document_view"].setPlainText(text)
        data["document_view"].verticalScrollBar().setValue(0)
        data["preview_stack"].setCurrentWidget(data["document_view"])

    def show_file_tab_html(self, tab, preview_html):
        # Shows formatted Office previews in a dynamic tab.
        data = tab.preview_data
        data["document_view"].setHtml(preview_html)
        data["document_view"].verticalScrollBar().setValue(0)
        data["preview_stack"].setCurrentWidget(data["document_view"])

    def render_file_tab_pdf(self, tab, file_path):
        # Loads PDF metadata for a dynamic tab before rendering the current page.
        data = tab.preview_data
        if fitz is None:
            self.show_file_tab_message(tab, "PDF preview requires PyMuPDF")
            return

        try:
            with fitz.open(file_path) as document:
                if document.page_count == 0:
                    raise RuntimeError("PDF has no pages")
                data["pdf_path"] = file_path
                data["pdf_page"] = 0
                data["pdf_page_count"] = document.page_count
        except Exception:
            self.show_file_tab_message(tab, "Could not render this PDF preview")
            return

        self.render_current_file_tab_pdf_page(tab)
        data["preview_stack"].setCurrentWidget(data["pdf_scroll"])

    def render_current_file_tab_pdf_page(self, tab):
        # Converts the selected PDF page into a pixmap for display in a dynamic tab.
        data = tab.preview_data
        if not data["pdf_path"] or fitz is None:
            return

        try:
            with fitz.open(data["pdf_path"]) as document:
                if data["pdf_page"] < 0 or data["pdf_page"] >= document.page_count:
                    data["pdf_page"] = max(0, min(data["pdf_page"], document.page_count - 1))
                page = document.load_page(data["pdf_page"])
                matrix = fitz.Matrix(data["pdf_zoom"], data["pdf_zoom"])
                rendered_page = page.get_pixmap(matrix=matrix, alpha=False)
        except Exception:
            self.show_file_tab_message(tab, "Could not render this PDF page")
            self.set_file_tab_pdf_controls(tab, False)
            return

        pixmap = QPixmap()
        pixmap.loadFromData(rendered_page.tobytes("png"), "PNG")

        data["pdf_label"].setPixmap(pixmap)
        data["pdf_label"].setMinimumSize(pixmap.size())
        data["page_indicator"].setText(
            f"Page {data['pdf_page'] + 1} / {data['pdf_page_count']}"
        )
        data["pdf_scroll"].verticalScrollBar().setValue(0)
        data["pdf_scroll"].horizontalScrollBar().setValue(0)
        self.set_file_tab_pdf_controls(tab, True)

    def show_previous_file_tab_pdf_page(self, tab):
        # PDF previous-page button for dynamic tabs.
        data = tab.preview_data
        if data["pdf_page"] > 0:
            data["pdf_page"] -= 1
            self.render_current_file_tab_pdf_page(tab)

    def show_next_file_tab_pdf_page(self, tab):
        # PDF next-page button for dynamic tabs.
        data = tab.preview_data
        if data["pdf_page"] < data["pdf_page_count"] - 1:
            data["pdf_page"] += 1
            self.render_current_file_tab_pdf_page(tab)

    def zoom_file_tab_pdf(self, tab, amount):
        # PDF zoom buttons for dynamic tabs.
        data = tab.preview_data
        if not data["pdf_path"]:
            return

        data["pdf_zoom"] = min(3.0, max(0.8, data["pdf_zoom"] + amount))
        self.render_current_file_tab_pdf_page(tab)

    def set_file_tab_media_controls(self, tab, enabled):
        # Enables/disables player controls in a dynamic tab.
        data = tab.preview_data
        for widget_key in (
            "btn_play",
            "media_slider",
            "media_time_label",
            "volume_slider",
            "volume_label",
        ):
            data[widget_key].setVisible(enabled)

        data["btn_play"].setEnabled(enabled)
        data["media_slider"].setEnabled(enabled)
        data["volume_slider"].setEnabled(enabled)
        if not enabled:
            data["btn_play"].setText("Play")
            data["btn_play"].setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
            data["media_slider"].setRange(0, 0)
            data["media_slider"].setValue(0)
            data["media_time_label"].setText("0:00 / 0:00")
            data["media_slider_pressed"] = False

    def set_file_tab_image_controls(self, tab, enabled):
        # Enables/disables image editing controls in a dynamic tab.
        tab.preview_data["btn_crop_image"].setVisible(enabled)
        tab.preview_data["btn_crop_image"].setEnabled(enabled)

    def toggle_file_tab_media(self, tab):
        # Toggles play/pause for video or audio inside a dynamic tab.
        data = tab.preview_data
        if not data["btn_play"].isEnabled():
            return

        player = data["player"]
        if player.state() == QMediaPlayer.PlayingState:
            player.pause()
        else:
            player.play()

    def update_file_tab_media_button(self, tab, state):
        # Updates the dynamic tab media button label/icon when playback state changes.
        data = tab.preview_data
        if state == QMediaPlayer.PlayingState:
            data["btn_play"].setText("Pause")
            data["btn_play"].setIcon(self.style().standardIcon(QStyle.SP_MediaPause))
        else:
            data["btn_play"].setText("Play")
            data["btn_play"].setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))

    def update_file_tab_media_duration(self, tab, duration):
        # Sets the seek bar range when media duration becomes known.
        data = tab.preview_data
        data["media_slider"].setRange(0, max(0, duration))
        self.update_file_tab_media_time(tab, data["player"].position())

    def update_file_tab_media_position(self, tab, position):
        # Moves the seek bar while media plays.
        data = tab.preview_data
        if not data["media_slider_pressed"]:
            data["media_slider"].setValue(position)
        self.update_file_tab_media_time(tab, position)

    def start_file_tab_media_scrub(self, tab):
        # Marks the seek bar as being dragged by the user.
        tab.preview_data["media_slider_pressed"] = True

    def preview_file_tab_media_scrub(self, tab, position):
        # Updates the time label while the user drags the seek bar.
        self.update_file_tab_media_time(tab, position)

    def finish_file_tab_media_scrub(self, tab):
        # Seeks to the chosen position after the user releases the seek bar.
        data = tab.preview_data
        data["media_slider_pressed"] = False
        data["player"].setPosition(data["media_slider"].value())

    def update_file_tab_media_time(self, tab, position):
        # Shows elapsed and total time for media in dynamic tabs.
        data = tab.preview_data
        duration = data["media_slider"].maximum()
        data["media_time_label"].setText(
            f"{self.format_media_time(position)} / {self.format_media_time(duration)}"
        )

    def update_file_tab_volume(self, tab, value):
        # Updates audio/video volume in a dynamic tab.
        data = tab.preview_data
        data["player"].setVolume(value)
        data["volume_label"].setText(f"{value}%")

    def set_file_tab_pdf_controls(self, tab, enabled):
        # Enables/disables PDF navigation and zoom controls in a dynamic tab.
        data = tab.preview_data
        has_pdf = enabled and data["pdf_page_count"] > 0
        data["btn_pdf_prev"].setEnabled(has_pdf and data["pdf_page"] > 0)
        data["btn_pdf_next"].setEnabled(has_pdf and data["pdf_page"] < data["pdf_page_count"] - 1)
        data["btn_pdf_zoom_out"].setEnabled(has_pdf and data["pdf_zoom"] > 0.8)
        data["btn_pdf_zoom_in"].setEnabled(has_pdf and data["pdf_zoom"] < 3.0)

    @staticmethod
    def default_library_root():
        # Default folder where MediaVault stores imported library folders.
        documents_folder = os.path.join(os.path.expanduser("~"), "Documents")
        base_folder = documents_folder if os.path.isdir(documents_folder) else os.path.expanduser("~")
        return os.path.join(base_folder, "MediaVault Library")

    def load_library_root_from_disk(self):
        # Reads the saved library location, or falls back to the default Documents folder.
        state = self.read_state_file()
        library_root = state.get("library_root")
        if not isinstance(library_root, str) or not library_root.strip():
            library_root = self.default_library_root()

        return os.path.abspath(os.path.expanduser(library_root))

    def ensure_library_root(self):
        # Creates the library root if it does not already exist.
        os.makedirs(self.library_root, exist_ok=True)

    @staticmethod
    def default_preferences():
        # Default app settings used on first run or after Reset Settings.
        return {
            "theme": "Dark",
            "font_family": "Segoe UI",
            "font_size": 17,
            "restore_session": True,
            "remember_selected_file": True,
            "auto_play_media": True,
            "pdf_zoom": 1.5,
        }

    @staticmethod
    def available_font_families():
        # Common Windows fonts that give the app distinct but safe visual styles.
        return [
            "Segoe UI",
            "Arial",
            "Calibri",
            "Verdana",
            "Tahoma",
            "Trebuchet MS",
            "Georgia",
            "Times New Roman",
            "Consolas",
            "Courier New",
        ]

    def read_state_file(self):
        # Safely reads mediavault_state.json and returns an empty dict if missing/broken.
        try:
            with open(self.state_file_path(), "r", encoding="utf-8") as state_file:
                return json.load(state_file)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def load_preferences_from_disk(self):
        # Loads only the settings values that are valid and safe to apply.
        state = self.read_state_file()
        saved_preferences = state.get("preferences", {})
        if not isinstance(saved_preferences, dict):
            return

        theme = saved_preferences.get("theme")
        if theme in ("Dark", "Light"):
            self.preferences["theme"] = theme

        font_family = saved_preferences.get("font_family")
        if font_family in self.available_font_families():
            self.preferences["font_family"] = font_family

        font_size = saved_preferences.get("font_size")
        saved_state_version = state.get("state_version", 1)
        if isinstance(font_size, int) and saved_state_version >= self.STATE_VERSION:
            self.preferences["font_size"] = min(20, max(11, font_size))

        pdf_zoom = saved_preferences.get("pdf_zoom")
        if isinstance(pdf_zoom, (int, float)):
            self.preferences["pdf_zoom"] = min(3.0, max(0.8, float(pdf_zoom)))

        for key in ("restore_session", "remember_selected_file", "auto_play_media"):
            value = saved_preferences.get(key)
            if isinstance(value, bool):
                self.preferences[key] = value

    def update_preferences(self):
        # Saves settings whenever the user changes a setting in the dialog.
        if self.loading_state:
            return

        old_pdf_zoom = self.pdf_zoom
        self.preferences = {
            "theme": self.theme_box.currentText(),
            "font_family": self.font_family_box.currentText(),
            "font_size": self.font_size_spin.value(),
            "restore_session": self.restore_session_check.isChecked(),
            "remember_selected_file": self.remember_selected_check.isChecked(),
            "auto_play_media": self.auto_play_check.isChecked(),
            "pdf_zoom": self.pdf_zoom_spin.value(),
        }
        self.pdf_zoom = self.preferences["pdf_zoom"]

        self.apply_style()
        self.scale_current_image()

        if self.current_pdf_path and old_pdf_zoom != self.pdf_zoom:
            self.render_current_pdf_page()

        self.save_state()

    def reset_settings(self):
        # Restores default settings and immediately reapplies the app style.
        self.loading_state = True
        self.preferences = self.default_preferences()
        self.theme_box.setCurrentText(self.preferences["theme"])
        self.font_family_box.setCurrentText(self.preferences["font_family"])
        self.font_size_spin.setValue(self.preferences["font_size"])
        self.pdf_zoom_spin.setValue(self.preferences["pdf_zoom"])
        self.restore_session_check.setChecked(self.preferences["restore_session"])
        self.remember_selected_check.setChecked(self.preferences["remember_selected_file"])
        self.auto_play_check.setChecked(self.preferences["auto_play_media"])
        self.pdf_zoom = self.preferences["pdf_zoom"]
        self.loading_state = False

        self.apply_style()
        self.scale_current_image()
        if self.current_pdf_path:
            self.render_current_pdf_page()
        self.save_state()

    def load_saved_state(self):
        # Restores saved folder metadata, favorites, pins, and preferences on startup.
        self.loading_state = True

        state = self.read_state_file()
        if not state:
            self.loading_state = False
            return

        sort_option = state.get("sort_option")
        if sort_option:
            sort_index = self.sort_box.findText(sort_option)
            if sort_index >= 0:
                self.sort_box.setCurrentIndex(sort_index)

        self.recent_files = [
            file_path
            for file_path in state.get("recent_files", [])
            if isinstance(file_path, str) and os.path.exists(file_path)
        ]
        self.favorite_folders = [
            os.path.abspath(folder_path)
            for folder_path in state.get("favorite_folders", [])
            if isinstance(folder_path, str) and os.path.isdir(folder_path)
        ]
        self.pinned_folders = [
            os.path.abspath(folder_path)
            for folder_path in state.get("pinned_folders", [])
            if isinstance(folder_path, str) and os.path.isdir(folder_path)
        ]

        if not self.preferences["restore_session"]:
            self.loading_state = False
            return

        folder = state.get("current_folder")
        if isinstance(folder, str) and os.path.isdir(folder):
            self.current_folder = folder
            self.selected_library_folder = folder if self.is_library_folder(folder) else ""

        self.loading_state = False

    def save_state(self):
        # Writes current app state back to mediavault_state.json.
        if self.loading_state:
            return

        state = {
            "state_version": self.STATE_VERSION,
            "library_root": self.library_root,
            "current_folder": self.current_folder,
            "current_file_path": (
                self.current_file_path
                if self.preferences["remember_selected_file"]
                else None
            ),
            "recent_files": self.recent_files[-100:],
            "favorite_folders": self.favorite_folders,
            "pinned_folders": self.pinned_folders,
            "sort_option": self.sort_box.currentText(),
            "preferences": self.preferences,
        }

        try:
            with open(self.state_file_path(), "w", encoding="utf-8") as state_file:
                json.dump(state, state_file, indent=2)
        except OSError:
            pass

    def restore_selected_file(self, file_path):
        # Restores the selected file in the main Preview tab when that behavior is enabled.
        if not file_path or not os.path.exists(file_path):
            return

        for row in range(self.file_list.count()):
            item = self.file_list.item(row)
            if item.data(Qt.UserRole) == file_path:
                self.file_list.setCurrentItem(item)
                self.current_file_path = file_path
                self.btn_open_file.setEnabled(True)
                self.selection_label.setText(os.path.basename(file_path))
                self.update_file_info(file_path)
                self.select_home_file(file_path)
                return

    def refresh_library_tiles(self):
        # Refreshes the home page file/folder tiles and quick access shortcut list.
        self.ensure_library_root()
        self.library_tiles.clear()
        self.quick_access_tiles.clear()
        self.library_root_label.setText(self.library_root)

        keyword = self.search_bar.text().strip().lower()
        entries = self.folder_tab_entries(self.library_root)
        self.favorite_folders = [
            folder_path
            for folder_path in self.favorite_folders
            if os.path.isdir(folder_path)
        ]
        self.pinned_folders = [
            folder_path
            for folder_path in self.pinned_folders
            if os.path.isdir(folder_path)
        ]

        entries = sorted(
            entries,
            key=lambda item: (
                not item[1],
                not (item[1] and self.is_pinned_folder(item[0])),
                not (item[1] and self.is_favorite_folder(item[0])),
            )
        )

        folder_icon = self.style().standardIcon(QStyle.SP_DirIcon)
        visible_count = 0

        for item_path, is_folder in entries:
            item_name = os.path.basename(item_path)
            if keyword and keyword not in item_name.lower():
                continue

            icon = folder_icon if is_folder else self.icon_for_file(item_path)
            item = QListWidgetItem(icon, self.home_tile_text(item_path, is_folder))
            item.setTextAlignment(Qt.AlignCenter)
            item.setData(Qt.UserRole, item_path)
            item.setData(Qt.UserRole + 1, is_folder)
            item.setToolTip(item_path)
            self.library_tiles.addItem(item)
            visible_count += 1

            if is_folder and (
                self.is_pinned_folder(item_path)
                or self.is_favorite_folder(item_path)
            ):
                label_lines = self.folder_status_lines(item_path)
                quick_item = QListWidgetItem(
                    folder_icon,
                    f"{''.join(label_lines)}{item_name}",
                )
                quick_item.setTextAlignment(Qt.AlignCenter)
                quick_item.setData(Qt.UserRole, item_path)
                quick_item.setData(Qt.UserRole + 1, True)
                quick_item.setToolTip(item_path)
                self.quick_access_tiles.addItem(quick_item)

        self.home_count_label.setText(f"{visible_count} item{'s' if visible_count != 1 else ''}")
        shortcut_count = self.quick_access_tiles.count()
        self.quick_access_count_label.setText(
            f"{shortcut_count} shortcut{'s' if shortcut_count != 1 else ''}"
        )
        self.select_library_folder(self.selected_library_folder or self.current_folder)
        self.update_library_actions()

    def home_tile_text(self, item_path, is_folder):
        # Builds the two-line label for a main-page tile.
        name = os.path.basename(item_path)
        if is_folder:
            file_count = self.count_files_in_folder(item_path)
            label_lines = self.folder_status_lines(item_path)
            return (
                f"{''.join(label_lines)}{name}\n"
                f"{file_count} file{'s' if file_count != 1 else ''}"
            )

        try:
            size_text = self.format_size(os.path.getsize(item_path))
        except OSError:
            size_text = "Unknown size"

        return f"{name}\n{self.file_type_label(item_path)} - {size_text}"

    @staticmethod
    def count_files_in_folder(folder_path):
        # Counts files inside a folder so each home tile can show a file total.
        total = 0
        for _, _, file_names in os.walk(folder_path):
            total += len(file_names)

        return total

    def create_library_folder(self):
        # Creates a new folder inside the MediaVault library root.
        folder_name, accepted = QInputDialog.getText(
            self,
            "New Folder",
            "Folder name:",
        )
        if not accepted:
            return

        folder_name = self.safe_folder_name(folder_name)
        if not folder_name:
            QMessageBox.warning(self, "New Folder", "Enter a valid folder name.")
            return

        folder_path = os.path.join(self.library_root, folder_name)
        if os.path.exists(folder_path):
            QMessageBox.warning(self, "New Folder", "A folder with this name already exists.")
            return

        try:
            os.makedirs(folder_path)
        except OSError as error:
            QMessageBox.critical(self, "New Folder", f"Could not create folder:\n{error}")
            return

        self.selected_library_folder = folder_path
        self.refresh_library_tiles()
        self.select_library_folder(folder_path)
        self.save_state()

    def import_files_to_library(self):
        # Copies selected files into the selected folder, or into the library root.
        target_folder = self.selected_or_current_library_folder()
        if not target_folder:
            target_folder = self.library_root

        if not os.path.isdir(target_folder):
            QMessageBox.warning(self, "Import Files", "Select a valid folder first.")
            return

        file_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Import Files",
            target_folder,
            self.supported_file_dialog_filter(),
        )
        if not file_paths:
            return

        copied_count = 0
        for source_path in file_paths:
            if not os.path.isfile(source_path):
                continue

            destination_path = self.unique_destination_path(
                target_folder,
                os.path.basename(source_path),
            )

            if os.path.abspath(source_path) == os.path.abspath(destination_path):
                continue

            try:
                shutil.copy2(source_path, destination_path)
                copied_count += 1
            except OSError as error:
                QMessageBox.warning(
                    self,
                    "Import Files",
                    f"Could not import {os.path.basename(source_path)}:\n{error}",
                )

        self.refresh_library_tiles()
        self.save_state()

        if copied_count:
            QMessageBox.information(
                self,
                "Import Files",
                f"Imported {copied_count} file{'s' if copied_count != 1 else ''}.",
            )

    def import_folder_to_library(self):
        # Copies an entire external folder into the MediaVault library.
        source_folder = QFileDialog.getExistingDirectory(self, "Import Folder")
        if not source_folder:
            return

        source_folder = os.path.abspath(source_folder)
        library_root = os.path.abspath(self.library_root)

        try:
            source_contains_library = os.path.commonpath([source_folder, library_root]) == source_folder
            source_inside_library = os.path.commonpath([library_root, source_folder]) == library_root
        except ValueError:
            source_contains_library = False
            source_inside_library = False

        if source_contains_library or source_inside_library:
            QMessageBox.warning(
                self,
                "Import Folder",
                "Choose a folder outside the MediaVault library.",
            )
            return

        destination_folder = self.unique_destination_folder(
            self.library_root,
            os.path.basename(source_folder),
        )

        try:
            shutil.copytree(source_folder, destination_folder)
        except OSError as error:
            QMessageBox.critical(
                self,
                "Import Folder",
                f"Could not import folder:\n{error}",
            )
            return

        self.selected_library_folder = destination_folder
        self.refresh_library_tiles()
        self.select_library_folder(destination_folder)
        self.save_state()

    def show_library_context_menu(self, position):
        # Right-click menu for the main folder library tile area.
        self.show_folder_context_menu(self.library_tiles, position)

    def show_quick_access_context_menu(self, position):
        # Right-click menu for quick access folder tiles.
        self.show_folder_context_menu(self.quick_access_tiles, position)

    def show_folder_context_menu(self, widget, position):
        # Home/quick-access menu: folders get folder actions, files get preview actions.
        item = widget.itemAt(position)
        menu = QMenu(self)

        if item:
            item_path = item.data(Qt.UserRole)
            if not item_path or not os.path.exists(item_path):
                return

            is_folder = item.data(Qt.UserRole + 1)
            if is_folder is None:
                is_folder = os.path.isdir(item_path)

            if is_folder:
                open_action = menu.addAction("Open")
                open_tab_action = menu.addAction("Open in New Tab")
                copy_action = menu.addAction("Copy")
                paste_action = menu.addAction("Paste Into This Folder")
                compress_action = menu.addAction("Compress to ZIP")
                import_files_action = menu.addAction("Import Files")
                pin_action = menu.addAction(
                    "Unpin from Quick Access"
                    if self.is_pinned_folder(item_path)
                    else "Pin to Quick Access"
                )
                favorite_action = menu.addAction(
                    "Remove from Favorites"
                    if self.is_favorite_folder(item_path)
                    else "Add to Favorites"
                )
                rename_action = menu.addAction("Rename")
                delete_action = menu.addAction("Delete")
                menu.addSeparator()
                paste_here_action = menu.addAction("Paste Here")
                new_folder_action = menu.addAction("Create New Folder")

                selected_action = menu.exec_(widget.mapToGlobal(position))

                if selected_action == open_action:
                    self.open_library_folder(item_path, switch_to_preview=True)
                elif selected_action == open_tab_action:
                    self.add_folder_tab(item_path)
                elif selected_action == copy_action:
                    self.set_clipboard_paths([item_path])
                elif selected_action == paste_action:
                    self.paste_paths_from_clipboard(item_path)
                elif selected_action == compress_action:
                    selected_paths = self.selected_paths_from_list(widget, item_path)
                    self.compress_paths(selected_paths, os.path.dirname(item_path))
                elif selected_action == import_files_action:
                    self.selected_library_folder = item_path
                    self.import_files_to_library()
                elif selected_action == pin_action:
                    self.toggle_pinned_folder(item_path)
                elif selected_action == favorite_action:
                    self.toggle_favorite_folder(item_path)
                elif selected_action == rename_action:
                    self.rename_library_folder(item_path)
                elif selected_action == delete_action:
                    self.delete_library_folder(item_path)
                elif selected_action == paste_here_action:
                    self.paste_paths_from_clipboard(self.library_root)
                elif selected_action == new_folder_action:
                    self.create_library_folder()
                return

            preview_action = menu.addAction("Preview")
            open_action = menu.addAction("Open in Default App")
            copy_action = menu.addAction("Copy")
            compress_action = menu.addAction("Compress to ZIP")
            extract_action = None
            if self.is_supported_archive(item_path):
                extract_action = menu.addAction("Extract Here")
            rename_action = menu.addAction("Rename")
            delete_action = menu.addAction("Delete")
            menu.addSeparator()
            paste_here_action = menu.addAction("Paste Here")
            new_folder_action = menu.addAction("Create New Folder")

            selected_action = menu.exec_(widget.mapToGlobal(position))

            if selected_action == preview_action:
                self.open_or_focus_file_tab(item_path)
            elif selected_action == open_action:
                QDesktopServices.openUrl(QUrl.fromLocalFile(item_path))
            elif selected_action == copy_action:
                self.set_clipboard_paths([item_path])
            elif selected_action == compress_action:
                selected_paths = self.selected_paths_from_list(widget, item_path)
                self.compress_paths(selected_paths, os.path.dirname(item_path))
            elif extract_action is not None and selected_action == extract_action:
                self.extract_archive(item_path, os.path.dirname(item_path))
            elif selected_action == rename_action:
                self.rename_library_file(item_path)
            elif selected_action == delete_action:
                self.delete_library_file(item_path)
            elif selected_action == paste_here_action:
                self.paste_paths_from_clipboard(self.library_root)
            elif selected_action == new_folder_action:
                self.create_library_folder()
            return

        new_folder_action = menu.addAction("Create New Folder")
        paste_action = menu.addAction("Paste")
        selected_paths = self.selected_paths_from_list(widget)
        compress_action = None
        if selected_paths:
            compress_action = menu.addAction("Compress Selected to ZIP")
        selected_action = menu.exec_(widget.mapToGlobal(position))
        if selected_action == new_folder_action:
            self.create_library_folder()
        elif selected_action == paste_action:
            self.paste_paths_from_clipboard()
        elif compress_action is not None and selected_action == compress_action:
            self.compress_paths(selected_paths, self.library_root)

    def show_file_context_menu(self, position):
        # File menu in the main Preview tab: open, open in tab, rename, delete, or create folder.
        item = self.file_list.itemAt(position)
        menu = QMenu(self)

        if item:
            file_path = item.data(Qt.UserRole)
            open_action = menu.addAction("Open")
            open_tab_action = menu.addAction("Open in New Tab")
            copy_action = menu.addAction("Copy")
            compress_action = menu.addAction("Compress to ZIP")
            extract_action = None
            if self.is_supported_archive(file_path):
                extract_action = menu.addAction("Extract Here")
            rename_action = menu.addAction("Rename")
            delete_action = menu.addAction("Delete")
            menu.addSeparator()
            paste_action = menu.addAction("Paste Here")
            new_folder_action = menu.addAction("Create New Folder")

            selected_action = menu.exec_(self.file_list.mapToGlobal(position))

            if selected_action == open_action:
                self.preview_file_path(file_path)
                self.open_current_file()
            elif selected_action == open_tab_action:
                self.add_file_tab(file_path)
            elif selected_action == copy_action:
                self.set_clipboard_paths([file_path])
            elif selected_action == compress_action:
                selected_paths = self.selected_paths_from_list(self.file_list, file_path)
                self.compress_paths(selected_paths, os.path.dirname(file_path))
            elif extract_action is not None and selected_action == extract_action:
                self.extract_archive(file_path, os.path.dirname(file_path))
            elif selected_action == rename_action:
                self.rename_file(file_path)
            elif selected_action == delete_action:
                self.delete_file(file_path)
            elif selected_action == paste_action:
                self.paste_paths_from_clipboard(self.current_folder)
            elif selected_action == new_folder_action:
                self.create_folder_in_current_location()
            return

        paste_action = menu.addAction("Paste")
        new_folder_action = menu.addAction("Create New Folder")
        selected_action = menu.exec_(self.file_list.mapToGlobal(position))
        if selected_action == paste_action:
            self.paste_paths_from_clipboard(self.current_folder)
        elif selected_action == new_folder_action:
            self.create_folder_in_current_location()

    def is_favorite_folder(self, folder_path):
        # Checks whether a folder is marked as favorite.
        folder_path = os.path.abspath(folder_path)
        return any(
            os.path.abspath(saved_path) == folder_path
            for saved_path in self.favorite_folders
        )

    def is_pinned_folder(self, folder_path):
        # Checks whether a folder is pinned to quick access.
        folder_path = os.path.abspath(folder_path)
        return any(
            os.path.abspath(saved_path) == folder_path
            for saved_path in self.pinned_folders
        )

    def folder_status_lines(self, folder_path):
        # Adds visible labels to folder tiles for pinned/favorite folders.
        lines = []
        if self.is_pinned_folder(folder_path):
            lines.append("[Pinned]\n")
        if self.is_favorite_folder(folder_path):
            lines.append("[Favorite]\n")
        return lines

    def toggle_pinned_folder(self, folder_path):
        # Adds or removes a folder from quick access.
        if not folder_path or not os.path.isdir(folder_path):
            return

        folder_path = os.path.abspath(folder_path)
        if self.is_pinned_folder(folder_path):
            self.pinned_folders = [
                saved_path
                for saved_path in self.pinned_folders
                if os.path.abspath(saved_path) != folder_path
            ]
        else:
            self.pinned_folders.append(folder_path)

        self.refresh_library_tiles()
        self.select_library_folder(folder_path)
        self.save_state()

    def toggle_favorite_folder(self, folder_path):
        # Adds or removes a folder from favorites.
        if not folder_path or not os.path.isdir(folder_path):
            return

        folder_path = os.path.abspath(folder_path)
        if self.is_favorite_folder(folder_path):
            self.favorite_folders = [
                saved_path
                for saved_path in self.favorite_folders
                if os.path.abspath(saved_path) != folder_path
            ]
        else:
            self.favorite_folders.append(folder_path)

        self.refresh_library_tiles()
        self.select_library_folder(folder_path)
        self.save_state()

    def rename_library_folder(self, folder_path):
        # Renames a library folder and updates saved/pinned/favorite paths.
        if not folder_path or not os.path.isdir(folder_path):
            return

        current_name = os.path.basename(folder_path)
        new_name, accepted = QInputDialog.getText(
            self,
            "Rename Folder",
            "Folder name:",
            text=current_name,
        )
        if not accepted:
            return

        new_name = self.safe_folder_name(new_name)
        if not new_name:
            QMessageBox.warning(self, "Rename Folder", "Enter a valid folder name.")
            return

        new_folder_path = os.path.join(os.path.dirname(folder_path), new_name)
        if os.path.abspath(new_folder_path) == os.path.abspath(folder_path):
            return

        if os.path.exists(new_folder_path):
            QMessageBox.warning(self, "Rename Folder", "A folder with this name already exists.")
            return

        try:
            os.rename(folder_path, new_folder_path)
        except OSError as error:
            QMessageBox.critical(self, "Rename Folder", f"Could not rename folder:\n{error}")
            return

        self.remap_current_paths(folder_path, new_folder_path)
        self.favorite_folders = [
            new_folder_path
            if os.path.abspath(saved_path) == os.path.abspath(folder_path)
            else saved_path
            for saved_path in self.favorite_folders
        ]
        self.pinned_folders = [
            new_folder_path
            if os.path.abspath(saved_path) == os.path.abspath(folder_path)
            else saved_path
            for saved_path in self.pinned_folders
        ]
        self.selected_library_folder = new_folder_path
        self.refresh_library_tiles()
        self.save_state()

    def delete_library_folder(self, folder_path):
        # Deletes a library folder after user confirmation and cleans saved references.
        if not folder_path or not os.path.isdir(folder_path):
            return

        reply = QMessageBox.question(
            self,
            "Delete Folder",
            f"Delete '{os.path.basename(folder_path)}' and all files inside it?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        try:
            shutil.rmtree(folder_path)
        except OSError as error:
            QMessageBox.critical(self, "Delete Folder", f"Could not delete folder:\n{error}")
            return

        self.recent_files = [
            file_path
            for file_path in self.recent_files
            if not self.path_is_inside(file_path, folder_path)
        ]
        self.favorite_folders = [
            saved_path
            for saved_path in self.favorite_folders
            if not self.path_is_inside(saved_path, folder_path)
        ]
        self.pinned_folders = [
            saved_path
            for saved_path in self.pinned_folders
            if not self.path_is_inside(saved_path, folder_path)
        ]

        if self.path_is_inside(self.current_folder, folder_path):
            self.files = []
            self.current_folder = ""
            self.selected_library_folder = ""
            self.folder_label.setText("No folder selected")
            self.file_list.clear()

        self.refresh_library_tiles()
        self.save_state()

    def rename_library_file(self, file_path):
        # Renames a loose file shown on the home library page.
        if not file_path or not os.path.isfile(file_path):
            return

        current_name = os.path.basename(file_path)
        new_name, accepted = QInputDialog.getText(
            self,
            "Rename File",
            "File name:",
            text=current_name,
        )
        if not accepted:
            return

        new_name = self.safe_file_name(new_name)
        if not new_name:
            QMessageBox.warning(self, "Rename File", "Enter a valid file name.")
            return

        new_file_path = os.path.join(os.path.dirname(file_path), new_name)
        if os.path.abspath(new_file_path) == os.path.abspath(file_path):
            return

        if os.path.exists(new_file_path):
            QMessageBox.warning(self, "Rename File", "A file with this name already exists.")
            return

        try:
            os.rename(file_path, new_file_path)
        except OSError as error:
            QMessageBox.critical(self, "Rename File", f"Could not rename file:\n{error}")
            return

        self.recent_files = [
            new_file_path if saved_path == file_path else saved_path
            for saved_path in self.recent_files
        ]
        self.update_file_preview_tabs_after_rename(file_path, new_file_path)
        self.refresh_views_after_drop(os.path.dirname(new_file_path))
        self.save_state()

    def delete_library_file(self, file_path):
        # Deletes a loose file shown on the home library page.
        if not file_path or not os.path.isfile(file_path):
            return

        reply = QMessageBox.question(
            self,
            "Delete File",
            f"Delete '{os.path.basename(file_path)}'?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        try:
            os.remove(file_path)
        except OSError as error:
            QMessageBox.critical(self, "Delete File", f"Could not delete file:\n{error}")
            return

        self.recent_files = [
            saved_path for saved_path in self.recent_files if saved_path != file_path
        ]
        self.clear_file_preview_tabs_for_deleted_path(file_path)
        self.refresh_views_after_drop(os.path.dirname(file_path))
        self.save_state()

    def update_file_preview_tabs_after_rename(self, old_file_path, new_file_path):
        # Keeps any open preview tab pointed at a file after it is renamed from home.
        for index in range(self.tabs.count()):
            tab = self.tabs.widget(index)
            if not hasattr(tab, "preview_data"):
                continue

            if tab.preview_data.get("file_path") != old_file_path:
                continue

            self.render_file_tab(tab, new_file_path)
            if not hasattr(tab, "folder_tab_data"):
                self.tabs.setTabText(index, self.short_tab_title(new_file_path))
                self.tabs.setTabIcon(index, self.icon_for_file(new_file_path))
                self.tabs.setTabToolTip(index, new_file_path)

    def clear_file_preview_tabs_for_deleted_path(self, file_path):
        # Shows a friendly empty state in tabs that were previewing a deleted file.
        for index in range(self.tabs.count()):
            tab = self.tabs.widget(index)
            if not hasattr(tab, "preview_data"):
                continue

            if tab.preview_data.get("file_path") != file_path:
                continue

            tab.preview_data["player"].stop()
            tab.preview_data["file_path"] = None
            self.show_file_tab_message(tab, "File deleted")
            tab.preview_data["info_label"].setText("File deleted")
            if not hasattr(tab, "folder_tab_data"):
                self.tabs.setTabText(index, "Deleted file")
                self.tabs.setTabToolTip(index, "")

    def rename_file(self, file_path):
        # Renames a file in the currently opened folder.
        if not file_path or not os.path.isfile(file_path):
            return

        current_name = os.path.basename(file_path)
        new_name, accepted = QInputDialog.getText(
            self,
            "Rename File",
            "File name:",
            text=current_name,
        )
        if not accepted:
            return

        new_name = self.safe_file_name(new_name)
        if not new_name:
            QMessageBox.warning(self, "Rename File", "Enter a valid file name.")
            return

        new_file_path = os.path.join(os.path.dirname(file_path), new_name)
        if os.path.abspath(new_file_path) == os.path.abspath(file_path):
            return

        if os.path.exists(new_file_path):
            QMessageBox.warning(self, "Rename File", "A file with this name already exists.")
            return

        self.player.stop()

        try:
            os.rename(file_path, new_file_path)
        except OSError as error:
            QMessageBox.critical(self, "Rename File", f"Could not rename file:\n{error}")
            return

        self.recent_files = [
            new_file_path if path == file_path else path
            for path in self.recent_files
        ]
        self.current_file_path = new_file_path
        self.load_folder(self.current_folder)
        self.preview_file_path(new_file_path)
        self.select_file_list_item(new_file_path)
        self.refresh_library_tiles()
        self.save_state()

    def delete_file(self, file_path):
        # Deletes a file from the currently opened folder after confirmation.
        if not file_path or not os.path.isfile(file_path):
            return

        reply = QMessageBox.question(
            self,
            "Delete File",
            f"Delete '{os.path.basename(file_path)}'?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        self.player.stop()

        try:
            os.remove(file_path)
        except OSError as error:
            QMessageBox.critical(self, "Delete File", f"Could not delete file:\n{error}")
            return

        self.recent_files = [
            path for path in self.recent_files if path != file_path
        ]
        self.load_folder(self.current_folder)
        self.clear_preview("File deleted")
        self.refresh_library_tiles()
        self.save_state()

    def create_folder_in_current_location(self):
        # Creates a subfolder in the currently opened folder, or in the library root as fallback.
        target_parent = self.current_folder if self.current_folder else self.library_root
        if not os.path.isdir(target_parent):
            target_parent = self.library_root

        folder_name, accepted = QInputDialog.getText(
            self,
            "Create New Folder",
            "Folder name:",
        )
        if not accepted:
            return

        folder_name = self.safe_folder_name(folder_name)
        if not folder_name:
            QMessageBox.warning(self, "Create New Folder", "Enter a valid folder name.")
            return

        folder_path = os.path.join(target_parent, folder_name)
        if os.path.exists(folder_path):
            QMessageBox.warning(self, "Create New Folder", "A folder with this name already exists.")
            return

        try:
            os.makedirs(folder_path)
        except OSError as error:
            QMessageBox.critical(self, "Create New Folder", f"Could not create folder:\n{error}")
            return

        self.refresh_library_tiles()
        self.save_state()

    def open_library_tile(self, item):
        # Double-click handler for home/quick-access tiles.
        item_path = item.data(Qt.UserRole)
        if not item_path or not os.path.exists(item_path):
            return

        if os.path.isdir(item_path):
            self.open_library_folder(item_path, switch_to_preview=True)
        else:
            self.open_or_focus_file_tab(item_path)

    def preview_library_file_tile(self, item):
        # Single-clicking a loose file on the home page opens its preview tab.
        item_path = item.data(Qt.UserRole)
        if not item_path or not os.path.isfile(item_path):
            return

        if QApplication.mouseButtons() & Qt.RightButton:
            return

        modifiers = QApplication.keyboardModifiers()
        if modifiers & (Qt.ControlModifier | Qt.ShiftModifier):
            return

        self.open_or_focus_file_tab(item_path)

    def open_library_folder(self, folder_path, switch_to_preview=False):
        # Opens a library folder as its own closable folder tab.
        if not folder_path or not os.path.isdir(folder_path):
            return

        self.add_folder_tab(folder_path)

    def update_library_selection(self):
        # Tracks selected folders only; loose files should not become import targets.
        item = self.library_tiles.currentItem()
        item_path = item.data(Qt.UserRole) if item else ""
        self.selected_library_folder = item_path if item_path and os.path.isdir(item_path) else ""
        self.update_library_actions()

    def update_quick_access_selection(self):
        # Tracks selection from the quick access panel.
        item = self.quick_access_tiles.currentItem()
        if not item:
            return

        folder_path = item.data(Qt.UserRole)
        self.selected_library_folder = folder_path
        self.select_library_folder(folder_path)
        self.update_library_actions()

    def update_library_actions(self):
        # Enables import controls only when a valid target folder exists.
        has_selection = bool(self.selected_or_current_library_folder())
        self.btn_import_files.setEnabled(has_selection)

    def install_interaction_animations(self, root=None):
        # Gives buttons a subtle opacity pulse when clicked.
        root = root or self
        buttons = root.findChildren(QPushButton)
        if isinstance(root, QPushButton):
            buttons.append(root)

        for button in buttons:
            if button.property("interactionAnimated"):
                continue

            effect = QGraphicsOpacityEffect(button)
            effect.setOpacity(1.0)
            button.setGraphicsEffect(effect)
            button.setProperty("interactionAnimated", True)
            button.installEventFilter(self)

    def eventFilter(self, watched, event):
        # Small interaction animation for registered buttons.
        if isinstance(watched, QPushButton) and watched.property("interactionAnimated"):
            if event.type() == QEvent.MouseButtonPress and watched.isEnabled():
                self.animate_button_opacity(watched, 0.74, 70)
            elif event.type() in (QEvent.MouseButtonRelease, QEvent.Leave):
                self.animate_button_opacity(watched, 1.0, 150)

        return super().eventFilter(watched, event)

    def animate_button_opacity(self, button, opacity, duration):
        # Runs one opacity animation per button and keeps it referenced while active.
        effect = button.graphicsEffect()
        if not isinstance(effect, QGraphicsOpacityEffect):
            return

        key = id(button)
        old_animation = self.button_animations.get(key)
        if old_animation:
            old_animation.stop()

        animation = QPropertyAnimation(effect, b"opacity", self)
        animation.setDuration(duration)
        animation.setStartValue(effect.opacity())
        animation.setEndValue(opacity)
        animation.setEasingCurve(QEasingCurve.OutCubic)
        animation.finished.connect(lambda key=key: self.button_animations.pop(key, None))
        self.button_animations[key] = animation
        animation.start()

    def animate_tab_change(self, index):
        # Adds a small slide animation when switching tabs.
        if index < 0 or index == self.previous_tab_index:
            return

        current_widget = self.tabs.widget(index)
        if not current_widget:
            self.previous_tab_index = index
            return

        stack_widget = current_widget.parentWidget()
        if not stack_widget:
            self.previous_tab_index = index
            return

        direction = 1 if index > self.previous_tab_index else -1
        self.previous_tab_index = index

        width = stack_widget.width()
        if width <= 0:
            return

        end_position = current_widget.pos()
        start_position = QPoint(direction * width, end_position.y())

        current_widget.move(start_position)
        current_widget.raise_()

        animation = QPropertyAnimation(current_widget, b"pos", self)
        animation.setDuration(230)
        animation.setStartValue(start_position)
        animation.setEndValue(end_position)
        animation.setEasingCurve(QEasingCurve.OutCubic)
        animation.finished.connect(lambda: current_widget.move(end_position))

        self.tab_transition_animation = animation
        animation.start()

    def selected_or_current_library_folder(self):
        # Chooses the best current target folder for imports and folder creation.
        if self.selected_library_folder and os.path.isdir(self.selected_library_folder):
            return self.selected_library_folder

        if self.tabs.currentWidget() == self.home_tab:
            return self.library_root

        if self.is_library_folder(self.current_folder):
            return self.current_folder

        return ""

    def select_library_folder(self, folder_path):
        # Highlights a folder tile on the home page if it exists in the library.
        if not folder_path:
            self.update_library_actions()
            return

        for row in range(self.library_tiles.count()):
            item = self.library_tiles.item(row)
            if item.data(Qt.UserRole) == folder_path:
                self.library_tiles.setCurrentItem(item)
                self.selected_library_folder = folder_path
                self.update_library_actions()
                return

        self.update_library_actions()

    def is_library_folder(self, folder_path):
        # Checks whether a path belongs to the MediaVault library root.
        if not folder_path or not os.path.isdir(folder_path):
            return False

        try:
            library_root = os.path.abspath(self.library_root)
            folder_path = os.path.abspath(folder_path)
            return (
                folder_path != library_root
                and os.path.commonpath([library_root, folder_path]) == library_root
            )
        except ValueError:
            return False

    @staticmethod
    def safe_folder_name(folder_name):
        # Removes characters Windows does not allow in folder names.
        invalid_chars = '<>:"/\\|?*'
        cleaned = "".join("_" if char in invalid_chars else char for char in folder_name)
        return cleaned.strip().strip(".")

    @staticmethod
    def safe_file_name(file_name):
        # Removes characters Windows does not allow in file names.
        invalid_chars = '<>:"/\\|?*'
        cleaned = "".join("_" if char in invalid_chars else char for char in file_name)
        return cleaned.strip().strip(".")

    @staticmethod
    def unique_destination_path(folder_path, file_name):
        # Avoids overwriting files by adding " (1)", " (2)", etc.
        base_name, extension = os.path.splitext(file_name)
        destination_path = os.path.join(folder_path, file_name)
        counter = 1

        while os.path.exists(destination_path):
            destination_path = os.path.join(
                folder_path,
                f"{base_name} ({counter}){extension}",
            )
            counter += 1

        return destination_path

    @staticmethod
    def unique_destination_folder(parent_folder, folder_name):
        # Avoids overwriting imported folders by adding " (1)", " (2)", etc.
        folder_name = MediaVault.safe_folder_name(folder_name) or "Imported Folder"
        destination_folder = os.path.join(parent_folder, folder_name)
        counter = 1

        while os.path.exists(destination_folder):
            destination_folder = os.path.join(parent_folder, f"{folder_name} ({counter})")
            counter += 1

        return destination_folder

    @staticmethod
    def path_is_inside(path, folder_path):
        # Safe helper for checking if one path is inside another path.
        if not path or not folder_path:
            return False

        try:
            path = os.path.abspath(path)
            folder_path = os.path.abspath(folder_path)
            return os.path.commonpath([path, folder_path]) == folder_path
        except ValueError:
            return False

    def remap_current_paths(self, old_root, new_root):
        # Updates saved paths after a folder is renamed.
        def remap(path):
            if not self.path_is_inside(path, old_root):
                return path

            relative_path = os.path.relpath(path, old_root)
            return os.path.join(new_root, relative_path)

        self.current_folder = remap(self.current_folder)
        self.current_file_path = remap(self.current_file_path)
        self.current_image_path = remap(self.current_image_path)
        self.current_pdf_path = remap(self.current_pdf_path)
        self.recent_files = [remap(path) for path in self.recent_files]
        self.favorite_folders = [remap(path) for path in self.favorite_folders]
        self.pinned_folders = [remap(path) for path in self.pinned_folders]

    def open_folder(self):
        # Top toolbar Open Folder action; opens the folder as a closable tab.
        folder = QFileDialog.getExistingDirectory(self, "Select Folder")
        if not folder:
            return

        self.search_bar.clear()
        self.add_folder_tab(folder)

    def load_folder(self, folder, show_preview=True):
        # Legacy helper for the old main file list; it no longer creates a Preview tab.
        self.current_folder = folder
        self.files = []

        for root, _, file_names in os.walk(folder):
            for file_name in file_names:
                self.files.append(os.path.join(root, file_name))

        self.folder_label.setText(folder)
        self.refresh_list()

    def refresh_list(self):
        # Applies search and sort filters to the main Preview file list.
        keyword = self.search_bar.text().strip().lower()
        files = self.sorted_files(self.files)

        self.file_list.clear()

        for file_path in files:
            file_name = self.display_file_name(file_path)
            if keyword and keyword not in file_name.lower():
                continue
            self.add_file_item(file_path)

        if self.current_folder:
            count = self.file_list.count()
            self.selection_label.setText(f"{count} file{'s' if count != 1 else ''}")

    def sorted_files(self, files):
        # Returns files sorted by the option selected in the top-right sort box.
        option = self.sort_box.currentText()

        if option == "Sort by Type":
            return sorted(files, key=lambda path: (os.path.splitext(path)[1].lower(), os.path.basename(path).lower()))

        if option == "Sort by Size":
            return sorted(files, key=lambda path: os.path.getsize(path))

        return sorted(files, key=lambda path: os.path.basename(path).lower())

    def add_file_item(self, file_path):
        # Adds one file row to the main Preview file list.
        item = QListWidgetItem(self.display_file_name(file_path))
        item.setData(Qt.UserRole, file_path)
        item.setToolTip(file_path)
        item.setIcon(self.icon_for_file(file_path))
        self.file_list.addItem(item)

    def display_file_name(self, file_path):
        # Shows relative paths when viewing nested files from a folder.
        if self.current_folder and self.path_is_inside(file_path, self.current_folder):
            return os.path.relpath(file_path, self.current_folder)

        return os.path.basename(file_path)

    def select_home_file(self, file_path):
        # Highlights the matching home tile, falling back to the file's folder.
        for row in range(self.library_tiles.count()):
            item = self.library_tiles.item(row)
            if item.data(Qt.UserRole) == file_path:
                self.library_tiles.setCurrentItem(item)
                return

        self.select_library_folder(os.path.dirname(file_path))

    def select_file_list_item(self, file_path):
        # Selects a specific file row in the main Preview file list.
        for row in range(self.file_list.count()):
            item = self.file_list.item(row)
            if item.data(Qt.UserRole) == file_path:
                self.file_list.setCurrentItem(item)
                return

    def icon_for_file(self, file_path):
        # Picks an icon or thumbnail based on file type.
        lower_path = file_path.lower()
        extension = os.path.splitext(lower_path)[1]

        if lower_path.endswith(self.IMAGE_EXTENSIONS):
            return QIcon(file_path)

        if lower_path.endswith(self.VIDEO_EXTENSIONS):
            return self.video_thumbnail_icon(file_path)

        if lower_path.endswith(self.AUDIO_EXTENSIONS):
            return self.type_badge_icon("AUD", "#7c3aed")

        if lower_path.endswith(self.PDF_EXTENSIONS):
            return self.type_badge_icon("PDF", "#dc2626")

        if self.is_supported_archive(file_path):
            archive_label = "ZIP" if lower_path.endswith(".zip") else "ARC"
            return self.type_badge_icon(archive_label, "#ca8a04")

        if extension in (".doc", ".docx", ".odt", ".rtf"):
            return self.type_badge_icon("DOC", "#2563eb")

        if lower_path.endswith(self.EXCEL_EXTENSIONS) or extension in (".xls", ".ods", ".csv"):
            return self.type_badge_icon("XLS", "#16a34a")

        if lower_path.endswith(self.POWERPOINT_EXTENSIONS) or extension in (".ppt", ".odp"):
            return self.type_badge_icon("PPT", "#ea580c")

        if extension in (".py", ".js", ".css", ".html", ".htm"):
            return self.type_badge_icon("CODE", "#0891b2")

        if extension in (".json", ".xml", ".yaml", ".yml", ".ini"):
            return self.type_badge_icon("DATA", "#64748b")

        if lower_path.endswith(self.TEXT_EXTENSIONS):
            return self.type_badge_icon("TXT", "#475569")

        return self.style().standardIcon(QStyle.SP_FileIcon)

    def type_badge_icon(self, label, color):
        # Creates a clear colored file-type icon, such as PDF, DOC, TXT, or AUD.
        cache_key = ("type", label, color)
        if cache_key in self.icon_cache:
            return self.icon_cache[cache_key]

        size = 96
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)

        base_color = QColor(color)
        painter.setBrush(base_color)
        painter.drawRoundedRect(12, 8, 72, 80, 10, 10)

        fold_color = QColor(255, 255, 255, 70)
        painter.setBrush(fold_color)
        painter.drawPolygon(QPoint(62, 8), QPoint(84, 30), QPoint(62, 30))

        painter.setPen(QPen(QColor(255, 255, 255), 2))
        painter.drawLine(25, 38, 71, 38)
        painter.drawLine(25, 48, 71, 48)

        font = QFont("Segoe UI", 18)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(12, 54, 72, 28, Qt.AlignCenter, label)

        painter.end()
        icon = QIcon(pixmap)
        self.icon_cache[cache_key] = icon
        return icon

    def video_thumbnail_icon(self, file_path):
        # Uses OpenCV to capture a video frame; falls back to a video badge icon if unavailable.
        try:
            cache_key = (
                "video",
                file_path,
                os.path.getmtime(file_path),
                os.path.getsize(file_path),
            )
        except OSError:
            return self.video_badge_icon()

        if cache_key in self.icon_cache:
            return self.icon_cache[cache_key]

        icon = None
        if cv2 is not None:
            icon = self.capture_video_thumbnail_icon(file_path)

        if icon is None:
            icon = self.video_badge_icon()

        self.icon_cache[cache_key] = icon
        return icon

    def capture_video_thumbnail_icon(self, file_path):
        # Captures a frame from a video and draws a small play badge on top.
        capture = cv2.VideoCapture(file_path)
        if not capture.isOpened():
            capture.release()
            return None

        try:
            frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            if frame_count > 10:
                capture.set(cv2.CAP_PROP_POS_FRAMES, min(frame_count - 1, 24))

            ok, frame = capture.read()
            if not ok or frame is None:
                return None

            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            height, width, channels = rgb_frame.shape
            image = QImage(
                rgb_frame.data,
                width,
                height,
                channels * width,
                QImage.Format_RGB888,
            ).copy()
            pixmap = QPixmap.fromImage(image)
            pixmap = self.square_thumbnail_pixmap(pixmap, 96)
            return QIcon(self.add_play_badge(pixmap))
        except Exception:
            return None
        finally:
            capture.release()

    @staticmethod
    def square_thumbnail_pixmap(pixmap, size):
        # Crops a pixmap into a square thumbnail.
        scaled = pixmap.scaled(
            size,
            size,
            Qt.KeepAspectRatioByExpanding,
            Qt.SmoothTransformation,
        )
        x = max(0, (scaled.width() - size) // 2)
        y = max(0, (scaled.height() - size) // 2)
        return scaled.copy(x, y, size, size)

    def add_play_badge(self, pixmap):
        # Adds a play button overlay to a video thumbnail.
        result = QPixmap(pixmap)
        painter = QPainter(result)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 145))
        painter.drawEllipse(28, 28, 40, 40)
        painter.setBrush(QColor("#ffffff"))
        painter.drawPolygon(QPoint(43, 38), QPoint(43, 58), QPoint(58, 48))
        painter.end()
        return result

    def video_badge_icon(self):
        # Fallback video icon when a real thumbnail cannot be generated.
        cache_key = ("type", "VID", "#0f766e")
        if cache_key in self.icon_cache:
            return self.icon_cache[cache_key]

        icon = self.type_badge_icon("VID", "#0f766e")
        pixmap = icon.pixmap(96, 96)
        icon = QIcon(self.add_play_badge(pixmap))
        self.icon_cache[cache_key] = icon
        return icon

    def preview_file(self, item):
        # Handles clicking a file in the main Preview tab file list.
        file_path = item.data(Qt.UserRole)
        self.preview_file_path(file_path)
        self.select_home_file(file_path)

    def preview_file_path(self, file_path):
        # Main Preview dispatcher: chooses which preview method to use for a selected file.
        if not file_path or not os.path.exists(file_path):
            self.clear_preview("File not found")
            return

        self.current_file_path = file_path
        self.player.stop()
        self.current_image_path = None
        self.document_view.clear()
        self.clear_pdf_preview()
        self.enable_media_controls(False)
        self.enable_image_edit_controls(False)
        self.btn_open_file.setEnabled(True)

        if file_path not in self.recent_files:
            self.recent_files.append(file_path)

        lower_path = file_path.lower()
        file_name = os.path.basename(file_path)
        self.selection_label.setText(file_name)

        if lower_path.endswith(self.IMAGE_EXTENSIONS):
            self.show_image(file_path)
        elif lower_path.endswith(self.VIDEO_EXTENSIONS):
            self.show_video(file_path)
        elif lower_path.endswith(self.AUDIO_EXTENSIONS):
            self.show_audio(file_path)
        elif lower_path.endswith(self.PDF_EXTENSIONS):
            self.show_pdf(file_path)
        elif lower_path.endswith(self.WORD_EXTENSIONS):
            self.show_docx(file_path)
        elif lower_path.endswith(self.POWERPOINT_EXTENSIONS):
            self.show_pptx(file_path)
        elif lower_path.endswith(self.EXCEL_EXTENSIONS):
            self.show_xlsx(file_path)
        elif lower_path.endswith(self.TEXT_EXTENSIONS):
            self.show_text_file(file_path)
        elif self.is_supported_archive(file_path):
            self.show_message("Compressed file ready to extract")
        elif lower_path.endswith(self.EXTERNAL_DOCUMENT_EXTENSIONS):
            self.show_external_document(file_path)
        else:
            self.show_message("Preview not available for this file type")

        self.update_file_info(file_path)
        self.save_state()

    def show_image(self, file_path):
        # Displays an image in the main Preview tab and scales it to fit.
        self.current_image_path = file_path
        self.preview_stack.setCurrentWidget(self.preview_label)
        self.scale_current_image()
        self.enable_image_edit_controls(True)

    def show_video(self, file_path):
        # Loads video into QMediaPlayer and shows the video widget.
        self.preview_stack.setCurrentWidget(self.video_widget)
        self.player.setMedia(QMediaContent(QUrl.fromLocalFile(file_path)))
        self.enable_media_controls(True)
        if self.preferences["auto_play_media"]:
            self.player.play()

    def show_audio(self, file_path):
        # Loads audio into QMediaPlayer and shows a simple audio status message.
        self.preview_stack.setCurrentWidget(self.preview_label)
        status = "Playing audio" if self.preferences["auto_play_media"] else "Audio ready"
        self.show_message(status)
        self.player.setMedia(QMediaContent(QUrl.fromLocalFile(file_path)))
        self.enable_media_controls(True)
        if self.preferences["auto_play_media"]:
            self.player.play()

    def show_pdf(self, file_path):
        # Starts PDF preview using PyMuPDF if it is installed.
        if fitz is None:
            self.show_message(
                "PDF preview requires PyMuPDF.\n"
                "Install it with: pip install pymupdf\n"
                "Use Open File to view this PDF in your default app."
            )
            return

        try:
            self.render_pdf(file_path)
        except Exception:
            self.show_message(
                "Could not render this PDF preview.\n"
                "Use Open File to view it in your default app."
            )
            return

        self.preview_stack.setCurrentWidget(self.pdf_scroll)

    def render_pdf(self, file_path):
        # Prepares PDF state for the main Preview tab.
        self.clear_pdf_preview()

        with fitz.open(file_path) as document:
            if document.page_count == 0:
                raise RuntimeError("PDF has no pages")

            self.current_pdf_path = file_path
            self.current_pdf_page = 0
            self.current_pdf_page_count = document.page_count

        self.render_current_pdf_page()
        self.enable_pdf_controls(True)

    def render_current_pdf_page(self):
        # Renders the current PDF page into the main Preview tab.
        if not self.current_pdf_path or fitz is None:
            return

        with fitz.open(self.current_pdf_path) as document:
            page = document.load_page(self.current_pdf_page)
            matrix = fitz.Matrix(self.pdf_zoom, self.pdf_zoom)
            rendered_page = page.get_pixmap(matrix=matrix, alpha=False)

        pixmap = QPixmap()
        pixmap.loadFromData(rendered_page.tobytes("png"), "PNG")

        self.pdf_page_label.setPixmap(pixmap)
        self.pdf_page_label.setMinimumSize(pixmap.size())
        self.pdf_page_indicator.setText(
            f"Page {self.current_pdf_page + 1} / {self.current_pdf_page_count}"
        )

        self.pdf_scroll.verticalScrollBar().setValue(0)
        self.pdf_scroll.horizontalScrollBar().setValue(0)
        self.enable_pdf_controls(True)

    def clear_pdf_preview(self):
        # Resets PDF preview state and disables PDF controls.
        self.current_pdf_path = None
        self.current_pdf_page = 0
        self.current_pdf_page_count = 0
        self.pdf_page_label.clear()
        self.pdf_page_label.setMinimumSize(0, 0)
        self.pdf_page_indicator.setText("Page - / -")
        self.enable_pdf_controls(False)

    def show_docx(self, file_path):
        # Extracts readable text from DOCX files for a lightweight preview.
        try:
            text = self.extract_docx_text(file_path)
        except (KeyError, zipfile.BadZipFile, ET.ParseError):
            self.show_message(
                "Could not read this DOCX preview.\n"
                "Use Open File to view it in your default app."
            )
            return

        if not text.strip():
            self.show_message("This DOCX does not contain previewable text")
            return

        self.show_document_text(text)

    def show_text_file(self, file_path):
        # Reads text-like files and displays the first preview chunk.
        try:
            text = self.read_text_file(file_path)
        except UnicodeDecodeError:
            self.show_message(
                "Could not decode this text file.\n"
                "Use Open File to view it in your default app."
            )
            return

        self.show_document_text(text)

    def show_pptx(self, file_path):
        # Shows a lightweight PowerPoint slide-text preview.
        try:
            preview_html = self.extract_pptx_preview_html(file_path)
        except (KeyError, zipfile.BadZipFile, ET.ParseError):
            self.show_message(
                "Could not read this PowerPoint preview.\n"
                "Use Open File to view it in your default app."
            )
            return

        self.show_document_html(preview_html)

    def show_xlsx(self, file_path):
        # Shows a lightweight Excel workbook table preview.
        try:
            preview_html = self.extract_xlsx_preview_html(file_path)
        except (KeyError, zipfile.BadZipFile, ET.ParseError, ValueError):
            self.show_message(
                "Could not read this Excel preview.\n"
                "Use Open File to view it in your default app."
            )
            return

        self.show_document_html(preview_html)

    def show_external_document(self, file_path):
        # Shows a fallback message for document formats that need external apps.
        extension = os.path.splitext(file_path)[1].upper()
        self.show_message(
            f"{extension} files are best opened with their native app.\n"
            "Use Open File to view this document."
        )

    def show_document_text(self, text):
        # Displays plain text content in the document viewer.
        self.document_view.setPlainText(text)
        self.document_view.verticalScrollBar().setValue(0)
        self.preview_stack.setCurrentWidget(self.document_view)

    def show_document_html(self, preview_html):
        # Displays formatted document content in the main Preview document viewer.
        self.document_view.setHtml(preview_html)
        self.document_view.verticalScrollBar().setValue(0)
        self.preview_stack.setCurrentWidget(self.document_view)

    def show_message(self, text):
        # Displays a centered message in the main Preview area.
        self.preview_label.setPixmap(QPixmap())
        self.preview_label.setText(text)
        self.preview_stack.setCurrentWidget(self.preview_label)

    def clear_preview(self, text):
        # Clears the selected file and resets the main Preview panel.
        self.player.stop()
        self.current_file_path = None
        self.current_image_path = None
        self.document_view.clear()
        self.clear_pdf_preview()
        self.enable_media_controls(False)
        self.enable_image_edit_controls(False)
        self.btn_open_file.setEnabled(False)
        self.show_message(text)
        self.selection_label.setText("Select a file")
        self.file_info.setText("File Info: No file selected")

    def stop_media(self):
        # Stops audio/video playback in the main Preview tab.
        self.player.stop()

    def enable_media_controls(self, enabled):
        # Enables/disables video/audio player controls in the main Preview tab.
        for widget in (
            self.btn_play,
            self.media_slider,
            self.media_time_label,
            self.volume_slider,
            self.volume_label,
        ):
            widget.setVisible(enabled)

        self.btn_play.setEnabled(enabled)
        self.media_slider.setEnabled(enabled)
        self.volume_slider.setEnabled(enabled)
        if not enabled:
            self.btn_play.setText("Play")
            self.btn_play.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
            self.media_slider.setRange(0, 0)
            self.media_slider.setValue(0)
            self.media_time_label.setText("0:00 / 0:00")
            self.media_slider_pressed = False

    def enable_image_edit_controls(self, enabled):
        # Enables/disables image editing controls in the main Preview tab.
        self.btn_crop_image.setVisible(enabled)
        self.btn_crop_image.setEnabled(enabled)

    def update_main_volume(self, value):
        # Updates audio/video volume in the main Preview tab.
        self.player.setVolume(value)
        self.volume_label.setText(f"{value}%")

    def toggle_main_media(self):
        # Toggles play/pause in the legacy main Preview tab.
        if not self.btn_play.isEnabled():
            return

        if self.player.state() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def update_main_media_button(self, state):
        # Updates the main media button when playback starts or pauses.
        if state == QMediaPlayer.PlayingState:
            self.btn_play.setText("Pause")
            self.btn_play.setIcon(self.style().standardIcon(QStyle.SP_MediaPause))
        else:
            self.btn_play.setText("Play")
            self.btn_play.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))

    def update_main_media_duration(self, duration):
        # Sets the main media seek bar range.
        self.media_slider.setRange(0, max(0, duration))
        self.update_main_media_time(self.player.position())

    def update_main_media_position(self, position):
        # Moves the main media seek bar while video/audio plays.
        if not self.media_slider_pressed:
            self.media_slider.setValue(position)
        self.update_main_media_time(position)

    def start_main_media_scrub(self):
        # Marks the main seek bar as being dragged.
        self.media_slider_pressed = True

    def preview_main_media_scrub(self, position):
        # Updates the main time label while dragging the seek bar.
        self.update_main_media_time(position)

    def finish_main_media_scrub(self):
        # Seeks main media to the slider position.
        self.media_slider_pressed = False
        self.player.setPosition(self.media_slider.value())

    def update_main_media_time(self, position):
        # Shows elapsed and total time in the main Preview tab.
        duration = self.media_slider.maximum()
        self.media_time_label.setText(
            f"{self.format_media_time(position)} / {self.format_media_time(duration)}"
        )

    def enable_pdf_controls(self, enabled):
        # Enables/disables PDF page and zoom buttons.
        has_pdf = enabled and self.current_pdf_page_count > 0
        self.btn_pdf_prev.setEnabled(has_pdf and self.current_pdf_page > 0)
        self.btn_pdf_next.setEnabled(
            has_pdf and self.current_pdf_page < self.current_pdf_page_count - 1
        )
        self.btn_pdf_zoom_out.setEnabled(has_pdf and self.pdf_zoom > 0.8)
        self.btn_pdf_zoom_in.setEnabled(has_pdf and self.pdf_zoom < 3.0)

    def show_previous_pdf_page(self):
        # Goes to the previous PDF page in the main Preview tab.
        if self.current_pdf_page > 0:
            self.current_pdf_page -= 1
            self.render_current_pdf_page()

    def show_next_pdf_page(self):
        # Goes to the next PDF page in the main Preview tab.
        if self.current_pdf_page < self.current_pdf_page_count - 1:
            self.current_pdf_page += 1
            self.render_current_pdf_page()

    def zoom_pdf_out(self):
        # Zooms out the main PDF preview.
        if self.current_pdf_path:
            self.pdf_zoom = max(0.8, self.pdf_zoom - 0.2)
            self.render_current_pdf_page()

    def zoom_pdf_in(self):
        # Zooms in the main PDF preview.
        if self.current_pdf_path:
            self.pdf_zoom = min(3.0, self.pdf_zoom + 0.2)
            self.render_current_pdf_page()

    def open_current_file(self):
        # Opens the selected main-preview file in the system default app.
        if self.current_file_path and os.path.exists(self.current_file_path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.current_file_path))

    def crop_file_tab_image(self, tab):
        # Opens the crop dialog for the image currently shown in a dynamic tab.
        self.crop_image_file(tab.preview_data.get("file_path"), tab)

    def crop_image_file(self, file_path, tab=None):
        # Crops an image and saves the edited result.
        if not file_path or not os.path.isfile(file_path):
            return

        if not file_path.lower().endswith(self.IMAGE_EXTENSIONS):
            QMessageBox.information(self, "Crop Image", "Select an image file to crop.")
            return

        crop_dialog = ImageCropDialog(file_path, self)
        self.install_interaction_animations(crop_dialog)
        if crop_dialog.original_pixmap.isNull():
            QMessageBox.warning(self, "Crop Image", "Unable to load this image.")
            return

        if crop_dialog.exec_() != QDialog.Accepted:
            return

        cropped_pixmap = crop_dialog.cropped_pixmap()
        if cropped_pixmap.isNull():
            QMessageBox.warning(self, "Crop Image", "Could not crop this image.")
            return

        if crop_dialog.save_mode == "overwrite":
            reply = QMessageBox.question(
                self,
                "Overwrite Image",
                f"Overwrite '{os.path.basename(file_path)}' with the cropped image?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            destination_path = file_path
        else:
            destination_path, _ = QFileDialog.getSaveFileName(
                self,
                "Save Cropped Image",
                self.default_cropped_image_path(file_path),
                "Images (*.png *.jpg *.jpeg *.bmp *.webp);;All Files (*)",
            )
            if not destination_path:
                return

        if not os.path.splitext(destination_path)[1]:
            destination_path += ".png"

        if not cropped_pixmap.save(destination_path):
            QMessageBox.critical(self, "Crop Image", "Could not save the cropped image.")
            return

        self.refresh_views_after_drop(os.path.dirname(destination_path))
        if os.path.abspath(destination_path) == os.path.abspath(file_path):
            self.refresh_image_previews(file_path, tab)
        else:
            self.open_or_focus_file_tab(destination_path)

    @staticmethod
    def default_cropped_image_path(file_path):
        # Creates the default "name cropped.ext" path.
        folder = os.path.dirname(file_path)
        base_name, extension = os.path.splitext(os.path.basename(file_path))
        extension = extension or ".png"
        return os.path.join(folder, f"{base_name} cropped{extension}")

    def refresh_image_previews(self, file_path, active_tab=None):
        # Re-renders image previews after the original file is overwritten.
        if active_tab and hasattr(active_tab, "preview_data"):
            self.render_file_tab(active_tab, file_path)

        for index in range(self.tabs.count()):
            tab = self.tabs.widget(index)
            if tab is active_tab or not hasattr(tab, "preview_data"):
                continue

            if tab.preview_data.get("file_path") == file_path:
                self.render_file_tab(tab, file_path)

        if self.current_file_path == file_path:
            self.preview_file_path(file_path)

    @staticmethod
    def file_type_label(file_path):
        # Converts ".png" into "PNG" for the file info box.
        extension = os.path.splitext(file_path)[1].upper().replace(".", "")
        return extension if extension else "FILE"

    def update_file_info(self, file_path):
        # Updates the file info box below the main Preview panel.
        file_type = self.file_type_label(file_path)
        self.file_info.setText(
            f"Name: {os.path.basename(file_path)}\n"
            f"Type: {file_type}\n"
            f"Size: {self.format_size(os.path.getsize(file_path))}\n"
            f"Path: {file_path}"
        )

    @staticmethod
    def extract_docx_text(file_path):
        # Minimal DOCX reader: extracts paragraph text from word/document.xml.
        text_tag = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
        tab_tag = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}tab"
        break_tag = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}br"
        paragraph_tag = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"

        with zipfile.ZipFile(file_path) as docx:
            xml = docx.read("word/document.xml")

        root = ET.fromstring(xml)
        paragraphs = []

        for paragraph in root.iter(paragraph_tag):
            pieces = []
            for node in paragraph.iter():
                if node.tag == text_tag and node.text:
                    pieces.append(node.text)
                elif node.tag == tab_tag:
                    pieces.append("\t")
                elif node.tag == break_tag:
                    pieces.append("\n")

            text = "".join(pieces).strip()
            if text:
                paragraphs.append(text)

        return "\n\n".join(paragraphs)

    def extract_pptx_preview_html(self, file_path):
        # Minimal PPTX reader: extracts readable text from each slide XML file.
        slides = []

        with zipfile.ZipFile(file_path) as pptx:
            slide_files = [
                name for name in pptx.namelist()
                if name.startswith("ppt/slides/slide") and name.endswith(".xml")
            ]
            slide_files.sort(key=self.office_part_number)

            for slide_index, slide_file in enumerate(slide_files, start=1):
                root = ET.fromstring(pptx.read(slide_file))
                paragraphs = self.extract_office_paragraphs(root)
                slides.append((slide_index, paragraphs))

        if not slides:
            return self.office_preview_html(
                "PowerPoint Preview",
                "<p>No previewable slides found.</p>",
            )

        slide_cards = []
        for slide_index, paragraphs in slides:
            if paragraphs:
                content = "".join(
                    f"<p>{html.escape(paragraph)}</p>"
                    for paragraph in paragraphs
                )
            else:
                content = "<p class='muted'>No readable text on this slide.</p>"

            slide_cards.append(
                "<section class='office-card'>"
                f"<h2>Slide {slide_index}</h2>"
                f"{content}"
                "</section>"
            )

        return self.office_preview_html("PowerPoint Preview", "".join(slide_cards))

    def extract_xlsx_preview_html(self, file_path):
        # Minimal XLSX reader: extracts visible cell values from the first few sheets.
        sheet_limit = 5
        row_limit = 40
        column_limit = 12

        with zipfile.ZipFile(file_path) as workbook:
            shared_strings = self.read_xlsx_shared_strings(workbook)
            sheets = self.read_xlsx_sheet_references(workbook)

            if not sheets:
                sheets = [
                    (f"Sheet {index + 1}", name)
                    for index, name in enumerate(
                        sorted(
                            (
                                item for item in workbook.namelist()
                                if item.startswith("xl/worksheets/sheet")
                                and item.endswith(".xml")
                            ),
                            key=self.office_part_number,
                        )
                    )
                ]

            sheet_cards = []
            for sheet_name, sheet_path in sheets[:sheet_limit]:
                if sheet_path not in workbook.namelist():
                    continue

                rows = self.read_xlsx_sheet_rows(
                    workbook,
                    sheet_path,
                    shared_strings,
                    row_limit,
                    column_limit,
                )
                sheet_cards.append(
                    self.xlsx_sheet_html(sheet_name, rows, row_limit, column_limit)
                )

        if not sheet_cards:
            return self.office_preview_html(
                "Excel Preview",
                "<p>No previewable sheets found.</p>",
            )

        return self.office_preview_html("Excel Preview", "".join(sheet_cards))

    def office_preview_html(self, title, body_html):
        # Wraps Office previews with compact styling for QTextEdit.
        font_css = self.css_font_family(self.preferences["font_family"])
        return f"""
        <html>
        <head>
            <style>
                body {{
                    color: #1f232b;
                    font-family: {font_css};
                    font-size: 13px;
                    line-height: 1.45;
                }}
                h1 {{
                    margin: 0 0 14px 0;
                    font-size: 22px;
                }}
                h2 {{
                    margin: 0 0 10px 0;
                    font-size: 16px;
                }}
                p {{
                    margin: 4px 0;
                }}
                table {{
                    border-collapse: collapse;
                    margin-top: 8px;
                    width: 100%;
                }}
                th, td {{
                    border: 1px solid #ccd3dc;
                    padding: 5px 7px;
                    vertical-align: top;
                }}
                th {{
                    background-color: #e6edf5;
                    font-weight: 600;
                }}
                .office-card {{
                    background-color: #ffffff;
                    border: 1px solid #d8dee8;
                    border-radius: 8px;
                    margin: 0 0 14px 0;
                    padding: 14px;
                }}
                .muted {{
                    color: #6b7280;
                }}
            </style>
        </head>
        <body>
            <h1>{html.escape(title)}</h1>
            {body_html}
        </body>
        </html>
        """

    @staticmethod
    def office_part_number(path):
        # Natural sort helper for slide1.xml, slide2.xml, sheet10.xml, etc.
        digits = "".join(character for character in os.path.basename(path) if character.isdigit())
        return int(digits) if digits else 0

    @staticmethod
    def xml_local_name(tag):
        # Removes an XML namespace from a tag name.
        return tag.rsplit("}", 1)[-1] if "}" in tag else tag

    def extract_office_paragraphs(self, root):
        # Extracts paragraph-like text runs from Word/PowerPoint-style XML.
        paragraphs = []

        for paragraph in root.iter():
            if self.xml_local_name(paragraph.tag) != "p":
                continue

            pieces = [
                node.text or ""
                for node in paragraph.iter()
                if self.xml_local_name(node.tag) == "t" and node.text
            ]
            text = "".join(pieces).strip()
            if text:
                paragraphs.append(text)

        if paragraphs:
            return paragraphs

        fallback_text = [
            node.text or ""
            for node in root.iter()
            if self.xml_local_name(node.tag) == "t" and node.text
        ]
        text = " ".join(piece.strip() for piece in fallback_text if piece.strip())
        return [text] if text else []

    def read_xlsx_shared_strings(self, workbook):
        # Reads the shared string table used by many XLSX cell values.
        if "xl/sharedStrings.xml" not in workbook.namelist():
            return []

        root = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
        strings = []

        for item in root:
            if self.xml_local_name(item.tag) != "si":
                continue

            pieces = [
                node.text or ""
                for node in item.iter()
                if self.xml_local_name(node.tag) == "t" and node.text
            ]
            strings.append("".join(pieces))

        return strings

    def read_xlsx_sheet_references(self, workbook):
        # Maps workbook sheet names to their worksheet XML paths.
        if (
            "xl/workbook.xml" not in workbook.namelist()
            or "xl/_rels/workbook.xml.rels" not in workbook.namelist()
        ):
            return []

        relationships = {}
        rels_root = ET.fromstring(workbook.read("xl/_rels/workbook.xml.rels"))
        for relationship in rels_root:
            if self.xml_local_name(relationship.tag) != "Relationship":
                continue
            rel_id = relationship.attrib.get("Id")
            target = relationship.attrib.get("Target")
            if rel_id and target:
                relationships[rel_id] = self.normalize_xlsx_part_path(target)

        workbook_root = ET.fromstring(workbook.read("xl/workbook.xml"))
        sheets = []
        relationship_key = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"

        for sheet in workbook_root.iter():
            if self.xml_local_name(sheet.tag) != "sheet":
                continue

            sheet_name = sheet.attrib.get("name", "Sheet")
            rel_id = sheet.attrib.get(relationship_key)
            sheet_path = relationships.get(rel_id)
            if sheet_path:
                sheets.append((sheet_name, sheet_path))

        return sheets

    @staticmethod
    def normalize_xlsx_part_path(target):
        # Converts workbook relationship targets into ZIP paths.
        target = target.replace("\\", "/")
        if target.startswith("/"):
            return target.lstrip("/")
        if target.startswith("xl/"):
            return target
        return posixpath.normpath(posixpath.join("xl", target))

    def read_xlsx_sheet_rows(self, workbook, sheet_path, shared_strings, row_limit, column_limit):
        # Reads cell values from one worksheet XML file.
        root = ET.fromstring(workbook.read(sheet_path))
        rows = []

        for row in root.iter():
            if self.xml_local_name(row.tag) != "row":
                continue

            row_number = row.attrib.get("r", str(len(rows) + 1))
            cells = {}
            fallback_column = 1

            for cell in row:
                if self.xml_local_name(cell.tag) != "c":
                    continue

                column_index = self.xlsx_cell_column(cell.attrib.get("r", "")) or fallback_column
                fallback_column = column_index + 1
                if column_index > column_limit:
                    continue

                value = self.xlsx_cell_value(cell, shared_strings)
                if value:
                    cells[column_index] = value

            if cells:
                rows.append((row_number, cells))
                if len(rows) >= row_limit:
                    break

        return rows

    def xlsx_cell_value(self, cell, shared_strings):
        # Converts one XLSX cell node into display text.
        cell_type = cell.attrib.get("t", "")

        if cell_type == "inlineStr":
            pieces = [
                node.text or ""
                for node in cell.iter()
                if self.xml_local_name(node.tag) == "t" and node.text
            ]
            return "".join(pieces)

        value_node = None
        for child in cell:
            if self.xml_local_name(child.tag) == "v":
                value_node = child
                break

        raw_value = value_node.text if value_node is not None and value_node.text else ""
        if not raw_value:
            return ""

        if cell_type == "s":
            try:
                return shared_strings[int(raw_value)]
            except (IndexError, ValueError):
                return raw_value

        if cell_type == "b":
            return "TRUE" if raw_value == "1" else "FALSE"

        return raw_value

    @staticmethod
    def xlsx_cell_column(cell_reference):
        # Converts "C12" into 3.
        letters = "".join(character for character in cell_reference if character.isalpha()).upper()
        column = 0
        for letter in letters:
            column = column * 26 + (ord(letter) - ord("A") + 1)
        return column

    @staticmethod
    def xlsx_column_name(index):
        # Converts 3 into "C".
        name = ""
        while index > 0:
            index, remainder = divmod(index - 1, 26)
            name = chr(ord("A") + remainder) + name
        return name

    def xlsx_sheet_html(self, sheet_name, rows, row_limit, column_limit):
        # Builds an HTML table for one Excel sheet preview.
        if not rows:
            return (
                "<section class='office-card'>"
                f"<h2>{html.escape(sheet_name)}</h2>"
                "<p class='muted'>No previewable cells found.</p>"
                "</section>"
            )

        max_column = min(
            column_limit,
            max(max(cells.keys()) for _, cells in rows),
        )
        headers = "<th></th>" + "".join(
            f"<th>{self.xlsx_column_name(column)}</th>"
            for column in range(1, max_column + 1)
        )
        body_rows = []

        for row_number, cells in rows:
            values = [
                f"<td>{html.escape(cells.get(column, ''))}</td>"
                for column in range(1, max_column + 1)
            ]
            body_rows.append(
                f"<tr><th>{html.escape(str(row_number))}</th>{''.join(values)}</tr>"
            )

        return (
            "<section class='office-card'>"
            f"<h2>{html.escape(sheet_name)}</h2>"
            "<table>"
            f"<thead><tr>{headers}</tr></thead>"
            f"<tbody>{''.join(body_rows)}</tbody>"
            "</table>"
            f"<p class='muted'>Showing up to {row_limit} rows and {column_limit} columns.</p>"
            "</section>"
        )

    @staticmethod
    def read_text_file(file_path):
        # Reads text files with a few common encodings and limits huge previews.
        last_error = None
        preview_limit = 500000

        for encoding in ("utf-8-sig", "utf-8", "cp1252"):
            try:
                with open(file_path, "r", encoding=encoding) as text_file:
                    text = text_file.read(preview_limit)
                    if text_file.read(1):
                        text += "\n\n[Preview truncated because the file is large.]"
                    return text
            except UnicodeDecodeError as error:
                last_error = error

        raise last_error

    def scale_current_image(self):
        # Resizes the selected image preview when the window size changes.
        if not self.current_image_path:
            return

        pixmap = QPixmap(self.current_image_path)
        if pixmap.isNull():
            self.show_message("Unable to load image")
            return

        available_size = self.preview_stack.size() - QSize(28, 28)
        scaled = pixmap.scaled(
            available_size,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self.preview_label.setText("")
        self.preview_label.setPixmap(scaled)

    def resizeEvent(self, event):
        # Qt event: keep image previews fitted after resizing the app window.
        super().resizeEvent(event)
        self.scale_current_image()

    def closeEvent(self, event):
        # Qt event: save state before the app closes.
        self.save_state()
        super().closeEvent(event)

    def showEvent(self, event):
        # Qt event: reapply native Windows title-bar color once the window handle exists.
        super().showEvent(event)
        self.apply_native_title_bar_colors(getattr(self, "current_theme_colors", None))

    @staticmethod
    def format_media_time(milliseconds):
        # Converts media time from milliseconds into m:ss or h:mm:ss.
        seconds = max(0, int(milliseconds / 1000))
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        seconds = seconds % 60

        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"

        return f"{minutes}:{seconds:02d}"

    @staticmethod
    def format_size(size_bytes):
        # Converts bytes into B, KB, MB, GB, or TB for display.
        units = ["B", "KB", "MB", "GB", "TB"]
        size = float(size_bytes)

        for unit in units:
            if size < 1024 or unit == units[-1]:
                return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
            size /= 1024

        return f"{size_bytes} B"

    @staticmethod
    def css_font_family(font_family):
        # Escapes the selected font family for use inside Qt stylesheets.
        safe_font = str(font_family).replace('"', "").strip() or "Segoe UI"
        return f'"{safe_font}", "Segoe UI", Arial, sans-serif'

    @staticmethod
    def hex_to_colorref(hex_color):
        # Windows DWM expects COLORREF values as 0x00BBGGRR.
        color = QColor(hex_color)
        return color.red() | (color.green() << 8) | (color.blue() << 16)

    def apply_native_title_bar_colors(self, colors):
        # Qt stylesheets cannot color the real Windows title bar; DWM can on supported Windows builds.
        if sys.platform != "win32" or not colors:
            return

        try:
            hwnd = ctypes.c_void_p(int(self.winId()))
            dark_mode = ctypes.c_int(1 if self.preferences["theme"] == "Dark" else 0)

            for attribute in (20, 19):
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd,
                    ctypes.c_uint(attribute),
                    ctypes.byref(dark_mode),
                    ctypes.sizeof(dark_mode),
                )

            caption_color = ctypes.c_uint(self.hex_to_colorref(colors["panel"]))
            text_color = ctypes.c_uint(self.hex_to_colorref(colors["title"]))

            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd,
                ctypes.c_uint(35),
                ctypes.byref(caption_color),
                ctypes.sizeof(caption_color),
            )
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd,
                ctypes.c_uint(36),
                ctypes.byref(text_color),
                ctypes.sizeof(text_color),
            )
        except (AttributeError, OSError, ValueError):
            pass

    def apply_style(self):
        # Builds the stylesheet for dark/light mode and the selected font size.
        font_size = self.preferences["font_size"]
        font_css = self.css_font_family(self.preferences["font_family"])

        if self.preferences["theme"] == "Light":
            colors = {
                "bg": "#f7f3ec",
                "panel": "#fffdf8",
                "surface": "#fbf7ef",
                "surface_alt": "#f1ebe1",
                "border": "#ddd4c7",
                "border_soft": "#cfc4b5",
                "text": "#2a2723",
                "title": "#161412",
                "muted": "#766f65",
                "control": "#f3eee6",
                "control_hover": "#e9e1d7",
                "control_pressed": "#ded4c7",
                "disabled_bg": "#eee7dc",
                "disabled_text": "#a49a8c",
                "accent": "#14b8a6",
                "accent_hover": "#0f9f90",
                "accent_border": "#2dd4bf",
                "accent_soft": "#dff8f3",
                "accent_deep": "#0f766e",
                "selection_text": "#ffffff",
                "preview_border": "#cfc4b5",
                "pdf_bg": "#eee8df",
                "doc_bg": "#fffaf0",
                "doc_text": "#2a2723",
                "header_bg": "#ece5db",
                "list_item_bg": "#fffdf8",
            }
        else:
            colors = {
                "bg": "#0f1011",
                "panel": "#171819",
                "surface": "#1d1f21",
                "surface_alt": "#242629",
                "border": "#303236",
                "border_soft": "#3b3d42",
                "text": "#ececea",
                "title": "#ffffff",
                "muted": "#a4a3a0",
                "control": "#242629",
                "control_hover": "#303236",
                "control_pressed": "#1a1b1d",
                "disabled_bg": "#202124",
                "disabled_text": "#70706d",
                "accent": "#22c7b8",
                "accent_hover": "#18ad9f",
                "accent_border": "#5eead4",
                "accent_soft": "#13312f",
                "accent_deep": "#0f766e",
                "selection_text": "#061211",
                "preview_border": "#3b3d42",
                "pdf_bg": "#151617",
                "doc_bg": "#f6f2e8",
                "doc_text": "#1f232b",
                "header_bg": "#1b1c1e",
                "list_item_bg": "#1b1c1e",
            }

        self.current_theme_colors = colors
        self.setStyleSheet(f"""
            QWidget {{
                background-color: {colors["bg"]};
                color: {colors["text"]};
                font-family: {font_css};
                font-size: {font_size}px;
                selection-background-color: {colors["accent"]};
                selection-color: {colors["selection_text"]};
            }}

            QWidget:disabled {{
                color: {colors["disabled_text"]};
            }}

            QFrame#TopBar {{
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 {colors["panel"]}, stop:1 {colors["surface_alt"]});
                border: 1px solid {colors["border"]};
                border-radius: 8px;
            }}

            QLabel#AppTitle {{
                background-color: transparent;
                color: {colors["title"]};
                font-size: {font_size + 7}px;
                font-weight: 800;
            }}

            QLabel#AppSubtitle {{
                background-color: transparent;
                color: {colors["muted"]};
                font-size: {max(11, font_size - 3)}px;
                font-weight: 500;
            }}

            QFrame#Panel {{
                background-color: {colors["panel"]};
                border: 1px solid {colors["border"]};
                border-radius: 8px;
            }}

            QFrame#InnerPanel {{
                background-color: {colors["surface_alt"]};
                border: 1px solid {colors["border"]};
                border-radius: 8px;
            }}

            QTabWidget#MainTabs::pane {{
                border: 0;
                border-top: 1px solid {colors["border"]};
                padding-top: 10px;
            }}

            QTabBar#ExplorerTabs {{
                background-color: {colors["bg"]};
            }}

            QTabBar#ExplorerTabs::tab {{
                background-color: {colors["header_bg"]};
                border: 1px solid {colors["border"]};
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
                border-bottom-left-radius: 0;
                border-bottom-right-radius: 0;
                color: {colors["muted"]};
                font-weight: 600;
                min-width: 128px;
                margin-right: 4px;
                margin-top: 4px;
                padding: 9px 16px 10px 14px;
            }}

            QTabBar#ExplorerTabs::tab:selected {{
                background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 {colors["surface_alt"]}, stop:1 {colors["panel"]});
                border: 1px solid {colors["border"]};
                border-top: 2px solid {colors["accent_border"]};
                border-bottom-color: {colors["panel"]};
                color: {colors["title"]};
                margin-top: 0;
                padding-top: 11px;
                padding-bottom: 11px;
            }}

            QTabBar#ExplorerTabs::tab:hover:!selected {{
                background-color: {colors["control_hover"]};
                color: {colors["title"]};
            }}

            QWidget#AddTabCorner {{
                background-color: {colors["bg"]};
            }}

            QLabel#PanelTitle {{
                background-color: transparent;
                color: {colors["title"]};
                font-size: {font_size + 3}px;
                font-weight: 600;
            }}

            QLabel#MutedLabel {{
                background-color: transparent;
                color: {colors["muted"]};
                font-size: {max(10, font_size - 2)}px;
            }}

            QLabel#PreviewLabel {{
                background-color: {colors["surface"]};
                border: 1px dashed {colors["preview_border"]};
                border-radius: 8px;
                color: {colors["muted"]};
                padding: 22px;
            }}

            QStackedWidget#PreviewStack {{
                background-color: {colors["surface"]};
                border: 1px solid {colors["border"]};
                border-radius: 8px;
            }}

            QLabel#InfoBox {{
                background-color: {colors["surface"]};
                border: 1px solid {colors["border"]};
                border-radius: 8px;
                color: {colors["text"]};
                padding: 12px;
            }}

            QTextEdit#DocumentView {{
                background-color: {colors["doc_bg"]};
                border: 1px solid {colors["border"]};
                border-radius: 8px;
                color: {colors["doc_text"]};
                font-family: {font_css};
                font-size: {max(11, font_size - 1)}px;
                padding: 18px;
                selection-background-color: #8fb4ff;
            }}

            QScrollArea#PdfScrollArea {{
                background-color: {colors["pdf_bg"]};
                border: 1px solid {colors["border"]};
                border-radius: 8px;
            }}

            QWidget#PdfPages {{
                background-color: {colors["pdf_bg"]};
            }}

            QLabel#PdfPage {{
                background-color: #ffffff;
                border: 1px solid #cfd5df;
                border-radius: 3px;
                padding: 0;
            }}

            QLabel#PdfNotice {{
                background-color: {colors["surface"]};
                border: 1px solid {colors["preview_border"]};
                border-radius: 8px;
                color: {colors["text"]};
                padding: 10px;
            }}

            QPushButton {{
                background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 {colors["control_hover"]}, stop:1 {colors["control"]});
                border: 1px solid {colors["border_soft"]};
                border-radius: 7px;
                color: {colors["text"]};
                font-weight: 600;
                padding: 8px 13px;
            }}

            QPushButton:hover {{
                background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 {colors["accent_soft"]}, stop:1 {colors["control_hover"]});
                border-color: {colors["accent_border"]};
            }}

            QPushButton:pressed {{
                background-color: {colors["control_pressed"]};
            }}

            QPushButton:focus {{
                border: 1px solid {colors["accent_border"]};
            }}

            QPushButton:disabled {{
                background-color: {colors["disabled_bg"]};
                color: {colors["disabled_text"]};
                border-color: {colors["border"]};
            }}

            QPushButton#PrimaryButton {{
                background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 {colors["accent_border"]}, stop:1 {colors["accent"]});
                border-color: {colors["accent_border"]};
                color: {colors["selection_text"]};
            }}

            QPushButton#PrimaryButton:hover {{
                background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 {colors["accent_border"]}, stop:1 {colors["accent_hover"]});
                border-color: {colors["accent_border"]};
            }}

            QPushButton#PrimaryButton:pressed {{
                background-color: {colors["accent_deep"]};
            }}

            QPushButton#PrimaryButton:disabled {{
                background-color: {colors["disabled_bg"]};
                border-color: {colors["border"]};
                color: {colors["disabled_text"]};
            }}

            QPushButton#ToolbarButton {{
                background-color: {colors["surface_alt"]};
                border-color: {colors["border"]};
            }}

            QPushButton#AddTabButton {{
                background-color: {colors["surface_alt"]};
                border: 1px solid {colors["border_soft"]};
                border-radius: 8px;
                color: {colors["title"]};
                font-size: {font_size + 8}px;
                font-weight: 700;
                padding: 0;
            }}

            QPushButton#AddTabButton:hover {{
                background-color: {colors["accent_soft"]};
                border-color: {colors["accent_border"]};
                color: {colors["title"]};
            }}

            QPushButton#AddTabButton:pressed {{
                background-color: {colors["control_pressed"]};
            }}

            QLineEdit,
            QComboBox,
            QSpinBox,
            QDoubleSpinBox {{
                background-color: {colors["surface"]};
                border: 1px solid {colors["border_soft"]};
                border-radius: 7px;
                color: {colors["text"]};
                padding: 8px 11px;
            }}

            QLineEdit#SearchBar {{
                background-color: {colors["surface"]};
                border-color: {colors["border"]};
                padding-left: 14px;
            }}

            QLineEdit:focus,
            QComboBox:focus,
            QSpinBox:focus,
            QDoubleSpinBox:focus {{
                border-color: {colors["accent_border"]};
                background-color: {colors["panel"]};
            }}

            QLineEdit:hover,
            QComboBox:hover,
            QSpinBox:hover,
            QDoubleSpinBox:hover {{
                border-color: {colors["accent_border"]};
            }}

            QComboBox::drop-down {{
                border: 0;
                width: 28px;
            }}

            QComboBox QAbstractItemView {{
                background-color: {colors["panel"]};
                border: 1px solid {colors["border"]};
                border-radius: 7px;
                color: {colors["text"]};
                padding: 6px;
                selection-background-color: {colors["accent"]};
                selection-color: {colors["selection_text"]};
            }}

            QToolTip {{
                background-color: {colors["panel"]};
                border: 1px solid {colors["border_soft"]};
                border-radius: 6px;
                color: {colors["text"]};
                padding: 6px 8px;
            }}

            QCheckBox {{
                color: {colors["text"]};
                spacing: 8px;
            }}

            QCheckBox::indicator {{
                background-color: {colors["surface"]};
                border: 1px solid {colors["border_soft"]};
                border-radius: 4px;
                height: 17px;
                width: 17px;
            }}

            QCheckBox::indicator:checked {{
                background-color: {colors["accent"]};
                border-color: {colors["accent_border"]};
            }}

            QSlider::groove:horizontal {{
                background-color: {colors["border"]};
                border-radius: 3px;
                height: 6px;
            }}

            QSlider::sub-page:horizontal {{
                background-color: {colors["accent"]};
                border-radius: 3px;
            }}

            QSlider::handle:horizontal {{
                background-color: {colors["panel"]};
                border: 2px solid {colors["accent_border"]};
                border-radius: 7px;
                height: 14px;
                margin: -5px 0;
                width: 14px;
            }}

            QSlider::handle:horizontal:hover {{
                background-color: #ffffff;
            }}

            QSlider:disabled::groove:horizontal {{
                background-color: {colors["disabled_bg"]};
            }}

            QSlider:disabled::handle:horizontal {{
                background-color: {colors["disabled_text"]};
                border-color: {colors["border"]};
            }}

            QListWidget {{
                background-color: {colors["surface"]};
                border: 1px solid {colors["border"]};
                border-radius: 8px;
                outline: none;
                padding: 8px;
            }}

            QListWidget::item {{
                background-color: {colors["list_item_bg"]};
                border: 1px solid transparent;
                border-radius: 7px;
                padding: 10px;
            }}

            QListWidget::item:alternate {{
                background-color: {colors["list_item_bg"]};
            }}

            QListWidget::item:selected {{
                background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 {colors["accent_border"]}, stop:1 {colors["accent"]});
                border-color: {colors["accent_border"]};
                color: {colors["selection_text"]};
            }}

            QListWidget::item:hover {{
                background-color: {colors["accent_soft"]};
                border-color: {colors["border_soft"]};
            }}

            QListWidget#FolderTileView {{
                background-color: {colors["surface"]};
                border: 1px solid {colors["border"]};
                border-radius: 8px;
                padding: 16px;
            }}

            QListWidget#FolderTileView::item {{
                background-color: {colors["panel"]};
                border: 1px solid {colors["border"]};
                border-radius: 8px;
                margin: 4px;
                padding: 14px;
            }}

            QListWidget#FolderTileView::item:selected {{
                background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 {colors["accent_border"]}, stop:1 {colors["accent"]});
                border-color: {colors["accent_border"]};
                color: {colors["selection_text"]};
            }}

            QListWidget#FolderTileView::item:hover {{
                background-color: {colors["accent_soft"]};
                border-color: {colors["accent_border"]};
            }}

            QListWidget#FolderContentView {{
                background-color: {colors["surface"]};
                border: 1px solid {colors["border"]};
                border-radius: 8px;
                padding: 14px;
            }}

            QListWidget#FolderContentView::item {{
                background-color: {colors["panel"]};
                border: 1px solid {colors["border"]};
                border-radius: 8px;
                margin: 4px;
                padding: 12px;
            }}

            QListWidget#FolderContentView::item:selected {{
                background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 {colors["accent_border"]}, stop:1 {colors["accent"]});
                border-color: {colors["accent_border"]};
                color: {colors["selection_text"]};
            }}

            QListWidget#FolderContentView::item:hover {{
                background-color: {colors["accent_soft"]};
                border-color: {colors["accent_border"]};
            }}

            QListWidget#QuickAccessView {{
                background-color: {colors["surface"]};
                border: 1px solid {colors["border"]};
                border-radius: 8px;
                padding: 10px;
            }}

            QListWidget#QuickAccessView::item {{
                background-color: {colors["panel"]};
                border: 1px solid {colors["border"]};
                border-radius: 8px;
                margin: 3px;
                padding: 10px;
            }}

            QListWidget#QuickAccessView::item:selected {{
                background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 {colors["accent_border"]}, stop:1 {colors["accent"]});
                border-color: {colors["accent_border"]};
                color: {colors["selection_text"]};
            }}

            QListWidget#QuickAccessView::item:hover {{
                background-color: {colors["accent_soft"]};
                border-color: {colors["accent_border"]};
            }}

            QMenu {{
                background-color: {colors["panel"]};
                border: 1px solid {colors["border"]};
                border-radius: 8px;
                color: {colors["text"]};
                padding: 8px;
            }}

            QMenu::item {{
                border-radius: 6px;
                padding: 8px 24px 8px 12px;
            }}

            QMenu::item:selected {{
                background-color: {colors["accent_soft"]};
                color: {colors["title"]};
            }}

            QMenu::item:pressed {{
                background-color: {colors["accent"]};
                color: {colors["selection_text"]};
            }}

            QMenu::separator {{
                background-color: {colors["border"]};
                height: 1px;
                margin: 6px 8px;
            }}

            QProgressBar {{
                background-color: {colors["surface"]};
                border: 1px solid {colors["border"]};
                border-radius: 7px;
                color: {colors["text"]};
                min-height: 18px;
                text-align: center;
            }}

            QProgressBar::chunk {{
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 {colors["accent_deep"]}, stop:1 {colors["accent_border"]});
                border-radius: 6px;
            }}

            QDialog,
            QMessageBox,
            QInputDialog,
            QProgressDialog {{
                background-color: {colors["panel"]};
                color: {colors["text"]};
            }}

            QDialog QLabel,
            QMessageBox QLabel,
            QInputDialog QLabel,
            QProgressDialog QLabel {{
                background-color: transparent;
                color: {colors["text"]};
            }}

            QDialog QLineEdit,
            QInputDialog QLineEdit {{
                background-color: {colors["surface"]};
                border: 1px solid {colors["border_soft"]};
                border-radius: 7px;
                padding: 8px 11px;
            }}

            QScrollBar:vertical {{
                background-color: transparent;
                width: 10px;
                margin: 2px;
            }}

            QScrollBar::handle:vertical {{
                background-color: {colors["border_soft"]};
                border-radius: 5px;
                min-height: 28px;
            }}

            QScrollBar::handle:vertical:hover {{
                background-color: {colors["muted"]};
            }}

            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {{
                height: 0;
            }}

            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical {{
                background-color: transparent;
            }}

            QScrollBar:horizontal {{
                background-color: transparent;
                height: 10px;
                margin: 2px;
            }}

            QScrollBar::handle:horizontal {{
                background-color: {colors["border_soft"]};
                border-radius: 5px;
                min-width: 28px;
            }}

            QScrollBar::handle:horizontal:hover {{
                background-color: {colors["muted"]};
            }}

            QScrollBar::add-line:horizontal,
            QScrollBar::sub-line:horizontal {{
                width: 0;
            }}

            QScrollBar::add-page:horizontal,
            QScrollBar::sub-page:horizontal {{
                background-color: transparent;
            }}

            QSplitter::handle {{
                background-color: transparent;
            }}

            QSplitter::handle:hover {{
                background-color: {colors["accent_soft"]};
            }}
        """)
        self.apply_native_title_bar_colors(colors)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MediaVault()
    app.setWindowIcon(window.windowIcon())
    window.show()
    sys.exit(app.exec_())
