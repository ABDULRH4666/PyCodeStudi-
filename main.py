import contextlib, importlib, importlib.metadata as md, importlib.util
import io, json, os, re, shlex, ssl, sys, threading, traceback
import urllib.request, zipfile

import jedi
from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.metrics import dp, sp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.codeinput import CodeInput
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.screenmanager import NoTransition, Screen, ScreenManager
from kivy.uix.scrollview import ScrollView
from kivy.uix.slider import Slider
from kivy.uix.switch import Switch
from kivy.uix.textinput import TextInput
from kivy.utils import get_color_from_hex as hx
from pygments.lexers import PythonLexer

from catalog import CATALOG

PAL = {
    True: dict(bg="#111827", editor="#0B1220", bar="#1F2937",
               text="#E5E7EB", accent="#2563EB", style="monokai"),
    False: dict(bg="#F9FAFB", editor="#FFFFFF", bar="#E5E7EB",
                text="#111827", accent="#2563EB", style="default"),
}
DEFAULTS = {"dark": True, "font_size": 16, "autocomplete": True,
            "confirm_run": True}
MODE_TEXT = {
    "pure": "install from Terminal",
    "build": "add at build time",
    "limited": "limited support",
    "no": "not supported",
    "builtin": "included",
}
SAMPLE = "import math\n\nprint('Hello from PyCode Studio')\nprint(math.sqrt(16))\n"
HELP = """Commands:
  help | clear | ls | cat <file> | rm <file>
  python <file>          run a project file
  pip install <pkg...>   pure-Python wheels only (version pins are ignored)
  pip list
"""


def app():
    return App.get_running_app()


# ---------- small themed widgets ----------
def mk_btn(text, cb=None, accent=False, reg=True, **kw):
    b = Button(text=text, background_normal="", **kw)

    def th(p):
        b.background_color = hx(p["accent"] if accent else p["bar"])
        b.color = (1, 1, 1, 1) if accent else hx(p["text"])

    if reg:
        app().register(th)
    else:
        th(app().pal)
    if cb:
        b.bind(on_release=cb)
    return b


def mk_label(text="", **kw):
    halign = kw.pop("halign", "left")
    lab = Label(text=text, halign=halign, valign="middle", **kw)
    lab.bind(size=lambda i, s: setattr(i, "text_size", s))
    app().register(lambda p: setattr(lab, "color", hx(p["text"])))
    return lab


class LogView(ScrollView):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.lab = Label(text="", size_hint_y=None, halign="left",
                         valign="top", font_name="RobotoMono-Regular")
        self.lab.bind(
            texture_size=lambda i, s: setattr(i, "height", max(s[1], self.height)),
            width=lambda i, w: setattr(i, "text_size", (w, None)))
        self.add_widget(self.lab)
        app().register(lambda p: setattr(self.lab, "color", hx(p["text"])))

    def add(self, s):
        t = self.lab.text + s
        self.lab.text = t[-20000:]
        Clock.schedule_once(lambda dt: setattr(self, "scroll_y", 0), 0.05)

    def clear(self):
        self.lab.text = ""


# ---------- running code / pip ----------
class Stream(io.TextIOBase):
    def __init__(self, cb):
        self.cb = cb

    def write(self, s):
        if s:
            self.cb(s)
        return len(s)

    def flush(self):
        pass


def run_python(code, name, write):
    stream = Stream(write)
    with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
        try:
            exec(compile(code, name or "<editor>", "exec"), {"__name__": "__main__"})
        except BaseException:
            traceback.print_exc()


def _ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def pip_install(pkg, log, site, seen):
    pkg = re.split(r"[<>=!~\[;\s]", pkg.strip())[0].lower().replace("_", "-")
    if not pkg or pkg in seen:
        return
    seen.add(pkg)
    log(f"Collecting {pkg}\n")
    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{pkg}/json",
                                    timeout=20, context=_ctx()) as r:
            data = json.load(r)
        wheel = next((u for u in data["urls"]
                      if u["filename"].endswith("py3-none-any.whl")), None)
        if not wheel:
            log("  no pure-Python wheel; add it at build time instead\n")
            return
        with urllib.request.urlopen(wheel["url"], timeout=60, context=_ctx()) as r:
            blob = r.read()
        tmp = os.path.join(site, wheel["filename"])
        with open(tmp, "wb") as f:
            f.write(blob)
        with zipfile.ZipFile(tmp) as z:
            z.extractall(site)
            meta = next((n for n in z.namelist()
                         if n.endswith(".dist-info/METADATA")), None)
            text = z.read(meta).decode("utf8", "ignore") if meta else ""
        os.remove(tmp)
        importlib.invalidate_caches()
        log(f"  installed {data['info']['name']} {data['info']['version']}\n")
    except Exception as e:
        log(f"  error: {e}\n")
        return
    skipped = False
    for line in text.splitlines():
        if line.startswith("Requires-Dist:"):
            if ";" in line:
                skipped = True
                continue
            dep = re.split(r"[<>=!~\s(\[]", line.split(":", 1)[1].strip(), 1)[0]
            pip_install(dep, log, site, seen)
    if skipped:
        log("  note: dependencies with conditions were skipped\n")


