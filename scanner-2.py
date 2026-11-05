#!/usr/bin/env python3
"""
AES (Altai Enduro Scalper) — сканер сетапов №1 и №2.
Данные: Twelve Data. Уведомления: Telegram. Запуск: GitHub Actions.
Время везде Барнаул (UTC+7), внутри расчёты в UTC.
"""
import os, sys, time, json, math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests

UTC = timezone.utc
LOCAL = timezone(timedelta(hours=7))          # Барнаул
NY = ZoneInfo("America/New_York")
LON = ZoneInfo("Europe/London")


def env(k, d):
    v = os.environ.get(k)
    return v if v not in (None, "") else d


# ============================ НАСТРОЙКИ ============================
TD_KEY = env("TWELVE_DATA_KEY", "")
TG_TOKEN = env("TELEGRAM_TOKEN", "")
TG_CHAT = env("TELEGRAM_CHAT_ID", "")
BALANCE = float(env("ACCOUNT_BALANCE", "5000"))
RISK_PCT = float(env("RISK_PCT", "1.0"))

PAIRS = {"EURUSD": "EUR/USD", "GBPUSD": "GBP/USD"}
DXY = "DXY"
DXY_SYMBOL = env("TD_DXY_SYMBOL", "DXY")
SYM = dict(PAIRS)
SYM[DXY] = DXY_SYMBOL
COMP = {"USDJPY": "USD/JPY", "USDCAD": "USD/CAD", "USDSEK": "USD/SEK", "USDCHF": "USD/CHF"}
SYMX = dict(SYM)
SYMX.update(COMP)
DXY_W = {"EURUSD": -0.576, "USDJPY": 0.136, "GBPUSD": -0.119,
         "USDCAD": 0.091, "USDSEK": 0.042, "USDCHF": 0.036}
DXY_MODE = env("DXY_MODE", "auto")       # auto | symbol | synthetic
H1_TOUCH_MIN_PIPS = float(env("H1_TOUCH_MIN_PIPS", "3"))
MINOR_EVERY = int(env("MINOR_EVERY", "3"))   # синт. DXY: мелкие пары раз в N минут

PIP = 0.0001
START_HM = (13, 30)      # работа сканера (Барнаул)
END_HM = tuple(int(x) for x in env("SESSION_END", "18:00").split(":"))
ASIA_HM = ((4, 0), (11, 0))
KZ_HM = ((14, 30), (16, 30))
CLOSE_HM = (17, 30)

MSS_TF = int(env("MSS_TF", "3"))        # 3 или 5 минут
FVG_TF = 5
PIV_N = 2                                # фрактал для M3/M5
D1_N, H4_N = 2, 2
NEWS_MIN = 20                            # ± минут вокруг High Impact
MAX_SIGNALS = 2
SWEEP_LOOKBACK_MIN = int(env("SWEEP_LOOKBACK_MIN", "0"))   # 0 = строго с 14:30
DXY_LEAD = 2                             # DXY может развернуться на N свечей раньше
DXY_FIRST_STRICT = env("DXY_FIRST_STRICT", "1") == "1"      # слом DXY строго раньше пары
HEADSUP = env("HEADSUP", "1") == "1"     # предварительное уведомление «DXY сломался»
REQUIRE_BIAS = env("REQUIRE_BIAS", "1") == "1"

MIN_FVG_PIPS = 0.5
FVG_WAIT_MIN = 30
SL_BUF_S1 = 1.0                          # пипсов за экстремумом свипа
MAGNET_MIN_PIPS = 5                      # H1 FVG > 5 пипсов
MAGNET_DIST_PIPS = float(env("MAGNET_DIST_PIPS", "30"))
ASIA_WIDE_PIPS = float(env("ASIA_WIDE_PIPS", "40"))
S2_SL_BUF = 3.5
S2_H1_FVG_MIN = 3.0
S2_H1_FVG_DIST = 40.0
CREDIT_BUDGET = int(env("CREDIT_BUDGET", "760"))
STATE_FILE = env("STATE_FILE", "state.json")
DEBUG = env("DEBUG", "0") == "1"


def log(*a):
    print(datetime.now(UTC).strftime("%H:%M:%S"), *a, flush=True)


# ============================ СВЕЧИ ============================
@dataclass
class C:
    t: datetime
    o: float
    h: float
    l: float
    c: float
    k: object = None


def mir(cs):
    """Зеркало цен: шорт-логика превращается в лонг-логику."""
    return [C(x.t, -x.o, -x.l, -x.h, -x.c, x.k) for x in cs]


def resample(m1, n):
    """1-минутки -> свечи n минут. Незавершённая последняя свеча отбрасывается."""
    keys = sorted(m1)
    out, cur, ck = [], None, None
    for t in keys:
        b = m1[t]
        k = int(t.timestamp()) // (n * 60)
        if k != ck:
            if cur:
                out.append(cur)
            cur = C(datetime.fromtimestamp(k * n * 60, UTC), b.o, b.h, b.l, b.c)
            ck = k
        else:
            cur.h = max(cur.h, b.h)
            cur.l = min(cur.l, b.l)
            cur.c = b.c
    if cur:
        out.append(cur)
    if out and keys and keys[-1] < out[-1].t + timedelta(minutes=n - 1):
        out.pop()
    return out


