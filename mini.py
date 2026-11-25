"""周历小窗：置顶的小窗口，通过 Super Productivity 的本地 REST API 读写任务。

数据和同步都还是 Super Productivity 自己管，这个窗口只负责显示和快捷操作。
需要：Super Productivity 桌面版在运行，且 设置 → Misc Settings → Enable local REST API 已勾选。
"""

import ctypes
import json
from ctypes import wintypes
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import webview

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(APP_DIR, "config.json")
COURSES_PATH = os.path.join(APP_DIR, "courses.json")
TOKEN_FILE = os.path.join(os.environ.get("APPDATA", ""), "superProductivity", "local-rest-api-token")
SP_EXE = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Super Productivity", "Super Productivity.exe")
BASE = "http://127.0.0.1:3876"

FULL_SIZE = (340, 620)  # 小窗展开时的默认大小（按 100% 缩放算，实际乘上屏幕缩放比例）
BAR_HEIGHT = 46
PILL_SIZE = (220, 36)   # 收起后的小条，同上

# 单位说明：
# - 进程按「每块屏幕各自的缩放」声明 DPI（见 _dpi_aware）。以前用的是「整个系统一个缩放」，
#   换了屏幕或改了缩放、没重新登录时，系统会把整个窗口按位图拉大，字就糊了。
# - config.json 里窗口大小存成 100% 缩放下的尺寸（units = "css"），用的时候乘当前屏幕的缩放，
#   换到 200% 的屏幕上窗口也一样大；位置 x/y 存屏幕像素。
# - pywebview 建窗口时会把位置乘一次缩放、宽高也会被 WinForms 按 DPI 放大，所以窗口显示后
#   再用屏幕像素精确设一次大小（_apply_size）。


def _dpi_aware():
    """每块屏幕各自的缩放（Per-Monitor V2）。必须在建任何窗口之前调用。"""
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


def _scale():
    """主屏当前的缩放比例，200% 返回 2.0。"""
    try:
        mon = ctypes.windll.user32.MonitorFromPoint(wintypes.POINT(0, 0), 1)  # MONITOR_DEFAULTTOPRIMARY
        dx, dy = ctypes.c_uint(), ctypes.c_uint()
        ctypes.windll.shcore.GetDpiForMonitor(mon, 0, ctypes.byref(dx), ctypes.byref(dy))
        if dx.value:
            return dx.value / 96
    except Exception:
        pass
    try:
        return ctypes.windll.shcore.GetScaleFactorForDevice(0) / 100 or 1
    except Exception:
        return 1


def _css(v):
    return round(v / _scale())

# 不走系统代理：本机流量走 Clash，代理会拿到 Bearer token。
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 自己的一份 user32，参数类型写清楚（64 位下 HWND、LPARAM 不能当 32 位 int 传），也不影响 pywebview 用的那份。
_u32 = ctypes.WinDLL("user32", use_last_error=True)
_u32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_u32.SendMessageW.restype = wintypes.LPARAM
_u32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
_u32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_u32.GetAsyncKeyState.restype = ctypes.c_short

class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND), ("hwndCapture", wintypes.HWND),
                ("hwndMenuOwner", wintypes.HWND), ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                ("rcCaret", wintypes.RECT)]


_u32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(_GUITHREADINFO)]


def _system_is_moving():
    """当前（界面）线程是不是已经在系统的拖动里了。"""
    gi = _GUITHREADINFO(cbSize=ctypes.sizeof(_GUITHREADINFO))
    if not _u32.GetGUIThreadInfo(ctypes.windll.kernel32.GetCurrentThreadId(), ctypes.byref(gi)):
        return False
    return bool(gi.flags & 0x2)  # GUI_INMOVESIZE


def _enable_native_drag():
    """打开 WebView2 的「非客户区」支持：网页里标了 app-region: drag 的地方就是标题栏。

    这样按住顶栏拖动全程是系统在挪窗口，网页和 Python 都不参与，和普通窗口一样顺。
    要在第一次导航之前打开才生效，所以包一层 pywebview 初始化 WebView2 的回调。
    打不开（比如 WebView2 太旧）也没关系：顶栏的 mousedown 会走 start_drag。
    """
    try:
        from webview.platforms import edgechromium
    except Exception:
        return
    orig = edgechromium.EdgeChrome.on_webview_ready

    def ready(self, sender, args):
        try:
            if args.IsSuccess:
                sender.CoreWebView2.Settings.IsNonClientRegionSupportEnabled = True
        except Exception:
            pass
        orig(self, sender, args)

    edgechromium.EdgeChrome.on_webview_ready = ready