def confirm(on_yes):
    box = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(10))
    msg = Label(text="This code runs inside the app with access to its "
                     "files and network. Run it?", halign="center")
    msg.bind(size=lambda i, s: setattr(i, "text_size", (s[0], None)))
    row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(10))
    pop = Popup(title="Run code?", content=box, size_hint=(0.85, 0.4))
    yes, no = Button(text="Run"), Button(text="Cancel")
    yes.bind(on_release=lambda *_: (pop.dismiss(), on_yes()))
    no.bind(on_release=pop.dismiss)
    row.add_widget(no)
    row.add_widget(yes)
    box.add_widget(msg)
    box.add_widget(row)
    pop.open()


def ask_name(on_ok):
    box = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(10))
    ti = TextInput(hint_text="file name, e.g. utils.py", multiline=False)
    row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(10))
    pop = Popup(title="New file", content=box, size_hint=(0.85, 0.35))
    ok, cancel = Button(text="Create"), Button(text="Cancel")

    def go(*_):
        name = os.path.basename(ti.text.strip())
        pop.dismiss()
        if name:
            on_ok(name if "." in name else name + ".py")

    ok.bind(on_release=go)
    cancel.bind(on_release=pop.dismiss)
    row.add_widget(cancel)
    row.add_widget(ok)
    box.add_widget(ti)
    box.add_widget(row)
    pop.open()


# ---------- screens ----------
class EditorScreen(Screen):
    SHORTCUTS = ["Tab", "(", ")", "[", "]", "{", "}", ":", '"', "'", "=",
                 "#", ".", ",", "_"]

    def __init__(self, **kw):
        super().__init__(name="editor", **kw)
        self.path = None
        box = BoxLayout(orientation="vertical")

        top = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(4))
        top.add_widget(mk_btn("Run", self.run, accent=True,
                              size_hint_x=None, width=dp(80)))
        top.add_widget(mk_btn("Save", self.save, size_hint_x=None, width=dp(80)))
        self.title_lab = mk_label("")
        top.add_widget(self.title_lab)
        box.add_widget(top)

        self.editor = CodeInput(lexer=PythonLexer(), auto_indent=True)
        self.editor.bind(text=self.schedule, cursor=self.schedule)
        box.add_widget(self.editor)

        self.log = LogView(size_hint_y=0.25)
        box.add_widget(self.log)

        sug_sv = ScrollView(size_hint_y=None, height=dp(44), do_scroll_y=False)
        self.sug = BoxLayout(size_hint_x=None, spacing=dp(4))
        self.sug.bind(minimum_width=self.sug.setter("width"))
        sug_sv.add_widget(self.sug)
        box.add_widget(sug_sv)

        keys = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(2))
        for k in self.SHORTCUTS:
            keys.add_widget(mk_btn(k, lambda _, k=k: self.put("    " if k == "Tab" else k)))
        box.add_widget(keys)
        self.add_widget(box)
        app().register(self.apply)

    def apply(self, p):
        e = self.editor
        if e.style_name != p["style"]:
            e.style_name = p["style"]
        e.background_color = hx(p["editor"])
        e.foreground_color = hx(p["text"])
        e.cursor_color = hx(p["accent"])
        e.font_size = sp(app().cfg["font_size"])

    def load(self, path):
        self.path = path
        with open(path, encoding="utf8") as f:
            self.editor.text = f.read()
        self.title_lab.text = os.path.basename(path)

    def save(self, *_):
        if self.path:
            with open(self.path, "w", encoding="utf8") as f:
                f.write(self.editor.text)

    def put(self, s):
        self.editor.insert_text(s)
        Clock.schedule_once(lambda dt: setattr(self.editor, "focus", True))

    def schedule(self, *_):
        Clock.unschedule(self._complete)
        if app().cfg["autocomplete"]:
            Clock.schedule_once(self._complete, 0.35)
        else:
            self.sug.clear_widgets()

    def _complete(self, dt):
        self.sug.clear_widgets()
        row, col = self.editor.cursor_row, self.editor.cursor_col
        lines = self.editor.text.split("\n")
        line = lines[row] if row < len(lines) else ""
        if col == 0 or col > len(line) or not (line[col - 1].isalnum() or line[col - 1] in "._"):
            return
        try:
            script = jedi.Script(self.editor.text, project=app().jedi_project)
            comps = script.complete(row + 1, col)[:20]
        except Exception:
            return
        for c in comps:
            b = mk_btn(c.name, lambda _, c=c: self.pick(c), reg=False,
                       size_hint_x=None, width=max(dp(90), len(c.name) * dp(11)))
            self.sug.add_widget(b)

    def pick(self, c):
        tail = c.complete or ""
        if c.type == "function":
            self.editor.insert_text(tail + "()")
            self.editor.do_cursor_movement("cursor_left")
        else:
            self.editor.insert_text(tail)
        Clock.schedule_once(lambda dt: setattr(self.editor, "focus", True))

    def run(self, *_):
        self.save()
        if app().cfg["confirm_run"]:
            confirm(self._start)
        else:
            self._start()

    def _start(self):
        self.log.clear()
        code, name = self.editor.text, self.path
        w = lambda s: Clock.schedule_once(lambda dt: self.log.add(s))
        threading.Thread(target=run_python, args=(code, name, w), daemon=True).start()