def htf(h1, hours):
    """H1 -> H4 / D1, якорь 17:00 Нью-Йорк (= Daily Open 04:00/05:00 Барнаул)."""
    out, ck = [], None
    for x in h1:
        s = x.t.astimezone(NY) + timedelta(hours=7)
        k = s.date() if hours == 24 else (s.date(), s.hour // hours)
        if k != ck:
            out.append(C(x.t, x.o, x.h, x.l, x.c, s.date()))
            ck = k
        else:
            y = out[-1]
            y.h = max(y.h, x.h)
            y.l = min(y.l, x.l)
            y.c = x.c
    return out


def pivots(cs, n):
    hi, lo = [], []
    for i in range(n, len(cs) - n):
        if all(cs[i].h > cs[i - j].h and cs[i].h > cs[i + j].h for j in range(1, n + 1)):
            hi.append(i)
        if all(cs[i].l < cs[i - j].l and cs[i].l < cs[i + j].l for j in range(1, n + 1)):
            lo.append(i)
    return hi, lo


def find_mss_short(cs, start, level=None, n=PIV_N):
    """Свип хая (выше level) и MSS вниз: закрытие тела ниже последнего
    подтверждённого swing low, предшествующего экстремуму.
    Возвращает dict(ext, k, level) или None. k=None — свип есть, MSS ещё нет."""
    _, pl = pivots(cs, n)
    ext = None
    for k in range(max(start, 0), len(cs)):
        x = cs[k]
        if (level is None or x.h > level) and (ext is None or x.h > cs[ext].h):
            ext = k
        if ext is None:
            continue
        cands = [p for p in pl if p < ext and p + n <= k]
        if not cands:
            continue
        lvl = cs[cands[-1]].l
        if cs[k].c < lvl:
            return dict(ext=ext, k=k, level=lvl)
    return dict(ext=ext, k=None, level=None) if ext is not None else None


def fvg_gaps(cs, span_min):
    """Все FVG: dir long (c1.h<c3.l) / short (c1.l>c3.h)."""
    out = []
    for i in range(1, len(cs) - 1):
        c1, c3 = cs[i - 1], cs[i + 1]
        end = cs[i + 1].t + timedelta(minutes=span_min)
        if c1.h < c3.l:
            out.append(dict(dir="long", lo=c1.h, hi=c3.l, i=i, t2=cs[i].t, end=end))
        elif c1.l > c3.h:
            out.append(dict(dir="short", lo=c3.h, hi=c1.l, i=i, t2=cs[i].t, end=end))
    return out


def trend(cs, n):
    ph, pl = pivots(cs, n)
    if len(ph) < 2 or len(pl) < 2:
        return None
    hh = cs[ph[-1]].h > cs[ph[-2]].h
    hl = cs[pl[-1]].l > cs[pl[-2]].l
    if hh and hl:
        return "long"
    if not hh and not hl:
        return "short"
    return None


# ============================ КОНТЕКСТ D1 / H4 ============================
@dataclass
class Ctx:
    state: str
    dir: str
    note: str = ""

    def text(self):
        arrow = {"long": "↑ BUY", "short": "↓ SELL", None: "— нет"}[self.dir]
        return f"State {self.state}: {arrow}" + (f" ({self.note})" if self.note else "")


def context(h1):
    d1, h4 = htf(h1, 24), htf(h1, 4)
    done = d1[:-1]
    h4c = h4[:-1]
    if len(done) < 10 or len(h4c) < 20:
        return Ctx("?", None, "мало данных")
    tr = trend(done, D1_N)
    if tr:
        return Ctx("A", tr, "тренд D1 HH/HL" if tr == "long" else "тренд D1 LH/LL")
    ph, pl = pivots(done, D1_N)
    if len(ph) < 2 or len(pl) < 2:
        return Ctx("?", None, "нет структуры D1")
    rh = max(done[i].h for i in ph[-2:])
    rl = min(done[i].l for i in pl[-2:])
    recent = d1[-3:]
    th = max(x.h for x in recent) >= rh
    tl = min(x.l for x in recent) <= rl
    if th and tl:
        return Ctx("C", None, "оба края D1 задеты")
    if th or tl:
        st = next((i for i, x in enumerate(h4c) if x.t >= recent[0].t), len(h4c))
        if th:
            r = find_mss_short(h4c, st, rh, H4_N)
            if r and r["k"] is not None:
                return Ctx("C", "short", "край D1 High взят, MSS H4 вниз")
            return Ctx("C", None, "у D1 High, ждём MSS H4 вниз")
        r = find_mss_short(mir(h4c), st, -rl, H4_N)
        if r and r["k"] is not None:
            return Ctx("C", "long", "край D1 Low взят, MSS H4 вверх")
        return Ctx("C", None, "у D1 Low, ждём MSS H4 вверх")
    want = "long" if pl[-1] > ph[-1] else "short"      # к немитигированной границе
    h4tr = trend(h4c, H4_N)
    if h4tr == want:
        return Ctx("B", want, "рейндж D1, H4 по потоку")
    return Ctx("B", None, f"рейндж D1, H4 не подтверждает {want}")


def swept_after(level, side, since, h1, m1):
    """True если уровень уже снят после времени since."""
    for x in h1:
        if x.t > since and ((side == "up" and x.h > level) or (side == "dn" and x.l < level)):
            return True
    for t, x in m1.items():
        if t > since and ((side == "up" and x.h > level) or (side == "dn" and x.l < level)):
            return True
    return False


def htf_levels(h1, m1):
    """Ликвидность HTF: PDH/PDL, PWH/PWL, пивоты D1 и H4. Только неснятые."""
    d1, h4 = htf(h1, 24), htf(h1, 4)
    done, h4c = d1[:-1], h4[:-1]
    raw = []   # (price, side, since)
    if done:
        p = done[-1]
        raw += [(p.h, "up", p.t), (p.l, "dn", p.t)]
        wk = {}
        for x in done:
            wk.setdefault(x.k.isocalendar()[:2], []).append(x)
        keys = sorted(wk)
        cur = d1[-1].k.isocalendar()[:2]
        prev = [k for k in keys if k < cur]
        if prev:
            w = wk[prev[-1]]
            raw += [(max(x.h for x in w), "up", w[-1].t), (min(x.l for x in w), "dn", w[-1].t)]
    for cs, n, lim in ((done, D1_N, 40), (h4c, H4_N, 120)):
        ph, pl = pivots(cs, n)
        for i in ph[-15:]:
            raw.append((cs[i].h, "up", cs[i].t))
        for i in pl[-15:]:
            raw.append((cs[i].l, "dn", cs[i].t))
    return [(p, s) for p, s, t in raw if not swept_after(p, s, t, h1, m1)]


def unmitigated_gaps(h1, m1, days=10):
    """H1 и H4 FVG, у которых 50% не тронуты."""
    res = []
    last_h1_end = h1[-1].t + timedelta(hours=1) if h1 else None
    for cs, span in ((h1[-24 * days:], 60), (htf(h1, 4)[:-1][-6 * days:], 240)):
        for g in fvg_gaps(cs, span):
            mid = (g["lo"] + g["hi"]) / 2
            hit = any(x.t >= g["end"] and x.l <= mid <= x.h for x in h1)
            if not hit and last_h1_end:
                hit = any(t >= max(g["end"], last_h1_end) and x.l <= mid <= x.h for t, x in m1.items())
            if not hit:
                g["tf"] = span
                res.append(g)
    return res


def synth_dxy_bar(parts):
    """DXY по формуле ICE из 6 пар. h/l — середина между close-оценкой и огибающей."""
    def calc(px):
        v = 50.14348112
        for k, w in DXY_W.items():
            v *= px[k] ** w
        return v
    o = calc({k: parts[k].o for k in DXY_W})
    c = calc({k: parts[k].c for k in DXY_W})
    env_h = calc({k: (parts[k].l if w < 0 else parts[k].h) for k, w in DXY_W.items()})
    env_l = calc({k: (parts[k].h if w < 0 else parts[k].l) for k, w in DXY_W.items()})
    hi, lo = max(o, c), min(o, c)
    return o, max(hi, (hi + env_h) / 2), min(lo, (lo + env_l) / 2), c


# ============================ TWELVE DATA ============================
class RateLimit(Exception):
    pass


class TD:
    LIMIT = int(env("TD_CREDITS_PER_MIN", "8"))     # бесплатный тариф = 8

    def __init__(self):
        self.s = requests.Session()
        self.credits = 0
        self.win = []          # (время, кредитов) за последнюю минуту
        self.err = {}          # последняя ошибка по символу

    def _throttle(self, n):
        while True:
            now = time.time()
            self.win = [(t, c) for t, c in self.win if now - t < 61]
            if sum(c for _, c in self.win) + n <= self.LIMIT:
                break
            time.sleep(max(self.win[0][0] + 61 - now, 1))
        self.win.append((time.time(), n))

    def series(self, symbols, interval, size):
        out = {}
        for k in range(0, len(symbols), self.LIMIT):
            out.update(self._series(symbols[k:k + self.LIMIT], interval, size))
        return out

    def _series(self, symbols, interval, size):
        p = dict(symbol=",".join(symbols), interval=interval, outputsize=size,
                 timezone="UTC", apikey=TD_KEY)
        for attempt in range(3):
            self._throttle(len(symbols))
            r = self.s.get("https://api.twelvedata.com/time_series", params=p, timeout=15)
            data = r.json()
            self.credits += len(symbols)
            if isinstance(data, dict) and data.get("code") == 429:
                msg = str(data.get("message", ""))
                log("TD 429:", msg[:110])
                if "minute" in msg and attempt < 2:
                    time.sleep(61 - time.time() % 60 + 1)     # ждём начала следующей минуты
                    self.win = []
                    continue
                raise RateLimit(msg)
            break
        if len(symbols) == 1:
            data = {symbols[0]: data}
        out = {}
        for sym in symbols:
            d = data.get(sym) if isinstance(data, dict) else None
            if not d or d.get("status") == "error" or "values" not in d:
                msg = str((d or data).get("message", "")) if isinstance(d or data, dict) else ""
                self.err[sym] = msg
                log("TD error", sym, msg[:120])
                out[sym] = None
                continue
            bars = []
            for v in d["values"]:
                fmt = "%Y-%m-%d %H:%M:%S" if " " in v["datetime"] else "%Y-%m-%d"
                t = datetime.strptime(v["datetime"][:19], fmt).replace(tzinfo=UTC)
                bars.append(C(t, float(v["open"]), float(v["high"]), float(v["low"]), float(v["close"])))
            out[sym] = sorted(bars, key=lambda b: b.t)
        return out


# ============================ TELEGRAM / НОВОСТИ ============================
def tg(text):
    if not TG_TOKEN or not TG_CHAT:
        log("TG>", text.replace("\n", " | "))
        return True
    for i in range(3):
        try:
            r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                              json=dict(chat_id=TG_CHAT, text=text, parse_mode="HTML",
                                        disable_web_page_preview=True), timeout=8)
            if r.ok:
                return True
            log("TG fail", r.status_code, r.text[:100])
        except Exception as e:
            log("TG exc", e)
        time.sleep(1)
    return False