def _err(code, message):
    return {"ok": False, "error": {"code": code, "message": message}}


def _call(method, path, body=None):
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            token = f.read().strip()
    except OSError:
        return _err("NO_TOKEN", "没找到 REST API 的令牌文件")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    try:
        with _opener.open(req, timeout=8) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except Exception:
            return _err("HTTP_%d" % e.code, str(e))
    except (urllib.error.URLError, OSError) as e:
        return _err("OFFLINE", str(e))


_sp_launched_at = 0.0
_sp_lock = threading.Lock()
LOG_PATH = os.path.join(APP_DIR, "mini.log")
SP_SETTINGS = os.path.join(os.environ.get("APPDATA", ""), "superProductivity", "simpleSettings")


def _log(msg):
    """记一行到 mini.log（开机时发生了什么，事后能查）。超过 200KB 就清空重记。"""
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 200_000:
            os.remove(LOG_PATH)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def _sp_process_running():
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Super Productivity.exe", "/NH"],
                             capture_output=True, text=True, timeout=5, creationflags=0x08000000).stdout
        return "Super Productivity.exe" in out
    except Exception:
        return False


def _api_listening():
    """本地 REST API 的端口有没有在听。只连一下，不发请求。"""
    try:
        socket.create_connection(("127.0.0.1", 3876), timeout=1).close()
        return True
    except OSError:
        return False


def _api_enabled():
    """主程序设置里有没有勾「启用本地 REST API」。读不到就当勾了。"""
    try:
        with open(SP_SETTINGS, encoding="utf-8") as f:
            return json.load(f).get("localRestApiEnabled", True) is not False
    except (OSError, ValueError):
        return True


def _launch_sp(reason):
    """最小化打开主程序（不弹窗口）。45 秒内只开一次。"""
    global _sp_launched_at
    with _sp_lock:
        if time.time() - _sp_launched_at < 45 or not os.path.exists(SP_EXE):
            return False
        _sp_launched_at = time.time()
        subprocess.Popen([SP_EXE, "--start-minimized"], close_fds=True, creationflags=0x08000000)
    _log("打开主程序：" + reason)
    return True


def _watchdog():
    """后台一直看着主程序：没在运行就最小化打开它。只开，不关。

    以前这里还会在「进程在、接口不通」时强制重启主程序，那是误判：接口不通的真正原因是
    主程序设置里没打开「本地 REST API」（见 README「为什么开机连不上」），重启没用，
    还会把用户正在用的主程序关掉。现在遇到这种情况只在界面上提示去打开开关。
    """
    was_up = None
    _log("看门狗启动")
    while True:
        try:
            if _api_listening():
                if was_up is not True:
                    _log("接口已连上")
                was_up = True
            else:
                if was_up is not False:
                    _log("接口连不上（主程序%s）" % ("在运行，多半是没打开本地 REST API" if _sp_process_running() else "没在运行"))
                was_up = False
                if not _sp_process_running():
                    _launch_sp("没在运行")
        except Exception as e:
            _log("看门狗出错：%r" % e)
        time.sleep(3)


def _ensure_sp():
    """给界面用：True 表示「主程序正在打开，等一会儿就好」；
    False 表示它开着却连不上——多半是设置里没打开「本地 REST API」，界面提示用户去打开（只需一次）。"""
    if not _sp_process_running():
        _launch_sp("界面发现没在运行")
        return True
    return time.time() - _sp_launched_at < 45


def _install_sp_skin():
    """把纸墨皮肤 styles.css 放进主程序的数据文件夹（不同就覆盖），主程序会自动加载。"""
    src = os.path.join(APP_DIR, "styles.css")
    dst = os.path.join(os.environ.get("APPDATA", ""), "superProductivity", "styles.css")
    try:
        with open(src, "rb") as f:
            want = f.read()
        try:
            with open(dst, "rb") as f:
                if f.read() == want:
                    return
        except OSError:
            pass
        if os.path.isdir(os.path.dirname(dst)):
            with open(dst, "wb") as f:
                f.write(want)
            _log("已放入主程序皮肤 styles.css")
    except OSError:
        pass


def _load_cfg():
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_cfg(cfg):
    try:
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


