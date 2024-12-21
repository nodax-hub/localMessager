# Название проекта
TEMPLATE = app
TARGET = MyQtApp
CONFIG += console c++17
CONFIG -= app_bundle
QT += core network websockets zeroconf

# Файлы проекта
SOURCES += \
    main.cpp

HEADERS += \
    ServicePublisher.h \
    DeviceDiscovery.h \
    WebSocketServer.h \
    WebSocketClient.h \
    Coordinator.h

# Поддержка кроссплатформенности
QT += gui
android {
    CONFIG += mobility
    MOBILITY = networkinfo
}

# Кросс-компиляция для Android и Windows
android: {
    ANDROID_PACKAGE_SOURCE_DIR = $$PWD/android
    ANDROID_TARGET_ARCH = armeabi-v7a
}

win32: {
    LIBS += -lws2_32
}

# Путь к библиотекам (при необходимости)
INCLUDEPATH += $$PWD/includes
LIBS += -L$$PWD/libs

