"""学校 WebVPN（网瑞达 wengine）登录客户端。

服务器不再登录 aTrust，改用 WebVPN 接入校园网：WebVPN 支持 WebSocket，chisel 隧道经它连到
各 GPU 服务器。本模块负责登录 WebVPN、拿到会话 Cookie（wengine_vpn_ticket...），供隧道复用。

登录流程（从登录页 JS 逆向）：
  1. GET  /login                拿 _csrf 和初始 Cookie
  2. GET  /login/image          拿滑块验证码：{p: 背景图, s: 拼图块, h: 纵向位置}
  3. 识别缺口横坐标，生成拖动轨迹
  4. POST /login/verify         {w, t, locations} -> {success}
  5. POST /do-login             {username, password(加密), auth_type=local, _csrf, ...} -> {success, url}

密码用 AES-128-CFB 加密（key=iv="wrdvpnisawesome!"），和登录页 encrypt() 一致。
滑块识别用拼图块轮廓在背景边缘图上做相关匹配，见 solve_gap()。

依赖：标准库 + Pillow + numpy（仅 solve_gap 用到，延迟导入）。不依赖 FastAPI，可独立运行：
    python -m app.webvpn --portal https://webvpn.whut.edu.cn --user 学号   （会提示输入密码）
"""
import argparse
import getpass
import http.cookiejar
import json
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --------------------------------------------------------------------------
# AES-128-CFB（纯标准库实现，和登录页的 aes-js ModeOfOperation.cfb(…, 16) 对应：
# 段长 16 字节 = CFB-128，每次整块异或）。只用于加密登录字段，调用不频繁。
# 用已知向量自检：enc("192.168.247.150", "wrdvpnisthebest!") 应得到 WebVPN 的主机编码串。
# --------------------------------------------------------------------------
# w = 识别出的缺口横坐标 + GAP_OFFSET（拼图块初始左边距，真机标定为 -2）。
# 登录重试时在它附近小幅抖动，抵消个别图识别差 1~2 像素。
GAP_OFFSET = -2
_SBOX = []
_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]


def _init_sbox():
    p = q = 1
    sbox = [0] * 256
    while True:
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        xformed = q ^ ((q << 1) | (q >> 7)) ^ ((q << 2) | (q >> 6)) ^ ((q << 3) | (q >> 5)) ^ ((q << 4) | (q >> 4))
        sbox[p] = (xformed ^ 0x63) & 0xFF
        if p == 1:
            break
    sbox[0] = 0x63
    return sbox


def _xtime(a):
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a & 0xFF