class TaskApi:
    """两个窗口共用的任务操作。_cfg 是同一个 dict，两个窗口改的设置都存进 config.json。"""

    def __init__(self, cfg):
        self._cfg = cfg

    def tasks(self):
        r = _call("GET", "/tasks?includeDone=true")
        if not r.get("ok") and (r.get("error") or {}).get("code") in ("OFFLINE", "NO_TOKEN"):
            if _ensure_sp():
                r = _err("STARTING", "正在打开 Super Productivity")
            else:
                r = _err("API_OFF", "主程序在运行，但没打开本地 REST API")
        # 心跳：记下最近一次页面来取数据的结果，排查窗口空白时看这个文件。
        try:
            with open(os.path.join(APP_DIR, "last_poll.txt"), "w", encoding="utf-8") as f:
                f.write("%s ok=%s n=%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), r.get("ok"), len(r.get("data") or [])))
        except OSError:
            pass
        return r

    def projects(self):
        return _call("GET", "/projects")

    def add(self, title, day=None, as_deadline=False, at=None):
        body = {"title": title}
        if day:
            body["deadlineDay" if as_deadline else "dueDay"] = day
        r = _call("POST", "/tasks", body)
        # 日视图里点时间轴加的：再补上开始时间。
        if at and r.get("ok") and r.get("data"):
            r2 = _call("PATCH", "/tasks/%s" % r["data"]["id"], {"dueWithTime": int(at)})
            if r2.get("ok"):
                r = r2
        # 只记截止日时，主程序会顺手把它排到今天；撤掉，让它待在「还没排日子」里。
        if as_deadline and r.get("ok") and (r.get("data") or {}).get("dueDay") and r["data"]["dueDay"] != day:
            r2 = _call("PATCH", "/tasks/%s" % r["data"]["id"], {"dueDay": None})
            if r2.get("ok"):
                r = r2
        return r

    def set_done(self, task_id, done):
        return _call("PATCH", "/tasks/%s" % task_id, {"isDone": bool(done)})

    def move(self, task_id, day, had_time=False):
        body = {"dueDay": day or None}
        if had_time:
            body["dueWithTime"] = None
        return _call("PATCH", "/tasks/%s" % task_id, body)

    def schedule(self, task_id, day, at):
        """排到某天的某个时刻（at 是毫秒时间戳）；at 为空就只留日子、去掉时间。"""
        return _call("PATCH", "/tasks/%s" % task_id, {"dueDay": day, "dueWithTime": int(at) if at else None})

    def set_estimate(self, task_id, minutes):
        return _call("PATCH", "/tasks/%s" % task_id, {"timeEstimate": max(0, int(minutes)) * 60000})

    def courses(self):
        try:
            with open(COURSES_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def save_courses(self, data):
        """data = {"name": 课表名, "events": [{d, s, e, t, l}, ...]}；None 表示移除。"""
        try:
            if data is None:
                if os.path.exists(COURSES_PATH):
                    os.remove(COURSES_PATH)
                return 0
            with open(COURSES_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            return len(data.get("events") or [])
        except OSError:
            return -1

    def set_deadline(self, task_id, day):
        if day:
            return _call("PATCH", "/tasks/%s" % task_id, {"deadlineDay": day})
        return _call("PATCH", "/tasks/%s" % task_id, {"deadlineDay": None, "deadlineWithTime": None})

    def rename(self, task_id, title):
        return _call("PATCH", "/tasks/%s" % task_id, {"title": title, "isIgnoreShortSyntax": True})

    def delete(self, task_id):
        return _call("DELETE", "/tasks/%s" % task_id)

    def open_main(self):
        if os.path.exists(SP_EXE):
            subprocess.Popen([SP_EXE], close_fds=True)
            return True
        return False

    def board_prefs(self):
        b = self._cfg.get("board", {})
        return {"mode": b.get("mode", "month"), "endDay": b.get("endDay"), "theme": self._cfg.get("theme", "ink")}

    def set_board_pref(self, key, value):
        if key == "theme":
            self._cfg["theme"] = value
        elif key in ("mode", "endDay"):
            self._cfg.setdefault("board", {})[key] = value
        _save_cfg(self._cfg)
        return True


class BoardApi(TaskApi):
    pass


class Api(TaskApi):
    """小窗。"""

    def __init__(self):
        super().__init__(_load_cfg())
        self._window = None
        self._board = None
        self._moving = False

    def set_pin(self, pinned):
        self._window.on_top = bool(pinned)
        self._cfg["pinned"] = bool(pinned)
        _save_cfg(self._cfg)
        return bool(pinned)

    def set_collapsed(self, collapsed, pill_w=None, pill_h=None):
        """收起成小条 / 展开。右上角不动，窗口往左下方向伸缩（小窗一般贴在屏幕右边）。"""
        from webview.window import FixPoint
        anchor = FixPoint.NORTH | FixPoint.EAST
        w = self._window
        if collapsed:
            if not self._cfg.get("collapsed"):
                self._cfg["full"] = [_css(w.width), _css(w.height)]   # 记下收起前的大小，展开时原样恢复
            if pill_w and pill_h:
                self._cfg["pill"] = [_css(pill_w), _css(pill_h)]
            self._cfg["collapsed"] = True
            w.resize(*_pill_size(self._cfg), anchor)
        else:
            self._cfg["collapsed"] = False
            w.resize(*_full_size(self._cfg), anchor)
        _save_cfg(self._cfg)
        return bool(collapsed)

    def prefs(self):
        return {
            "pinned": self._cfg.get("pinned", True),
            "collapsed": self._cfg.get("collapsed", False),
            "theme": self._cfg.get("theme", "ink"),
        }

    def open_board(self):
        if self._board is not None:
            try:
                self._board.restore()
                self._board.show()
                return True
            except Exception:
                self._board = None
        b = self._cfg.get("board", {})
        bw, bh = _board_size(b)
        win = webview.create_window(
            "周历 · 平面",
            url=os.path.join(APP_DIR, "board.html"),
            js_api=BoardApi(self._cfg),
            width=bw,
            height=bh,
            min_size=(820, 520),
            background_color="#FFFFFF",
        )

        def shown():
            # 建窗口时 WinForms 放大的倍数不一定等于当前屏幕的缩放，这里按屏幕像素精确设一次，并且不超出屏幕
            try:
                sc = _scale()
                sw, sh = ctypes.windll.user32.GetSystemMetrics(0), ctypes.windll.user32.GetSystemMetrics(1)
                pw, ph = min(round(bw * sc), int(sw * 0.94)), min(round(bh * sc), int(sh * 0.9))
                win.resize(pw, ph)
                win.move(round((sw - pw) / 2 / win.native.scale_factor), round((sh - ph) / 2.4 / win.native.scale_factor))
            except Exception:
                pass

        win.events.shown += shown
        win.events.shown += lambda: _set_icon(win)

        def closing():
            try:
                self._cfg.setdefault("board", {}).update({"w": _css(win.width), "h": _css(win.height)})
                _save_cfg(self._cfg)
            except Exception:
                pass

        def closed():
            self._board = None

        win.events.closing += closing
        win.events.closed += closed
        self._board = win
        return True

    def start_drag(self):
        """（备用）WebView2 没接管顶栏时，按住顶栏由这里把拖动交给 Windows。

        正常情况下 WebView2 自己处理 app-region: drag，网页收不到这次 mousedown，也就不会调到这里；
        万一两边都动了，下面会先看系统是不是已经在拖，是就什么都不做，保证只有一次拖动。

        做法是在窗口的界面线程里放掉鼠标捕获，再发一条「在标题栏上按下了左键」
        （WM_NCLBUTTONDOWN + HTCAPTION）。之后跟手、贴边吸附都由系统负责，
        Python 和网页都不再参与每一帧。系统的拖动结束后，记下窗口位置。
        """
        form = self._window.native
        if form is None:
            return False
        hwnd = form.Handle.ToInt64()
        pt = wintypes.POINT()
        _u32.GetCursorPos(ctypes.byref(pt))
        lparam = (pt.x & 0xFFFF) | ((pt.y & 0xFFFF) << 16)

        def go():
            if self._moving or _system_is_moving() or not (_u32.GetAsyncKeyState(0x01) & 0x8000):
                return  # 已经在拖，或者已经松手
            self._moving = True
            try:
                _u32.ReleaseCapture()
                _u32.SendMessageW(hwnd, 0x00A1, 2, lparam)  # WM_NCLBUTTONDOWN, HTCAPTION；松手后才返回
            except Exception:
                pass
            finally:
                self._moving = False

        from System import Action  # pythonnet，pywebview 的 WinForms 后端已经加载
        form.BeginInvoke(Action(go))
        return True

    def close(self):
        self._window.destroy()


_save_timer = None


def _full_size(cfg):
    """屏幕像素。"""
    f = cfg.get("full") or FULL_SIZE
    return round(f[0] * _scale()), round(f[1] * _scale())


def _board_size(b):
    """100% 缩放下的尺寸。"""
    return (b.get("w") or 1240), (b.get("h") or 780)


def _migrate_units(cfg):
    """旧配置里的大小是屏幕像素（而且是在 125% 下记的），换成 100% 缩放下的尺寸。"""
    if cfg.get("units") == "css":
        return
    old = 1.25
    for k in ("full", "pill"):
        if cfg.get(k):
            cfg[k] = [round(v / old) for v in cfg[k]]
    b = cfg.get("board") or {}
    for k in ("w", "h"):
        if b.get(k):
            b[k] = round(b[k] / old)
    cfg["units"] = "css"


def _apply_size(api):
    """窗口刚显示时按记下的大小再设一次。

    pywebview 建窗口时给的宽高和之后 resize 用的不是同一个口径，只靠建窗口时传进去，
    每次开机窗口都会大一点（之前高度就这样涨到了 1400 多）。这里统一用 resize 的口径。
    """
    from webview.window import FixPoint
    size = _pill_size(api._cfg) if api._cfg.get("collapsed") else _full_size(api._cfg)
    try:
        api._window.resize(*size, FixPoint.NORTH | FixPoint.WEST)
    except Exception:
        pass


def _on_screen(x, y):
    pt = wintypes.POINT(int(x), int(y))
    return bool(ctypes.windll.user32.MonitorFromPoint(pt, 0))  # MONITOR_DEFAULTTONULL


def _pill_size(cfg):
    """屏幕像素。"""
    p = cfg.get("pill") or PILL_SIZE
    return round(p[0] * _scale()), round(p[1] * _scale())


def _remember_geometry_later(api):
    """窗口挪动时会连续触发很多次，停下来 0.6 秒后再存一次。"""
    global _save_timer
    if _save_timer:
        _save_timer.cancel()
    _save_timer = threading.Timer(0.6, _remember_geometry, args=(api,))
    _save_timer.daemon = True
    _save_timer.start()


ICON = os.path.join(APP_DIR, "icon.ico")


def _set_icon(window):
    """窗口和任务栏用自己的图标（icon.ico，make_icon.py 画的），不再显示 pythonw 的 Python 图标。"""
    try:
        from System import Action
        from System.Drawing import Icon
        form = window.native

        def go():
            form.Icon = Icon(ICON)

        form.BeginInvoke(Action(go))
    except Exception:
        pass


def _no_maximize(window):
    """顶栏当标题栏后，双击它会最大化；小窗不需要，关掉。"""
    try:
        from System import Action
        form = window.native

        def off():
            form.MaximizeBox = False

        form.BeginInvoke(Action(off))
    except Exception:
        pass


def _remember_geometry(api):
    w = api._window
    try:
        api._cfg["x"], api._cfg["y"] = w.x, w.y
        if not api._cfg.get("collapsed") and w.height > BAR_HEIGHT + 20:
            api._cfg["full"] = [_css(w.width), _css(w.height)]
        _save_cfg(api._cfg)
    except Exception:
        pass


def main():
    # 只开一个：已有一个在跑就直接退出。
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\sp-mini-weekly")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        sys.exit(0)

    _dpi_aware()
    # 任务栏按这个 ID 归组；不设的话会和别的 Python 程序归成一组，图标也是 Python 的
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SpMini.Weekly")
    except Exception:
        pass
    _enable_native_drag()
    _install_sp_skin()
    threading.Thread(target=_watchdog, daemon=True).start()
    api = Api()
    cfg = api._cfg
    cfg.pop("height", None)                  # 旧配置只记了高度，而且每次开机都会涨，不要了
    _migrate_units(cfg)
    _save_cfg(cfg)
    w, h = _pill_size(cfg) if cfg.get("collapsed") else _full_size(cfg)
    x, y = cfg.get("x"), cfg.get("y")
    if x is None or y is None or not _on_screen(x + 40, y + 20):
        # 第一次打开，或记下的位置已经不在任何屏幕上（比如拔了显示器）：放到主屏右上角
        sw = ctypes.windll.user32.GetSystemMetrics(0)
        x, y = sw - _full_size(cfg)[0] - 24, 80
    # 上面都是屏幕像素；pywebview 建窗口时还会再乘一次缩放比例，先除掉，不然每次开机都往外漂、变大。
    # 大小在窗口显示后由 _apply_size 精确再设一次。
    sc = _scale()
    x, y, w, h = (round(v / sc) for v in (x, y, w, h))

    window = webview.create_window(
        "周历小窗",
        url=os.path.join(APP_DIR, "ui.html"),
        js_api=api,
        width=w,
        height=h,
        x=x,
        y=y,
        min_size=(120, 30),
        frameless=True,
        easy_drag=False,
        on_top=cfg.get("pinned", True),
        background_color="#F6F6F6",
    )
    api._window = window
    if "--board" in sys.argv:
        api.open_board()
    window.events.closing += lambda: _remember_geometry(api)

    def moved(x, y):
        _remember_geometry_later(api)

    window.events.moved += moved
    window.events.shown += lambda: (_no_maximize(window), _set_icon(window), _apply_size(api))
    webview.start(private_mode=False, storage_path=os.path.join(APP_DIR, ".webview"))


if __name__ == "__main__":
    main()