class FilesScreen(Screen):
    def __init__(self, **kw):
        super().__init__(name="files", **kw)
        box = BoxLayout(orientation="vertical")
        box.add_widget(mk_btn("+ New file", self.new_file, accent=True,
                              size_hint_y=None, height=dp(44)))
        sv = ScrollView()
        self.grid = GridLayout(cols=1, size_hint_y=None, spacing=dp(4))
        self.grid.bind(minimum_height=self.grid.setter("height"))
        sv.add_widget(self.grid)
        box.add_widget(sv)
        self.add_widget(box)

    def on_pre_enter(self, *_):
        self.refresh()

    def refresh(self):
        self.grid.clear_widgets()
        for n in sorted(os.listdir(app().proj_dir)):
            row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(4))
            row.add_widget(mk_btn(n, lambda _, n=n: app().open_file(n), reg=False))
            row.add_widget(mk_btn("Delete", lambda _, n=n: self.delete(n),
                                  reg=False, size_hint_x=None, width=dp(80)))
            self.grid.add_widget(row)

    def new_file(self, *_):
        ask_name(app().open_file)

    def delete(self, name):
        path = os.path.join(app().proj_dir, name)
        os.remove(path)
        ed = app().root.sm.get_screen("editor")
        if ed.path == path:
            ed.path = None
            rest = sorted(os.listdir(app().proj_dir))
            app().ensure_main()
            ed.load(os.path.join(app().proj_dir, rest[0] if rest else "main.py"))
        self.refresh()


class TerminalScreen(Screen):
    def __init__(self, **kw):
        super().__init__(name="terminal", **kw)
        box = BoxLayout(orientation="vertical")
        self.log = LogView()
        self.log.add("PyCode Studio terminal. Type help\n")
        self.inp = TextInput(multiline=False, size_hint_y=None, height=dp(44),
                             hint_text="command", write_tab=False)
        self.inp.bind(on_text_validate=self.on_cmd)
        box.add_widget(self.log)
        box.add_widget(self.inp)
        self.add_widget(box)

    def w(self, s):
        Clock.schedule_once(lambda dt: self.log.add(s))

    def on_cmd(self, inst):
        cmd = inst.text.strip()
        inst.text = ""
        Clock.schedule_once(lambda dt: setattr(inst, "focus", True))
        if cmd:
            self.log.add(f"$ {cmd}\n")
            threading.Thread(target=self.dispatch, args=(cmd,), daemon=True).start()

    def dispatch(self, cmd):
        try:
            a = shlex.split(cmd)
            c, rest = a[0], a[1:]
            pdir = app().proj_dir
            path = lambda n: os.path.join(pdir, os.path.basename(n))
            if c == "help":
                self.w(HELP)
            elif c == "clear":
                Clock.schedule_once(lambda dt: self.log.clear())
            elif c == "ls":
                self.w("\n".join(sorted(os.listdir(pdir))) + "\n")
            elif c == "cat" and rest:
                with open(path(rest[0]), encoding="utf8") as f:
                    self.w(f.read() + "\n")
            elif c == "rm" and rest:
                os.remove(path(rest[0]))
            elif c == "python" and rest:
                with open(path(rest[0]), encoding="utf8") as f:
                    run_python(f.read(), path(rest[0]), self.w)
            elif c == "pip" and rest[:1] == ["list"]:
                items = sorted({f"{d.metadata['Name']} {d.version}"
                                for d in md.distributions() if d.metadata["Name"]})
                self.w("\n".join(items) + "\n")
            elif c == "pip" and rest[:1] == ["install"] and len(rest) > 1:
                seen = set()
                for pkg in rest[1:]:
                    pip_install(pkg, self.w, app().site_dir, seen)
            else:
                self.w("unknown command, type help\n")
        except Exception:
            self.w(traceback.format_exc())


