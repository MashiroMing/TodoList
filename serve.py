# -*- coding: utf-8 -*-
"""
TODO 小组件 · 桌面启动器（托盘版）
====================================
职责：
  1. 在 127.0.0.1:8765 启动本地静态服务（保证数据用 http 协议存储，可靠持久化）
  2. 以 Edge/Chrome 应用模式打开独立小窗口（无浏览器界面，像原生应用）
  3. 系统托盘驻留：窗口不占任务栏，托盘图标双击切换显示/隐藏，右键菜单退出
  4. 支持 --install 一键创建「桌面快捷方式 + 开机启动项」

用法：
  python serve.py            启动服务 + 打开窗口 + 托盘驻留
  python serve.py --install  安装桌面快捷方式与开机启动项（运行一次即可）
  python serve.py --debug    前台运行，打印日志（调试用）

托盘交互：
  - 双击托盘图标：显示 / 隐藏小组件窗口
  - 右键托盘图标 → 退出：彻底退出（含后台服务）
  - 窗口显示时不占用任务栏（工具窗口样式）
"""
import base64
import ctypes
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from ctypes import wintypes
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

# pythonw（无控制台）运行时 stdout/stderr 为 None，print 会崩溃，重定向到空设备
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")

PORT = 8765
BASE = os.path.dirname(os.path.abspath(__file__))
URL = "http://127.0.0.1:{0}/todo-widget.html".format(PORT)
BACKUP_FILE = os.path.join(BASE, "widget-backup.json")  # WebView2 迁移用一次性备份
# 日常持久化数据文件（serve.py 侧兜底）。
# 背景：pywebview 6.2.1 + WebView2 沙盒下 localStorage 跨 reload/重启不可靠，
# 因此页面每次保存都会同步一份 {state, ui, images} 到这里，启动时作为权威数据源恢复。
# 日常持久化数据文件（serve.py 侧兜底），拆分存储（性能优化）：
#   widget-state.json    —— {version, state, ui}，仅几 KB，每次保存全量重写开销可忽略
#   widget-images.json   —— {imageId: dataUrl}，只在图片增/删时写入，避免大文件反复重写
# 旧版单文件 widget-data.json 在首次读取时自动迁移拆分（见 _ensure_migrated）。
DATA_FILE = os.path.join(BASE, "widget-data.json")     # 旧版单文件（迁移源，迁移后归档 .migrated）
STATE_FILE = os.path.join(BASE, "widget-state.json")
IMAGES_FILE = os.path.join(BASE, "widget-images.json")
# 数据文件读-改-写互斥锁（RLock：_read_data → _ensure_migrated 会重入加锁）。
# ThreadingHTTPServer 多线程并发下，若不加锁，两个 /__image__ 请求会同时读到
# 旧 images，后写覆盖先写 → 丢图。
DATA_LOCK = threading.RLock()


def _read_json(path):
    """读取 JSON 文件，不存在或解析失败返回 None"""
    try:
        if os.path.exists(path):
            with open(path, "rb") as f:
                return json.loads(f.read().decode("utf-8"))
    except Exception:
        pass
    return None