def _key_expansion(key):
    sbox = _SBOX
    words = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    for i in range(4, 44):
        t = list(words[i - 1])
        if i % 4 == 0:
            t = t[1:] + t[:1]
            t = [sbox[b] for b in t]
            t[0] ^= _RCON[i // 4 - 1]
        words.append([words[i - 4][j] ^ t[j] for j in range(4)])
    return words


def _aes_encrypt_block(block, round_keys):
    sbox = _SBOX
    s = [[block[r + 4 * c] for c in range(4)] for r in range(4)]

    def add_round_key(rnd):
        for c in range(4):
            w = round_keys[rnd * 4 + c]
            for r in range(4):
                s[r][c] ^= w[r]

    add_round_key(0)
    for rnd in range(1, 11):
        for r in range(4):
            for c in range(4):
                s[r][c] = sbox[s[r][c]]
        for r in range(1, 4):
            s[r] = s[r][r:] + s[r][:r]
        if rnd != 10:
            for c in range(4):
                col = [s[r][c] for r in range(4)]
                t = col[0] ^ col[1] ^ col[2] ^ col[3]
                c0 = col[0]
                s[0][c] = col[0] ^ t ^ _xtime(col[0] ^ col[1])
                s[1][c] = col[1] ^ t ^ _xtime(col[1] ^ col[2])
                s[2][c] = col[2] ^ t ^ _xtime(col[2] ^ col[3])
                s[3][c] = col[3] ^ t ^ _xtime(col[3] ^ c0)
        add_round_key(rnd)
    return bytes(s[r][c] for c in range(4) for r in range(4))


def _cfb_encrypt(plaintext, key, iv):
    rk = _key_expansion(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(plaintext), 16):
        ks = _aes_encrypt_block(prev, rk)
        block = plaintext[i:i + 16]
        ct = bytes(block[j] ^ ks[j] for j in range(len(block)))
        out += ct
        prev = ct
    return bytes(out)


def wrdvpn_encrypt(text, key="wrdvpnisawesome!"):
    """复刻登录页 encrypt(text, key, key)：右补 '0' 到 16 的整数倍，CFB 加密，
    返回 hex(iv) + hex(cipher) 的前 len(text)*2 个字符（iv == key）。"""
    kb = key.encode()
    padded = text + "0" * ((16 - len(text) % 16) % 16)
    ct = _cfb_encrypt(padded.encode(), kb, kb)
    return kb.hex() + ct.hex()[: len(text) * 2]


# --------------------------------------------------------------------------
# 滑块缺口识别
# --------------------------------------------------------------------------
def _png_decode(data):
    """纯标准库解码 PNG（8 位、非隔行、颜色类型 2=RGB / 6=RGBA / 0=灰 / 4=灰+A）。
    返回 (width, height, channels, 像素字节串)。"""
    import struct
    import zlib

    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是 PNG")
    width = height = bit = ctype = 0
    idat = bytearray()
    i = 8
    while i < len(data):
        (ln,) = struct.unpack(">I", data[i:i + 4])
        typ = data[i + 4:i + 8]
        body = data[i + 8:i + 8 + ln]
        if typ == b"IHDR":
            width, height, bit, ctype, comp, filt, interlace = struct.unpack(">IIBBBBB", body)
            if bit != 8 or interlace != 0 or ctype not in (0, 2, 4, 6):
                raise ValueError("只支持 8 位非隔行的灰/RGB/RGBA PNG")
        elif typ == b"IDAT":
            idat += body
        elif typ == b"IEND":
            break
        i += 12 + ln
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[ctype]
    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    out = bytearray(height * stride)
    prev = bytearray(stride)
    pos = 0
    for y in range(height):
        ft = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        if ft == 1:  # Sub
            for x in range(channels, stride):
                line[x] = (line[x] + line[x - channels]) & 0xFF
        elif ft == 2:  # Up
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 0xFF
        elif ft == 3:  # Average
            for x in range(stride):
                a = line[x - channels] if x >= channels else 0
                line[x] = (line[x] + ((a + prev[x]) >> 1)) & 0xFF
        elif ft == 4:  # Paeth
            for x in range(stride):
                a = line[x - channels] if x >= channels else 0
                b = prev[x]
                c = prev[x - channels] if x >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[x] = (line[x] + pr) & 0xFF
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return width, height, channels, bytes(out)


def solve_gap(bg_png, piece_png, piece_top=None):
    """返回缺口在背景图里的横坐标（拼图块左边缘应到达的 x）。
    原理：缺口是把拼图形状半透明叠加到背景上，其边缘在背景的边缘图里形成拼图轮廓；
    用拼图块 alpha 通道的轮廓在背景边缘图上滑动做相关，峰值即缺口位置。
    piece_top（响应里的 h）给出缺口纵向位置，只在它附近搜索，省去全图扫描。"""
    bw, bh, bc, bpx = _png_decode(bg_png)
    pw, ph, pc, ppx = _png_decode(piece_png)

    def gray_edges(width, height, chan, px):
        gray = [[0] * width for _ in range(height)]
        for y in range(height):
            row = y * width * chan
            for x in range(width):
                o = row + x * chan
                if chan >= 3:
                    gray[y][x] = (px[o] * 299 + px[o + 1] * 587 + px[o + 2] * 114) // 1000
                else:
                    gray[y][x] = px[o]
        edge = [[0] * width for _ in range(height)]
        for y in range(height):
            for x in range(width):
                gx = abs(gray[y][x] - gray[y][x - 1]) if x else 0
                gy = abs(gray[y][x] - gray[y - 1][x]) if y else 0
                edge[y][x] = gx + gy
        return edge

    bg_edge = gray_edges(bw, bh, bc, bpx)
    # 拼图块轮廓 = alpha 通道的边缘；只保留非零点（稀疏），相关时只累加这些点
    alpha = [[ppx[(y * pw + x) * pc + (pc - 1)] if pc in (2, 4) else 255
              for x in range(pw)] for y in range(ph)]
    outline = []
    total = 0
    for y in range(ph):
        for x in range(pw):
            ax = abs(alpha[y][x] - alpha[y][x - 1]) if x else 0
            ay = abs(alpha[y][x] - alpha[y - 1][x]) if y else 0
            if ax + ay:
                outline.append((y, x, ax + ay))
                total += ax + ay
    total = total or 1

    tops = range(0, bh - ph + 1)
    if piece_top is not None:
        lo, hi = max(0, piece_top - 8), min(bh - ph, piece_top + 8)
        tops = range(lo, hi + 1)

    best_x, best_score = pw, -1.0
    for top in tops:
        for x in range(pw, bw - pw):
            score = 0
            for dy, dx, w in outline:
                score += bg_edge[top + dy][x + dx] * w
            if score > best_score:
                best_score, best_x = score, x
    return best_x


def _verify_fields(w):
    """构造 /login/verify 的表单，和登录页 drag.js 一致：
    locations 是 jQuery 展开的数组（locations[0][x]=...），只含起点和终点两点；
    值用页面坐标即可（服务器拿不到页面布局，主要校验 w 与真实缺口的距离）。"""
    x0, y0 = random.randint(300, 360), random.randint(360, 420)  # 页面上滑块起点附近
    t = random.randint(600, 1600)
    fields = [("w", w), ("t", t)]
    fields += [("locations[0][x]", x0), ("locations[0][y]", y0)]
    fields += [("locations[1][x]", x0 + w), ("locations[1][y]", y0 + random.randint(-2, 2))]
    return fields


class WrongCredentials(Exception):
    """账号或密码错误 / 账号锁定：不可重试，否则会把账号试到锁定。"""


class Banned(Exception):
    """登录失败次数过多，被 WebVPN 临时封禁（通常 5 分钟）：要等较久再试，不能频繁重试。"""


class WebVPNClient:
    WRONG = ("用户名或密码错误", "密码错误", "账号或密码错误", "账号已锁定", "账户已锁定",
             "USERNAME_PASSWORD_ERROR", "USER_LOCKED", "PASSWORD_ERROR")

    def __init__(self, portal, timeout=20, debug=False, proxy=None):
        self.portal = portal.rstrip("/")
        self.host = urllib.parse.urlparse(self.portal).netloc
        self.timeout = timeout
        self.debug = debug
        self.jar = http.cookiejar.CookieJar()
        handlers = [urllib.request.HTTPCookieProcessor(self.jar)]
        # proxy（如 http://proxy:7891）：学校 WebVPN 封了云服务器 IP，经家宽代理出口访问
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        else:
            handlers.append(urllib.request.ProxyHandler({}))  # 不用代理时显式禁用环境代理
        self.opener = urllib.request.build_opener(*handlers)
        self.opener.addheaders = [("User-Agent", "Mozilla/5.0 (Macintosh) lab-webvpn")]

    def _log(self, *a):
        if self.debug:
            print("[webvpn]", *a, file=sys.stderr)

    BAN_HINTS = ("多次登录失败", "自动封禁", "访问被拒绝", "请等待", "稍后再试")

    def _get(self, path):
        try:
            with self.opener.open(self.portal + path, timeout=self.timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code == 403 and any(h in body for h in self.BAN_HINTS):
                raise Banned("登录失败次数过多，被临时封禁，请等待约 5 分钟")
            raise

    def _post(self, path, fields):
        data = urllib.parse.urlencode(fields, doseq=True).encode()
        req = urllib.request.Request(self.portal + path, data=data,
                                     headers={"Content-Type": "application/x-www-form-urlencoded",
                                              "X-Requested-With": "XMLHttpRequest",
                                              "Origin": self.portal,
                                              "Referer": self.portal + "/login"})
        with self.opener.open(req, timeout=self.timeout) as r:
            return r.read()

    def cookie_header(self):
        # jar 里只有 WebVPN 门户的 Cookie（本客户端只和门户通信），全部带上即可
        return "; ".join(f"{c.name}={c.value}" for c in self.jar)

    def ticket_cookie(self):
        for c in self.jar:
            if c.name.startswith("wengine_vpn_ticket"):
                return c.name, c.value
        return None, None

    def _csrf(self):
        import re
        html = self._get("/login").decode("utf-8", "replace")
        m = re.search(r'name="_csrf"[^>]*value="([^"]*)"', html) or \
            re.search(r'value="([^"]*)"[^>]*name="_csrf"', html)
        if not m:
            raise RuntimeError("登录页里找不到 _csrf")
        return m.group(1)

    def _fetch_captcha(self):
        import base64
        data = json.loads(self._get(f"/login/image?_={int(time.time()*1000)}"))
        bg = base64.b64decode(data["p"].split(",", 1)[1])
        piece = base64.b64decode(data["s"].split(",", 1)[1])
        return bg, piece, data.get("h")

    def _verify(self, w):
        return json.loads(self._post("/login/verify", _verify_fields(w)))

    def _pass_captcha(self, attempts=6):
        jitter = [0, 1, -1, 2, -2, 0]  # 在标定偏移附近抖动，抵消个别图识别差 1~2 像素
        for i in range(attempts):
            bg, piece, h = self._fetch_captcha()
            gap_x = solve_gap(bg, piece, h)
            w = max(1, gap_x + GAP_OFFSET + jitter[i % len(jitter)])
            res = self._verify(w)
            self._log(f"验证码第{i+1}次：缺口x={gap_x} w={w} -> {res}")
            if res.get("success"):
                return True
        return False

    def calibrate(self):
        """找出"识别出的缺口 x"与"要提交的 w"之间的换算关系。
        第一阶段：假设 w = gap_x + delta（纯偏移），每次换新图试不同 delta。
        第二阶段（第一阶段全失败时）：对同一张图扫描不同 w，看服务器接受哪一段——
        能同时暴露是否存在缩放（w 随 gap_x 成比例而非差一个常数）。"""
        print("第一阶段：按纯偏移标定（每次换新图）")
        for delta in [-10, -8, -12, -6, -14, -4, -2, -16, -18, 0, -20]:
            bg, piece, h = self._fetch_captcha()
            gap_x = solve_gap(bg, piece, h)
            w = max(1, gap_x + delta)
            res = self._verify(w)
            print(f"  delta={delta:>4}  缺口x={gap_x:>3}  w={w:>3}  -> {res}")
            if res.get("success"):
                return ("offset", delta, gap_x)
            time.sleep(0.6)

        print("\n第二阶段：对同一张图扫描 w，定位服务器接受的区间")
        bg, piece, h = self._fetch_captcha()
        gap_x = solve_gap(bg, piece, h)
        print(f"  这张图识别出的缺口 x = {gap_x}")
        passes = []
        for w in range(6, 262, 4):
            res = self._verify(w)
            if res.get("success"):
                passes.append(w)
                print(f"  w={w:>3} -> 通过")
            time.sleep(0.3)
        if passes:
            center = passes[len(passes) // 2]
            print(f"  接受区间 {passes[0]}..{passes[-1]}，中心 w≈{center}，而识别缺口 x={gap_x}")
            return ("scan", center - gap_x, gap_x)
        return None

    def login(self, username, password):
        csrf = self._csrf()
        if not self._pass_captcha():
            raise RuntimeError("滑块验证码多次未通过")
        fields = {
            "_csrf": csrf,
            "auth_type": "local",
            "username": username,
            "sms_code": "",
            "password": wrdvpn_encrypt(password),
            "remember_cookie": "on",
        }
        res = json.loads(self._post("/do-login", fields))
        self._log("do-login ->", {k: v for k, v in res.items() if k != "url"})
        if res.get("success"):
            name, _ = self.ticket_cookie()
            if not name:
                raise RuntimeError("登录返回成功但没有拿到 wengine_vpn_ticket Cookie")
            return True
        msg = (res.get("message") or "") + " " + (res.get("error") or "")
        if any(k in msg for k in self.WRONG):
            raise WrongCredentials(msg.strip())
        raise RuntimeError(f"登录失败：{msg.strip() or res}")

    def logged_in(self):
        """已登录时访问门户不会跳到 /login。
        返回 True / False；网络错误（代理抖动、超时、5xx）时无法判断，返回 None——调用方应保持现状，
        不要当作会话失效去重新登录（每次重新登录都占用每小时的限速名额，还会把所有隧道断一遍）。"""
        last = None
        for i in range(3):
            try:
                with self.opener.open(self.portal + "/", timeout=self.timeout) as r:
                    return "/login" not in r.geturl()
            except urllib.error.HTTPError as e:
                if e.code < 500:  # 401 / 403 等：门户明确拒绝，会话已失效
                    return False
                last = e
            except Exception as e:  # noqa: BLE001  URLError / 超时 / 连接被重置 / http.client 异常
                last = e
            time.sleep(2)
        self._log("会话检查遇到网络错误，暂时无法判断：%r" % (last,))
        return None


def _self_test():
    """用已知向量自检 AES：WebVPN 用 key='wrdvpnisthebest!' 编码主机名。"""
    got = wrdvpn_encrypt("192.168.247.150", "wrdvpnisthebest!")
    want = "77726476706e69737468656265737421a1ae13d27666301e2c5cdee2c90073"
    assert got == want, f"AES 自检失败：\n got={got}\nwant={want}"
    print("AES 自检通过")


def main(argv=None):
    ap = argparse.ArgumentParser(description="WebVPN 登录测试")
    ap.add_argument("--portal", default="https://webvpn.whut.edu.cn")
    ap.add_argument("--user")
    ap.add_argument("--self-test", action="store_true", help="只做 AES 自检，不联网")
    ap.add_argument("--calibrate", action="store_true",
                    help="只标定滑块偏移（不输密码、不登录）：扫描 w，找出服务器接受的偏移")
    args = ap.parse_args(argv)
    _self_test()
    if args.self_test:
        return
    c = WebVPNClient(args.portal, debug=True)
    if args.calibrate:
        print("标定滑块偏移（只测验证码，不登录，不消耗密码尝试次数）：")
        result = c.calibrate()
        if result is None:
            print("\n没找到能通过的 w。请把上面每一行发给我，我来调整。")
            sys.exit(1)
        kind, delta, gap_x = result
        print(f"\n找到了（{kind}）！w = 缺口x + ({delta})。请把这几行发给我。")
        return
    if not args.user:
        ap.error("登录需要 --user 学号（只做标定用 --calibrate，自检用 --self-test）")
    pw = getpass.getpass(f"{args.user} 的 WebVPN 密码（不回显）：")
    try:
        c.login(args.user, pw)
    except WrongCredentials as e:
        print("账号或密码错误，已停止（避免锁定）：", e)
        sys.exit(2)
    name, value = c.ticket_cookie()
    print("登录成功！会话 Cookie 名称：", name, "（值不显示）")
    print("长度：", len(value or ""))


if __name__ == "__main__":
    _SBOX = _init_sbox()
    main()
else:
    _SBOX = _init_sbox()
