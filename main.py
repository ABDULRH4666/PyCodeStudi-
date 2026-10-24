import io, contextlib, traceback
import jedi
from kivy.app import App
from kivy.clock import Clock
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.codeinput import CodeInput
from pygments.lexers import PythonLexer


class Root(BoxLayout):
    def __init__(self, **kw):
        super().__init__(orientation="vertical", **kw)

        top = BoxLayout(size_hint_y=None, height=48)
        run = Button(text="Run")
        run.bind(on_release=self.run_code)
        top.add_widget(run)
        self.add_widget(top)

        self.editor = CodeInput(lexer=PythonLexer(), font_size=16,
                                text="import math\nprint(math.sqrt(16))\n")
        self.editor.bind(text=self.schedule, cursor=self.schedule)
        self.add_widget(self.editor)

        # شريط الاقتراحات (الإكمال التلقائي)
        sv = ScrollView(size_hint_y=None, height=44, do_scroll_y=False)
        self.sug = BoxLayout(size_hint_x=None, spacing=4)
        self.sug.bind(minimum_width=self.sug.setter("width"))
        sv.add_widget(self.sug)
        self.add_widget(sv)

        # المخرجات
        out_sv = ScrollView(size_hint_y=0.3)
        self.out = Label(text="", size_hint_y=None, halign="left", valign="top")
        self.out.bind(texture_size=lambda i, s: setattr(i, "height", s[1]),
                      width=lambda i, w: setattr(i, "text_size", (w, None)))
        out_sv.add_widget(self.out)
        self.add_widget(out_sv)

    def schedule(self, *a):
        Clock.unschedule(self.complete)
        Clock.schedule_once(self.complete, 0.3)

    def complete(self, dt):
        self.sug.clear_widgets()
        try:
            row, col = self.editor.cursor_row, self.editor.cursor_col
            comps = jedi.Script(self.editor.text).complete(row + 1, col)[:15]
        except Exception:
            return
        for c in comps:
            b = Button(text=c.name, size_hint_x=None,
                       width=max(90, len(c.name) * 14))
            b.bind(on_release=lambda _, c=c: self.editor.insert_text(c.complete or ""))
            self.sug.add_widget(b)

    def run_code(self, *a):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                exec(compile(self.editor.text, "<editor>", "exec"), {"__name__": "__main__"})
            except Exception:
                traceback.print_exc()
        self.out.text = buf.getvalue()


class PyEditorApp(App):
    def build(self):
        return Root()


PyEditorApp().run()