class LibsScreen(Screen):
    def __init__(self, **kw):
        super().__init__(name="libs", **kw)
        box = BoxLayout(orientation="vertical")
        self.search = TextInput(hint_text="Search libraries", multiline=False,
                                size_hint_y=None, height=dp(44))
        self.search.bind(text=self.refresh)
        sv = ScrollView()
        self.grid = GridLayout(cols=1, size_hint_y=None, spacing=dp(6))
        self.grid.bind(minimum_height=self.grid.setter("height"))
        sv.add_widget(self.grid)
        box.add_widget(self.search)
        box.add_widget(sv)
        self.add_widget(box)

    def on_pre_enter(self, *_):
        self.refresh()

    @staticmethod
    def _has(mod):
        try:
            return importlib.util.find_spec(mod) is not None
        except Exception:
            return False

    def refresh(self, *_):
        q = self.search.text.lower().strip()
        col = hx(app().pal["text"])
        self.grid.clear_widgets()
        for e in CATALOG:
            if q and q not in (e["name"] + e["category"] + e["note"]).lower():
                continue
            status = "available now" if self._has(e["module"]) else MODE_TEXT[e["mode"]]
            lab = Label(text=f"{e['name']}  [{e['category']}]  -  {status}\n{e['note']}",
                        size_hint_y=None, height=dp(72), halign="left",
                        valign="middle", color=col)
            lab.bind(size=lambda i, s: setattr(i, "text_size", s))
            self.grid.add_widget(lab)


class SettingsScreen(Screen):
    def __init__(self, **kw):
        super().__init__(name="settings", **kw)
        box = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(10))
        for key, text in (("dark", "Dark theme"),
                          ("autocomplete", "Autocomplete"),
                          ("confirm_run", "Ask before running code")):
            row = BoxLayout(size_hint_y=None, height=dp(48))
            row.add_widget(mk_label(text))
            sw = Switch(active=app().cfg[key])
            sw.bind(active=lambda i, v, k=key: app().set_cfg(k, v))
            row.add_widget(sw)
            box.add_widget(row)
        self.fs_lab = mk_label(f"Font size: {app().cfg['font_size']}",
                               size_hint_y=None, height=dp(36))
        sl = Slider(min=10, max=30, step=1, value=app().cfg["font_size"],
                    size_hint_y=None, height=dp(44))
        sl.bind(value=self.on_font)
        box.add_widget(self.fs_lab)
        box.add_widget(sl)
        box.add_widget(BoxLayout())
        self.add_widget(box)

    def on_font(self, inst, v):
        self.fs_lab.text = f"Font size: {int(v)}"
        app().set_cfg("font_size", int(v))


class RootUI(BoxLayout):
    def __init__(self, **kw):
        super().__init__(orientation="vertical", **kw)

        def bg(p):
            Window.clearcolor = hx(p["bg"])

        app().register(bg)
        self.sm = ScreenManager(transition=NoTransition())
        for s in (EditorScreen(), FilesScreen(), TerminalScreen(),
                  LibsScreen(), SettingsScreen()):
            self.sm.add_widget(s)
        nav = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(2))
        for label, name in (("Editor", "editor"), ("Files", "files"),
                            ("Terminal", "terminal"), ("Libs", "libs"),
                            ("Settings", "settings")):
            nav.add_widget(mk_btn(label, lambda _, n=name: setattr(self.sm, "current", n)))
        self.add_widget(nav)
        self.add_widget(self.sm)
        Window.bind(on_keyboard=self.on_key)

    def on_key(self, win, key, *a):
        if key == 27 and self.sm.current != "editor":
            self.sm.current = "editor"
            return True
        return False


class PyCodeStudioApp(App):
    title = "PyCode Studio"

    def build(self):
        self.proj_dir = os.path.join(self.user_data_dir, "projects")
        self.site_dir = os.path.join(self.user_data_dir, "site-packages")
        for d in (self.proj_dir, self.site_dir):
            os.makedirs(d, exist_ok=True)
            if d not in sys.path:
                sys.path.insert(0, d)
        self.cfg_path = os.path.join(self.user_data_dir, "settings.json")
        self.cfg = dict(DEFAULTS)
        try:
            with open(self.cfg_path, encoding="utf8") as f:
                self.cfg.update(json.load(f))
        except Exception:
            pass
        self.pal = PAL[bool(self.cfg["dark"])]
        self.themers = []
        self.jedi_project = jedi.Project(path=self.proj_dir)
        self.ensure_main()
        Window.softinput_mode = "resize"
        self.root = RootUI()
        self.root.sm.get_screen("editor").load(os.path.join(self.proj_dir, "main.py"))
        return self.root

    def ensure_main(self):
        if not os.listdir(self.proj_dir):
            with open(os.path.join(self.proj_dir, "main.py"), "w", encoding="utf8") as f:
                f.write(SAMPLE)

    def register(self, fn):
        self.themers.append(fn)
        
