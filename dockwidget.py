# -*- coding: utf-8 -*-

# Form implementation generated from reading ui file 'dockwidget.ui'
#
# Created by: PyQt5 UI code generator 5.15.11
#
# WARNING: Any manual changes made to this file will be lost when pyuic5 is
# run again.  Do not edit this file unless you know what you are doing.


from PyQt5 import QtCore, QtGui, QtWidgets


class Ui_DockWidget(object):
    def setupUi(self, DockWidget):
        DockWidget.setObjectName("DockWidget")
        DockWidget.resize(473, 1110)
        self.dockWidgetContents = QtWidgets.QWidget()
        self.dockWidgetContents.setStyleSheet("background-color: rgb(255, 255, 255);")
        self.dockWidgetContents.setObjectName("dockWidgetContents")
        self.mainLayout = QtWidgets.QVBoxLayout(self.dockWidgetContents)
        self.mainLayout.setObjectName("mainLayout")
        self.headerLayout = QtWidgets.QVBoxLayout()
        self.headerLayout.setObjectName("headerLayout")
        self.icon_img = QtWidgets.QLabel(self.dockWidgetContents)
        sizePolicy = QtWidgets.QSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
        sizePolicy.setHorizontalStretch(50)
        sizePolicy.setVerticalStretch(50)
        sizePolicy.setHeightForWidth(self.icon_img.sizePolicy().hasHeightForWidth())
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
        self.mainLayout.addLayout(self.headerLayout)
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
        self.chatScrollArea = QtWidgets.QScrollArea(self.dockWidgetContents)
        self.chatScrollArea.setStyleSheet("\n"
"QScrollBar:vertical {\n"
"    border: none;\n"
"    background: transparent;\n"
"    width: 10px;\n"
"    margin: 0px;\n"
"}\n"
"QScrollBar::handle:vertical {\n"
"    background: #A9A9A9;\n"
"    min-height: 20px;\n"
"    border-radius: 5px;\n"
"}\n"
"QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {\n"
"    background: none;\n"
"    height: 0px;\n"
"}\n"
"       ")
        self.chatScrollArea.setWidgetResizable(True)
        self.chatScrollArea.setObjectName("chatScrollArea")
        self.chatWidget = QtWidgets.QWidget()
        self.chatWidget.setGeometry(QtCore.QRect(0, 0, 449, 676))
        self.chatWidget.setObjectName("chatWidget")
        self.chatLayout = QtWidgets.QVBoxLayout(self.chatWidget)
        self.chatLayout.setObjectName("chatLayout")
        spacerItem = QtWidgets.QSpacerItem(20, 20, QtWidgets.QSizePolicy.Minimum, QtWidgets.QSizePolicy.Expanding)
        self.chatLayout.addItem(spacerItem)
        self.chatScrollArea.setWidget(self.chatWidget)
        self.mainLayout.addWidget(self.chatScrollArea)
        self.inputLayout = QtWidgets.QHBoxLayout()
        self.inputLayout.setObjectName("inputLayout")
        self.text_query = QtWidgets.QTextEdit(self.dockWidgetContents)
        sizePolicy = QtWidgets.QSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        sizePolicy.setHorizontalStretch(0)
        sizePolicy.setVerticalStretch(0)
        sizePolicy.setHeightForWidth(self.text_query.sizePolicy().hasHeightForWidth())
        self.text_query.setSizePolicy(sizePolicy)
        self.text_query.setMinimumSize(QtCore.QSize(0, 150))
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
        sizePolicy.setHorizontalStretch(0)
        sizePolicy.setVerticalStretch(0)
        sizePolicy.setHeightForWidth(self.btn_ask.sizePolicy().hasHeightForWidth())
        self.btn_ask.setSizePolicy(sizePolicy)
        font = QtGui.QFont()
        font.setFamily("Arial Narrow")
        font.setBold(False)
        font.setWeight(50)
        self.btn_ask.setFont(font)
        self.btn_ask.setStyleSheet("border-color: rgb(213, 213, 213);")
        self.btn_ask.setObjectName("btn_ask")
        self.inputLayout.addWidget(self.btn_ask)
        self.mainLayout.addLayout(self.inputLayout)
        self.optionsLayout = QtWidgets.QHBoxLayout()
        self.optionsLayout.setObjectName("optionsLayout")
        self.chk_ask_run = QtWidgets.QCheckBox(self.dockWidgetContents)
        font = QtGui.QFont()
        font.setFamily("Arial Narrow")
        self.chk_ask_run.setFont(font)
        self.chk_ask_run.setObjectName("chk_ask_run")
        self.optionsLayout.addWidget(self.chk_ask_run)
        self.chk_reason = QtWidgets.QCheckBox(self.dockWidgetContents)
        font = QtGui.QFont()
        font.setFamily("Arial Narrow")
        self.chk_reason.setFont(font)
        self.chk_reason.setObjectName("chk_reason")
        self.optionsLayout.addWidget(self.chk_reason)
        self.chk_rag = QtWidgets.QCheckBox(self.dockWidgetContents)
        font = QtGui.QFont()
        font.setFamily("Arial Narrow")
        self.chk_rag.setFont(font)
        self.chk_rag.setObjectName("chk_rag")
        self.optionsLayout.addWidget(self.chk_rag)
        self.mainLayout.addLayout(self.optionsLayout)
        self.status_label = QtWidgets.QLabel(self.dockWidgetContents)
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
        self.btn_ask.setText(_translate("DockWidget", "Ask\n"
"(Ctrl + Enter)"))
        self.chk_ask_run.setText(_translate("DockWidget", "Ask and Run"))
        self.chk_reason.setText(_translate("DockWidget", "Reasoning (Better, Slower)"))
        self.chk_rag.setText(_translate("DockWidget", "RAG(Better, Slower)"))
        self.status_label.setText(_translate("DockWidget", "Status: Waiting for your next command."))
from . import resources
