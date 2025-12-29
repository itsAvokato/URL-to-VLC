import time
import subprocess
import pyperclip
import threading
import json
import os
import urllib.parse
import winreg
import queue
import ctypes
import sys
from ctypes import wintypes

from plyer import notification
from pystray import MenuItem as item
import pystray
from PIL import Image, ImageDraw, ImageOps


# ===================== НАСТРОЙКИ =====================
VLC_ONE_INSTANCE = True
VLC_ENQUEUE = True
ANTI_BOUNCE_SEC = 0.7
# =====================================================


# -------- WinAPI types/const (совместимо с разными версиями Python) --------
if ctypes.sizeof(ctypes.c_void_p) == 8:
    LONG_PTR = ctypes.c_int64
    ULONG_PTR = ctypes.c_uint64
else:
    LONG_PTR = ctypes.c_int32
    ULONG_PTR = ctypes.c_uint32

LRESULT = LONG_PTR
WPARAM = ULONG_PTR
LPARAM = LONG_PTR

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_CLIPBOARDUPDATE = 0x031D

CS_VREDRAW = 0x0001
CS_HREDRAW = 0x0002

HWND_MESSAGE = wintypes.HWND(-3)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

AddClipboardFormatListener = user32.AddClipboardFormatListener
AddClipboardFormatListener.argtypes = [wintypes.HWND]
AddClipboardFormatListener.restype = wintypes.BOOL

RemoveClipboardFormatListener = user32.RemoveClipboardFormatListener
RemoveClipboardFormatListener.argtypes = [wintypes.HWND]
RemoveClipboardFormatListener.restype = wintypes.BOOL

GetMessageW = user32.GetMessageW
GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
GetMessageW.restype = wintypes.BOOL

TranslateMessage = user32.TranslateMessage
TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
TranslateMessage.restype = wintypes.BOOL

DispatchMessageW = user32.DispatchMessageW
DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
DispatchMessageW.restype = LRESULT

PostQuitMessage = user32.PostQuitMessage
PostQuitMessage.argtypes = [ctypes.c_int]
PostQuitMessage.restype = None

DefWindowProcW = user32.DefWindowProcW
DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
DefWindowProcW.restype = LRESULT

PostMessageW = user32.PostMessageW
PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
PostMessageW.restype = wintypes.BOOL

RegisterClassW = user32.RegisterClassW
CreateWindowExW = user32.CreateWindowExW
DestroyWindow = user32.DestroyWindow

WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, WPARAM, LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HCURSOR),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
RegisterClassW.restype = wintypes.ATOM

CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID
]
CreateWindowExW.restype = wintypes.HWND

DestroyWindow.argtypes = [wintypes.HWND]
DestroyWindow.restype = wintypes.BOOL