def _write_json(path, obj):
    """原子写 JSON 文件（先写临时文件再替换，避免写一半损坏）"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def _ensure_migrated():
    """一次性迁移：旧版单文件 widget-data.json → state/images 两个拆分文件。

    背景：旧版把 {state, ui, images(全部 base64 原图)} 写进一个文件，图片多了之后
    每次保存都全量重写大文件，明显变慢。拆分后 state 文件只有几 KB，保存开销可忽略。
    迁移把原文件改名为 widget-data.json.migrated 归档备份，绝不直接删除。
    """
    if not os.path.exists(DATA_FILE):
        return
    with DATA_LOCK:
        if os.path.exists(STATE_FILE):
            # 新文件已存在：widget-data.json 只能是旧实例退出前残留，改名归档
            try:
                os.replace(DATA_FILE, DATA_FILE + ".migrated")
                log("[migrate] 旧数据文件已归档: widget-data.json.migrated")
            except Exception:
                pass
            return
        try:
            d = _read_json(DATA_FILE)
            if not d or "state" not in d:
                return
            _write_json(STATE_FILE, {
                "version": d.get("version", 1),
                "state": d.get("state"),
                "ui": d.get("ui"),
            })
            _write_json(IMAGES_FILE, d.get("images") or {})
            os.replace(DATA_FILE, DATA_FILE + ".migrated")
            log("[migrate] 数据已拆分迁移: widget-data.json → widget-state.json + widget-images.json")
        except Exception as e:
            log_error("[migrate] 拆分迁移失败（保留原文件，不影响运行）: %s" % e)


def _read_state():
    """读取拆分后的 state 文件（含迁移），无则 None"""
    _ensure_migrated()
    return _read_json(STATE_FILE)


def _read_images():
    """读取拆分后的 images 文件（无则空 dict）"""
    _ensure_migrated()
    return _read_json(IMAGES_FILE) or {}


def _read_data():
    """兼容接口：合并返回完整结构 {version, state, ui, images}"""
    s = _read_state()
    if s is None:
        return None
    d = dict(s)
    d["images"] = _read_images()
    return d


def _cleanup_view_temps(skip=None):
    """清理「外部查看原图」历史临时文件（todo-widget-*），保留正在使用的 skip。

    背景：每次查看原图都会在 %TEMP% 留一个文件、永不删除，长期会积累。
    方案：写新文件前清理旧残留；本次文件 60 秒后延迟清理（图片查看器早已完成读取）。
    """
    try:
        tmpdir = tempfile.gettempdir()
        for name in os.listdir(tmpdir):
            if not name.startswith("todo-widget-"):
                continue
            p = os.path.join(tmpdir, name)
            if skip and os.path.abspath(p) == os.path.abspath(skip):
                continue
            try:
                os.remove(p)
            except Exception:
                pass
    except Exception:
        pass

EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

DEBUG = "--debug" in sys.argv
CTRL = None        # WidgetController 实例（供 HTTP 回调使用）
TRAY_ICON = None   # pystray 图标实例
SHUTTING_DOWN = False             # 主实例是否正在退出（供新实例判断是等待接管还是交给它）
SERVER_READY = threading.Event()  # 本地服务是否已成功 bind 并就绪
SERVER_ERROR = None               # 服务启动失败原因（服务线程内无法直接抛给主线程）

# ================= Win32 API =================
user32 = ctypes.windll.user32
GWL_EXSTYLE = -20
GWL_STYLE = -16
WS_EX_TOOLWINDOW = 0x00000080   # 工具窗口：不显示在任务栏 / Alt+Tab
WS_EX_APPWINDOW = 0x00040000    # 强制显示在任务栏（需清除）
WS_CAPTION = 0x00C00000         # 标题栏（含 WS_SYSMENU）
WS_SYSMENU = 0x00080000         # 系统菜单 / — □ × 按钮
WS_THICKFRAME = 0x00040000      # 可调边框
SW_HIDE, SW_SHOW, WM_CLOSE = 0, 5, 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SM_CYCAPTION = 4                # 系统标题栏标准高度
if hasattr(user32, "GetWindowLongPtrW"):
    GetWindowLong = user32.GetWindowLongPtrW
    SetWindowLong = user32.SetWindowLongPtrW
else:
    GetWindowLong = user32.GetWindowLongW
    SetWindowLong = user32.SetWindowLongW
IsWindow = user32.IsWindow
IsWindowVisible = user32.IsWindowVisible
ShowWindow = user32.ShowWindow
SetForegroundWindow = user32.SetForegroundWindow
PostMessageW = user32.PostMessageW
GetWindowTextLengthW = user32.GetWindowTextLengthW
GetWindowTextW = user32.GetWindowTextW

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
TITLE = "TODO 小组件"


def log(*args):
    if DEBUG:
        print(*args, flush=True)


def log_error(msg):
    """写错误到 %TEMP%\\todo-widget-error.log，便于 pythonw（无控制台）运行时排查问题"""
    try:
        err_file = os.path.join(os.environ.get("TEMP", BASE), "todo-widget-error.log")
        with open(err_file, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def show_error_box(msg):
    """pythonw 无控制台，关键错误用系统消息框直接提示用户（同时写日志）"""
    log_error(msg)
    try:
        ctypes.windll.user32.MessageBoxW(
            0, msg + "\n\n详细日志：%TEMP%\\todo-widget-error.log",
            "TODO 小组件", 0x10,  # MB_ICONERROR
        )
    except Exception:
        pass


def mark_shutting_down():
    """标记"本实例正在退出"，供新实例判断应等待接管而不是交给它。

    单独封装为模块级函数的原因：do_GET 内既要【读】SHUTTING_DOWN（/__ping__ 探活），
    又要【写】（/__quit__ 退出）。若在 do_GET 内写 global 声明，会因"声明前已使用该名字"
    触发 SyntaxError。故赋值统一走这里，do_GET 内保持纯读取。
    """
    global SHUTTING_DOWN
    SHUTTING_DOWN = True


def find_browser():
    for p in EDGE_PATHS + CHROME_PATHS:
        if os.path.exists(p):
            return p
    return None


def port_open(port):
    s = socket.socket()
    try:
        s.settimeout(0.5)
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def probe_main_instance():
    """探测已有主实例是否健康。返回:
        ("active", info) 主实例健康，已请求它显示窗口 → 本实例应退出
        ("busy",   info) 主实例正在退出 → 本实例应等待端口释放后接管
        ("none",   None) 端口被无关程序占用或无响应 → 本实例应报告并放弃
    """
    try:
        r = urllib.request.urlopen("http://127.0.0.1:%d/__ping__" % PORT, timeout=2)
        info = json.loads(r.read().decode("utf-8"))
    except Exception:
        return ("none", None)
    if info.get("app") != "todo-widget":
        return ("none", None)
    if info.get("shutting_down"):
        return ("busy", info)
    try:
        urllib.request.urlopen("http://127.0.0.1:%d/__act__" % PORT, timeout=2)
    except Exception as e:
        log("[act] 激活请求失败: %s" % e)
    return ("active", info)


def wait_port_closed(timeout=20):
    """等待端口被释放（主实例退出中）。返回是否等到"""
    waited = 0.0
    while waited < timeout:
        if not port_open(PORT):
            return True
        time.sleep(0.4)
        waited += 0.4
    return False


def wait_server_ready(timeout=10):
    """等待本地服务就绪（替代盲等 sleep）。返回是否就绪"""
    waited = 0.0
    while waited < timeout:
        if SERVER_ERROR:
            return False
        if SERVER_READY.is_set() and port_open(PORT):
            return True
        time.sleep(0.2)
        waited += 0.2
    return False


def list_widget_windows():
    """枚举所有可见的、标题为「TODO 小组件」的窗口句柄"""
    found = []

    @WNDENUMPROC
    def cb(hwnd, lparam):
        if IsWindowVisible(hwnd):
            n = GetWindowTextLengthW(hwnd)
            if 0 < n < 200:
                buf = ctypes.create_unicode_buffer(n + 1)
                GetWindowTextW(hwnd, buf, n + 1)
                if buf.value == TITLE:
                    found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return set(found)


# ================= 本地静态服务 =================
# 来源校验白名单：仅允许来自本小组件页面（同源）的请求读写数据
ALLOWED_ORIGIN = "http://127.0.0.1:%d" % PORT


def start_server():
    os.chdir(BASE)

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass  # 静默，不打印访问日志

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def _from_widget(self):
            """请求来源校验：仅接受来自小组件页面本身（同源）的请求。

            浏览器同源 fetch：POST 会带 Origin: http://127.0.0.1:<port>，
            GET 会带 Referer: http://127.0.0.1:<port>/todo-widget.html。
            其他来源（恶意网页、外部程序等）不带这些头或不同源 → 拒绝。
            注意：/__ping__、/__act__ 由 serve.py 用 urllib 内部调用（不带这些头），
            因此这两个端点不校验；其余 GET 与全部 POST 均校验。
            """
            origin = self.headers.get("Origin") or ""
            referer = self.headers.get("Referer") or ""
            return (origin.startswith(ALLOWED_ORIGIN)
                    or referer.startswith(ALLOWED_ORIGIN + "/"))

        def do_GET(self):
            # 特殊路由：探活。供新实例判断"交给主实例"还是"等它退出后接管"
            if self.path == "/__ping__":
                body = json.dumps({
                    "app": "todo-widget",
                    "pid": os.getpid(),
                    "shutting_down": SHUTTING_DOWN,
                    "window": bool(CTRL and CTRL._hwnd()),
                }, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            # 特殊路由：二次双击/启动时，请求主实例显示窗口
            if self.path == "/__act__":
                self.send_response(204)
                self.end_headers()
                try:
                    if CTRL:
                        CTRL.show()
                except Exception:
                    pass
                return
            # 特殊路由：顶栏 × 按钮请求退出整个小组件（窗口+服务+托盘）
            if self.path == "/__quit__":
                # 必须在收到请求的【瞬间】置位：_shutdown 有 0.3s 延迟，
                # 若等它才置位，这 0.3s 内到达的新实例会被误判为"主实例健康"而自我退出，
                # 结果旧实例随即退出、新实例也已消失 —— 表现为双击后什么都拉不起来。
                mark_shutting_down()
                self.send_response(204)
                self.end_headers()

                def _shutdown():
                    try:
                        if CTRL:
                            CTRL.quit_app()
                    except Exception:
                        pass
                    try:
                        if TRAY_ICON:
                            TRAY_ICON.stop()
                    except Exception:
                        pass
                    # 兜底：pystray 偶发停不干净，1.2s 后强制退出 pythonw
                    import os as _os
                    threading.Timer(1.2, lambda: _os._exit(0)).start()

                threading.Timer(0.3, _shutdown).start()
                return
            # 特殊路由：HTML 文件修改时间。页面轮询此接口，本地文件更新后自动刷新
            if self.path == "/__mtime__":
                try:
                    mt = os.path.getmtime(os.path.join(BASE, "todo-widget.html"))
                    body = json.dumps({"mtime": mt}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception:
                    self.send_response(404)
                    self.end_headers()
                return
            # 特殊路由：日常持久化数据（serve.py 侧兜底）。
            # 页面启动时若 localStorage 为空，优先从这里拉取；不存在则 204
            if self.path == "/__data__":
                if not self._from_widget():
                    self.send_response(403)
                    self.end_headers()
                    return
                d = _read_data()
                if d is None:
                    self.send_response(204)
                    self.end_headers()
                    return
                try:
                    body = json.dumps(d, ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    log("[sync] 已下发日常数据（%d 字节）" % len(body))
                except Exception as e:
                    log("[sync] 下发日常数据失败: %s" % e)
                    self.send_response(500)
                    self.end_headers()
                return
            # 特殊路由：一次性数据恢复（WebView2 迁移用）。
            # 备份文件存在则返回其内容并立即删除（只恢复一次，避免旧数据复活）
            if self.path == "/__restore__":
                if not self._from_widget():
                    self.send_response(403)
                    self.end_headers()
                    return
                if os.path.exists(BACKUP_FILE):
                    try:
                        with open(BACKUP_FILE, "rb") as f:
                            body = f.read()
                        os.remove(BACKUP_FILE)
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        log("[migrate] 已下发数据备份并删除备份文件")
                    except Exception as e:
                        log("[migrate] 恢复失败: %s" % e)
                        self.send_response(500)
                        self.end_headers()
                else:
                    self.send_response(204)
                    self.end_headers()
                return
            super().do_GET()

        def do_POST(self):
            # 所有 POST 都涉及数据读写（保存/图片/查看/备份），统一校验来源
            if not self._from_widget():
                # 先排空请求体再响应：若直接关闭连接，仍在发送 body 的客户端会撞上
                # 连接重置（如 urllib 报 WinError 10053）。浏览器 fetch 不受影响，但
                # 排空后对任意 HTTP 客户端都干净。
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    if length:
                        self.rfile.read(length)
                except Exception:
                    pass
                self.send_response(403)
                self.end_headers()
                log("[auth] 已拒绝非本页来源的 POST: %s" % self.path)
                return
            # 特殊路由：日常数据保存（state + ui，可选附带 images）。
            # 页面每次 save() 后调用。拆分存储：state 只写小文件；images 仅随附时增量合并
            if self.path == "/__save__":
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                    with DATA_LOCK:
                        _write_json(STATE_FILE, {
                            "version": 1,
                            "state": body.get("state"),
                            "ui": body.get("ui"),
                        })
                        if body.get("images"):
                            images = _read_images()
                            images.update(body["images"])
                            _write_json(IMAGES_FILE, images)
                    self.send_response(200)
                    self.end_headers()
                    log("[sync] 已保存日常数据（state 文件）")
                except Exception as e:
                    log("[sync] 保存日常数据失败: %s" % e)
                    self.send_response(400)
                    self.end_headers()
                return
            # 特殊路由：图片上传（持久化兜底）。页面新增图片任务时调用
            if self.path == "/__image__":
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                    if body.get("imageId") and body.get("dataUrl"):
                        with DATA_LOCK:
                            images = _read_images()
                            images[body["imageId"]] = body["dataUrl"]
                            _write_json(IMAGES_FILE, images)
                        self.send_response(200)
                        self.end_headers()
                        log("[sync] 图片已保存: %s" % body["imageId"])
                    else:
                        self.send_response(400)
                        self.end_headers()
                except Exception as e:
                    log("[sync] 图片保存失败: %s" % e)
                    self.send_response(400)
                    self.end_headers()
                return
            # 特殊路由：图片删除。页面删除图片任务 5 秒确认后调用
            if self.path == "/__image_del__":
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                    with DATA_LOCK:
                        images = _read_images()
                        if body.get("imageId") and body["imageId"] in images:
                            del images[body["imageId"]]
                            _write_json(IMAGES_FILE, images)
                    self.send_response(200)
                    self.end_headers()
                    log("[sync] 图片已删除: %s" % body.get("imageId"))
                except Exception as e:
                    log("[sync] 图片删除失败: %s" % e)
                    self.send_response(400)
                    self.end_headers()
                return
            # 特殊路由：外部查看原图。把图片写入临时文件，用系统图片查看器打开（脱离小组件窗口）
            if self.path == "/__view_image__":
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                    data = None
                    # 优先从服务端持久化数据取原图（避免大 dataUrl 反复传输）
                    if body.get("imageId"):
                        data = _read_images().get(body["imageId"])
                    if not data:
                        data = body.get("dataUrl")
                    if not data or not data.startswith("data:image/"):
                        self.send_response(400)
                        self.end_headers()
                        return
                    m = re.match(r"data:image/(\w+);base64,(.+)", data, re.S)
                    if not m:
                        self.send_response(400)
                        self.end_headers()
                        return
                    ext = m.group(1).lower()
                    if ext == "jpeg":
                        ext = "jpg"
                    raw = base64.b64decode(m.group(2))
                    _cleanup_view_temps()   # 先清理上一次查看的残留临时文件
                    fd, path = tempfile.mkstemp(suffix="." + ext, prefix="todo-widget-")
                    with os.fdopen(fd, "wb") as f:
                        f.write(raw)
                    os.startfile(path)   # 系统默认图片查看器打开
                    # 本次文件 60 秒后延迟清理（查看器早已完成读取），避免 %TEMP% 累积
                    threading.Timer(60, _cleanup_view_temps, args=(path,)).start()
                    self.send_response(200)
                    self.end_headers()
                    log("[view] 已在系统查看器打开原图: %s" % path)
                except Exception as e:
                    log_error("[view] 打开原图失败: %s" % e)
                    self.send_response(500)
                    self.end_headers()
                return
            # 特殊路由：数据备份（WebView2 迁移用）。
            # 页面以 ?backup=1 打开时，把 localStorage + IndexedDB 图片打包 POST 到这里
            if self.path == "/__backup__":
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    body = self.rfile.read(length)
                    # 简单校验是 JSON
                    import json as _json
                    _json.loads(body.decode("utf-8"))
                    with open(BACKUP_FILE, "wb") as f:
                        f.write(body)
                    self.send_response(200)
                    self.end_headers()
                    log("[migrate] 数据备份已写入 %s（%d 字节）" % (BACKUP_FILE, len(body)))
                except Exception as e:
                    log("[migrate] 备份失败: %s" % e)
                    self.send_response(400)
                    self.end_headers()
                return
            self.send_response(404)
            self.end_headers()

    global SERVER_ERROR
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except Exception as e:
        # 端口被无关程序占用等：记录并通知主线程，避免开出加载失败的空白窗口
        SERVER_ERROR = "本地服务启动失败（端口 %d 可能被其他程序占用）：%s" % (PORT, e)
        log_error(SERVER_ERROR)
        return
    SERVER_READY.set()
    log("[server] http://127.0.0.1:%d 已启动" % PORT)
    httpd.serve_forever()


# ================= 窗口控制（WebView2 无边框宿主） =================
# 背景：Edge --app 窗口的标题栏是 Chromium 自己绘制的，Win32 剥样式无效（实测）。
# 方案：改用 pywebview + 系统自带 WebView2 运行时，frameless=True 原生无边框。

def widget_hwnd():
    """当前小组件窗口句柄（标题匹配），无则返回 None"""
    ws = list_widget_windows()
    return sorted(ws)[0] if ws else None


class DragApi:
    """JS 桥接：顶栏 mousedown 时触发原生窗口拖动"""

    def begin_drag(self):
        try:
            hwnd = widget_hwnd()
            if hwnd:
                user32.ReleaseCapture()
                user32.SendMessageW(hwnd, 0xA1, 2, 0)  # WM_NCLBUTTONDOWN / HTCAPTION
        except Exception:
            pass


class WidgetController:
    """WebView2 无边框窗口控制器：显隐 / 退出 / 任务栏隐藏"""

    def __init__(self):
        # 缓存窗口句柄：窗口被隐藏后 IsWindowVisible 为 False，单靠标题枚举会找不到句柄，
        # 导致「隐藏后再也显示不出来」。缓存后只要句柄仍有效（IsWindow）就能重新 ShowWindow。
        self._cached_hwnd = None

    def _hwnd(self):
        """取窗口句柄：优先复用缓存（含已隐藏窗口），句柄失效才重新枚举"""
        if self._cached_hwnd and IsWindow(self._cached_hwnd):
            return self._cached_hwnd
        self._cached_hwnd = None
        ws = list_widget_windows()
        if ws:
            self._cached_hwnd = sorted(ws)[0]
        return self._cached_hwnd

    def _ensure_toolwindow(self):
        """窗口不占任务栏（工具窗口样式）"""
        hwnd = self._hwnd()
        if not hwnd:
            return
        try:
            ex = GetWindowLong(hwnd, GWL_EXSTYLE) or 0
            if (ex & WS_EX_APPWINDOW) or not (ex & WS_EX_TOOLWINDOW):
                ex |= WS_EX_TOOLWINDOW
                ex &= ~WS_EX_APPWINDOW
                SetWindowLong(hwnd, GWL_EXSTYLE, ex)
        except Exception:
            pass

    def open(self):
        pass  # 窗口由 webview.start() 在主线程创建，此接口仅保持兼容

    def show(self):
        hwnd = self._hwnd()
        if hwnd:
            self._ensure_toolwindow()
            ShowWindow(hwnd, SW_SHOW)
            SetForegroundWindow(hwnd)
            log("[window] 显示窗口")

    def hide(self):
        hwnd = self._hwnd()
        if hwnd:
            ShowWindow(hwnd, SW_HIDE)
            log("[window] 隐藏窗口")

    def toggle(self):
        hwnd = self._hwnd()
        if not hwnd:
            return
        if IsWindowVisible(hwnd):
            self.hide()
        else:
            self.show()

    def quit_app(self):
        hwnd = self._hwnd()
        if hwnd:
            PostMessageW(hwnd, WM_CLOSE, 0, 0)
            log("[window] 已请求关闭窗口")


# ================= 系统托盘 =================
def run_tray():
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        log("[tray] 未安装 pystray/pillow，降级为无托盘常驻模式")
        while True:
            time.sleep(3600)
        return

    # 托盘图标：优先使用自定义 ICO（与快捷方式图标一致），否则蓝色对勾兜底
    ico_path = os.path.join(BASE, "85037897_p0.ico")
    try:
        img = Image.open(ico_path).convert("RGBA")
        img.thumbnail((64, 64), Image.LANCZOS)
    except Exception:
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([3, 3, 61, 61], radius=16, fill=(59, 130, 246, 255))
        d.line([(17, 34), (28, 46), (47, 19)], fill=(255, 255, 255, 255), width=8, joint="curve")

    def on_toggle(icon, item):
        CTRL.toggle()

    def on_quit(icon, item):
        mark_shutting_down()
        CTRL.quit_app()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("显示 / 隐藏小组件", on_toggle, default=True),
        pystray.MenuItem("退出", on_quit),
    )
    global TRAY_ICON
    TRAY_ICON = pystray.Icon("TODO_小组件", img, "TODO 小组件", menu)
    log("[tray] 托盘图标已就绪（双击切换显示，右键菜单退出）")

    # 首次启动弹一个气泡通知（Win11 首次创建的图标默认在 ^ 溢出区，提示用户固定）
    def _welcome():
        try:
            TRAY_ICON.notify(
                "TODO 小组件已驻留托盘",
                "双击托盘图标切换窗口；右键 ^ 选「显示」可固定到主区",
            )
        except Exception:
            pass

    threading.Timer(1.5, _welcome).start()
    TRAY_ICON.run()


# ================= 安装 =================
def powershell(script):
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "PowerShell 执行失败")
    return r.stdout.strip()


def install():
    """创建桌面快捷方式 + 开机启动项"""
    py = sys.executable
    pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = py

    # 图标：优先使用小组件目录下的自定义 ICO（避免与 Edge 混淆），否则回退浏览器图标
    custom_ico = os.path.join(BASE, "85037897_p0.ico")
    if os.path.exists(custom_ico):
        icon = custom_ico
    else:
        icon = find_browser() or r"C:\Windows\System32\shell32.dll"
    icon_arg = icon + ",0" if icon else ""

    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    startup = os.path.join(
        os.environ.get("APPDATA", ""),
        "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
    )

    ps = (
        "$ws = New-Object -ComObject WScript.Shell;"
        "$sc = $ws.CreateShortcut('{lnk}');"
        "$sc.TargetPath = '{target}';"
        "$sc.Arguments = '\"{script}\"';"
        "$sc.WorkingDirectory = '{workdir}';"
        "$sc.IconLocation = '{icon}';"
        "$sc.Description = 'TODO 小组件';"
        "$sc.Save();"
    )
    common = dict(target=pyw, script=os.path.join(BASE, "serve.py"),
                  workdir=BASE, icon=icon_arg)

    try:
        powershell(ps.format(lnk=os.path.join(desktop, "TODO小组件.lnk"), **common))
        print("  [OK] 桌面 -> %s" % os.path.join(desktop, "TODO小组件.lnk"))
    except Exception as e:
        print("  [FAIL] 桌面快捷方式: %s" % e)
    try:
        powershell(ps.format(lnk=os.path.join(startup, "TODO小组件.lnk"), **common))
        print("  [OK] 开机启动 -> %s" % os.path.join(startup, "TODO小组件.lnk"))
    except Exception as e:
        print("  [FAIL] 开机启动项: %s" % e)

    print("完成！双击桌面「TODO小组件」启动。")
    print("提示：启动后右下角托盘出现图标，双击托盘图标切换窗口，右键可退出。")


# ================= 主流程 =================
def open_webview():
    """创建 WebView2 无边框窗口并进入 GUI 主循环（阻塞直到窗口关闭）"""
    try:
        import webview
    except ImportError:
        msg = "缺少依赖 pywebview，请先安装：\n\n  pip install pywebview pystray pillow"
        print(msg)
        show_error_box(msg)
        os._exit(1)

    # 关键：固定 WebView2 用户数据目录
    # pywebview 6.x 默认 private_mode=True，每次启动都用临时目录，
    # 导致 localStorage / IndexedDB 不可持久化（重启即清空）。
    # 显式设置 _state['storage_path'] 后，缓存落到固定目录，数据长期保留。
    storage_dir = os.path.join(os.environ.get("APPDATA", BASE), "TODO小组件", "WebView2")
    os.makedirs(storage_dir, exist_ok=True)
    webview._state["storage_path"] = storage_dir
    log("[window] WebView2 用户目录: %s" % storage_dir)

    window = webview.create_window(
        TITLE, URL,
        width=480, height=777, x=120, y=60,
        frameless=True, resizable=False,
        js_api=DragApi(),
    )
    log("[window] WebView2 无边框窗口已创建")

    # 窗口出现后立即隐藏任务栏按钮（工具窗口样式）
    def _hide_taskbar_later():
        for _ in range(40):
            if CTRL and CTRL._hwnd():
                CTRL._ensure_toolwindow()
                break
            time.sleep(0.25)

    threading.Thread(target=_hide_taskbar_later, daemon=True).start()
    try:
        webview.start()
    except Exception as e:
        show_error_box("WebView2 窗口启动失败：%s\n\n请确认已安装 Microsoft Edge WebView2 运行时。" % e)
        os._exit(1)
    mark_shutting_down()
    log("[window] GUI 主循环结束，进程退出")
    os._exit(0)


def main():
    if "--install" in sys.argv:
        install()
        return

    # 单实例：端口开着 ≠ 主实例健康，必须先探活再决定行为
    if port_open(PORT):
        state, info = probe_main_instance()
        if state == "active":
            log("[act] 主实例健康，已请求显示窗口，本实例退出")
            return
        if state == "busy":
            # 主实例正在退出（约 1.5s）：等端口释放后接管，避免"双击无反应"
            log("[act] 主实例正在退出，等待端口释放后接管")
            if not wait_port_closed(20):
                show_error_box("等待上一个实例退出超时（20 秒）。\n请稍后重试，或在任务管理器中结束 pythonw.exe。")
                return
            log("[act] 端口已释放，本实例接管")
        else:
            show_error_box(
                "端口 %d 被其他程序占用，小组件无法启动。\n"
                "请关闭占用该端口的程序，或修改 serve.py 中的 PORT 常量后"
                "重新运行「安装桌面小组件.bat」。" % PORT)
            return

    global CTRL
    CTRL = WidgetController()
    threading.Thread(target=start_server, daemon=True).start()
    if not wait_server_ready(10):
        show_error_box(SERVER_ERROR or "本地服务启动超时（10 秒），小组件无法启动。")
        return
    threading.Thread(target=run_tray, daemon=True).start()
    open_webview()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        import traceback
        log_error("serve.py 未捕获异常：\n" + traceback.format_exc())
        raise
