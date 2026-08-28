import sys, os, time
sys.path.insert(0, "src")
from PySide6.QtWidgets import QApplication
from PySide6.QtQml import QQmlComponent, QQmlEngine
app = QApplication(sys.argv)
engine = QQmlEngine()
component = QQmlComponent(engine)
component.setData(b"""
import QtQuick 2.15
import QtQuick.Controls 2.15
ApplicationWindow {
    visible: true
    width: 400; height: 300
    title: "unified test"
    color: "#1c1c1e"
}
""")
win = component.create()
app.processEvents()
time.sleep(0.6)
app.processEvents()
import ctypes, ctypes.util
objcruntime = ctypes.util.find_library("objc")
objc = ctypes.cdll.LoadLibrary(objcruntime)
objc.objc_msgSend.restype = ctypes.c_void_p
objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
selr = objc.sel_registerName
selr.restype = ctypes.c_void_p
selr.argtypes = [ctypes.c_char_p]
def sel(n): return selr(n.encode())
hw = win.windowHandle()
print("winId:", hex(hw.winId()))
view = ctypes.c_void_p(hw.winId())
nswin = objc.objc_msgSend(view, sel("window"))
print("NSWindow ptr:", nswin)
if nswin:
    objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulonglong]
    style = objc.objc_msgSend(nswin, sel("styleMask"))
    print("styleMask BEFORE:", style, "hasFullSize:", bool(style & (1 << 14)))
    objc.objc_msgSend(nswin, sel("setStyleMask:"), style | (1 << 14))
    objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool]
    objc.objc_msgSend(nswin, sel("setTitlebarAppearsTransparent:"), True)
    objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
    objc.objc_msgSend(nswin, sel("setTitleVisibility:"), 1)
    objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    style2 = objc.objc_msgSend(nswin, sel("styleMask"))
    print("styleMask AFTER:", style2, "hasFullSize:", bool(style2 & (1 << 14)))
app.quit()
