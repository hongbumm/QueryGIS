# -*- coding: utf-8 -*-
# Form implementation generated from reading ui file 'dockwidget.ui'
# Created by: PyQt5 UI code generator 5.15.11

from PyQt5 import QtCore, QtGui, QtWidgets

class Ui_DockWidget(object):
    def setupUi(self, DockWidget):
        DockWidget.setObjectName("DockWidget")
        DockWidget.resize(516, 955)
        self.dockWidgetContents = QtWidgets.QWidget()
        self.dockWidgetContents.setMaximumSize(QtCore.QSize(1300, 16777215))
        self.dockWidgetContents.setStyleSheet("background-color: rgb(255, 255, 255);")
        self.dockWidgetContents.setObjectName("dockWidgetContents")
        self.mainLayout = QtWidgets.QVBoxLayout(self.dockWidgetContents)
        self.mainLayout.setObjectName("mainLayout")

        # --- Header ---
        self.topHeaderLayout = QtWidgets.QGridLayout()
        # To strictly center the logo/title, we use 3 columns
        self.topHeaderLayout.setColumnStretch(0, 1) # Left expanding
        self.topHeaderLayout.setColumnStretch(1, 0) # Center fixed
        self.topHeaderLayout.setColumnStretch(2, 1) # Right expanding
        self.mainLayout.addLayout(self.topHeaderLayout)

        self.headerLayout = QtWidgets.QVBoxLayout()
        self.headerLayout.setObjectName("headerLayout")
        self.icon_img = QtWidgets.QLabel(self.dockWidgetContents)
        sizePolicy = QtWidgets.QSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
        sizePolicy.setHorizontalStretch(50)
        sizePolicy.setVerticalStretch(50)
        self.icon_img.setSizePolicy(sizePolicy)
        self.icon_img.setMinimumSize(QtCore.QSize(50, 50))
        self.icon_img.setStyleSheet("border-image: url(:/plugins/query_gis/icon.png);")
        self.icon_img.setText("")
        self.icon_img.setObjectName("icon_img")
        self.headerLayout.addWidget(self.icon_img, 0, QtCore.Qt.AlignHCenter)

        self.label_title = QtWidgets.QLabel(self.dockWidgetContents)
        font = QtGui.QFont()
        font.setFamily("Arial Narrow")
        font.setPointSize(12)
        self.label_title.setFont(font)
        self.label_title.setAlignment(QtCore.Qt.AlignCenter)
        self.label_title.setObjectName("label_title")
        self.headerLayout.addWidget(self.label_title, 0, QtCore.Qt.AlignHCenter)
        # Add center block to column 1
        self.topHeaderLayout.addLayout(self.headerLayout, 0, 1, QtCore.Qt.AlignCenter)

        # Right block with Google Login button and Welcome message
        self.rightTopLayout = QtWidgets.QVBoxLayout()
        self.rightTopLayout.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignTop)
        self.rightTopLayout.setSpacing(2)

        self.label_welcome_user = QtWidgets.QLabel(self.dockWidgetContents)
        font = QtGui.QFont()
        font.setFamily("Arial Narrow")
        font.setPointSize(9)
        self.label_welcome_user.setFont(font)
        self.label_welcome_user.setStyleSheet("color: #666; margin-bottom: 2px;")
        self.label_welcome_user.setText("") # Hidden initially
        self.label_welcome_user.setObjectName("label_welcome_user")
        self.rightTopLayout.addWidget(self.label_welcome_user, 0, QtCore.Qt.AlignRight)

        self.btn_google_login = QtWidgets.QPushButton(self.dockWidgetContents)
        font = QtGui.QFont()
        font.setFamily("Arial Narrow")
        font.setBold(True)
        self.btn_google_login.setFont(font)
        self.btn_google_login.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
        self.btn_google_login.setStyleSheet("""
            QPushButton {
                background-color: #ffffff;
                border: 1px solid #dcdcdc;
                color: #444;
                border-radius: 4px;
                padding: 4px 10px;
                font-size: 11px;
            }
            QPushButton:hover {
                background-color: #f8f8f8;
            }
        """)
        self.btn_google_login.setObjectName("btn_google_login")
        self.rightTopLayout.addWidget(self.btn_google_login, 0, QtCore.Qt.AlignRight)

        # Align the whole right layout to the top-right
        self.topHeaderLayout.addLayout(self.rightTopLayout, 0, 2, QtCore.Qt.AlignRight | QtCore.Qt.AlignTop)

        # --- API ---
        self.apiLayout = QtWidgets.QHBoxLayout()
        self.apiLayout.setObjectName("apiLayout")
        self.label_api = QtWidgets.QLabel(self.dockWidgetContents)
        font = QtGui.QFont()
        font.setFamily("Arial Narrow")
        font.setBold(True)
        font.setWeight(75)
        self.label_api.setFont(font)
        self.label_api.setObjectName("label_api")
        self.apiLayout.addWidget(self.label_api)
        self.line_apikey = QtWidgets.QLineEdit(self.dockWidgetContents)
        self.line_apikey.setObjectName("line_apikey")
        self.apiLayout.addWidget(self.line_apikey)
        self.mainLayout.addLayout(self.apiLayout)

        # --- Chat Scroll Area ---
        self.chatScrollArea = QtWidgets.QScrollArea(self.dockWidgetContents)
        self.chatScrollArea.setStyleSheet(
            "QScrollBar:vertical {border:none; background:transparent; width:10px; margin:0px;}"
            "QScrollBar::handle:vertical {background:#A9A9A9; min-height:20px; border-radius:5px;}"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {background:none; height:0px;}"
        )
        self.chatScrollArea.setWidgetResizable(True)
        self.chatScrollArea.setObjectName("chatScrollArea")
        self.chatWidget = QtWidgets.QWidget()
        self.chatWidget.setGeometry(QtCore.QRect(0, 0, 492, 530))
        self.chatWidget.setObjectName("chatWidget")
        self.chatLayout = QtWidgets.QVBoxLayout(self.chatWidget)
        self.chatLayout.setObjectName("chatLayout")
        spacerItem = QtWidgets.QSpacerItem(20, 20, QtWidgets.QSizePolicy.Minimum, QtWidgets.QSizePolicy.Expanding)
        self.chatLayout.addItem(spacerItem)
        self.chatScrollArea.setWidget(self.chatWidget)
        self.mainLayout.addWidget(self.chatScrollArea)

        # --- Layer List (New) ---
        self.layerHeaderLayout = QtWidgets.QHBoxLayout()
        self.label_layers = QtWidgets.QLabel(self.dockWidgetContents)
        font = QtGui.QFont()
        font.setFamily("Arial Narrow")
        font.setBold(True)
        self.label_layers.setFont(font)
        self.label_layers.setText("Layers in Project:")
        self.layerHeaderLayout.addWidget(self.label_layers)
        self.mainLayout.addLayout(self.layerHeaderLayout)

        self.layerScrollArea = QtWidgets.QScrollArea(self.dockWidgetContents)
        self.layerScrollArea.setMinimumSize(QtCore.QSize(0, 90))
        self.layerScrollArea.setMaximumSize(QtCore.QSize(16777215, 90))
        self.layerScrollArea.setStyleSheet(
            "QScrollArea { border: 1px solid #D9D9D9; border-radius: 5px; background-color: #F9F9F9; }"
            "QScrollBar:vertical { border: none; background: transparent; width: 6px; }"
            "QScrollBar::handle:vertical { background: #C1C1C1; border-radius: 3px; }"
        )
        self.layerScrollArea.setWidgetResizable(True)
        self.layerListWidget = QtWidgets.QWidget()
        self.layerListLayout = QtWidgets.QVBoxLayout(self.layerListWidget)
        self.layerListLayout.setContentsMargins(5, 5, 5, 5)
        self.layerListLayout.setSpacing(4)
        self.layerListLayout.addStretch()
        self.layerScrollArea.setWidget(self.layerListWidget)
        self.mainLayout.addWidget(self.layerScrollArea)

        # --- Input ---
        self.inputLayout = QtWidgets.QHBoxLayout()
        self.inputLayout.setObjectName("inputLayout")
        self.text_query = QtWidgets.QTextEdit(self.dockWidgetContents)
        sizePolicy = QtWidgets.QSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.text_query.setSizePolicy(sizePolicy)
        self.text_query.setMinimumSize(QtCore.QSize(0, 100))
        font = QtGui.QFont()
        font.setFamily("맑은 고딕")
        font.setPointSize(10)
        self.text_query.setFont(font)
        self.text_query.setStyleSheet("border-radius: 7px; border: 2px solid #D9D9D9; padding: 5px;")
        self.text_query.setAcceptRichText(False)
        self.text_query.setObjectName("text_query")
        self.inputLayout.addWidget(self.text_query)

        self.btn_ask = QtWidgets.QPushButton(self.dockWidgetContents)
        sizePolicy = QtWidgets.QSizePolicy(QtWidgets.QSizePolicy.Minimum, QtWidgets.QSizePolicy.Minimum)
        self.btn_ask.setSizePolicy(sizePolicy)
        font = QtGui.QFont()
        font.setFamily("Arial Narrow")
        self.btn_ask.setFont(font)
        self.btn_ask.setStyleSheet("border-color: rgb(213, 213, 213);")
        self.btn_ask.setObjectName("btn_ask")
        self.inputLayout.addWidget(self.btn_ask)
        self.mainLayout.addLayout(self.inputLayout)

        # --- Options ---
        self.optionsLayout = QtWidgets.QHBoxLayout()
        self.optionsLayout.setObjectName("optionsLayout")
        self.chk_ask_run = QtWidgets.QCheckBox(self.dockWidgetContents)
        self.chk_ask_run.setObjectName("chk_ask_run")
        self.optionsLayout.addWidget(self.chk_ask_run)
        self.mainLayout.addLayout(self.optionsLayout)

        # --- Progress Bar (initially hidden) ---
        self.progressBar = QtWidgets.QProgressBar(self.dockWidgetContents)
        self.progressBar.setMinimum(0)
        self.progressBar.setMaximum(100)
        self.progressBar.setValue(0)
        self.progressBar.setTextVisible(True)
        self.progressBar.setStyleSheet("""
        QProgressBar {
        border: 1px solid #C9C9C9;
        border-radius: 4px;
        background: #E9E9E9;
        text-align: center;
        height: 16px;
        font: 10pt 'Arial';
        }
        QProgressBar::chunk {
        background: #2FA7FF;
        }
        """)
        self.progressBar.setVisible(False)
        self.mainLayout.addWidget(self.progressBar)
        # --- ETA Label ---
        self.etaLabel = QtWidgets.QLabel(self.dockWidgetContents)
        self.etaLabel.setAlignment(QtCore.Qt.AlignHCenter)
        self.etaLabel.setStyleSheet("color:#6b6b6b; font-style: italic;")
        self.etaLabel.setText("") 
        self.etaLabel.setVisible(False)
        self.mainLayout.addWidget(self.etaLabel)

        # --- Status Label ---
        self.status_label = QtWidgets.QLabel(self.dockWidgetContents)
        self.status_label.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Minimum)
        self.status_label.setMaximumSize(QtCore.QSize(16777215, 600))
        font = QtGui.QFont()
        font.setFamily("Cascadia Code")
        font.setItalic(True)
        self.status_label.setFont(font)
        self.status_label.setAlignment(QtCore.Qt.AlignCenter)
        self.status_label.setObjectName("status_label")
        self.mainLayout.addWidget(self.status_label)

        DockWidget.setWidget(self.dockWidgetContents)
        self.retranslateUi(DockWidget)
        QtCore.QMetaObject.connectSlotsByName(DockWidget)

    def retranslateUi(self, DockWidget):
        _translate = QtCore.QCoreApplication.translate
        DockWidget.setWindowTitle(_translate("DockWidget", "QueryGIS"))
        self.label_title.setText(_translate("DockWidget", "QueryGIS"))
        self.label_api.setText(_translate("DockWidget", "API KEY"))
        self.btn_ask.setText(_translate("DockWidget", "Ask\n(Ctrl + Enter)"))
        self.chk_ask_run.setText(_translate("DockWidget", "Ask and Run"))
        self.status_label.setText(_translate("DockWidget", "Status: Ready"))
        self.btn_google_login.setText(_translate("DockWidget", "Google Login"))

from . import resources
