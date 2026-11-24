import asyncio
import concurrent.futures
import glob
import os
import threading

from kivy.app import App
from kivy.clock import Clock
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.checkbox import CheckBox
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.progressbar import ProgressBar
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
from kivy.uix.textinput import TextInput

from telethon import TelegramClient, functions, types
from telethon.errors import (
    FloodWaitError,
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PasswordHashInvalidError,
)

API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"

KEEP_IDS = set()   # id, которые не трогать никогда

CATEGORIES = [
    ("people",   "Люди (личные переписки)"),
    ("bots",     "Боты (удалить + заблокировать)"),
    ("channels", "Каналы (выйти)"),
    ("groups",   "Чаты и группы (выйти)"),
    ("contacts", "Контакты"),
]
TITLES = dict(CATEGORIES)

SPEEDS = {
    "Медленно, 2.0 сек": 2.0,
    "Обычно, 0.7 сек": 0.7,
    "Быстро, 0.3 сек": 0.3,
    "Своя пауза (поле ниже)": None,
}

loop = asyncio.new_event_loop()
threading.Thread(target=loop.run_forever, daemon=True).start()


def classify(ent):
    if isinstance(ent, types.User):
        return "bots" if ent.bot else "people"
    if isinstance(ent, types.Channel):
        return "channels" if ent.broadcast else "groups"
    if isinstance(ent, types.Chat):
        return "groups"
    return None


def mk_button(text, cb, h=46):
    b = Button(text=text, size_hint_y=None, height=dp(h))
    b.bind(on_release=lambda *_: cb())
    return b