class ClipboardMonitor:
    APP_NAME = "URL to VLC"
    HISTORY_FILENAME = "lampa_urls.json"

    def __init__(self):
        self.monitoring = True
        self.is_active = True

        self.max_recent_urls = 5
        self.recent_urls = []
        self.lock = threading.Lock()

        # Вариант B (armed)
        self._armed_after_enable = False
        self._armed_baseline_text = ""
        self._armed_time = 0.0
        self.ARM_DELAY_SEC = 0.35

        # Антидребезг только для stream URL
        self._last_opened_url = ""
        self._last_opened_time = 0.0

        self.clip_queue: "queue.Queue[str]" = queue.Queue()
        self.gui_queue: "queue.Queue[callable]" = queue.Queue()

        self._listener_hwnd = None
        self._wndproc_ref = None

        self.vlc_path = self.find_vlc()
        self.load_history()

        if not self.vlc_path:
            print("VLC не найден. Убедитесь, что VLC установлен.")
            return

        self.icon = self.create_icon()
        # tooltip в трее
        self.tray_icon = pystray.Icon(self.APP_NAME, self.icon, title=self.get_tray_title())
        self.tray_icon.menu = self.create_menu()

        self.listener_thread = threading.Thread(target=self.clipboard_listener_thread, daemon=True)
        self.listener_thread.start()

        self.worker_thread = threading.Thread(target=self.clipboard_worker_thread, daemon=True)
        self.worker_thread.start()

        # Авто-обновление меню без кликов
        self.gui_pump_thread = threading.Thread(target=self.gui_pump_loop, daemon=True)
        self.gui_pump_thread.start()

    # ----------------- Tray tooltip -----------------
    def get_tray_title(self) -> str:
        return f"{self.APP_NAME} ({'Вкл' if self.is_active else 'Выкл'})"

    # ----------------- Paths рядом с exe -----------------
    def get_app_dir(self) -> str:
        return os.path.dirname(os.path.abspath(sys.argv[0]))

    def get_data_path(self, filename: str) -> str:
        return os.path.join(self.get_app_dir(), filename)

    def history_path(self) -> str:
        return self.get_data_path(self.HISTORY_FILENAME)

    # ----------------- VLC -----------------
    def find_vlc(self):
        possible_paths = [
            r"C:\Program Files\VideoLAN\VLC\vlc.exe",
            r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
            r"C:\VLC\vlc.exe"
        ]
        for path in possible_paths:
            if os.path.exists(path):
                return path
        try:
            result = subprocess.run("where vlc", capture_output=True, text=True, shell=True)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip().splitlines()[0]
        except Exception:
            pass
        return None

    def build_vlc_args(self, url: str) -> list[str]:
        args: list[str] = [self.vlc_path]
        if VLC_ONE_INSTANCE:
            args.append("--one-instance")
            args.append("--started-from-file")
            if VLC_ENQUEUE:
                args.append("--playlist-enqueue")
        args.append(url)
        return args

    def open_in_vlc(self, url: str):
        try:
            subprocess.Popen(self.build_vlc_args(url))
            self.show_notification(f"Открываю в VLC:\n{url[:70]}{'...' if len(url) > 70 else ''}")
            print(f"Открываю в VLC: {url}")
        except Exception as e:
            print(f"Ошибка при открытии VLC: {e}")
            self.show_notification(f"Ошибка открытия в VLC: {str(e)}")

    # ----------------- Tray icon -----------------
    def create_icon(self):
        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)

        outline = (15, 15, 15, 255)
        eye_fill = (255, 255, 255, 255)
        iris = (0, 150, 255, 255)
        pupil = (0, 0, 0, 255)

        pad_x = 5
        top = int(size * 0.14)
        bottom = int(size * 0.86)
        eye_box = (pad_x, top, size - pad_x, bottom)

        d.ellipse(eye_box, fill=eye_fill, outline=outline, width=4)

        cx, cy = size // 2, size // 2
        iris_r = int(size * 0.21)
        pupil_r = int(size * 0.085)
        d.ellipse((cx - iris_r, cy - iris_r, cx + iris_r, cy + iris_r), fill=iris)
        d.ellipse((cx - pupil_r, cy - pupil_r, cx + pupil_r, cy + pupil_r), fill=pupil)

        d.ellipse((cx - 10, cy - 10, cx - 6, cy - 6), fill=(255, 255, 255, 220))

        if not self.is_active:
            alpha = img.getchannel("A")
            gray = ImageOps.grayscale(img.convert("RGB"))
            img = Image.merge("RGBA", (gray, gray, gray, alpha))
            d = ImageDraw.Draw(img)
            d.line([(10, 54), (54, 10)], fill=(30, 30, 30, 230), width=5)

        return img

    def _enqueue_gui(self, fn):
        try:
            self.gui_queue.put(fn)
        except Exception:
            pass

    def _process_gui_queue(self, icon) -> bool:
        executed = False
        while True:
            try:
                fn = self.gui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn(icon)
                executed = True
            except Exception as e:
                print(f"Ошибка GUI action: {e}")
        return executed

    def _refresh_menu_and_icon(self, icon=None):
        if icon is None:
            icon = self.tray_icon

        try:
            icon.menu = self.create_menu()
        except Exception as e:
            print(f"Ошибка обновления меню: {e}")

        try:
            self.icon = self.create_icon()
            icon.icon = self.icon
        except Exception as e:
            print(f"Ошибка обновления иконки: {e}")

        # обновляем tooltip
        try:
            icon.title = self.get_tray_title()
        except Exception:
            pass

        try:
            icon.update_menu()
        except Exception:
            pass

    def gui_pump_loop(self):
        while self.monitoring:
            try:
                did = self._process_gui_queue(self.tray_icon)
                time.sleep(0.2 if did else 0.4)
            except Exception:
                time.sleep(0.5)

    # ----------------- History (рядом с exe) -----------------
    def load_history(self):
        path = self.history_path()
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                self.recent_urls = data.get('recent_urls', [])
        except Exception:
            self.recent_urls = []

    def save_history(self):
        path = self.history_path()
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({'recent_urls': self.recent_urls}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Ошибка сохранения истории: {e}")

    def extract_filename_from_url(self, url: str) -> str:
        try:
            parsed_url = urllib.parse.urlparse(url)
            filename = os.path.basename(parsed_url.path)
            if '.' in filename:
                name_without_ext = os.path.splitext(filename)[0]
                return urllib.parse.unquote(name_without_ext.replace('.', ' '))
            return urllib.parse.unquote(filename)
        except Exception:
            return url if len(url) <= 50 else url[:47] + "..."

    def add_to_recent(self, url: str):
        """Если URL уже есть в списке — НЕ добавляем и НЕ двигаем."""
        with self.lock:
            for it in self.recent_urls:
                if isinstance(it, dict) and it.get('url') == url:
                    return
                if isinstance(it, str) and it == url:
                    return

            display_name = self.extract_filename_from_url(url)
            self.recent_urls.insert(0, {'url': url, 'display_name': display_name})

            if len(self.recent_urls) > self.max_recent_urls:
                self.recent_urls = self.recent_urls[:self.max_recent_urls]

            self.save_history()

        self._enqueue_gui(lambda icon: self._refresh_menu_and_icon(icon))

    def remove_from_recent(self, index: int):
        with self.lock:
            if 0 <= index < len(self.recent_urls):
                self.recent_urls.pop(index)
                self.save_history()
        self._enqueue_gui(lambda icon: self._refresh_menu_and_icon(icon))

    def clear_all_recent(self, icon, item_):
        with self.lock:
            self.recent_urls = []
            self.save_history()
        self._refresh_menu_and_icon(icon)

    # ----------------- Autostart -----------------
    def is_autostart_enabled(self) -> bool:
        try:
            key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                winreg.QueryValueEx(key, self.APP_NAME)
                return True
        except Exception:
            return False

    def build_autostart_command(self) -> str:
        app_path = os.path.abspath(sys.argv[0])

        if app_path.endswith('.py'):
            exe_path = app_path[:-3] + '.exe'
            if os.path.exists(exe_path):
                return exe_path
            return f'"{sys.executable}" "{app_path}"'

        return app_path

    def toggle_autostart(self, icon, item_):
        try:
            key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"
            app_name = self.APP_NAME
            command = self.build_autostart_command()

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS) as key:
                try:
                    winreg.QueryValueEx(key, app_name)
                    winreg.DeleteValue(key, app_name)
                    self.show_notification("Автозапуск выключен")
                except FileNotFoundError:
                    winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, command)
                    self.show_notification("Автозапуск включён")

            self._refresh_menu_and_icon(icon)
        except Exception as e:
            self.show_notification(f"Ошибка автозапуска: {str(e)}")
            print(f"Ошибка автозапуска: {e}")

    # ----------------- Menu actions -----------------
    def create_open_url_function(self, url: str):
        def open_url(icon, item_):
            self.open_in_vlc(url)
        return open_url

    def create_remove_function(self, index: int):
        def remove_url(icon, item_):
            self.remove_from_recent(index)
        return remove_url

    def open_from_clipboard(self, icon, item_):
        text = ""
        try:
            text = pyperclip.paste()
        except Exception:
            text = ""

        text = (text or "").strip()
        if not text:
            self.show_notification("Буфер обмена пуст")
            return

        if not self.is_lampa_stream_url(text):
            self.show_notification("В буфере нет ссылки формата http(s)://host[:port]/stream/...")
            return

        if self.should_skip_open(text):
            return

        self.open_in_vlc(text)
        self.add_to_recent(text)

    def toggle_active(self, icon, item_):
        self.is_active = not self.is_active
        self.show_notification(f"Мониторинг {'включён' if self.is_active else 'выключен'}")
        self._refresh_menu_and_icon(icon)

        if self.is_active:
            try:
                baseline = pyperclip.paste()
            except Exception:
                baseline = ""
            baseline = (baseline or "").strip()

            self._armed_after_enable = True
            self._armed_baseline_text = baseline
            self._armed_time = time.time()
        else:
            self._armed_after_enable = False
            self._armed_baseline_text = ""
            self._armed_time = 0.0

    def quit_app(self, icon, item_):
        self.monitoring = False
        try:
            if self._listener_hwnd:
                PostMessageW(self._listener_hwnd, WM_CLOSE, 0, 0)
        except Exception:
            pass
        try:
            icon.stop()
        except Exception:
            pass

    def create_recent_url_menu(self):
        menu_items = []
        with self.lock:
            data = list(self.recent_urls)

        if not data:
            menu_items.append(item("Нет недавних ссылок", lambda icon, item_: None, enabled=False))
        else:
            for i, item_data in enumerate(data):
                if isinstance(item_data, dict):
                    url = item_data.get('url', '')
                    display_name = item_data.get('display_name') or self.extract_filename_from_url(url)
                else:
                    url = str(item_data)
                    display_name = self.extract_filename_from_url(url)

                display_text = display_name if len(display_name) <= 50 else display_name[:47] + "..."

                submenu = pystray.Menu(
                    item("Открыть", self.create_open_url_function(url)),
                    item("Удалить", self.create_remove_function(i))
                )
                menu_items.append(item(f"{i + 1}. {display_text}", submenu))

            menu_items.append(pystray.Menu.SEPARATOR)
            menu_items.append(item("Очистить все", self.clear_all_recent))

        return menu_items

    def create_menu(self):
        recent_menu = self.create_recent_url_menu()
        autostart_enabled = self.is_autostart_enabled()
        autostart_text = "Выключить автозапуск" if autostart_enabled else "Включить автозапуск"

        return pystray.Menu(
            pystray.Menu.SEPARATOR,
            item("Выключить" if self.is_active else "Включить", self.toggle_active),
            item("Открыть из буфера", self.open_from_clipboard),
            pystray.Menu.SEPARATOR,
            item(autostart_text, self.toggle_autostart),
            pystray.Menu.SEPARATOR,
            item("Последние ссылки", pystray.Menu(*recent_menu)),
            pystray.Menu.SEPARATOR,
            item("Выход", self.quit_app),
        )

    def show_notification(self, message: str, title: str | None = None):
        try:
            notification.notify(
                title=title or self.APP_NAME,
                message=message,
                app_name=self.APP_NAME,
                timeout=3
            )
        except Exception:
            pass

    # ----------------- URL matching -----------------
    def is_lampa_stream_url(self, text: str) -> bool:
        """http(s)://<любой host или ip>[:port]/stream/<...> (+ /ts/stream/<...>)"""
        try:
            s = (text or "").strip()
            if not s:
                return False

            p = urllib.parse.urlparse(s)
            if p.scheme not in ("http", "https"):
                return False
            if not p.netloc:
                return False

            path = p.path or ""
            if not path.startswith("/"):
                path = "/" + path

            return path.startswith("/stream/") or path.startswith("/ts/stream/")
        except Exception:
            return False

    # ----------------- Anti-bounce -----------------
    def should_skip_open(self, url: str) -> bool:
        now = time.time()
        if url == self._last_opened_url and (now - self._last_opened_time) < ANTI_BOUNCE_SEC:
            return True
        self._last_opened_url = url
        self._last_opened_time = now
        return False

    # ----------------- WinAPI Clipboard Listener (ctypes) -----------------
    def clipboard_listener_thread(self):
        class_name = "LampaToVlcClipboardListenerCtypes"
        hinst = kernel32.GetModuleHandleW(None)

        @WNDPROC
        def wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_CLIPBOARDUPDATE:
                try:
                    text = pyperclip.paste()
                except Exception:
                    text = ""
                self.clip_queue.put(text)
                return 0

            if msg == WM_CLOSE:
                try:
                    RemoveClipboardFormatListener(hwnd)
                except Exception:
                    pass
                DestroyWindow(hwnd)
                return 0

            if msg == WM_DESTROY:
                PostQuitMessage(0)
                return 0

            return DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc_ref = wndproc

        wc = WNDCLASSW()
        wc.style = CS_HREDRAW | CS_VREDRAW
        wc.lpfnWndProc = wndproc
        wc.cbClsExtra = 0
        wc.cbWndExtra = 0
        wc.hInstance = hinst
        wc.hIcon = 0
        wc.hCursor = 0
        wc.hbrBackground = 0
        wc.lpszMenuName = None
        wc.lpszClassName = class_name

        RegisterClassW(ctypes.byref(wc))

        hwnd = CreateWindowExW(
            0,
            class_name,
            "LampaToVlcHiddenWindow",
            0,
            0, 0, 0, 0,
            HWND_MESSAGE, 0, hinst, None
        )
        self._listener_hwnd = hwnd

        if not hwnd:
            print("Не удалось создать скрытое окно для listener-а буфера обмена.")
            return

        ok = AddClipboardFormatListener(hwnd)
        if not ok:
            print("Не удалось подписаться на буфер обмена (AddClipboardFormatListener вернул False).")

        msg = wintypes.MSG()
        while self.monitoring:
            res = GetMessageW(ctypes.byref(msg), 0, 0, 0)
            if res == 0:
                break
            if res == -1:
                break
            TranslateMessage(ctypes.byref(msg))
            DispatchMessageW(ctypes.byref(msg))

    def clipboard_worker_thread(self):
        while self.monitoring:
            try:
                text = self.clip_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if not self.is_active:
                continue

            if not isinstance(text, str):
                continue

            text = text.strip()
            if not text:
                continue

            if not self.is_lampa_stream_url(text):
                continue

            if self._armed_after_enable:
                now = time.time()
                if text != self._armed_baseline_text:
                    self._armed_after_enable = False
                else:
                    if (now - self._armed_time) < self.ARM_DELAY_SEC:
                        continue
                    self._armed_after_enable = False

            if self.should_skip_open(text):
                continue

            self.open_in_vlc(text)
            self.add_to_recent(text)

    def run(self):
        if not self.vlc_path:
            print("VLC не найден. Программа не может работать без VLC.")
            return

        print("Программа запущена. Слушаю буфер обмена через WinAPI (ctypes, без опроса).")
        print("История сохраняется рядом с exe/скриптом:", self.history_path())
        print("Программа работает в трее. Нажмите ПКМ для меню.")
        self.tray_icon.run()


def main():
    print("Запуск мониторинга буфера обмена...")
    try:
        app = ClipboardMonitor()
        app.run()
    except KeyboardInterrupt:
        print("\nПрограмма остановлена")
    except Exception as e:
        print(f"Ошибка: {e}")


if __name__ == "__main__":
    main()