class News:
    URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

    def __init__(self):
        self.ev, self.ok = [], False

    def load(self):
        try:
            r = requests.get(self.URL, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            data = r.json()
            ev = []
            for e in data:
                if e.get("impact") == "High" and e.get("country") in ("USD", "EUR", "GBP"):
                    ev.append((datetime.fromisoformat(e["date"]).astimezone(UTC), e["country"], e["title"]))
            self.ev, self.ok = ev, True
        except Exception as e:
            log("news fail", e)
        return self.ok

    def block(self, now):
        for t, c, title in self.ev:
            if abs((now - t).total_seconds()) <= NEWS_MIN * 60:
                return (t, c, title)

    def upcoming(self, now, mins=90):
        return [(t, c, ti) for t, c, ti in self.ev if timedelta(0) <= t - now <= timedelta(minutes=mins + NEWS_MIN)]


# ============================ РЫНОК ============================
@dataclass
class Asia:
    high: float
    low: float

    @property
    def mid(self):
        return (self.high + self.low) / 2


class Market:
    def __init__(self):
        self.m1 = {s: {} for s in SYMX}
        self.h1 = {s: [] for s in PAIRS}

    def merge(self, sym, bars, now):
        for b in bars:
            if b.t + timedelta(minutes=1) <= now:
                self.m1[sym][b.t] = b

    def last(self, sym):
        return max(self.m1[sym]) if self.m1[sym] else None

    def tf(self, sym, n):
        return resample(self.m1[sym], n)


@dataclass
class Times:
    asia0: datetime
    asia1: datetime
    start: datetime
    end: datetime
    kz0: datetime
    kz1: datetime
    close: datetime
    day: object


def times(now):
    d = now.astimezone(LOCAL).date()

    def at(hm):
        return datetime(d.year, d.month, d.day, hm[0], hm[1], tzinfo=LOCAL).astimezone(UTC)
    return Times(at(ASIA_HM[0]), at(ASIA_HM[1]), at(START_HM), at(END_HM),
                 at(KZ_HM[0]) - timedelta(minutes=SWEEP_LOOKBACK_MIN), at(KZ_HM[1]), at(CLOSE_HM), d)


def f5(x, sym=None):
    return f"{x:.3f}" if sym == DXY else f"{x:.5f}"


def hm(t):
    return t.astimezone(LOCAL).strftime("%H:%M")


# ============================ СКАНЕР ============================
class Scanner:
    def __init__(self, now=None):
        self.td = TD()
        self.mk = Market()
        self.news = News()
        self.now = now or datetime.now(UTC)
        self.T = times(self.now)
        self.asia = {}
        self.ctx = {}
        self.sent = set()
        self.count = 0
        self.pending = []
        self.close_sent = False
        self.last_h1_hour = None
        self.stale_warn = False
        self.synth = DXY_MODE == "synthetic"
        self.minor_due = True
        self.touch_gaps = {}
        self.load_state()

    # ---------- состояние ----------
    def load_state(self):
        try:
            s = json.load(open(STATE_FILE))
            if s.get("day") == str(self.T.day):
                self.sent = set(s["sent"])
                self.count = s["count"]
                self.close_sent = s.get("close_sent", False)
                log("state restored", self.count, len(self.sent))
        except Exception:
            pass

    def save_state(self):
        try:
            json.dump(dict(day=str(self.T.day), sent=sorted(self.sent), count=self.count,
                           close_sent=self.close_sent), open(STATE_FILE, "w"))
        except Exception as e:
            log("state save fail", e)

    # ---------- данные ----------
    def expand(self, symbols):
        out = []
        for x in symbols:
            if x == DXY and self.synth:
                group = ["EURUSD", "GBPUSD"] + (list(COMP) if self.minor_due else [])
            else:
                group = [x]
            for y in group:
                if y not in out:
                    out.append(y)
        return out

    def rebuild_dxy(self, since=None):
        """Синтетический DXY. Мелкие пары, которых ещё нет за минуту, держим плоско
        по последнему close (потом пересчитается, когда придут реальные бары)."""
        minors = list(COMP)
        if not all(self.mk.m1[k] for k in minors):
            return
        base = set(self.mk.m1["EURUSD"]) & set(self.mk.m1["GBPUSD"])
        times = sorted(t for t in base if since is None or t >= since)
        srt = {k: sorted(self.mk.m1[k]) for k in minors}
        idx = {k: 0 for k in minors}
        for t in times:
            parts = {"EURUSD": self.mk.m1["EURUSD"][t], "GBPUSD": self.mk.m1["GBPUSD"][t]}
            for k in minors:
                ts, i = srt[k], idx[k]
                while i < len(ts) and ts[i] <= t:
                    i += 1
                idx[k] = i
                b = self.mk.m1[k][ts[i - 1]] if i > 0 else self.mk.m1[k][ts[0]]
                parts[k] = b if (i > 0 and ts[i - 1] == t) else C(t, b.c, b.c, b.c, b.c)
            o, h, l, c = synth_dxy_bar(parts)
            self.mk.m1[DXY][t] = C(t, o, h, l, c)

    def load_m1(self, symbols, size, now):
        req = self.expand(symbols)
        try:
            res = self.td.series([SYMX[s] for s in req], "1min", size)
        except RateLimit:
            log("rate limit, пропуск")
            return False
        except Exception as e:
            log("m1 fetch exc", e)
            return False
        ok = True
        for s in req:
            bars = res.get(SYMX[s])
            if bars:
                self.mk.merge(s, bars, now)
            else:
                ok = False
        if self.synth and DXY in symbols:
            self.rebuild_dxy(None if size > 100 else now - timedelta(minutes=60))
        return ok

    def poll(self, symbols, now, size=20):
        expected = now.replace(second=0, microsecond=0) - timedelta(minutes=1)
        todo = list(symbols)
        for attempt in range(3):
            self.load_m1(todo, size, now)
            todo = [s for s in todo if not self.mk.last(s) or self.mk.last(s) < expected]
            if not todo:
                return True
            if attempt < 2:
                time.sleep(2)
                now = datetime.now(UTC)
        log("лагают данные:", todo)
        return False

    def load_h1(self, now):
        try:
            res = self.td.series([SYM[p] for p in PAIRS], "1h", 1500)
        except Exception as e:
            log("h1 fail", e)
            return
        for p in PAIRS:
            bars = res.get(SYM[p])
            if bars:
                self.mk.h1[p] = [b for b in bars if b.t + timedelta(hours=1) <= now]
        self.update_context()
        self.build_touch_gaps()

    def build_touch_gaps(self):
        """Непотронутые H1 FVG (по часовым свечам)."""
        for p in PAIRS:
            h = self.mk.h1[p][-240:]
            out = []
            for g in fvg_gaps(h, 60):
                if (g["hi"] - g["lo"]) / PIP < H1_TOUCH_MIN_PIPS:
                    continue
                if any(x.t >= g["end"] and x.l <= g["hi"] and x.h >= g["lo"] for x in h):
                    continue
                g["key"] = f"T:{p}:{g['t2'].isoformat()}"
                out.append(g)
            self.touch_gaps[p] = out

    def check_touches(self, silent=False):
        for p in PAIRS:
            h1 = self.mk.h1[p]
            if not h1:
                continue
            last_end = h1[-1].t + timedelta(hours=1)
            for g in self.touch_gaps.get(p, []):
                if g["key"] in self.sent:
                    continue
                hit = next(((t, x) for t, x in sorted(self.mk.m1[p].items())
                            if t >= max(g["end"], last_end) and x.l <= g["hi"] and x.h >= g["lo"]), None)
                if not hit:
                    continue
                self.sent.add(g["key"])
                if silent:
                    continue
                t, x = hit
                c = self.ctx.get(p)
                tg(f"📍 <b>{p}</b> коснулся H1 FVG ({'бычий' if g['dir'] == 'long' else 'медвежий'}) "
                   f"{f5(g['lo'])}–{f5(g['hi'])}\n"
                   f"⏱ {hm(t)} · цена {f5(x.c)} · FVG от {hm(g['t2'])}\n"
                   f"🧭 {c.text() if c else '—'}")
        self.save_state()

    def update_context(self):
        for p in PAIRS:
            h = self.mk.h1[p]
            if h:
                self.ctx[p] = context(h)

    def compute_asia(self):
        for s in SYM:
            bars = [b for t, b in self.mk.m1[s].items() if self.T.asia0 <= t < self.T.asia1]
            if len(bars) >= 60:
                self.asia[s] = Asia(max(b.h for b in bars), min(b.l for b in bars))

    def daily_open(self, p):
        summer = bool(self.T.start.astimezone(LON).dst())
        hh = 4 if summer else 5
        o = datetime(self.T.day.year, self.T.day.month, self.T.day.day, hh, 0, tzinfo=LOCAL).astimezone(UTC)
        for x in self.mk.h1[p]:
            if x.t == o:
                return x.c
        return None

    # ---------- запуск ----------
    def init(self, now):
        self.now = now
        self.minor_due = True
        ok = self.load_m1(list(SYM), 1500, now)
        e = self.td.err.get(SYM[DXY], "")
        if DXY_MODE == "auto" and not self.mk.m1[DXY] and e and "credit" not in e.lower():
            log("DXY-символ недоступен -> считаю синтетический DXY из 6 пар:", e[:100])
            self.synth = True
            ok = self.load_m1(list(SYM), 1500, now)
        self.load_h1(now)
        self.news.load()
        self.compute_asia()
        self.check_touches(silent=True)
        self.last_h1_hour = now.replace(minute=0, second=0, microsecond=0)
        lines = [f"✅ <b>AES сканер запущен</b> · {hm(now)} (Барнаул)"]
        for p in PAIRS:
            c = self.ctx.get(p)
            a = self.asia.get(p)
            lines.append(f"• {p}: {c.text() if c else 'нет данных'}")
            if a:
                lines.append(f"   Asia {f5(a.low)} – {f5(a.high)} ({(a.high - a.low) / PIP:.0f}п)")
        if DXY in self.asia:
            a = self.asia[DXY]
            lines.append(f"• DXY{' (синтетический, из 6 пар)' if self.synth else ''}: Asia {f5(a.low, DXY)} – {f5(a.high, DXY)}")
        else:
            lines.append("⚠️ <b>DXY не загрузился</b> — сигналы невозможны. Проверь тариф Twelve Data / TD_DXY_SYMBOL")
        if not self.news.ok:
            lines.append("⚠️ Календарь новостей не загрузился — следи за High Impact вручную")
        lines.append(f"Окно сигналов: {hm(self.T.kz0)}–{hm(self.T.kz1)}")
        tg("\n".join(lines))

    # ---------- главный такт ----------
    def tick(self, now):
        self.now = now
        T = self.T
        hour = now.replace(minute=0, second=0, microsecond=0)
        if hour != self.last_h1_hour and now.minute >= 1:
            self.last_h1_hour = hour
            self.load_h1(now)
            if not self.news.ok or now.hour % 3 == 0:
                self.news.load()
            self.compute_asia()
        if not self.close_sent and now >= T.close and self.count > 0:
            self.close_sent = True
            self.save_state()
            tg(f"🕠 <b>{hm(T.close)}</b> — закрытие Лондона. Подтяни трейлинг или закрой открытые позиции.")
        in_kz = T.kz0 - timedelta(minutes=2) <= now <= T.kz1
        if in_kz:
            syms = list(SYM)
        elif now.minute % 2 == 0 or self.pending:
            syms = list(PAIRS)            # касания H1 FVG и ордера вне Kill Zone
        else:
            return
        if self.td.credits > CREDIT_BUDGET and now.minute % 5 != 0:
            return
        last = [self.mk.last(x) for x in syms]
        first = any(t is None or (now - t).total_seconds() > 900 for t in last)
        self.minor_due = first or now.minute % MINOR_EVERY == 0
        if not self.poll(syms, now, 1000 if first else 20):
            return
        self.check_touches()
        if in_kz:
            if DXY not in self.asia:
                self.compute_asia()
            if now >= T.kz0:
                self.evaluate(now)
        self.check_pending(now)

    # ---------- сетапы ----------
    def evaluate(self, now):
        if self.count >= MAX_SIGNALS:
            return
        for p in PAIRS:
            if not (self.mk.last(p) and self.mk.last(DXY)):
                continue
            if (now - self.mk.last(DXY)).total_seconds() > 240 or (now - self.mk.last(p)).total_seconds() > 240:
                continue
            for d in ("short", "long"):
                try:
                    self.setup1(now, p, d)
                except Exception as e:
                    log("setup1 err", p, d, repr(e))
            try:
                self.setup2(now, p)
            except Exception as e:
                log("setup2 err", p, repr(e))

    def bias_ok(self, p, d):
        c = self.ctx.get(p)
        if not REQUIRE_BIAS:
            return True
        return bool(c and c.dir == d)

    def setup1(self, now, p, d):
        key = f"{self.T.day}:{p}:S1:{d}"
        if key in self.sent or self.count >= MAX_SIGNALS or not self.bias_ok(p, d):
            return
        a, ad = self.asia.get(p), self.asia.get(DXY)
        if not (a and ad):
            return
        tf = MSS_TF
        P, D = self.mk.tf(p, tf), self.mk.tf(DXY, tf)
        start = next((i for i, x in enumerate(P) if x.t >= self.T.kz0), None)
        if start is None:
            return
        sgn = 1 if d == "short" else -1
        sv = P if d == "short" else mir(P)
        res = find_mss_short(sv, start, a.high if d == "short" else -a.low)
        if not res:
            return
        ext_t = P[res["ext"]].t
        ext_px = sgn * sv[res["ext"]].h
        dv = mir(D) if d == "short" else D
        ds = next((i for i, x in enumerate(D) if x.t >= max(self.T.kz0, ext_t - timedelta(minutes=DXY_LEAD * tf))), None)
        dres = find_mss_short(dv, ds, None) if ds is not None else None
        end_t = P[res["k"]].t + timedelta(minutes=tf) if res["k"] is not None else now
        dwin = [x for x in D if self.T.kz0 <= x.t < end_t]
        if not dwin:
            return
        if d == "short":
            dx = min(x.l for x in dwin)
            smt = dx >= ad.low
            dlvl = ad.low
        else:
            dx = max(x.h for x in dwin)
            smt = dx <= ad.high
            dlvl = ad.high
        if not smt or not dres or dres["k"] is None:
            return
        dxy_close = D[dres["k"]].t + timedelta(minutes=tf)
        side_txt = "BUY" if d == "long" else "SELL"
        dxy_dir = "вверх" if d == "short" else "вниз"
        pair_dir = "вниз" if d == "short" else "вверх"
        if res["k"] is None:
            hk = key + ":hu"
            if HEADSUP and hk not in self.sent and not self.news.block(now):
                self.sent.add(hk)
                self.save_state()
                tg(f"👀 <b>DXY слом {dxy_dir}</b> ({hm(dxy_close)}), SMT есть.\n"
                   f"Жду слом {p} {pair_dir} → {side_txt} {p} (Setup №1)")
            return
        pair_close = P[res["k"]].t + timedelta(minutes=tf)
        if DXY_FIRST_STRICT and not dxy_close < pair_close:
            log("S1", p, d, "DXY не первый", hm(dxy_close), hm(pair_close))
            return
        if not DXY_FIRST_STRICT and not dxy_close <= pair_close:
            return
        if now > pair_close + timedelta(minutes=FVG_WAIT_MIN):
            return
        # --- 5M FVG ---
        F = self.mk.tf(p, FVG_TF)
        fs = F if d == "short" else mir(F)
        ext5 = ext_t.replace(minute=ext_t.minute - ext_t.minute % FVG_TF)
        gaps = [g for g in fvg_gaps(fs, FVG_TF)
                if g["dir"] == "short" and fs[g["i"] - 1].t >= ext5
                and g["t2"] <= pair_close + timedelta(minutes=FVG_TF)
                and (g["hi"] - g["lo"]) / PIP >= MIN_FVG_PIPS]
        if not gaps:
            return
        g = gaps[-1]
        entry = sgn * (g["lo"] + g["hi"]) / 2
        gl, gh = (g["lo"], g["hi"]) if d == "short" else (-g["hi"], -g["lo"])
        if self.tapped(p, d, entry, g["end"]):
            log("S1", p, d, "лимитка уже тронута, пропуск")
            self.sent.add(key)
            return
        sl = ext_px + sgn * SL_BUF_S1 * PIP
        tp = a.low if d == "short" else a.high
        risk = abs(sl - entry) / PIP
        rew = abs(entry - tp) / PIP
        if (d == "short" and not (tp < entry < sl)) or (d == "long" and not (sl < entry < tp)):
            log("S1", p, d, "некорректные уровни")
            return
        mg = self.magnet(p, d, entry)
        if mg:
            log("S1", p, d, "H1 магнит — SKIP")
            self.sent.add(key)
            self.save_state()
            if DEBUG:
                tg(f"⛔ {p} {side_txt}: сетап №1 пропущен — H1 FVG-магнит")
            return
        lines = [
            f"{'🟢' if d == 'long' else '🔴'} <b>SETUP №1 · {side_txt} {p}</b>  ⏱ {hm(now)}",
            f"🧭 {self.ctx[p].text() if p in self.ctx else 'bias выкл.'}",
            f"✅ Свип Asia {'High' if d == 'short' else 'Low'} {f5(a.high if d == 'short' else a.low)} → экстремум {f5(ext_px)}",
            f"✅ DXY слом {dxy_dir} {hm(dxy_close)} → {p} слом {pair_dir} {hm(pair_close)} (M{tf})",
            f"✅ SMT: DXY не снял Asia {'Low' if d == 'short' else 'High'} "
            f"({f5(dx, DXY)} vs {f5(dlvl, DXY)})",
            f"✅ H1/H4 магнита против нет",
            "",
            f"📍 <b>Limit {f5(entry)}</b>  (50% FVG 5M {f5(gl)}–{f5(gh)})",
            f"🛑 <b>SL {f5(sl)}</b>  ({risk:.1f}п)",
            f"🎯 <b>TP {f5(tp)}</b>  Asia {'Low' if d == 'short' else 'High'} ({rew:.1f}п, R:R 1:{rew / risk:.1f})",
            self.size_line(risk),
        ]
        self.emit(key, p, d, entry, sl, tp, now, lines)

    def setup2(self, now, p):
        c = self.ctx.get(p)
        if not c or c.dir not in ("long", "short"):
            return
        d = c.dir
        key = f"{self.T.day}:{p}:S2:{d}"
        if key in self.sent or self.count >= MAX_SIGNALS:
            return
        a = self.asia.get(p)
        if not a or (a.high - a.low) / PIP < ASIA_WIDE_PIPS:
            return
        tf = MSS_TF
        P = self.mk.tf(p, tf)
        lv = P if d == "long" else mir(P)
        hi, lo = (a.high, a.low) if d == "long" else (-a.low, -a.high)
        sgn = 1 if d == "long" else -1
        brk = None
        for i, x in enumerate(lv):
            if x.t < self.T.kz0:
                continue
            if x.h > hi or x.l < lo:
                if x.c > hi and x.l >= lo:
                    brk = i
                break
        if brk is None:
            return
        brk_close = lv[brk].t + timedelta(minutes=tf)
        if now > brk_close + timedelta(minutes=FVG_WAIT_MIN):
            return
        F = self.mk.tf(p, FVG_TF)
        fl = F if d == "long" else mir(F)
        b5 = lv[brk].t.replace(minute=lv[brk].t.minute - lv[brk].t.minute % FVG_TF)
        gaps = [g for g in fvg_gaps(fl, FVG_TF) if g["dir"] == "long" and g["t2"] >= b5
                and (g["hi"] - g["lo"]) / PIP >= MIN_FVG_PIPS]
        if not gaps:
            return
        g = gaps[0]
        entry = sgn * (g["lo"] + g["hi"]) / 2
        gl, gh = (g["lo"], g["hi"]) if d == "long" else (-g["hi"], -g["lo"])
        if self.tapped(p, d, entry, g["end"]):
            self.sent.add(key)
            return
        sl = (a.low - S2_SL_BUF * PIP) if d == "long" else (a.high + S2_SL_BUF * PIP)
        if (d == "long" and not sl < entry) or (d == "short" and not entry < sl):
            return
        risk = abs(entry - sl) / PIP
        # H1 FVG по потоку рядом
        gp = self.mk.h1[p]
        aligned = [q for q in unmitigated_gaps(gp, self.mk.m1[p])
                   if q["dir"] == d and (q["hi"] - q["lo"]) / PIP >= S2_H1_FVG_MIN
                   and min(abs(q["lo"] - entry), abs(q["hi"] - entry)) / PIP <= S2_H1_FVG_DIST]
        if not aligned:
            log("S2", p, d, "нет H1 FVG по потоку")
            return
        lv_list = htf_levels(gp, self.mk.m1[p])
        if d == "long":
            cand = sorted(x for x, s in lv_list if s == "up" and x > entry)
        else:
            cand = sorted((x for x, s in lv_list if s == "dn" and x < entry), reverse=True)
        if not cand:
            return
        tp = cand[0]
        rew = abs(tp - entry) / PIP
        if rew < 2 * risk:
            log("S2", p, d, f"магнит близко {rew:.0f}п < 2×{risk:.0f}п — SKIP")
            self.sent.add(key)
            return
        side_txt = "BUY" if d == "long" else "SELL"
        q = aligned[0]
        lines = [
            f"{'🟢' if d == 'long' else '🔴'} <b>SETUP №2 · {side_txt} {p}</b>  ⏱ {hm(now)}",
            f"🧭 {c.text()}",
            f"✅ Asia широкий {(a.high - a.low) / PIP:.0f}п, пробой {'вверх' if d == 'long' else 'вниз'} без свипа ({hm(brk_close)}, M{tf})",
            f"✅ H1 FVG по потоку {f5(q['lo'])}–{f5(q['hi'])}",
            "",
            f"📍 <b>Limit {f5(entry)}</b>  (50% FVG 5M {f5(gl)}–{f5(gh)})",
            f"🛑 <b>SL {f5(sl)}</b>  ({risk:.1f}п, за границей Asia +{S2_SL_BUF}п)",
            f"🎯 <b>TP {f5(tp)}</b>  HTF ликвидность ({rew:.1f}п, R:R 1:{rew / risk:.1f})",
            self.size_line(risk),
        ]
        self.emit(key, p, d, entry, sl, tp, now, lines)

    # ---------- общее ----------
    def size_line(self, risk_pips):
        usd = BALANCE * RISK_PCT / 100
        lots = math.floor(usd / (risk_pips * 10) * 100) / 100
        return f"💰 Риск {RISK_PCT:g}% = ${usd:.0f} → ≈ {lots:.2f} лота (баланс ${BALANCE:.0f})"

    def tapped(self, p, d, entry, since):
        for t, x in self.mk.m1[p].items():
            if t >= since and ((d == "short" and x.h >= entry) or (d == "long" and x.l <= entry)):
                return True
        return False

    def magnet(self, p, d, entry):
        for g in unmitigated_gaps(self.mk.h1[p], self.mk.m1[p]):
            if (g["hi"] - g["lo"]) / PIP <= MAGNET_MIN_PIPS:
                continue
            if d == "short" and g["lo"] > entry and (g["lo"] - entry) / PIP <= MAGNET_DIST_PIPS:
                return g
            if d == "long" and g["hi"] < entry and (entry - g["hi"]) / PIP <= MAGNET_DIST_PIPS:
                return g
        return None

    def emit(self, key, p, d, entry, sl, tp, now, lines):
        blk = self.news.block(now)
        if blk:
            log("новость, сигнал заблокирован", blk)
            return
        for t, cur, title in self.news.upcoming(now, 60):
            lines.append(f"⚠️ Новость {cur} {hm(t)}: {title} — окно ±{NEWS_MIN} мин без ордеров")
        if not self.news.ok:
            lines.append("⚠️ Календарь новостей не загружен — проверь High Impact вручную")
        if tg("\n".join(lines)):
            self.sent.add(key)
            self.count += 1
            self.pending.append(dict(pair=p, d=d, entry=entry, sl=sl, tp=tp, since=now, key=key))
            self.save_state()

    def check_pending(self, now):
        keep = []
        for s in self.pending:
            filled = cancelled = False
            for t in sorted(self.mk.m1[s["pair"]]):
                if t < s["since"].replace(second=0, microsecond=0):
                    continue
                x = self.mk.m1[s["pair"]][t]
                if s["d"] == "short":
                    if x.h >= s["entry"]:
                        filled = True
                    elif x.l <= s["tp"]:
                        cancelled = True
                else:
                    if x.l <= s["entry"]:
                        filled = True
                    elif x.h >= s["tp"]:
                        cancelled = True
                if filled or cancelled:
                    break
            if cancelled:
                tg(f"❌ {s['pair']} {'SELL' if s['d'] == 'short' else 'BUY'}: цена дошла до TP без входа — "
                   f"<b>отмени лимитный ордер</b> {f5(s['entry'])}")
            elif not filled:
                keep.append(s)
        self.pending = keep


# ============================ ЗАПУСК ============================
def sleep_until(ts):
    while True:
        d = ts - time.time()
        if d <= 0:
            return
        time.sleep(min(d, 20))


def main():
    now = datetime.now(UTC)
    T = times(now)
    if now >= T.end:
        log("после окончания сессии — выход")
        return
    if now < T.start - timedelta(seconds=45):
        log("жду старта", hm(T.start))
        sleep_until((T.start - timedelta(seconds=45)).timestamp())
    sc = Scanner()
    sc.init(datetime.now(UTC))
    log("работаю")
    while True:
        nxt = (int(time.time()) // 60 + 1) * 60 + 3      # :03 после закрытия минутки
        sleep_until(nxt)
        now = datetime.now(UTC)
        if now >= T.end:
            break
        try:
            sc.tick(now)
        except Exception as e:
            log("tick error", repr(e))
    sc.save_state()
    log("сессия завершена, кредитов использовано:", sc.td.credits)


if __name__ == "__main__":
    main()