class Cleaner(App):
    title = "TG Cleaner"

    # ---------- UI ----------

    def build(self):
        self.sdir = self.user_data_dir
        os.makedirs(self.sdir, exist_ok=True)
        self.client = None
        self.me = None
        self.groups = {}
        self.contacts = []
        self.sessions = []
        self.labels = []
        self.busy = False
        self.cleaning = False
        self.stop_event = threading.Event()

        root = BoxLayout(orientation="vertical", padding=dp(6), spacing=dp(4))

        sv = ScrollView(size_hint_y=0.62)
        col = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(6))
        col.bind(minimum_height=col.setter("height"))
        sv.add_widget(col)

        # Сессия
        self.sp_sess = Spinner(text="", size_hint_y=None, height=dp(46))
        col.add_widget(self.sp_sess)
        self.btn_connect = mk_button("Подключиться", self.on_connect)
        self.btn_new = mk_button("Новая сессия", self.on_new)
        self.btn_refresh = mk_button("Обновить список", self.refresh_sessions)
        for b in (self.btn_connect, self.btn_new, self.btn_refresh):
            col.add_widget(b)
        self.status = Label(text="Не подключено", size_hint_y=None, height=dp(34),
                            color=(0.7, 0.7, 0.7, 1))
        col.add_widget(self.status)
        self.btn_disc = mk_button("Отключиться (сессия сохранится)", self.on_disconnect)
        self.btn_logout = mk_button("Выйти из сессии (удалить)", self.on_logout)
        col.add_widget(self.btn_disc)
        col.add_widget(self.btn_logout)

        # Категории
        col.add_widget(Label(text="Что удалять:", size_hint_y=None, height=dp(30)))
        self.cat_cbs, self.cat_lbls = {}, {}
        for key, title in CATEGORIES:
            row, cb, lbl = self.check_row(f"{title} - ?")
            col.add_widget(row)
            self.cat_cbs[key], self.cat_lbls[key] = cb, lbl
        row, self.cb_revoke, _ = self.check_row("Удалять переписки и у собеседника")
        col.add_widget(row)
        row, self.cb_block, _ = self.check_row("Блокировать ботов (чёрный список)", True)
        col.add_widget(row)
        self.btn_all = mk_button("Отметить всё", lambda: self.set_all(True))
        self.btn_none = mk_button("Снять всё", lambda: self.set_all(False))
        self.btn_rescan = mk_button("Пересканировать", self.on_rescan)
        for b in (self.btn_all, self.btn_none, self.btn_rescan):
            col.add_widget(b)

        # Скорость
        col.add_widget(Label(text="Скорость:", size_hint_y=None, height=dp(30)))
        self.sp_speed = Spinner(text="Обычно, 0.7 сек", values=list(SPEEDS),
                                size_hint_y=None, height=dp(46))
        col.add_widget(self.sp_speed)
        self.ti_delay = TextInput(text="1.5", multiline=False, input_filter="float",
                                  hint_text="Своя пауза, сек", size_hint_y=None, height=dp(44))
        col.add_widget(self.ti_delay)

        # Запуск
        self.btn_start = mk_button("НАЧАТЬ УДАЛЕНИЕ", self.on_start, 54)
        self.btn_stop = mk_button("Стоп", self.stop_clean)
        col.add_widget(self.btn_start)
        col.add_widget(self.btn_stop)
        root.add_widget(sv)

        self.pb = ProgressBar(max=1, value=0, size_hint_y=None, height=dp(14))
        root.add_widget(self.pb)

        self.log_sv = ScrollView(size_hint_y=0.3)
        self.log_label = Label(text="", size_hint_y=None, halign="left", valign="top",
                               font_size=dp(12))
        self.log_label.bind(width=lambda i, w: setattr(i, "text_size", (w, None)))
        self.log_label.bind(texture_size=lambda i, s: setattr(i, "height", s[1]))
        self.log_sv.add_widget(self.log_label)
        root.add_widget(self.log_sv)

        self.w_sess = [self.sp_sess, self.btn_connect, self.btn_new, self.btn_refresh]
        self.w_conn = [self.btn_disc, self.btn_logout, self.btn_all, self.btn_none,
                       self.btn_rescan, self.btn_start, self.cb_revoke, self.cb_block,
                       self.sp_speed, self.ti_delay] + list(self.cat_cbs.values())

        self.update_states()
        Clock.schedule_once(lambda dt: self.refresh_sessions(), 0.2)
        return root

    def check_row(self, text, active=False):
        row = BoxLayout(size_hint_y=None, height=dp(44))
        cb = CheckBox(size_hint_x=None, width=dp(48), active=active)
        lbl = Label(text=text, halign="left", valign="middle")
        lbl.bind(size=lambda i, s: setattr(i, "text_size", s))
        row.add_widget(cb)
        row.add_widget(lbl)
        return row, cb, lbl

    def update_states(self):
        connected = self.client is not None
        idle = not self.busy
        for w in self.w_sess:
            w.disabled = not (idle and not connected)
        for w in self.w_conn:
            w.disabled = not (idle and connected)
        self.btn_stop.disabled = not self.cleaning

    def set_busy(self, busy):
        self.busy = busy
        self.update_states()

    def set_all(self, state):
        for cb in self.cat_cbs.values():
            cb.active = state

    # ---------- диалоги и потоки ----------

    def ui(self, fn, *args):
        Clock.schedule_once(lambda dt: fn(*args))

    def log(self, msg):
        def _do(dt):
            self.log_label.text = (self.log_label.text + msg + "\n")[-20000:]
            self.log_sv.scroll_y = 0
        Clock.schedule_once(_do)

    def popup(self, title, content, h=0.5):
        p = Popup(title=title, content=content, size_hint=(0.92, h), auto_dismiss=False)
        p.open()
        return p

    def info(self, text):
        box = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(8))
        lbl = Label(text=text, halign="center", valign="middle")
        lbl.bind(size=lambda i, s: setattr(i, "text_size", s))
        box.add_widget(lbl)
        b = mk_button("OK", lambda: p.dismiss())
        box.add_widget(b)
        p = self.popup("Сообщение", box, 0.4)

    def prompt(self, title, text, cb, secret=False):
        box = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(8))
        lbl = Label(text=text, halign="center", valign="middle")
        lbl.bind(size=lambda i, s: setattr(i, "text_size", s))
        ti = TextInput(multiline=False, password=secret, size_hint_y=None, height=dp(44))
        row = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(8))
        ok, cancel = Button(text="OK"), Button(text="Отмена")
        row.add_widget(ok)
        row.add_widget(cancel)
        for w in (lbl, ti, row):
            box.add_widget(w)
        p = self.popup(title, box, 0.45)

        def done(val):
            p.dismiss()
            cb(val)
        ok.bind(on_release=lambda *_: done(ti.text))
        cancel.bind(on_release=lambda *_: done(None))
        Clock.schedule_once(lambda dt: setattr(ti, "focus", True), 0.3)

    def confirm(self, title, text, on_yes):
        box = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(8))
        lbl = Label(text=text, halign="center", valign="middle")
        lbl.bind(size=lambda i, s: setattr(i, "text_size", s))
        row = BoxLayout(size_hint_y=None, height=dp(50), spacing=dp(8))
        yes, no = Button(text="Да, удалить"), Button(text="Отмена")
        row.add_widget(yes)
        row.add_widget(no)
        box.add_widget(lbl)
        box.add_widget(row)
        p = self.popup(title, box, 0.7)
        yes.bind(on_release=lambda *_: (p.dismiss(), on_yes()))
        no.bind(on_release=lambda *_: p.dismiss())

    async def ask(self, title, text, secret=False):
        fut = concurrent.futures.Future()
        Clock.schedule_once(lambda dt: self.prompt(title, text, fut.set_result, secret))
        return await asyncio.get_running_loop().run_in_executor(None, fut.result)

    def submit(self, coro, busy=True):
        if busy:
            self.ui(self.set_busy, True)

        async def wrapper():
            try:
                await coro
            except Exception as e:
                self.log(f"Ошибка: {e}")
            finally:
                if busy:
                    self.ui(self.set_busy, False)
        asyncio.run_coroutine_threadsafe(wrapper(), loop)

    async def safe(self, func, *args, **kwargs):
        while True:
            try:
                return await func(*args, **kwargs)
            except FloodWaitError as e:
                self.log(f"  FloodWait: жду {e.seconds} сек...")
                await asyncio.sleep(e.seconds + 1)

    # ---------- сессии ----------

    def sel_index(self):
        try:
            return list(self.sp_sess.values).index(self.sp_sess.text)
        except ValueError:
            return None

    def apply_labels(self, keep=0):
        if self.labels:
            self.sp_sess.values = self.labels
            self.sp_sess.text = self.labels[keep if keep < len(self.labels) else 0]
        else:
            self.sp_sess.values = []
            self.sp_sess.text = "Нет сессий - нажми «Новая сессия»"

    def refresh_sessions(self):
        self.sessions = sorted(glob.glob(os.path.join(self.sdir, "*.session")))
        self.labels = [os.path.basename(p) + " - проверяю..." for p in self.sessions]
        self.apply_labels()
        if self.sessions:
            self.submit(self.load_infos(list(self.sessions)), busy=False)

    async def session_info(self, path):
        client = TelegramClient(path[:-len(".session")], API_ID, API_HASH)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                return "не авторизована"
            me = await client.get_me()
            uname = f"@{me.username}" if me.username else "без username"
            return f"{me.first_name} ({uname}) +{me.phone}"
        except Exception as e:
            return f"ошибка: {e}"
        finally:
            await client.disconnect()

    async def load_infos(self, paths):
        for i, p in enumerate(paths):
            info = await self.session_info(p)
            self.ui(self.set_label, i, f"{os.path.basename(p)} - {info}")

    def set_label(self, i, text):
        if i < len(self.labels):
            keep = self.sel_index() or 0
            self.labels[i] = text
            self.apply_labels(keep)

    def on_connect(self):
        i = self.sel_index()
        if i is None or i >= len(self.sessions):
            self.info("Выбери сессию в списке\nили создай новую.")
            return
        self.submit(self.connect_session(self.sessions[i][:-len(".session")], False))

    def on_new(self):
        def got(name):
            if not name:
                return
            name = "".join(c for c in name if c.isalnum() or c in "-_") or "new_session"
            if os.path.exists(os.path.join(self.sdir, name + ".session")):
                self.info("Сессия с таким именем уже есть,\nвыбери её в списке.")
                return
            self.submit(self.connect_session(os.path.join(self.sdir, name), True))
        self.prompt("Новая сессия", "Имя сессии (например acc2):", got)

    async def login(self, client):
        phone = await self.ask("Вход", "Номер телефона (+372...):")
        if not phone:
            return False
        phone = phone.strip()
        await client.send_code_request(phone)
        for _ in range(3):
            code = await self.ask("Вход", "Код из Telegram:")
            if not code:
                return False
            try:
                await client.sign_in(phone, code.strip())
                return True
            except SessionPasswordNeededError:
                for _ in range(3):
                    pwd = await self.ask("Вход", "Облачный пароль (2FA):", True)
                    if pwd is None:
                        return False
                    try:
                        await client.sign_in(password=pwd)
                        return True
                    except PasswordHashInvalidError:
                        self.log("Неверный пароль, ещё раз.")
                return False
            except PhoneCodeInvalidError:
                self.log("Неверный код, ещё раз.")
            except PhoneCodeExpiredError:
                self.log("Код устарел, подключись заново.")
                return False
        return False

    async def connect_session(self, stem, is_new):
        client = TelegramClient(stem, API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            if not await self.login(client):
                await client.disconnect()
                if is_new:
                    try:
                        os.remove(stem + ".session")
                    except OSError:
                        pass
                self.log("Вход не выполнен.")
                self.ui(self.refresh_sessions)
                return
        me = await client.get_me()
        self.client, self.me = client, me
        uname = f"@{me.username}" if me.username else "без username"
        txt = f"Вошёл как: {me.first_name} ({uname}) id={me.id}"
        self.ui(self.set_status, txt, (0.3, 0.9, 0.3, 1))
        self.log(txt)
        await self.do_scan()

    def set_status(self, text, color):
        self.status.text = text
        self.status.color = color

    def reset_state(self):
        self.client = None
        self.me = None
        self.groups = {}
        self.contacts = []
        self.set_status("Не подключено", (0.7, 0.7, 0.7, 1))
        for key, title in CATEGORIES:
            self.cat_lbls[key].text = f"{title} - ?"
            self.cat_cbs[key].active = False
        self.pb.value = 0
        self.refresh_sessions()

    def on_disconnect(self):
        async def job():
            await self.client.disconnect()
            self.ui(self.reset_state)
            self.log("Отключено, сессия сохранена.")
        self.submit(job())

    def on_logout(self):
        async def job():
            try:
                ok = await self.client.log_out()
                self.log("Сессия завершена, файл удалён." if ok else "Не удалось выйти.")
            except Exception as e:
                self.log(f"Ошибка при выходе: {e}")
                await self.client.disconnect()
            self.ui(self.reset_state)
        self.confirm("Выход из сессии",
                     "Завершить сессию на сервере Telegram\nи удалить файл сессии?",
                     lambda: self.submit(job()))

    # ---------- сканирование ----------

    async def scan(self):
        groups = {"people": [], "bots": [], "channels": [], "groups": []}
        async for d in self.client.iter_dialogs():
            if d.id == self.me.id or d.id == 777000 or d.id in KEEP_IDS:
                continue
            kind = classify(d.entity)
            if kind:
                groups[kind].append(d)
        res = await self.safe(self.client, functions.contacts.GetContactsRequest(hash=0))
        return groups, [u for u in res.users if u.id not in KEEP_IDS]

    async def do_scan(self):
        self.log("Сканирую аккаунт...")
        self.groups, self.contacts = await self.scan()
        self.ui(self.show_counts)
        self.log("Сканирование завершено.")

    def show_counts(self):
        for key, title in CATEGORIES:
            n = len(self.contacts) if key == "contacts" else len(self.groups.get(key, []))
            self.cat_lbls[key].text = f"{title} - {n}"

    def on_rescan(self):
        self.submit(self.do_scan())

    # ---------- удаление ----------

    def get_delay(self):
        v = SPEEDS.get(self.sp_speed.text)
        if v is not None:
            return v
        try:
            return max(0.0, float(self.ti_delay.text.replace(",", ".")))
        except ValueError:
            return None

    def stop_clean(self):
        self.stop_event.set()
        self.log("Останавливаю после текущего действия...")

    def on_start(self):
        selected = {k for k, cb in self.cat_cbs.items() if cb.active}
        if not selected:
            self.info("Ничего не выбрано.")
            return
        delay = self.get_delay()
        if delay is None:
            self.info("Некорректная пауза.")
            return
        revoke, block_bots = self.cb_revoke.active, self.cb_block.active
        names = "\n".join("- " + TITLES[k] for k, _ in CATEGORIES if k in selected)
        extra = ""
        if "people" in selected:
            extra += "\nУ собеседника: " + ("УДАЛЮ" if revoke else "оставлю")
        if "bots" in selected:
            extra += "\nБоты: " + ("удалю и заблокирую" if block_bots else "только удалю")
        text = f"Будет удалено:\n{names}\n\nПауза: {delay} сек{extra}\n\nЭто необратимо!"

        def go():
            self.stop_event.clear()
            self.cleaning = True
            self.update_states()
            self.submit(self.clean(selected, revoke, delay, block_bots))
        self.confirm("Подтверждение", text, go)

    async def delete_private(self, ent, revoke):
        while True:
            res = await self.safe(
                self.client,
                functions.messages.DeleteHistoryRequest(peer=ent, max_id=0, revoke=revoke))
            if not getattr(res, "offset", 0):
                break
            await asyncio.sleep(0.5)
        for _ in range(5):
            ids = [m.id async for m in self.client.iter_messages(ent, limit=200)]
            if not ids:
                break
            await self.safe(self.client.delete_messages, ent, ids, revoke=revoke)
            await asyncio.sleep(0.5)

    async def clean(self, selected, revoke, delay, block_bots=True):
        try:
            targets = [u for u in self.contacts
                       if "bots" in selected or not getattr(u, "bot", False)]
            total = sum(len(self.groups[k]) for k in ("people", "bots", "channels", "groups")
                        if k in selected)
            if "contacts" in selected:
                total += len(targets)
            self.ui(setattr, self.pb, "max", max(total, 1))
            self.ui(setattr, self.pb, "value", 0)
            done_total = 0

            for kind in ("people", "bots", "channels", "groups"):
                if kind not in selected:
                    continue
                items = self.groups[kind]
                self.log(f"\n--- {TITLES[kind]}: {len(items)} ---")
                ok = 0
                for d in items:
                    if self.stop_event.is_set():
                        self.log("Остановлено.")
                        return
                    name = d.name or str(d.id)
                    try:
                        if kind in ("people", "bots"):
                            await self.delete_private(d.entity, revoke)
                            if kind == "bots" and block_bots:
                                await self.safe(
                                    self.client,
                                    functions.contacts.BlockRequest(id=d.entity))
                        else:
                            await self.safe(self.client.delete_dialog, d.entity)
                        ok += 1
                        self.log(f"[ok] {name}")
                    except Exception as e:
                        self.log(f"[fail] {name}: {e}")
                    done_total += 1
                    self.ui(setattr, self.pb, "value", done_total)
                    await asyncio.sleep(delay)
                self.log(f"Обработано: {ok}/{len(items)}")

            if "contacts" in selected:
                self.log(f"\n--- Контакты: {len(targets)} ---")
                for i in range(0, len(targets), 50):
                    if self.stop_event.is_set():
                        self.log("Остановлено.")
                        return
                    batch = targets[i:i + 50]
                    await self.safe(self.client,
                                    functions.contacts.DeleteContactsRequest(id=batch))
                    done_total += len(batch)
                    self.ui(setattr, self.pb, "value", done_total)
                    self.log(f"  удалено {min(i + 50, len(targets))}/{len(targets)}")
                    await asyncio.sleep(max(delay, 1))

            self.log("\nПроверяю, что осталось на сервере...")
            await self.do_scan()
            for key, title in CATEGORIES:
                n = len(self.contacts) if key == "contacts" else len(self.groups[key])
                self.log(f"  {title}: {n}")
            self.log("Готово.")
        finally:
            self.cleaning = False
            self.ui(self.update_states)

    def on_stop(self):
        """Вызывается Kivy при закрытии приложения."""
        self.stop_event.set()


if __name__ == "__main__":
    Cleaner().run()
