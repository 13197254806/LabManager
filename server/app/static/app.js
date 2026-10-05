"use strict";

// ---------------- 工具函数 ----------------

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const WEEK = ["日", "一", "二", "三", "四", "五", "六"];

function parseDate(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, m - 1, d);
}
function fmtDate(dt) {
  const p = (n) => String(n).padStart(2, "0");
  return `${dt.getFullYear()}-${p(dt.getMonth() + 1)}-${p(dt.getDate())}`;
}
function addDays(iso, n) {
  const dt = parseDate(iso);
  dt.setDate(dt.getDate() + n);
  return fmtDate(dt);
}
function diffDays(a, b) {
  return Math.round((parseDate(b) - parseDate(a)) / 86400000);
}
function hue(id) {
  return (id * 137.508) % 360;
}
// 内联 SVG 图标（图标库在 index.html 顶部）
const icon = (name, cls = "") => `<svg class="i ${cls}"><use href="#i-${name}"/></svg>`;

// 必填项加红色 *：给带 required 控件的 <label> 里的文字加上星号（动态生成的表单也会处理）
function markRequired(root) {
  for (const label of root.querySelectorAll?.("label") || []) {
    if (label.dataset.req) continue;
    const ctl = [...label.children].find((c) => c.matches?.("input[required], select[required], textarea[required]"));
    if (!ctl) continue;
    label.dataset.req = "1";
    const wrap = document.createElement("span");
    while (label.firstChild && label.firstChild !== ctl) wrap.appendChild(label.firstChild);
    wrap.insertAdjacentHTML("beforeend", `<span class="req" aria-hidden="true">*</span>`);
    label.insertBefore(wrap, ctl);
  }
}
markRequired(document);
new MutationObserver((muts) => {
  for (const m of muts) for (const n of m.addedNodes) if (n.nodeType === 1) { if (n.matches("label")) markRequired(n.parentNode); markRequired(n); }
}).observe(document.body, { childList: true, subtree: true });

// 头像：优先用飞书头像，没有或加载失败时用姓名首字
function avatarHtml(name, url, id) {
  const ch = esc((name || "?").trim().slice(0, 1).toUpperCase());
  const style = `--h:${hue(id ?? 0)}`;
  return url
    ? `<img class="avatar" src="${esc(url)}" alt="" referrerpolicy="no-referrer" style="${style}" onerror="this.outerHTML='<span class=&quot;avatar&quot; style=&quot;${style}&quot;>${ch}</span>'">`
    : `<span class="avatar" style="${style}">${ch}</span>`;
}
function gb(mib) {
  return mib == null ? "?" : (mib / 1024).toFixed(mib >= 10240 ? 0 : 1);
}
function fmtBytes(n) {
  if (n == null) return "?";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i && n < 10 ? 1 : 0)}${units[i]}`;
}
function fmtDuration(sec) {
  if (!isFinite(sec) || sec <= 0) return "";
  if (sec < 60) return `${Math.ceil(sec)} 秒`;
  if (sec < 3600) return `${Math.ceil(sec / 60)} 分钟`;
  return `${(sec / 3600).toFixed(1)} 小时`;
}

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {}, credentials: "same-origin" };
  if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  const resp = await fetch(path, init);
  if (resp.status === 401 && !opts.allow401) {
    showLogin();
    throw new Error("请先登录");
  }
  const data = resp.headers.get("content-type")?.includes("json") ? await resp.json() : await resp.text();
  if (!resp.ok) {
    let msg = data?.detail ?? data;
    if (Array.isArray(msg)) msg = msg.map((e) => `${(e.loc || []).slice(1).join(".")}: ${e.msg}`).join("\n");
    throw new Error(typeof msg === "string" ? msg : `请求失败 (${resp.status})`);
  }
  return data;
}

// ---------------- 气泡：提示条（toast）和悬停提示（tooltip） ----------------
// 都用 popover 放进浏览器的顶层，不会被弹窗（dialog）遮住；样式统一在 style.css 的 .toast / .tip。
const hasPopover = typeof HTMLElement !== "undefined" && "showPopover" in HTMLElement.prototype;
if (!hasPopover) { $("#toast").hidden = true; $("#tip").hidden = true; }
function popShow(el) { if (hasPopover) { if (!el.matches(":popover-open")) el.showPopover(); } else el.hidden = false; }
function popHide(el) { if (hasPopover) { if (el.matches(":popover-open")) el.hidePopover(); } else el.hidden = true; }

// 提示条：鼠标放在上面时不消失，移开后再停留一会儿才消失
let toastTimer;
const toastEl = () => $("#toast");
function toastHideLater(ms) {
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => popHide(toastEl()), ms);
}
function toast(msg) {
  const el = toastEl();
  el.textContent = msg;
  popShow(el);
  toastHideLater(3500);
}
toastEl().addEventListener("mouseenter", () => clearTimeout(toastTimer));
toastEl().addEventListener("mouseleave", () => toastHideLater(1000));

// 悬停提示：接管页面里所有 title="..."（浏览器自带的提示丑、不能统一风格、鼠标一动就消失）。
// 鼠标从元素移到提示上时提示保持显示，移出提示后才消失。
const tipEl = $("#tip");
let tipAnchor = null, tipShowTimer, tipHideTimer, tipPinned = false;
function tipHideNow() { clearTimeout(tipShowTimer); clearTimeout(tipHideTimer); tipAnchor = null; tipPinned = false; tipEl.classList.remove("pinned"); popHide(tipEl); }
function tipHideLater(ms = 250) { if (tipPinned) return; clearTimeout(tipShowTimer); clearTimeout(tipHideTimer); tipHideTimer = setTimeout(tipHideNow, ms); }
const TIP_SEL = "[title], [data-tip], [data-tip-html]";
function tipShow(anchor) {
  const text = anchor.dataset.tip, html = anchor.dataset.tipHtml;
  if (!text && !html) return;
  tipAnchor = anchor;
  tipEl.classList.toggle("rich", !!html);
  if (html) tipEl.innerHTML = html; else tipEl.textContent = text;  // html 由调用方负责转义
  popShow(tipEl);
  const r = anchor.getBoundingClientRect(), t = tipEl.getBoundingClientRect();
  const left = Math.min(Math.max(8, r.left + r.width / 2 - t.width / 2), window.innerWidth - t.width - 8);
  // 默认显示在元素下方，放不下就放上方；中间留 0 间隙，鼠标能直接移上去
  const top = r.bottom + t.height + 8 <= window.innerHeight ? r.bottom : Math.max(8, r.top - t.height);
  tipEl.style.left = `${left}px`;
  tipEl.style.top = `${top}px`;
}
document.addEventListener("mouseover", (e) => {
  if (tipEl.contains(e.target) || tipPinned) return;  // 气泡被点击固定后，不再响应别处的悬停
  const a = e.target.closest?.(TIP_SEL);
  if (!a) return tipAnchor && tipHideLater();
  if (a.hasAttribute("title")) {  // 把 title 摘下来，避免浏览器再弹自带的提示
    if (a.title) a.dataset.tip = a.title;
    a.removeAttribute("title");
  }
  if (!a.dataset.tip && !a.dataset.tipHtml) return;
  if (a === tipAnchor) return clearTimeout(tipHideTimer);
  clearTimeout(tipHideTimer); clearTimeout(tipShowTimer);
  tipShowTimer = setTimeout(() => tipShow(a), tipAnchor || a.hasAttribute("data-pin") ? 0 : 300);
});
document.addEventListener("mouseout", (e) => {
  if (e.relatedTarget && (tipEl.contains(e.relatedTarget) || e.target.closest?.("[data-tip], [data-tip-html]")?.contains(e.relatedTarget))) return;
  if (e.target.closest?.("[data-tip], [data-tip-html]") || tipEl.contains(e.target)) tipHideLater();
});
tipEl.addEventListener("mouseenter", () => clearTimeout(tipHideTimer));
tipEl.addEventListener("mouseleave", () => tipHideLater());
document.addEventListener("pointerdown", (e) => {
  if (tipEl.contains(e.target)) return;
  const pin = e.target.closest?.("[data-pin]");
  if (pin && pin === tipAnchor) return;  // 点的是正在显示气泡的链接：交给下面的 click 处理（固定 / 取消固定）
  tipHideNow();                          // 点击其它地方：气泡消失（包括已固定的）
}, true);
// 蓝色链接（带 data-pin）：点击后气泡固定住，鼠标移开也不消失；再点一次链接或点击别处才消失
document.addEventListener("click", (e) => {
  const a = e.target.closest?.("[data-pin]");
  if (!a || !(a.dataset.tip || a.dataset.tipHtml)) return;
  if (tipPinned && tipAnchor === a) return tipHideNow();
  tipShow(a);
  tipPinned = true;
  tipEl.classList.add("pinned");
});
window.addEventListener("scroll", () => (tipPinned && tipAnchor?.isConnected ? tipShow(tipAnchor) : tipHideNow()), true);
setInterval(() => { if (tipAnchor && !tipAnchor.isConnected) tipHideNow(); }, 1000);  // 日历刷新后元素被替换

// 复制文本，成功返回 true。navigator.clipboard 只在 HTTPS / localhost 可用；平台用 http+IP 访问时退回 execCommand。
// 弹窗（showModal）打开时页面其余部分是 inert 的，临时输入框必须放进弹窗里才能聚焦、选中，否则复制会静默失败。
async function copyText(text, quiet = false) {
  const done = (ok) => { if (!quiet) toast(ok ? "已复制" : "复制失败，请手动选中复制"); return ok; };
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return done(true);
    }
  } catch {}
  const host = document.querySelector("dialog[open]") || document.body;
  const ta = document.createElement("textarea");
  try {
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.cssText = "position:fixed;top:0;left:0;width:1px;height:1px;opacity:0";
    host.appendChild(ta);
    ta.focus(); ta.select(); ta.setSelectionRange(0, text.length);
    return done(document.execCommand("copy"));
  } catch {
    return done(false);
  } finally {
    ta.remove();
  }
}

// ---------------- 全局状态 ----------------

const state = {
  config: null,
  me: null,
  month: null,
  cal: null,
  bars: [],
  users: [], // 管理员才有
  migrations: [],
};

function activeTab() {
  return $(".tabs button.active")?.dataset.tab;
}
function renewRuleText() {
  return `（到期前 ${state.config.auto_renew_before_hours} 小时自动延长 ${state.config.auto_renew_days} 天，并发飞书通知）`;
}

// ---------------- 登录 ----------------

function showLogin() {
  $("#app").classList.add("hidden");
  $("#login").classList.remove("hidden");
}

/** 飞书扫码登录：用飞书官方的二维码组件，扫码确认后带着 tmp_code 走一遍授权，最终回到 /auth/callback。 */
const QR_SDK = "https://lf-package-cn.feishucdn.com/obj/feishu-static/lark/passport/qrcode/LarkSSOSDKWebQRCode-1.0.3.js";
async function initQrLogin() {
  const hint = $("#qr-hint");
  try {
    const { goto } = await api("/auth/qr");
    if (!window.QRLogin) {
      await new Promise((ok, fail) => {
        const s = document.createElement("script");
        s.src = QR_SDK;
        s.onload = ok;
        s.onerror = fail;
        document.head.appendChild(s);
      });
    }
    $("#qr-box").innerHTML = "";
    const qr = window.QRLogin({ id: "qr-box", goto, width: "250", height: "250", style: "border:0;width:250px;height:250px" });
    window.addEventListener("message", (ev) => {
      if (qr.matchOrigin(ev.origin) && qr.matchData(ev.data)) location.href = `${goto}&tmp_code=${ev.data.tmp_code}`;
    });
  } catch {
    hint.textContent = "二维码加载失败，请使用下方按钮登录";
  }
}

$("#dev-login").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/auth/dev", { method: "POST", body: { name: e.target.name.value } });
    location.reload();
  } catch (err) {
    toast(err.message);
  }
});

$("#logout").addEventListener("click", async () => {
  await api("/auth/logout", { method: "POST" });
  location.reload();
});

// ---------------- 标签页 ----------------

function switchTab(name) {
  $$(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("hidden", t.id !== `tab-${name}`));
  try { localStorage.setItem("lab-tab", name); } catch {}
  if (name === "calendar") loadCalendar();
  if (name === "account") renderAccount();
  if (name === "admin") loadAdmin();
}
$$(".tabs button").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));

function renderBanner() {
  const me = state.me;
  const missing = [];
  if (!me.linux_username) missing.push("设置 Linux 用户名");
  if (!me.has_password && !me.key_count) missing.push("设置登录密码或添加 SSH 公钥");
  const el = $("#setup-banner");
  if (!missing.length) return el.classList.add("hidden");
  el.innerHTML = `你还需要 <b>${missing.join("、")}</b>，否则无法登录服务器。<a href="#" id="banner-go">去设置 →</a>`;
  el.classList.remove("hidden");
  $("#banner-go").onclick = (e) => { e.preventDefault(); openOnboarding(true); };
}

// ---------------- 日历 ----------------

// 日历永远按“月”展示：state.month = {y, m}。为了把首尾相接的预约合并成一条、并让合并后的备注与长期使用标记
// 在翻月时保持一致，向服务器多取前后各一段时间的预约。
const MONTH_CN = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "十二"];
const pad2 = (n) => String(n).padStart(2, "0");
const monthStart = () => `${state.month.y}-${pad2(state.month.m)}-01`;
const monthDays = () => new Date(state.month.y, state.month.m, 0).getDate();
function setMonth(y, m) {
  const dt = new Date(y, m - 1, 1);
  state.month = { y: dt.getFullYear(), m: dt.getMonth() + 1 };
  loadCalendar();
}

// 折叠的服务器记在浏览器里（只是个人习惯，读写失败也不影响使用）
const COLLAPSE_KEY = "lab-cal-collapsed";
function readCollapsed() {
  try { return new Set(JSON.parse(localStorage.getItem(COLLAPSE_KEY) || "[]")); } catch { return new Set(); }
}
function writeCollapsed() {
  try { localStorage.setItem(COLLAPSE_KEY, JSON.stringify([...state.collapsed])); } catch {}
}
state.collapsed = readCollapsed();

let calSeq = 0;
async function loadCalendar(quiet = false) {
  const seq = ++calSeq;
  try {
    const data = await api(`/api/calendar?start=${addDays(monthStart(), -45)}&days=${monthDays() + 90}`);
    if (seq !== calSeq) return;  // 期间又切换了月份，丢弃这次结果
    // 自动刷新时数据没变就不重画：避免悬浮提示、鼠标位置被无谓地打断
    const sig = monthStart() + JSON.stringify(data);
    state.cal = data;
    if (sig === state.calSig && $("#calendar").firstChild) return;
    state.calSig = sig;
    renderCalendar();
  } catch (err) {
    if (!quiet) toast(err.message);
  }
}

// 各 Linux 用户家目录占用的空间，从大到小，一人一行
function diskTable(users) {
  const rows = users.map((u) => `<tr><td>${esc(u.user)}</td><td>${gb(u.size)} GB</td></tr>`).join("");
  return `<table class="proc-table"><tr><th>Linux 用户名</th><th>占用空间</th></tr>${rows}</table>`;
}

// 服务器行：整机 CPU 利用率、内存和磁盘占用（GB）。进度条按占用率变色，数字保持黑色。
// 服务器离线时给出提示；在线但 Agent 还没上报则不显示
function serverStats(s) {
  if (!s.online) return `<div class="srv-stats"><span class="offline-tag">服务器离线，资源数据暂不可用</span></div>`;
  const y = s.system;
  if (!y || y.cpu == null) return "";  // 拿不到数据就不显示
  const item = (label, pct, text, tip, extra = "") =>
    `<span class="stat-chip ${pct >= 90 ? "hot" : pct >= 70 ? "mid" : ""}" title="${esc(tip)}">${label}<span class="meter"><i style="width:${Math.min(100, pct)}%"></i></span><b>${text}</b>${extra}</span>`;
  const pct = (u, t) => (t ? (u * 100) / t : 0);
  const parts = [item("CPU", y.cpu, `${Math.round(y.cpu)}%`, `CPU 利用率 ${y.cpu}%（各核平均）`)];
  if (y.mem_total) parts.push(item("内存", pct(y.mem_used, y.mem_total), `${gb(y.mem_used)}/${gb(y.mem_total)} GB`, `内存已用 ${gb(y.mem_used)} GB，共 ${gb(y.mem_total)} GB（${Math.round(pct(y.mem_used, y.mem_total))}%）`));
  if (y.disk_total) {
    // 各用户家目录占用（Agent 后台统计，新版 Agent 才会上报）：悬停显示，点击固定
    const usage = y.users?.length ? `<span class="bubble-link" data-pin data-tip-html="${esc(diskTable(y.users))}">占用情况</span>` : "";
    parts.push(item("磁盘", pct(y.disk_used, y.disk_total), `${gb(y.disk_used)}/${gb(y.disk_total)} GB`, `家目录所在磁盘已用 ${gb(y.disk_used)} GB，共 ${gb(y.disk_total)} GB（${Math.round(pct(y.disk_used, y.disk_total))}%）`, usage));
  }
  return `<div class="srv-stats">${parts.join("")}</div>`;
}

// 占用某张显卡的进程：每个进程一行（Linux 用户名、进程号、位置、显存），位置是进程所在目录
function procTable(procs) {
  if (!procs.length) return `<div class="proc-empty">当前没有进程占用这张显卡</div>`;
  const rows = procs.map((p) => `<tr><td>${esc(p.user || "?")}</td><td>${p.pid}</td><td class="path">${esc(p.path || "-")}</td><td>${p.used_memory == null ? "-" : `${gb(p.used_memory)} GB`}</td></tr>`).join("");
  return `<table class="proc-table"><tr><th>Linux 用户名</th><th>进程号</th><th>位置</th><th>显存占用</th></tr>${rows}</table>`;
}

const ALERT_ICON = `<svg viewBox="0 0 24 24" width="17" height="17" aria-hidden="true"><path d="M12 3.2 1.8 20.8h20.4L12 3.2z" fill="currentColor" fill-opacity=".16" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M12 9.6v5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="17.6" r="1.15" fill="currentColor"/></svg>`;

// 把一张卡上的预约整理成要画的色块：
//  - 同一个人首尾相接的预约合并成一条“连续预约”；
//  - 今天之前的部分单独画成浅色块，今天起的部分正常显示；
//  - 长期使用标记、备注以连续预约里第一条还没结束的（非浅色）预约为准（只在悬停提示里显示）；
//  - 多人共用同一张卡时，每条连续预约占一条“车道”，在同一行里上下并排。
function buildBlocks(list, today, vStart, vEnd) {
  const byUser = {};
  list.forEach((r) => (byUser[r.user_id] ||= []).push(r));
  const chains = [];
  for (const rs of Object.values(byUser)) {
    rs.sort((a, b) => (a.start_date < b.start_date ? -1 : a.start_date > b.start_date ? 1 : 0));
    let cur = null;
    for (const r of rs) {
      if (cur && diffDays(cur.end, r.start_date) <= 1) {
        cur.items.push(r);
        if (r.end_date > cur.end) cur.end = r.end_date;
      } else {
        cur = { user_id: r.user_id, user_name: r.user_name, user_avatar: r.user_avatar, linux_username: r.linux_username, start: r.start_date, end: r.end_date, items: [r] };
        chains.push(cur);
      }
    }
  }
  const visible = chains.filter((c) => c.start <= vEnd && c.end >= vStart).sort((a, b) => (a.start < b.start ? -1 : a.start > b.start ? 1 : 0));
  const laneEnds = [];
  const blocks = [];
  for (const c of visible) {
    c.head = c.items.find((r) => r.end_date >= today) || null;
    let lane = laneEnds.findIndex((e) => e < c.start);
    if (lane < 0) lane = laneEnds.length;
    laneEnds[lane] = c.end;
    const segs = [];
    if (c.start < today) segs.push({ past: true, s: c.start, e: c.end < today ? c.end : addDays(today, -1) });
    if (c.end >= today) segs.push({ past: false, s: c.start > today ? c.start : today, e: c.end });
    for (const g of segs) {
      const s = g.s < vStart ? vStart : g.s, e = g.e > vEnd ? vEnd : g.e;
      if (s > e) continue;
      blocks.push({
        chain: c, lane, past: g.past, c1: diffDays(vStart, s) + 1, c2: diffDays(vStart, e) + 1,
        cutL: g.s < vStart || (!g.past && c.start < today), cutR: g.e > vEnd || (g.past && c.end >= today),
      });
    }
  }
  return { blocks, lanes: Math.max(1, laneEnds.length) };
}

function renderCalendar() {
  const { cal } = state;
  const vStart = monthStart(), days = monthDays(), vEnd = addDays(vStart, days - 1);
  const today = cal.today;
  $("#month-label").textContent = `${state.month.y}年${state.month.m}月`;
  $("#cal-hint").textContent = `点击空白格子预约；点击色块查看详情。同一张卡每天最多 ${state.config.max_gpu_sharers} 人使用。`;
  const root = $("#calendar");
  root.style.setProperty("--days", days);
  state.bars = [];

  const dates = Array.from({ length: days }, (_, i) => addDays(vStart, i));
  const cellClass = (d) => {
    const wd = parseDate(d).getDay();
    return [wd === 0 || wd === 6 ? "weekend" : "", wd === 1 ? "mon" : "", d === today ? "today" : "", d < today ? "past" : ""].join(" ");
  };

  let html = `<div class="cal-row cal-head"><div class="cal-label">服务器 / 显卡</div><div class="cal-track">`;
  dates.forEach((d, i) => {
    const dt = parseDate(d);
    html += `<div class="cal-cell ${cellClass(d)}" style="grid-column:${i + 1}"><b>${dt.getDate()}</b><small>${d === today ? "今天" : `周${WEEK[dt.getDay()]}`}</small></div>`;
  });
  html += `</div></div>`;

  if (!cal.servers.length) {
    html += `<div class="empty">还没有 GPU 服务器。${state.me.is_admin ? "请在「管理」页添加服务器并安装 Agent。" : "请联系管理员添加。"}</div>`;
  }

  const byGpu = {};
  cal.reservations.forEach((r) => (byGpu[r.gpu_id] ||= []).push(r));
  const busyToday = (g) => (byGpu[g.id] || []).some((r) => r.start_date <= today && today <= r.end_date);
  // 已过去的日期共用一层斜线、今天这一列盖在色块上面：都是整段的覆盖层
  const pastN = Math.min(days, Math.max(0, diffDays(vStart, today))), todayIdx = diffDays(vStart, today);

  for (const s of cal.servers) {
    const alertGpus = s.gpus.filter((g) => g.alerts.length);
    const collapsed = state.collapsed.has(s.id);
    const freeN = s.gpus.filter((g) => !busyToday(g)).length;
    html += `<div class="cal-server-group ${collapsed ? "collapsed" : ""}" data-srv="${s.id}">
      <div class="cal-row cal-server" data-toggle-srv="${s.id}" role="button" tabindex="0" aria-expanded="${!collapsed}">
        <div class="cal-label">
          <span class="srv-title">${icon("chev", "chev")}<span class="dot ${s.online ? "on" : ""}"></span><span class="server-name">${esc(s.name)}</span>
            ${alertGpus.length ? `<span class="alert-ico" title="${esc(`${alertGpus.length} 张卡上有未预约的用户正在使用`)}">${ALERT_ICON}</span>` : ""}</span>
          <span class="srv-host"><span class="mono">${esc(s.host)}</span>${s.gpus.length ? `<span class="srv-free ${freeN ? "" : "none"}">${freeN ? `今天空闲 ${freeN}` : "今天已满"}</span>` : ""}</span></div>
        <div class="cal-track">${serverStats(s)}</div></div>
      <div class="srv-body"><div class="srv-inner">`;
    for (const g of s.gpus) {
      const util = g.utilization ?? 0;
      const live = s.online && g.memory_total != null;
      const stat = live
        ? `<span class="meter ${util >= 90 ? "hot" : util >= 70 ? "mid" : ""}" title="利用率 ${util}%"><i style="width:${util}%"></i></span>${util}% · ${gb(g.memory_used)}/${gb(g.memory_total)}G`
        : `<span>离线</span>`;
      const detail = live ? `<span class="gpu-detail bubble-link" data-pin data-tip-html="${esc(procTable(g.processes))}">详情</span>` : "";
      const alert = g.alerts.length
        ? `<span class="alert-ico" title="${esc(`这些用户未预约，但正在使用 GPU：\n${g.alerts.map((a) => `• ${a}`).join("\n")}`)}">${ALERT_ICON}</span>`
        : `<span></span>`;
      const busy = busyToday(g);
      const { blocks, lanes } = buildBlocks(byGpu[g.id] || [], today, vStart, vEnd);
      html += `<div class="cal-row cal-gpu"><div class="cal-label gpu-label">
        <span class="gpu-free ${busy ? "" : "free"}" title="${busy ? "今天已有人预约" : "今天空闲"}"></span>
        <div class="gpu-l1"><b>GPU${g.index}</b><span>${esc(g.name.replace(/^NVIDIA (GeForce )?/, ""))}</span></div>
        <div class="gpu-l2">${stat}</div>
        <div class="gpu-extra">${alert}${detail}</div></div>
        <div class="cal-track lanes-${Math.min(lanes, 3)}" style="--lanes:${lanes}">`;
      dates.forEach((d, i) => {
        html += `<div class="cal-cell ${cellClass(d)}" style="grid-column:${i + 1}" data-server="${s.id}" data-gpu="${g.id}" data-date="${d}"></div>`;
      });
      for (const b of blocks) {
        const c = b.chain, head = c.head;
        const mine = c.user_id === state.me.id;
        const n = state.bars.push(b) - 1;
        const tags = `${head?.auto_renew ? `<span class="badge running">长期使用</span>` : ""}${b.past ? `<span class="badge">已结束的部分</span>` : ""}${mine ? `<span class="badge ok">我的预约</span>` : ""}`;
        const tip = `<div class="tt-who">${avatarHtml(c.user_name, c.user_avatar, c.user_id)}${esc(c.user_name)}<small>${esc(c.linux_username || "-")}</small></div>
          <div class="tt-row">${c.start} ~ ${c.end}${c.items.length > 1 ? `（${c.items.length} 段连续预约）` : ""}</div>${head?.note ? `<div class="tt-row">${esc(head.note)}</div>` : ""}${tags ? `<div class="tt-tags">${tags}</div>` : ""}`;
        // 与后面正在生效的部分相连的“已结束”淡色段不再重复头像和名字
        const inner = b.past && b.cutR ? "" : `${avatarHtml(c.user_name, c.user_avatar, c.user_id)}<span class="nm">${esc(c.user_name)}</span>${head?.note ? `<span class="note">· ${esc(head.note)}</span>` : ""}${head?.auto_renew ? icon("renew", "renew") : ""}`;
        html += `<div class="bar ${mine ? "mine" : ""} ${b.past ? "past" : ""} ${b.cutL ? "cut-l" : ""} ${b.cutR ? "cut-r" : ""}"
          style="grid-column:${b.c1}/${b.c2 + 1};grid-row:${b.lane + 1};--h:${hue(c.user_id)}" data-bar="${n}" data-tip-html="${esc(tip)}">${inner}</div>`;
      }
      if (pastN > 0) html += `<div class="past-zone" style="grid-column:1/${pastN + 1}"></div>`;
      if (todayIdx >= 0 && todayIdx < days) html += `<div class="today-zone" style="grid-column:${todayIdx + 1}"></div>`;
      html += `</div></div>`;
    }
    html += `</div></div></div>`;
  }
  root.innerHTML = html;
}

$("#calendar").addEventListener("click", (e) => {
  const bar = e.target.closest(".bar");
  if (bar) {
    const b = state.bars[Number(bar.dataset.bar)];
    // 合并后的色块由多条预约组成：按点击的位置找到那一天对应的预约
    const rect = bar.parentElement.getBoundingClientRect();
    const col = Math.min(monthDays() - 1, Math.max(0, Math.floor(((e.clientX - rect.left) / rect.width) * monthDays())));
    const day = addDays(monthStart(), col);
    const r = b.chain.items.find((x) => x.start_date <= day && day <= x.end_date) || b.chain.head || b.chain.items[0];
    return openDetail(r);
  }
  const cell = e.target.closest(".cal-cell[data-gpu]");
  if (cell && !cell.classList.contains("past")) {
    return openBooking({ serverId: Number(cell.dataset.server), gpuId: Number(cell.dataset.gpu), date: cell.dataset.date });
  }
  const label = e.target.closest(".cal-server > .cal-label");  // 只有最左侧的服务器格子才折叠 / 展开
  if (label) toggleServer(label.parentElement);
});
$("#calendar").addEventListener("keydown", (e) => {
  const head = e.target.closest?.("[data-toggle-srv]");
  if (head && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); toggleServer(head); }
});

// 折叠 / 展开一台服务器：只切换 class，由 CSS 过渡负责动画（不重画日历，动画才不会被打断）
function toggleServer(head) {
  const id = Number(head.dataset.toggleSrv);
  const group = head.closest(".cal-server-group");
  const collapsed = group.classList.toggle("collapsed");
  head.setAttribute("aria-expanded", String(!collapsed));
  state.lastToggle = Date.now();  // 折叠动画进行中不要自动刷新（重画会打断动画）
  if (collapsed) state.collapsed.add(id); else state.collapsed.delete(id);
  writeCollapsed();
}

$("#prev").onclick = () => setMonth(state.month.y, state.month.m - 1);
$("#next").onclick = () => setMonth(state.month.y, state.month.m + 1);

// 年 / 月选择：点月份按钮在下方弹出，点别处自动收起
const monthPop = $("#month-pop");
const closeMonthPop = () => monthPop.classList.add("hidden");
let pickYear = 0;
function renderMonthPicker() {
  $("#year-label").textContent = `${pickYear} 年`;
  $("#month-grid").innerHTML = MONTH_CN.map((n, i) => {
    const cur = pickYear === state.month.y && i + 1 === state.month.m;
    return `<button type="button" class="btn ${cur ? "primary" : ""}" data-pick-month="${i + 1}">${i + 1} 月</button>`;
  }).join("");
}
$("#month-btn").onclick = () => { pickYear = state.month.y; renderMonthPicker(); monthPop.classList.toggle("hidden"); };
$("#year-prev").onclick = () => { pickYear--; renderMonthPicker(); };
$("#year-next").onclick = () => { pickYear++; renderMonthPicker(); };
$("#month-grid").addEventListener("click", (e) => {
  const b = e.target.closest("[data-pick-month]");
  if (!b) return;
  closeMonthPop();
  setMonth(pickYear, Number(b.dataset.pickMonth));
});
$("#month-this").onclick = () => {
  const [y, m] = state.config.today.split("-").map(Number);
  closeMonthPop();
  setMonth(y, m);
};
$("#month-close").onclick = closeMonthPop;
document.addEventListener("click", (e) => { if (!e.target.closest(".nav-group")) closeMonthPop(); });

// ---------------- 预约弹窗 ----------------

const bookingDialog = $("#booking-dialog");
const bookingForm = $("#booking-form");

// 根据当前已加载的日历数据标出每张卡在所选日期范围内的占用情况（只是提示，最终以服务端校验为准）：
// peak = 范围内某一天最多有几个人同时预约；mine = 预约人自己在这张卡上已有预约；names = 涉及的人
function gpuLoad(start, end) {
  const owner = state.me.is_admin && bookingForm.user_id.value ? Number(bookingForm.user_id.value) : state.me.id;
  const out = {};
  for (const r of state.cal?.reservations || []) {
    if (r.start_date > end || r.end_date < start) continue;
    if (bookingMode() === "long" && r.user_id === owner) continue;  // 长期使用会覆盖自己在这张卡上的临时预约
    const o = (out[r.gpu_id] ||= { rs: [], names: new Set(), mine: false, peak: 0 });
    o.rs.push(r);
    o.names.add(r.user_name);
    if (r.user_id === owner) o.mine = true;
  }
  for (const o of Object.values(out)) {
    for (let d = start, n = 0; d <= end && n < 400; d = addDays(d, 1), n++) {
      o.peak = Math.max(o.peak, o.rs.filter((r) => r.start_date <= d && d <= r.end_date).length);
    }
  }
  return out;
}

// 当前表单选择的日期范围：长期使用从今天起一个周期（之后自动延长），临时使用取所填日期
const bookingMode = () => bookingForm.mode.value;
function bookingRange() {
  if (bookingMode() === "long") {
    const t = state.config.today;
    return { start: t, end: addDays(t, Math.min(state.config.auto_renew_days, state.config.max_reservation_days) - 1) };
  }
  return { start: bookingForm.start_date.value, end: bookingForm.end_date.value || bookingForm.start_date.value };
}
function syncBookingMode() {
  const long = bookingMode() === "long";
  $$(".temp-only", bookingForm).forEach((el) => el.classList.toggle("hidden", long));
  $$(".long-only", bookingForm).forEach((el) => el.classList.toggle("hidden", !long));
  bookingForm.start_date.required = bookingForm.end_date.required = !long;
  $("#booking-rule").textContent = long
    ? `规则：从今天起预约 ${Math.min(state.config.auto_renew_days, state.config.max_reservation_days)} 天，并在到期前 ${state.config.auto_renew_before_hours} 小时自动再延长 ${state.config.auto_renew_days} 天，直到你改为临时使用或释放。这些显卡上你已有的临时预约会被覆盖（今天之前的使用记录保留）。同一张卡同一天最多 ${state.config.max_gpu_sharers} 人共用。`
    : `规则：单次最长 ${state.config.max_reservation_days} 天，最多提前 ${state.config.max_advance_days} 天；按自然日计算，开始当天 00:00 起可使用所选显卡，结束日 24:00 收回。同一张卡同一天最多 ${state.config.max_gpu_sharers} 人共用。`;
}
const reloadPicks = () => renderGpuPicks($$("input[name=gpu]:checked", bookingForm).map((i) => Number(i.value)));
$$("input[name=mode]", bookingForm).forEach((i) => (i.onchange = () => { syncBookingMode(); reloadPicks(); }));

function renderGpuPicks(selected = []) {
  const s = state.cal.servers.find((x) => x.id === Number(bookingForm.server.value));
  const range = bookingRange();
  const load = gpuLoad(range.start, range.end);
  const cap = state.config.max_gpu_sharers;
  $("#booking-gpus").innerHTML = (s?.gpus || [])
    .map((g) => {
      const o = load[g.id];
      const blocked = !!o && (o.mine || o.peak >= cap);
      const tip = !o ? "" : o.mine ? "你在这段时间已经预约了这张卡" : `${[...o.names].join("、")} 已预约${o.peak >= cap ? `，人数已满（最多 ${cap} 人共用）` : `，还可以再有 ${cap - o.peak} 人共用`}`;
      const sub = !o ? "空闲" : o.mine ? "你已预约" : o.peak >= cap ? `已满员（${o.peak}/${cap}）` : `已有 ${o.peak}/${cap} 人`;
      return `<label class="${blocked ? "busy" : ""}" title="${esc(tip)}">
        <input type="checkbox" name="gpu" value="${g.id}" ${selected.includes(g.id) && !blocked ? "checked" : ""} ${blocked ? "disabled" : ""}>
        <span class="ck">${icon("check")}</span><span><b>GPU${g.index}</b><small>${sub}</small></span></label>`;
    })
    .join("") || `<span class="muted small">该服务器还没有上报显卡</span>`;
}

async function openBooking({ serverId, gpuId, date }) {
  if (!state.cal?.servers.length) return toast("还没有可预约的服务器");
  const today = state.config.today;
  bookingForm.reset();
  $("#booking-error").textContent = "";
  bookingForm.server.innerHTML = state.cal.servers.map((s) => `<option value="${s.id}">${esc(s.name)}${s.description ? ` · ${esc(s.description)}` : ""}</option>`).join("");
  if (serverId) bookingForm.server.value = serverId;
  bookingForm.start_date.value = date || today;
  bookingForm.end_date.value = date || today;
  bookingForm.start_date.min = bookingForm.end_date.min = state.me.is_admin ? "" : today;
  $$(".renew-rule").forEach((el) => (el.textContent = renewRuleText()));
  syncBookingMode();
  if (state.me.is_admin) {
    const users = state.users.length ? state.users : (state.users = await api("/api/admin/users"));
    $("#booking-user").innerHTML = users
      .filter((u) => u.linux_username && u.enabled)
      .map((u) => `<option value="${u.id}" ${u.id === state.me.id ? "selected" : ""}>${esc(u.name)}（${esc(u.linux_username)}）</option>`)
      .join("");
  }
  const srv = state.cal.servers.find((x) => x.id === Number(bookingForm.server.value));
  const gpu = srv?.gpus.find((x) => x.id === gpuId);
  $("#booking-sub").textContent = [srv?.name, gpu && `GPU${gpu.index}`, date].filter(Boolean).join(" · ") || `同一张卡同一天最多 ${state.config.max_gpu_sharers} 人共用`;
  renderGpuPicks(gpuId ? [gpuId] : []);
  bookingDialog.showModal();
}

bookingForm.server.onchange = () => renderGpuPicks();
bookingForm.user_id.onchange = () => renderGpuPicks($$("input[name=gpu]:checked", bookingForm).map((i) => Number(i.value)));
bookingForm.start_date.onchange = () => {
  if (bookingForm.end_date.value < bookingForm.start_date.value) bookingForm.end_date.value = bookingForm.start_date.value;
  renderGpuPicks($$("input[name=gpu]:checked", bookingForm).map((i) => Number(i.value)));
};
bookingForm.end_date.onchange = () => renderGpuPicks($$("input[name=gpu]:checked", bookingForm).map((i) => Number(i.value)));

bookingForm.addEventListener("submit", async (e) => {
  if (e.submitter?.value === "cancel") return;
  e.preventDefault();
  const gpuIds = $$("input[name=gpu]:checked", bookingForm).map((i) => Number(i.value));
  if (!gpuIds.length) return ($("#booking-error").textContent = "请至少选择一张显卡");
  const body = {
    gpu_ids: gpuIds,
    note: bookingForm.note.value,
  };
  if (bookingMode() === "long") body.long_term = true;
  else Object.assign(body, { start_date: bookingForm.start_date.value, end_date: bookingForm.end_date.value });
  if (state.me.is_admin && bookingForm.user_id.value) body.user_id = Number(bookingForm.user_id.value);
  $("#booking-submit").disabled = true;
  try {
    await api("/api/reservations", { method: "POST", body });
    bookingDialog.close();
    toast("预约成功，预约当天起可以使用所选显卡");
    loadCalendar();
  } catch (err) {
    $("#booking-error").textContent = err.message;
  } finally {
    $("#booking-submit").disabled = false;
  }
});

// ---------------- 预约详情 ----------------

const detailDialog = $("#detail-dialog");
const detailForm = $("#detail-form");
let detailRes = null;

function openDetail(r) {
  if (!r) return;
  detailRes = r;
  const today = state.config.today;
  const mine = r.user_id === state.me.id;
  const ended = r.end_date < today;
  $("#detail-avatar").innerHTML = avatarHtml(r.user_name, r.user_avatar, r.user_id);
  $("#detail-title").textContent = mine ? "我的预约" : `${r.user_name} 的预约`;
  $("#detail-sub").textContent = `${r.server_name} · GPU${r.gpu_index}`;
  const status = ended ? `<span class="badge">已结束</span>` : `<span class="badge running">${esc(reservationStatus(r))}</span>`;
  const canEdit = r.editable && !ended;  // 可编辑时日期、备注就在下面的输入框里，这里不再重复
  $("#detail-info").innerHTML = `<dt>使用者</dt><dd>${esc(r.user_name)} <span class="muted mono">(${esc(r.linux_username || "-")})</span></dd>
    <dt>${canEdit ? "状态" : "日期"}</dt><dd>${canEdit ? "" : `${r.start_date} ~ ${r.end_date} `}${status}</dd>
    ${canEdit ? "" : `<dt>方式</dt><dd>${r.auto_renew ? `<span class="badge running">长期使用</span>${r.renew_count ? ` 已自动延长 ${r.renew_count} 次` : ""}` : "临时使用"}</dd>
    ${r.note ? `<dt>备注</dt><dd>${esc(r.note)}</dd>` : ""}`}`;
  $("#detail-error").textContent = "";
  const editable = r.editable && r.end_date >= today;  // 已结束的预约只能查看
  $$(".editable", detailForm).forEach((el) => el.classList.toggle("hidden", !editable));
  $$(".renew-rule").forEach((el) => (el.textContent = renewRuleText()));
  detailForm.start_date.value = r.start_date;
  detailForm.end_date.value = r.end_date;
  detailForm.note.value = r.note;
  detailForm.auto_renew.checked = r.auto_renew;
  detailForm.start_date.disabled = r.start_date <= today;  // 已经开始的预约，开始日期（已过去的部分）不能改
  detailForm.start_date.title = detailForm.start_date.disabled ? "已经开始的预约不能修改开始日期" : "";
  detailForm.end_date.min = state.me.is_admin ? "" : today;
  $("#detail-release").textContent = r.start_date >= today ? "取消预约" : "立即释放";
  detailDialog.showModal();
}

detailForm.addEventListener("submit", async (e) => {
  if (e.submitter?.value === "cancel") return;
  e.preventDefault();
  try {
    await api(`/api/reservations/${detailRes.id}`, {
      method: "PATCH",
      body: {
        start_date: detailForm.start_date.value,
        end_date: detailForm.end_date.value,
        note: detailForm.note.value,
        auto_renew: detailForm.auto_renew.checked,
      },
    });
    detailDialog.close();
    toast("已保存");
    refreshCurrentTab();
  } catch (err) {
    $("#detail-error").textContent = err.message;
  }
});

$("#detail-release").onclick = async () => {
  const r = detailRes;
  const msg = r.start_date >= state.config.today
    ? "确定取消这条预约吗？"
    : `确定立即释放吗？今天起这张卡会让给别人，你将不能再使用它${state.config.require_reservation_to_login ? "，也无法再登录该服务器" : ""}（若没有其他预约）。`;
  if (!confirm(msg)) return;
  try {
    await api(`/api/reservations/${r.id}`, { method: "DELETE" });
    detailDialog.close();
    toast("已释放");
    refreshCurrentTab();
  } catch (err) {
    $("#detail-error").textContent = err.message;
  }
};

function refreshCurrentTab() {
  if (activeTab() === "account") renderAccount();
  else loadCalendar();
}

// ---------------- 我的账号 ----------------

const ACCOUNT_STATE = { ok: "已同步", pending: "等待生效", error: "同步失败" };

let keyFormOpen = false;

// 预约的进度说明：进行中 · 剩 N 天
function reservationStatus(r) {
  const today = state.config.today;
  if (r.start_date > today) return `未开始 · ${diffDays(today, r.start_date)} 天后开始`;
  const left = diffDays(today, r.end_date);
  return left > 0 ? `进行中 · 剩 ${left} 天` : "进行中 · 今天到期";
}

async function renderAccount() {
  const [me, servers, rs] = await Promise.all([api("/api/me"), api("/api/me/servers"), api("/api/me/reservations")]);
  state.me = me;
  state.myServers = servers;
  renderBanner();

  // 顶部身份条
  $("#hero-avatar").innerHTML = avatarHtml(me.name, me.avatar_url, me.id);
  $("#hero-name").textContent = me.name;
  $("#hero-username").textContent = me.linux_username || "未设置";
  $("#rename-btn").classList.toggle("hidden", !me.linux_username);
  $("#rename-btn").onclick = openRename;
  const synced = servers.filter((s) => s.online && s.account?.state === "ok").length;
  const stat = (jump, cls, ic, val, label) =>
    `<a class="hero-stat" href="#" data-jump="${jump}"><span class="icon-box ${cls}">${icon(ic)}</span><div><b>${val}</b><span class="l">${label}</span></div></a>`;
  $("#hero-stats").innerHTML = [
    stat("c-servers", me.linux_username && servers.length && synced === servers.length ? "ok" : "warn", "server",
      me.linux_username ? `${synced} / ${servers.length}` : "未设置", "服务器已同步"),
    stat("c-password", me.has_password ? "ok" : "warn", "lock", me.has_password ? "已开启" : "未开启", "密码登录"),
    stat("c-keys", "", "key", `${me.ssh_keys.length} 个`, "SSH 公钥"),
    stat("c-reservations", "warn", "cal", `${rs.length} 个`, "当前预约"),
  ].join("");

  // 服务器账号
  const box = $("#linux-account");
  if (!me.linux_username) {
    box.innerHTML = `<div class="card-b"><p class="card-note" style="margin-bottom:12px">这是你在所有 GPU 服务器上的登录名，建议用姓名拼音，如 <code>zhangsan</code>。以后也可以修改。</p>
      <form id="username-form" class="stack"><label>Linux 用户名<input name="username" placeholder="小写字母开头，如 zhangsan" required></label>
      <div><button class="btn primary">保存</button></div></form></div>`;
    $("#username-form").onsubmit = async (e) => {
      e.preventDefault();
      try {
        await api("/api/me/linux-username", { method: "PUT", body: { username: e.target.username.value.trim() } });
        toast("已保存，约 1 分钟后同步到各服务器");
        renderAccount();
      } catch (err) {
        toast(err.message);
      }
    };
  } else {
    const rows = servers.map((s) => {
      const a = s.account;
      const st = !s.online ? `<span class="badge">服务器离线</span>`
        : !a ? `<span class="badge pending has-dot">同步中…</span>`
        : `<span class="badge ${a.state} has-dot" title="${esc(a.message)}">${ACCOUNT_STATE[a.state] || a.state}</span>`;
      const local = a && a.username !== me.linux_username ? `<div class="srv-acct-msg small warn-text">当前仍为 ${esc(a.username)}：${esc(a.message)}</div>`
        : a?.state === "error" ? `<div class="srv-acct-msg small" style="color:var(--danger)">${esc(a.message)}</div>` : "";
      const cmd = `ssh ${a?.username || me.linux_username}@${s.host || s.name}`;
      return `<div class="srv-acct"><div class="name"><span class="dot ${s.online ? "on" : ""}"></span>${esc(s.name)}</div>
        <div class="cmd"><span class="prompt mono">$</span><code>${esc(cmd)}</code><button class="btn ghost icon small" data-copy="${esc(cmd)}" title="复制">${icon("copy")}</button></div>
        ${st}${local}</div>`;
    }).join("");
    box.innerHTML = `<div class="card-b" style="padding-bottom:12px"><p class="card-note tip-line">${icon("info")}<span>${state.config.require_reservation_to_login
        ? "只有在<b>当天预约了该服务器显卡</b>时才能登录（需在实验室内网）。"
        : "随时都可以登录服务器（需在实验室内网），不需要预约；预约只决定你能用哪些显卡。"}登录后执行 <code>lab-gpus</code> 查看今天分配给你的显卡。</span></p></div>
      ${servers.length ? rows : `<div class="card-b"><p class="muted">暂无服务器</p></div>`}`;
  }

  // 密码
  const pw = $("#password-state");
  pw.className = `pw-state${me.has_password ? "" : " off"}`;
  pw.innerHTML = me.has_password ? `${icon("check")}<b>已设置</b><span>可以用密码登录</span>` : `<b>未设置</b><span>目前只能用公钥登录</span>`;
  $("#password-clear").classList.toggle("hidden", !me.has_password);

  // 公钥：还没有公钥时直接展开添加表单
  $("#key-list").innerHTML = me.ssh_keys.map((k) => `<div class="key-item"><span class="icon-box">${icon("key")}</span>
      <div class="key-main"><b>${esc(k.title)}</b><code>${esc(k.fingerprint)}</code></div>
      <button class="btn ghost icon small danger-ghost" data-del-key="${k.id}" title="删除">${icon("trash")}</button></div>`).join("");
  const noKeys = !me.ssh_keys.length;
  $("#key-form").classList.toggle("hidden", !(noKeys || keyFormOpen));
  $("#key-toggle").classList.toggle("hidden", noKeys || keyFormOpen);
  $("#key-cancel").classList.toggle("hidden", noKeys);

  // 预约
  $("#my-reservations").innerHTML = rs.length
    ? `<div style="padding-top:10px"><div class="res-row head"><span>服务器</span><span>显卡</span><span>日期</span><span>长期使用</span><span></span></div>
        ${rs.map((r) => `<div class="res-row"><div><div>${esc(r.server_name)}</div>${r.note ? `<div class="sub" title="${esc(r.note)}">${esc(r.note)}</div>` : ""}</div>
          <span><span class="gpu-chip">GPU${r.gpu_index}</span></span>
          <div class="res-date">${r.start_date} ~ ${r.end_date}<small>${reservationStatus(r)}</small></div>
          <label class="renew"><span class="switch"><input type="checkbox" data-renew="${r.id}" ${r.auto_renew ? "checked" : ""}><i></i></span>${r.renew_count ? `已续 ${r.renew_count} 次` : ""}</label>
          <button class="btn small" data-open-res="${r.id}">修改</button></div>`).join("")}</div>
      <div class="card-foot" style="margin-top:12px">长期使用${esc(renewRuleText())}。与他人预约冲突时只延长到冲突前一天。</div>`
    : `<div class="card-b"><p class="muted">没有进行中或即将开始的预约。</p></div>`;
  $$("[data-open-res]").forEach((b) => (b.onclick = () => openDetail(rs.find((r) => r.id === Number(b.dataset.openRes)))));
  $$("[data-renew]").forEach((c) => (c.onchange = async () => {
    try {
      await api(`/api/reservations/${c.dataset.renew}`, { method: "PATCH", body: { auto_renew: c.checked } });
      toast(c.checked ? "已改为长期使用" : "已改为临时使用");
    } catch (err) {
      c.checked = !c.checked;
      toast(err.message);
    }
  }));

  await refreshMigrations();
}

// 通用点击：复制命令、跳转到卡片、去日历、展开 / 收起添加公钥
document.addEventListener("click", (e) => {
  const copy = e.target.closest("[data-copy]");
  if (copy) copyText(copy.dataset.copy);
  const jump = e.target.closest("[data-jump]");
  if (jump) { e.preventDefault(); $(`#${jump.dataset.jump}`)?.scrollIntoView({ behavior: "smooth", block: "center" }); }
  const go = e.target.closest("[data-goto]");
  if (go) switchTab(go.dataset.goto);
});
$("#key-toggle").onclick = () => { keyFormOpen = true; $("#key-form").classList.remove("hidden"); $("#key-toggle").classList.add("hidden"); $("#key-form").public_key.focus(); };
$("#key-cancel").onclick = () => { keyFormOpen = false; $("#key-form").reset(); $("#key-form").classList.add("hidden"); $("#key-toggle").classList.remove("hidden"); };

$("#key-list").addEventListener("click", async (e) => {
  const id = e.target.closest("[data-del-key]")?.dataset.delKey;
  if (!id || !confirm("删除这个公钥？使用它的设备将无法再用公钥登录服务器。")) return;
  try {
    await api(`/api/me/keys/${id}`, { method: "DELETE" });
    renderAccount();
  } catch (err) {
    toast(err.message);
  }
});

async function addKey(form) {
  await api("/api/me/keys", { method: "POST", body: { title: form.title?.value || "", public_key: form.public_key.value } });
  form.reset();
}
$("#key-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await addKey(e.target);
    keyFormOpen = false;
    toast("公钥已添加，约 1 分钟后同步到各服务器");
    renderAccount();
  } catch (err) {
    toast(err.message);
  }
});

async function savePassword(form) {
  if (form.password.value !== form.confirm.value) throw new Error("两次输入的密码不一致");
  await api("/api/me/password", { method: "PUT", body: { password: form.password.value } });
  form.reset();
}
$("#password-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await savePassword(e.target);
    toast("密码已保存，约 1 分钟后在各服务器生效");
    renderAccount();
  } catch (err) {
    toast(err.message);
  }
});
$("#password-clear").onclick = async () => {
  if (!confirm("关闭后只能用 SSH 公钥登录服务器。确定吗？")) return;
  try {
    await api("/api/me/password", { method: "DELETE" });
    toast("已关闭密码登录");
  } catch (err) {
    toast(err.message);
  }
  renderAccount();
};

// ---------------- 修改用户名 ----------------

const renameDialog = $("#rename-dialog");
function openRename() {
  $("#rename-form").reset();
  $("#rename-error").textContent = "";
  $("#rename-form").username.value = state.me.linux_username || "";
  renameDialog.showModal();
}
$("#rename-form").addEventListener("submit", async (e) => {
  if (e.submitter?.value === "cancel") return;
  e.preventDefault();
  try {
    await api("/api/me/linux-username", { method: "PUT", body: { username: e.target.username.value.trim() } });
    renameDialog.close();
    toast("已提交，约 1 分钟后在各服务器生效，可在下方查看各服务器状态");
    renderAccount();
  } catch (err) {
    $("#rename-error").textContent = err.message;
  }
});

// ---------------- 文件迁移 ----------------

const TASK_STATE = {
  pending_approval: "等待管理员确认", queued: "排队中", running: "进行中", done: "已完成",
  failed: "失败", cancelled: "已取消", rejected: "已拒绝",
};
const samples = {}; // 迁移速度估算：id -> {t, bytes}

function taskProgress(t) {
  const copying = t.phase === "复制文件" || t.phase.startsWith("复制 ");
  if (t.status === "done") return 100;
  if (t.status !== "running") return 0;
  if (copying && t.bytes_total) return Math.min(100, (t.bytes_done / t.bytes_total) * 100);
  if (t.files_total && t.phase === "修改归属与权限") return Math.min(100, (t.files_done / t.files_total) * 100);
  return null; // 不确定进度
}

function taskHtml(t, admin = false) {
  const pct = taskProgress(t);
  let speed = "";
  if (t.status === "running" && (t.phase === "复制文件" || t.phase.startsWith("复制 "))) {
    const now = Date.now();
    const prev = samples[t.id];
    if (prev && now > prev.t && t.bytes_done > prev.bytes) {
      const rate = ((t.bytes_done - prev.bytes) / (now - prev.t)) * 1000;
      prev.rate = prev.rate ? prev.rate * 0.6 + rate * 0.4 : rate;
      speed = `${fmtBytes(prev.rate)}/s · 剩余约 ${fmtDuration((t.bytes_total - t.bytes_done) / prev.rate)}`;
    } else if (prev?.rate) {
      speed = `${fmtBytes(prev.rate)}/s`;
    }
    samples[t.id] = { t: now, bytes: t.bytes_done, rate: prev?.rate };
  }
  const detail = t.status === "running" || t.status === "done"
    ? `${t.phase === "复制文件" || t.phase.startsWith("复制 ") || t.status === "done" ? `${fmtBytes(t.bytes_done)} / ${fmtBytes(t.bytes_total)}` : fmtBytes(t.bytes_total)} · ${Math.min(t.files_done, t.files_total).toLocaleString()} / ${t.files_total.toLocaleString()} 个文件`
    : "";
  const actions = [];
  if (["pending_approval", "queued"].includes(t.status) && !admin) actions.push(`<button class="btn small" data-mig-cancel="${t.id}">取消</button>`);
  if (t.status === "failed" && !admin) actions.push(`<button class="btn small" data-mig-retry="${t.id}">重试</button>`);
  if (admin && t.status === "pending_approval") {
    actions.push(`<button class="btn small primary" data-mig-approve="${t.id}">同意</button><button class="btn small danger" data-mig-reject="${t.id}">拒绝</button>`);
  }
  const who = admin ? `<span class="mig-who">${avatarHtml(t.user_name, t.user_avatar, t.user_id)}${esc(t.user_name)}</span>` : "";
  const title = `${who}<b>${esc(t.server_name)}</b>${t.kind === "profile"
    ? esc(t.summary)
    : `<span class="path">${esc(t.source)} → ${esc(t.dest_path || `~/${t.dest_name}`)}</span> <span class="muted small">${MODE_NAMES[t.mode] || t.mode}</span>`}`;
  const badge = `<span class="badge ${t.status} has-dot">${TASK_STATE[t.status] || t.status}</span>`;
  const progress = ["running", "done"].includes(t.status)
    ? `<div class="progress ${t.status === "done" ? "done" : ""} ${pct == null ? "indeterminate" : ""}"><i style="width:${pct ?? 30}%"></i></div>` : "";
  const metaLeft = `${t.status === "running" ? esc(t.phase) + "　" : ""}${detail}`;
  const meta = metaLeft || speed ? `<div class="mig-meta"><span>${metaLeft}</span><span>${speed}</span></div>` : "";
  const msg = t.message ? `<div class="note ${t.status === "failed" ? "error" : ""}">${esc(t.message)}</div>` : "";
  const body = `<div class="mig-body">${progress}${meta}${msg}</div>`;
  // 已结束的任务折叠成一行，点开才看详情；进行中 / 等待 / 失败的保持展开
  if (["done", "cancelled", "rejected"].includes(t.status)) {
    return `<details class="mig" data-st="${migGroup(t)}" data-mig-id="${t.id}" ${migOpen.has(t.id) ? "open" : ""}>
      <summary>${badge}<span class="mig-title">${title}</span>${icon("chev", "mig-chev")}</summary>${body}</details>`;
  }
  return `<div class="mig ${t.status === "running" ? "running" : t.status === "failed" ? "failed" : ""}" data-st="${migGroup(t)}">
    <div class="mig-head">${badge}<span class="mig-title">${title}</span>${actions.join("")}</div>${body}</div>`;
}

// 管理页迁移记录的筛选分组
function migGroup(t) {
  if (["queued", "running", "pending_approval"].includes(t.status)) return "active";
  return t.status === "done" ? "done" : "other";
}
// 折叠任务的展开状态要记住：进行中任务每 3 秒会重画一次列表
const migOpen = new Set();
document.addEventListener("toggle", (e) => {
  const d = e.target;
  if (!d.matches?.("details.mig[data-mig-id]")) return;
  const id = Number(d.dataset.migId);
  if (d.open) migOpen.add(id); else migOpen.delete(id);
}, true);

let migTimer = null;
async function refreshMigrations() {
  clearTimeout(migTimer);
  try {
    state.migrations = await api("/api/me/migrations");
  } catch {
    return;
  }
  const active = state.migrations.filter((t) => ["queued", "running", "pending_approval"].includes(t.status));
  const running = state.migrations.find((t) => t.status === "running");
  const chip = $("#mig-chip");
  if (active.length) {
    const pct = running ? taskProgress(running) : null;
    chip.textContent = running ? `迁移中${pct != null ? ` ${Math.floor(pct)}%` : "…"}` : `${active.length} 个迁移等待中`;
    chip.classList.remove("hidden");
  } else {
    chip.classList.add("hidden");
  }
  const list = $("#my-migrations");
  list.innerHTML = state.migrations.length ? state.migrations.map((t) => taskHtml(t)).join("") : `<p class="muted small">还没有迁移记录。</p>`;
  const wiz = $("#onboard-migrations");
  if (wiz) wiz.innerHTML = state.migrations.map((t) => taskHtml(t)).join("");
  // 有进行中的任务时每 3 秒刷新一次
  if (active.length) migTimer = setTimeout(refreshMigrations, 3000);
}

document.addEventListener("click", async (e) => {
  const el = e.target.closest("[data-mig-cancel],[data-mig-retry],[data-mig-approve],[data-mig-reject]");
  if (!el) return;
  const d = el.dataset;
  try {
    if (d.migCancel) await api(`/api/migrations/${d.migCancel}`, { method: "DELETE" });
    if (d.migRetry) await api(`/api/migrations/${d.migRetry}/retry`, { method: "POST" });
    if (d.migApprove) await api(`/api/admin/migrations/${d.migApprove}/approve`, { method: "POST" });
    if (d.migReject) await api(`/api/admin/migrations/${d.migReject}/reject`, { method: "POST" });
    if (d.migApprove || d.migReject) loadAdmin();
    refreshMigrations();
  } catch (err) {
    toast(err.message);
  }
});

$("#mig-chip").onclick = () => {
  switchTab("account");
  setTimeout(() => $("#migration-card")?.scrollIntoView({ behavior: "smooth" }), 200);
};

/** 迁移选择器：用于「迁移已有文件」弹窗和首次引导。
 *  上半部分：从旧共享账号（如 ubuntu）迁移配置、软件、conda（环境是移动，其余是复制）；下半部分：迁移自己的文件 / 目录。 */
const CFG_GROUPS = {
  config: { title: "配置文件", hint: "", checked: true },
  software: { title: "用户级软件", hint: "", checked: false },
  credential: { title: "共享账号的凭据", hint: "", checked: false },
};
// 在界面上从上到下的顺序（Conda 在最前面，单独处理）
const CFG_ORDER = ["software", "config", "credential"];
// 选项名称里原来带的相对路径去掉，改为按实际找到的文件显示绝对路径
const cfgName = (c) => (c.key === "condarc" ? "Conda / Pip 配置" : c.label.replace(/[（(][^）)]*[）)]/, "").trim());
const cfgPaths = (lg, c) => (c.paths || []).map((p) => `${lg.home.replace(/\/$/, "")}/${p}`);
// 每个选项的说明，连同实际的绝对路径一起放进「?」的悬停提示里
const CFG_DESC = {
  shell: "终端启动脚本：别名、环境变量、PATH 等。",
  git: "Git 的用户名、邮箱和全局设置。",
  vim: "Vim 的配置和插件。",
  tmux: "tmux / screen 的配置。",
  condarc: "Conda、pip 的软件源和包管理配置。",
  ssh_config: "SSH 客户端配置和已信任的主机记录（不含私钥）。",
  jupyter: "Jupyter / IPython 的配置。",
  local_bin: "放在用户目录里的命令行工具。",
  local_py: "用 pip install --user 安装的 Python 包。",
  ssh_keys: "共享账号的 SSH 密钥（如 GitHub 密钥）。大家共用，建议以后换成自己的。",
  tokens: "共享账号保存的 Git 凭据、.netrc、Hugging Face token。大家共用，建议以后换成自己的。",
};
const helpIcon = (desc, paths) => `<span class="help" title="${esc([desc, ...(paths.length ? ["", ...paths] : [])].join("\n"))}">?</span>`;
const MODE_NAMES = { move: "移动", copy: "复制", link: "原地保留" };

async function renderPicker(container, onSubmitted, onClose) {
  // 读取失败 / 没有可选服务器时也要能关掉弹窗
  const bail = (html) => {
    container.innerHTML = html + (onClose ? `<div class="actions"><button type="button" class="btn pk-close">关闭</button></div>` : "");
    if (onClose) $(".pk-close", container).onclick = onClose;
  };
  container.innerHTML = `<p class="muted">正在读取服务器上的内容…</p>`;
  let servers;
  try {
    servers = await api("/api/me/servers");
  } catch (err) {
    bail(`<p class="error">${esc(err.message)}</p>`);
    return;
  }
  if (!state.me.linux_username) {
    bail(`<p class="muted">请先设置 Linux 用户名。</p>`);
    return;
  }
  const usable = servers.filter((s) => s.online);
  if (!usable.length) {
    bail(`<p class="muted">目前没有在线的服务器。</p>`);
    return;
  }
  container.innerHTML = `
    <label>服务器<select class="pk-server">${usable.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join("")}</select></label>
    <div class="pk-body"></div>
    <p class="pk-total small muted"></p>
    <p class="pk-warn small warn-text"></p>
    <p class="error pk-error"></p>
    <div class="actions">${onClose ? `<button type="button" class="btn pk-close">关闭</button>` : ""}<button class="btn primary pk-submit">开始迁移</button></div>`;
  if (onClose) $(".pk-close", container).onclick = onClose;
  const sel = $(".pk-server", container);
  const cur = () => usable.find((x) => x.id === Number(sel.value));

  const render = () => {
    const s = cur();
    let html = "";
    for (const [li, lg] of s.legacy.entries()) {
      const groups = {};
      for (const c of lg.configs) (groups[c.group] ||= []).push(c);
      html += `<div class="pk-section" data-legacy="${li}">
        <h3>① 迁移环境与配置${s.legacy.length > 1 ? ` <span class="muted small">（共享账号 <code>${esc(lg.user)}</code>）</span>` : ""}</h3>`;
      const home = lg.home.replace(/\/$/, "");
      if (lg.conda.length) html += `<div class="label">Conda</div>`;
      for (const [ci, c] of lg.conda.entries()) {
        const isUser = c.name === ".conda";
        if (!isUser) {
          html += `<div class="cfg-grid"><label class="check"><input type="checkbox" data-conda="${ci}" data-base="1" checked>
              <span>Conda 本体${helpIcon("Conda 的程序本体，不含环境，复制后自动修正路径。", [`${home}/${c.name}`])} <span class="muted small">${fmtBytes(c.base_size)}</span></span></label></div>`;
        }
        if (c.envs.length) {
          html += `<div class="env-head"><span>Conda 环境${helpIcon("已创建的 conda 环境，体积较大。迁移时是「移动」：迁移并自检通过后，原环境会从共享账号里移走；路径问题会自动修正。推荐自己 conda create 新建，确实不方便重建的再展开勾选。", [`${home}/${c.name}/envs`])}</span>
            <button type="button" class="link-btn" data-expand>展开</button></div>
            <div class="cfg-grid hidden" style="margin-top:6px">${c.envs.map((e) => `<label class="check"><input type="checkbox" data-conda="${ci}" data-env="${esc(e.name)}">
              <span>${esc(e.name)} <span class="muted small">${fmtBytes(e.size)}</span></span></label>`).join("")}</div>`;
        }
      }
      if (!lg.conda.length) html += `<p class="muted small">共享账号家目录里没有找到 conda。</p>`;
      for (const g of CFG_ORDER) {
        const meta = CFG_GROUPS[g];
        if (!groups[g]) continue;
        html += `<div class="label">${meta.title}${meta.hint ? ` <span class="muted small">· ${meta.hint}</span>` : ""}</div><div class="cfg-grid">`;
        html += groups[g].map((c) => `<label class="check cfg ${g === "credential" ? "cred" : ""}"><input type="checkbox" data-cfg="${esc(c.key)}" ${meta.checked ? "checked" : ""}>
          <span>${esc(cfgName(c))}${helpIcon(CFG_DESC[c.key] || "", cfgPaths(lg, c))} <span class="muted small">${fmtBytes(c.size)}</span></span></label>`).join("");
        html += `</div>`;
      }
      html += `<div class="muted small pk-tips">提示：
        <div>1. apt 安装的软件（git、vim、tmux、gcc、CUDA 等）是全系统共享的，新账号直接可用，不需要迁移。</div>
        <div>2. Conda 环境迁移是直接「移动」，其它配置项迁移时是「复制」。</div></div></div>`;
    }
    html += `<div class="pk-section"><h3>② 迁移我的文件和目录</h3>
      <p class="muted small">选择需要迁移到 <code>/home/${esc(state.me.linux_username)}</code> 的文件或目录</p>
      <div class="cand-list pk-list pk-picked"></div>
      <div style="margin-top:8px"><button type="button" class="btn small" data-add-file>添加文件或目录</button></div></div>`;
    $(".pk-body", container).innerHTML = html;
    renderPicked();
  };

  // 已添加的文件 / 目录（初始为空，通过「添加文件或目录」从服务器上选择）
  let picked = [];
  const renderPicked = () => {
    const box = $(".pk-picked", container);
    box.classList.toggle("hidden", !picked.length);
    box.innerHTML = picked.map((p, i) => {
      const modes = p.in_home ? ["move", "copy"] : ["move", "copy", "link"];
      return `<div class="cand picked">
        <span class="path">${esc(p.path)}${p.is_dir ? "/" : ""}</span>
        <span class="cand-ctl">
          <select data-pmode="${i}">${modes.map((m) => `<option value="${m}" ${p.mode === m ? "selected" : ""}>${MODE_NAMES[m]}</option>`).join("")}</select>
          <input class="dest" data-pdest="${i}" value="${esc(p.dest)}" title="放到家目录下的名字">
        </span>
        <button type="button" class="link-btn" data-pdel="${i}">移除</button>
        <span class="meta">${p.size === undefined ? "大小统计中…" : p.size == null ? "大小未知" : fmtBytes(p.size)} · 修改于 ${esc(p.mtime)} · 属主 ${esc(p.owner)}${p.same_fs ? "" : " · 与家目录不在同一磁盘"}</span>
      </div>`;
    }).join("");
    update();
  };
  const addPicked = async (s, e) => {
    if (picked.some((p) => p.path === e.path)) return;
    const p = { ...e, mode: "move", dest: e.name, size: e.is_dir ? undefined : e.size };
    picked.push(p);
    renderPicked();
    if (e.is_dir) {  // 目录大小要在服务器上统计，慢一点，先显示“统计中”
      try { p.size = (await api(`/api/servers/${s.id}/browse`, { method: "POST", body: { op: "size", path: e.path } })).size; } catch { p.size = null; }
      if (picked.includes(p)) renderPicked();
    }
  };

  const collect = () => {
    const s = cur();
    const profiles = s.legacy.map((lg, li) => {
      const sec = $(`[data-legacy="${li}"]`, container);
      const configs = $$("[data-cfg]:checked", sec).map((c) => c.dataset.cfg);
      const conda = lg.conda.map((c, ci) => ({
        name: c.name,
        base: !!$(`[data-conda="${ci}"][data-base]:checked`, sec),
        envs: $$(`[data-conda="${ci}"][data-env]:checked`, sec).map((x) => x.dataset.env),
      })).filter((c) => c.base || c.envs.length);
      let bytes = lg.configs.filter((c) => configs.includes(c.key)).reduce((a, c) => a + (c.size || 0), 0);
      conda.forEach((c) => {
        const info = lg.conda.find((x) => x.name === c.name);
        bytes += (info.base_size || 0) + info.envs.filter((e) => c.envs.includes(e.name)).reduce((a, e) => a + (e.size || 0), 0);
      });
      return { legacy_user: lg.user, configs, conda, bytes };
    }).filter((p) => p.configs.length || p.conda.length);
    const dirs = picked.map((p) => ({ cand: { path: p.path, size: p.size, same_fs: p.same_fs }, mode: p.mode, dest: p.dest.trim() }));
    return { s, profiles, dirs };
  };

  const update = () => {
    const { s, profiles, dirs } = collect();
    // 需要占用家目录空间的：复制的内容 + 跨盘移动的内容
    const copyBytes = profiles.reduce((a, p) => a + p.bytes, 0)
      + dirs.filter((d) => d.mode === "copy" || (d.mode === "move" && !d.cand.same_fs)).reduce((a, d) => a + (d.cand.size || 0), 0);
    const n = profiles.length + dirs.length;
    $(".pk-total", container).textContent = n
      ? `将复制约 ${fmtBytes(copyBytes)} 到家目录${s.home_free != null ? `（家目录所在磁盘剩余 ${fmtBytes(s.home_free)}）` : ""}。同盘移动不占额外空间。`
      : "";
    let warn = "";
    if (s.home_free != null && copyBytes > s.home_free * 0.9) warn = "家目录空间不够：请少选几个 conda 环境，或对大目录改用「移动」/「原地保留」。";
    $(".pk-warn", container).textContent = warn;
    $(".pk-submit", container).textContent = n ? `开始迁移（${n} 项任务）` : "开始迁移";
  };

  sel.onchange = () => { picked = []; render(); };
  container.onchange = (e) => {
    const t = e.target;
    if (t.dataset.pmode !== undefined) picked[Number(t.dataset.pmode)].mode = t.value;
    update();
  };
  container.oninput = (e) => {
    if (e.target.dataset.pdest !== undefined) picked[Number(e.target.dataset.pdest)].dest = e.target.value;
    if (e.target.matches(".dest")) update();
  };
  container.onclick = (e) => {
    const t = e.target;
    if (t.closest?.(".help")) return e.preventDefault();  // 点「?」不要勾选 / 取消勾选所在的选项
    if (t.dataset.expand !== undefined) {  // 展开 / 收起 conda 已有环境列表
      const list = t.parentElement.nextElementSibling;
      const hide = !list.classList.toggle("hidden");
      t.textContent = hide ? "收起" : "展开";
    } else if (t.dataset.pdel !== undefined) {
      picked.splice(Number(t.dataset.pdel), 1);
      renderPicked();
    } else if (t.dataset.addFile !== undefined) {
      const s = cur();
      openFsBrowser(s, picked.map((p) => p.path), (entry) => addPicked(s, entry));
    }
  };
  $(".pk-submit", container).onclick = async () => {
    const { s, profiles, dirs } = collect();
    const err = $(".pk-error", container);
    if (!profiles.length && !dirs.length) return (err.textContent = "请至少勾选一项");
    err.textContent = "";
    const errors = [];
    let ok = 0;
    for (const p of profiles) {
      try {
        await api("/api/migrations/profile", { method: "POST", body: { server_id: s.id, legacy_user: p.legacy_user, configs: p.configs, conda: p.conda } });
        ok++;
      } catch (e) {
        errors.push(`${p.legacy_user} 的环境与配置：${e.message}`);
      }
    }
    for (const d of dirs) {
      try {
        await api("/api/migrations", { method: "POST", body: { server_id: s.id, source: d.cand.path, mode: d.mode, dest_name: d.dest } });
        ok++;
      } catch (e) {
        errors.push(`${d.cand.path}：${e.message}`);
      }
    }
    if (errors.length) err.textContent = errors.join("\n");
    if (ok) {
      toast("迁移已提交，会在服务器后台进行");
      await refreshMigrations();
      onSubmitted?.();
    }
  };
  render();
}

/** 文件浏览器：列出 GPU 服务器上旧共享账号家目录 / 迁移目录里的内容，点「选择」选中一个文件或目录。 */
const fsDialog = $("#fs-dialog");
function openFsBrowser(server, taken, onPick) {
  let seq = 0;
  const load = async (path) => {
    const my = ++seq;
    $("#fs-list").innerHTML = `<div class="empty">正在读取 ${esc(server.name)} 上的目录…</div>`;
    renderCrumbs(path);
    try {
      const { entries } = await api(`/api/servers/${server.id}/browse`, { method: "POST", body: { op: "list", path } });
      if (my !== seq) return;
      draw(path, entries);
    } catch (err) {
      if (my !== seq) return;
      $("#fs-list").innerHTML = `<div class="empty error">${esc(err.message)}</div>`;
    }
  };
  const renderCrumbs = (path) => {
    const parts = [`<button type="button" class="link-btn" data-go="">${esc(server.name)}</button>`];
    if (path) {
      const base = (server.browse_roots || []).filter((r) => path === r || path.startsWith(`${r.replace(/\/$/, "")}/`)).sort((a, b) => b.length - a.length)[0] || "";
      let cur = base;
      if (base) parts.push(`<button type="button" class="link-btn" data-go="${esc(base)}">${esc(base)}</button>`);
      for (const seg of path.slice(base.length).split("/").filter(Boolean)) {
        cur = `${cur.replace(/\/$/, "")}/${seg}`;
        parts.push(`<button type="button" class="link-btn" data-go="${esc(cur)}">${esc(seg)}</button>`);
      }
    }
    $("#fs-path").innerHTML = parts.join(`<span class="sep">/</span>`);
  };
  const draw = (path, entries) => {
    if (!entries.length) {
      $("#fs-list").innerHTML = `<div class="empty">这里是空的</div>`;
      return;
    }
    const rows = entries.map((e, i) => {
      let act = "";
      if (e.top) act = "";
      else if (taken.includes(e.path)) act = `<span class="muted small">已添加</span>`;
      else if (e.claimed_by) act = `<span class="muted small">已被 ${esc(e.claimed_by)} 迁移</span>`;
      else if (!e.selectable) act = `<span class="muted small">不可选</span>`;
      else act = `<button type="button" class="link-btn" data-pick="${i}">选择</button>`;
      const icon = e.is_dir ? "📁" : "📄";
      return `<div class="fs-row"><span class="name ${e.is_dir ? "dir" : ""}" ${e.is_dir ? `data-go="${esc(e.path)}"` : ""}>${icon} ${esc(e.name)}</span>
        <span class="muted m">${esc(e.mtime)}</span><span class="muted m">${e.is_dir ? "" : fmtBytes(e.size)}</span><span class="muted m">${esc(e.owner)}</span><span class="act">${act}</span></div>`;
    }).join("");
    $("#fs-list").innerHTML = `<div class="fs-row head"><span>名称</span><span class="m">修改时间</span><span class="m">大小</span><span class="m">属主</span><span></span></div>${rows}`;
    $("#fs-list").onclick = (ev) => {
      const pick = ev.target.closest("[data-pick]");
      if (pick) { fsDialog.close(); onPick(entries[Number(pick.dataset.pick)]); return; }
      const go = ev.target.closest("[data-go]");
      if (go) load(go.dataset.go);
    };
  };
  $("#fs-path").onclick = (ev) => { const go = ev.target.closest("[data-go]"); if (go) load(go.dataset.go); };
  fsDialog.showModal();
  load("");
}
$("#fs-close").onclick = () => fsDialog.close();

$("#open-migrate").onclick = () => {
  $("#migrate-dialog").showModal();
  renderPicker($("#migrate-picker"), () => { $("#migrate-dialog").close(); }, () => $("#migrate-dialog").close());
};

// ---------------- 首次使用引导 ----------------

const onboard = { step: 0 };
const STEPS = ["设置用户名", "登录方式", "迁移旧文件", "完成"];

function openOnboarding(force = false) {
  const me = state.me;
  if (!force && me.onboarded) return;
  onboard.step = !me.linux_username ? 0 : !me.has_password && !me.key_count ? 1 : 2;
  renderOnboarding();
  $("#onboard-dialog").showModal();
}

function renderOnboarding() {
  const me = state.me;
  renderBanner();
  const step = onboard.step;
  $("#onboard-steps").innerHTML = STEPS.map((s, i) =>
    `<span class="${i === step ? "on" : i < step ? "ok" : ""}${i < step ? " clickable" : ""}" data-step="${i}">${i + 1}. ${s}</span>`).join("");
  // 点已完成的步骤可以回退（之前填的内容已保存）
  $("#onboard-steps").onclick = (e) => {
    const sp = e.target.closest("[data-step]");
    if (sp && Number(sp.dataset.step) < onboard.step) { onboard.step = Number(sp.dataset.step); renderOnboarding(); }
  };
  $("#onboard-error").textContent = "";
  const body = $("#onboard-body");
  const next = $("#onboard-next");
  const skip = $("#onboard-skip");
  next.classList.remove("hidden");
  skip.classList.toggle("hidden", step === 0 || step === 3);
  next.textContent = step === 3 ? "开始使用" : "下一步";
  if (step === 0) {
    body.innerHTML = `<h2>欢迎使用${esc(state.config.lab_name)} GPU 管理平台 👋</h2>
      <p>以后大家用<b>自己的账号</b>登录 GPU 服务器，不再共用 ubuntu 等公共账号。先给自己起一个 Linux 用户名（所有服务器通用，之后也可以修改）：</p>
      <label>Linux 用户名<input id="ob-username" placeholder="小写字母开头，如 zhangsan" value="${esc(me.linux_username || "")}" required></label>`;
  } else if (step === 1) {
    body.innerHTML = `<h2>设置登录方式</h2>
      <p class="muted small">密码和 SSH 公钥至少设置一种，两种都设也可以。</p>
      <form id="ob-pw" class="stack">
        <div class="label">登录密码 ${me.has_password ? `<span class="badge ok">已设置</span>` : ""}</div>
        <input type="password" name="password" placeholder="至少 8 位" autocomplete="new-password">
        <input type="password" name="confirm" placeholder="再输入一次" autocomplete="new-password">
        <div class="label">SSH 公钥（可选）${me.key_count ? `<span class="badge ok">已添加 ${me.key_count} 个</span>` : ""}</div>
        <textarea name="public_key" rows="2" placeholder="粘贴 ~/.ssh/id_ed25519.pub 的内容"></textarea>
      </form>`;
  } else if (step === 2) {
    body.innerHTML = `<h2>迁移旧环境和文件（可选）</h2>
      <p class="muted small">以前大家共用一个账号（如 ubuntu）。这里可以把共用账号里的 .bashrc、git 配置、需要的 conda 环境迁移到你的新账号，并把你自己的项目目录迁移过来。</p>
      <div id="ob-picker"></div><div id="onboard-migrations"></div>`;
    renderPicker($("#ob-picker"), () => {
      $("#ob-picker").innerHTML = `<p>✅ 迁移已开始，在服务器后台进行。你可以直接关闭这个窗口，进度随时可以在「我的账号 → 文件迁移」或顶部的「迁移中」标签查看。</p>`;
    });
  } else {
    const running = state.migrations.some((t) => ["queued", "running", "pending_approval"].includes(t.status));
    body.innerHTML = `<h2>设置完成 🎉</h2>
      <ul>
        <li>在「GPU 日历」上点空白格子即可预约显卡，预约当天起可以使用所选显卡${state.config.require_reservation_to_login ? "，也只有预约当天才能登录对应服务器" : "（没有预约也能登录服务器、迁移文件，只是看不到显卡）"}。</li>
        <li>登录命令：<code>ssh ${esc(me.linux_username)}@服务器地址</code>${me.has_password ? "，输入你刚设置的密码" : ""}。</li>
        ${running ? "<li>文件迁移正在后台进行，进度见顶部的「迁移中」标签。</li>" : ""}
      </ul>`;
  }
}

$("#onboard-next").onclick = async () => {
  const err = $("#onboard-error");
  try {
    if (onboard.step === 0) {
      const name = $("#ob-username").value.trim();
      if (name !== state.me.linux_username) {
        state.me = await api("/api/me/linux-username", { method: "PUT", body: { username: name } });
      }
    } else if (onboard.step === 1) {
      const f = $("#ob-pw");
      const keyText = f.public_key.value.trim();
      if (keyText) {
        await api("/api/me/keys", { method: "POST", body: { title: "", public_key: keyText } });
        f.public_key.value = "";
      }
      if (f.password.value || f.confirm.value) await savePassword(f);
      state.me = await api("/api/me");
      if (!state.me.has_password && !state.me.key_count) throw new Error("请设置密码或添加公钥");
    } else if (onboard.step === 3) {
      state.me = await api("/api/me/onboarded", { method: "POST" });
      $("#onboard-dialog").close();
      renderBanner();
      if (activeTab() === "account") renderAccount();
      return;
    }
    onboard.step++;
    renderOnboarding();
  } catch (e) {
    err.textContent = e.message;
  }
};
$("#onboard-skip").onclick = () => {
  onboard.step++;
  renderOnboarding();
};
const onboardLater = () => { try { sessionStorage.setItem("lab-onboard-later", "1"); } catch {} };
$("#onboard-later").onclick = () => {
  $("#onboard-dialog").close();
  onboardLater();
};
// 按 Esc 关掉引导和点「以后再说」一样：本次打开页面期间不再弹
$("#onboard-dialog").addEventListener("cancel", onboardLater);

// 「引导已完成」原来只在走完最后一步、点「开始使用」时才记录。中途关掉窗口（比如迁移已经开始，
// 按提示直接关闭）就一直是未完成，每次刷新都会再弹。所以：用户名、登录方式都设好了，并且已经
// 发起过文件迁移，就视为已完成，直接记下来。
async function autoFinishOnboarding() {
  const me = state.me;
  if (!me.linux_username || (!me.has_password && !me.key_count)) return false;
  try {
    if (!(await api("/api/me/migrations")).length) return false;
    state.me = await api("/api/me/onboarded", { method: "POST" });
    return true;
  } catch {
    return false;
  }
}

// ---------------- 管理 ----------------

// 管理页里会随时间变化的几张表（服务器、成员、迁移记录）；设置类表单只在打开页面时填一次，
// 自动刷新不碰它们，免得覆盖管理员正在输入的内容
async function fetchAdminTables() {
  const [servers, users, migrations, tunnel] = await Promise.all([
    api("/api/admin/servers"), api("/api/admin/users"), api("/api/admin/migrations"),
    api("/api/admin/tunnel").catch(() => ({ enabled: false })),
  ]);
  return { servers, users, migrations, tunnel };
}
function renderAdminTables({ servers, users, migrations, tunnel }) {
  state.users = users;
  state.tunnelEnabled = tunnel.enabled;
  state.adminMigrations = migrations;
  renderAdminKpis(servers, users, migrations);
  renderAdminServers(servers, tunnel);
  renderAdminUsers(users);
  renderAdminMigrations(migrations);
}

// 顶部 4 个数字卡 + 子标签上的数量
function renderAdminKpis(servers, users, migrations) {
  const online = servers.filter((s) => s.online).length;
  const gpus = servers.flatMap((s) => s.gpus);
  const models = [...new Set(gpus.map((g) => g.name.replace(/^NVIDIA (GeForce )?/, "")))].join(" / ");
  const admins = users.filter((u) => u.is_admin).length;
  const active = migrations.filter((t) => migGroup(t) === "active").length;
  const kpi = (cls, ic, val, label) => `<div class="card kpi"><span class="icon-box ${cls}">${icon(ic)}</span><div><b>${val}</b><span class="l">${label}</span></div></div>`;
  $("#admin-kpis").innerHTML = [
    kpi(servers.length && online === servers.length ? "ok" : "warn", "server", `${online}<small> / ${servers.length}</small>`, "GPU 服务器在线"),
    kpi("", "cpu", gpus.length, `显卡总数${models ? ` · ${esc(models)}` : ""}`),
    kpi("", "users", users.length, `成员 · ${admins} 位管理员`),
    kpi(active ? "warn" : "", "swap", `${active}<small> 进行中</small>`, `文件迁移 · 共 ${migrations.length} 条`),
  ].join("");
  $("#cnt-servers").textContent = servers.length;
  $("#cnt-members").textContent = users.length;
  $("#cnt-migrations").textContent = migrations.length;
}

// 管理页的子标签（记住上次看的是哪一个）
function switchAdminSub(name) {
  $$("#admin-subtabs button").forEach((b) => b.classList.toggle("active", b.dataset.sub === name));
  $$(".admin-sub").forEach((x) => x.classList.toggle("hidden", x.id !== `sub-${name}`));
  try { localStorage.setItem("lab-admin-sub", name); } catch {}
}
$$("#admin-subtabs button").forEach((b) => (b.onclick = () => switchAdminSub(b.dataset.sub)));
try { const n = localStorage.getItem("lab-admin-sub"); if (n && $(`#sub-${n}`)) switchAdminSub(n); } catch {}

// 「⋯」菜单：点别处自动收起
document.addEventListener("click", (e) => {
  const open = e.target.closest("[data-menu]")?.nextElementSibling;
  $$(".menu").forEach((m) => { if (m !== open) m.classList.add("hidden"); });
  if (open) open.classList.toggle("hidden");
});

let adminSig = "";
async function refreshAdmin() {
  try {
    const t = await fetchAdminTables();
    const sig = JSON.stringify(t);
    if (sig === adminSig) return;  // 没有变化就不重画
    adminSig = sig;
    renderAdminTables(t);
  } catch { /* 自动刷新失败时保持现状，下次再试 */ }
}

let rulesDirty = false, welcomeDirty = false;
function setRulesDirty(d) {
  rulesDirty = d;
  $("#rules-bar").classList.toggle("clean", !d);
  $("#rules-save").disabled = !d;
  $("#rules-msg").textContent = d ? "有未保存的更改" : "所有更改已保存";
}
$("#rules-form").addEventListener("input", () => setRulesDirty(true));

// 新人欢迎卡片的实时预览（只是示意：支持 **加粗** 和以 - 开头的列表）
function renderWelcomePreview() {
  const f = $("#welcome-form");
  $("#wp-title").textContent = f.welcome_title.value;
  let html = "", list = [];
  const inline = (t) => esc(t).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
  const flush = () => { if (list.length) { html += `<ul>${list.map((i) => `<li>${inline(i)}</li>`).join("")}</ul>`; list = []; } };
  for (const line of f.welcome_markdown.value.split("\n")) {
    if (/^\s*-\s+/.test(line)) list.push(line.replace(/^\s*-\s+/, ""));
    else { flush(); if (line.trim()) html += `<p>${inline(line)}</p>`; }
  }
  flush();
  $("#wp-body").innerHTML = html;
  $("#wp-btns").innerHTML = f.welcome_links.value.split("\n").filter((l) => l.includes("|")).map((l) => `<span>${esc(l.split("|")[0].trim())}</span>`).join("");
}
$("#welcome-form").addEventListener("input", () => { welcomeDirty = true; renderWelcomePreview(); });

async function loadAdmin() {
  try {
    const [tables, settings] = await Promise.all([fetchAdminTables(), api("/api/admin/settings")]);
    adminSig = JSON.stringify(tables);
    renderAdminTables(tables);
    if (!welcomeDirty) {
      const f = $("#welcome-form");
      for (const k of ["welcome_title", "welcome_markdown", "welcome_links", "welcome_chat_ids"]) f[k].value = settings[k];
      renderWelcomePreview();
    }
    if (!rulesDirty) {
      const r = $("#rules-form");
      for (const k of ["lab_name", "max_reservation_days", "max_advance_days", "max_gpu_sharers", "reminder_hour"]) r[k].value = settings[k];
      r.auto_renew_before_hours.value = settings.auto_renew_before_hours;
      r.auto_renew_days.value = settings.auto_renew_days;
      r.migration_requires_approval.checked = settings.migration_requires_approval === "1";
      r.require_reservation_to_login.checked = settings.require_reservation_to_login === "1";
      r.restrict_to_admin_tenant.checked = settings.restrict_to_admin_tenant === "1";
      r.refresh_interval_seconds.value = settings.refresh_interval_seconds;
      setRulesDirty(false);
    }
  } catch (err) {
    toast(err.message);
  }
}

function renderAdminServers(servers, tunnel) {
  $("#admin-vpn").innerHTML = webvpnLine(tunnel);
  $("#admin-servers").innerHTML = servers.length
    ? `<div class="tbl-row head srv-grid"><span>服务器</span><span>状态</span><span>显卡</span><span>公共用户名<span class="help" title="迁移前大家共用的 Linux 账号（如 ubuntu）。成员可以把这个账号里的配置、conda 环境和项目文件，迁移到自己的个人账号下。">?</span></span><span>校园网隧道</span><span></span></div>
      ${servers.map((s) => {
        const models = [...new Set(s.gpus.map((g) => g.name.replace(/^NVIDIA (GeForce )?/, "")))].join(", ");
        return `<div class="tbl-row srv-grid">
          <div><div class="cell-main">${esc(s.name)}</div><div class="cell-sub mono">${esc(s.host)}</div></div>
          <div title="${esc(`主机名 ${s.hostname || "-"}\nAgent 版本 ${s.agent_version || "-"}`)}"><div class="srv-status"><span class="dot ${s.online ? "on" : ""}"></span>${s.online ? "在线" : "离线"}</div>
            <div class="cell-sub">${s.last_seen_at ? s.last_seen_at.replace("T", " ") : "从未连接"}</div></div>
          <div>${s.gpus.length ? `<span class="gpu-chip">${s.gpus.length} × ${esc(models)}</span>` : `<span class="muted">-</span>`}</div>
          <div>${s.legacy_users?.length ? s.legacy_users.map((u) => `<code class="inline-code">${esc(u)}</code>`).join(" ") : `<span class="muted">-</span>`}</div>
          <div>${tunnelCell(s.tunnel)}</div>
          <div class="row-actions">${registerCell(s)}
            <button class="btn small" data-edit-server="${s.id}">编辑</button>
            <div class="menu-wrap"><button class="btn ghost icon small" data-menu title="更多">${icon("more")}</button>
              <div class="menu hidden"><button data-token-server="${s.id}">${icon("refresh")}重置 Token</button><button class="danger" data-del-server="${s.id}">${icon("trash")}删除服务器</button></div></div>
          </div></div>`;
      }).join("")}
      <div class="card-foot">「注册命令」只有管理员能看到，用来把 GPU 服务器接入平台。接入过的服务器重装 / 换 token 时，在「⋯」里点「重置 Token」会生成新命令。</div>`
    : `<div class="card-b"><p class="muted">还没有服务器。添加后这里会给出一条注册命令，照着执行即可把 GPU 接入平台。</p></div>`;
  $$("[data-install-server]").forEach((b) => (b.onclick = () => showInstallDialog(servers.find((x) => x.id === Number(b.dataset.installServer)))));
  $$("[data-edit-server]").forEach((b) => (b.onclick = async () => {
    openServerDialog(servers.find((x) => x.id === Number(b.dataset.editServer)));
  }));
  $$("[data-token-server]").forEach((b) => (b.onclick = async () => {
    if (!confirm("重置后旧 Token 立即失效。重置完会在这一行的「注册命令」里给出新命令，重新执行一次即可（会自动覆盖安装，账号和数据不受影响）。继续？")) return;
    try {
      await api(`/api/admin/servers/${b.dataset.tokenServer}/token`, { method: "POST" });
      toast("已重置，请在该行「注册命令」里查看新命令");
      loadAdmin();
    } catch (err) {
      toast(err.message);
    }
  }));
  $$("[data-del-server]").forEach((b) => (b.onclick = async () => {
    if (!confirm("删除服务器会同时删除它的所有预约记录。服务器上已创建的账号不会被删除。继续？")) return;
    try { await api(`/api/admin/servers/${b.dataset.delServer}`, { method: "DELETE" }); } catch (err) { toast(err.message); }
    loadAdmin();
  }));
}

const TUNNEL_STATE = { connected: ["ok", "已连接"], connecting: ["pending", "连接中"], disconnected: ["error", "已断开"], error: ["error", "出错"] };
// 公网服务器登录学校 WebVPN 的状态（隧道的前提）
const WEBVPN_STATE = {
  logged_in: ["ok", "已登录"], logging_in: ["pending", "登录中…"], login_failed: ["error", "登录失败，自动重试中"],
  banned: ["error", "被 WebVPN 临时封禁，稍后自动重试"], throttled: ["pending", "达到登录限速，等待中"],
  bad_credentials: ["error", "账号或密码错误，请更新 .env 里的 WEBVPN_USER / WEBVPN_PASSWORD"],
  misconfigured: ["error", "配置不完整"],
};
function webvpnLine(tunnel) {
  const w = tunnel?.enabled && tunnel.status?._webvpn;
  if (!w) return "";
  const [cls, text] = WEBVPN_STATE[w.state] || ["", w.state];
  return `<div class="card vpn"><span class="icon-box ${cls === "ok" ? "ok" : "warn"}">${icon("globe")}</span>
    <div class="grow"><b>校园网通道</b> <span class="path">公网服务器 → 学校 WebVPN</span>${w.error ? `<div class="cell-sub">${esc(w.error)}</div>` : ""}</div>
    <span class="badge ${cls} has-dot">${esc(text)}</span></div>`;
}
function tunnelCell(t) {
  if (!t) return `<span class="muted">-</span>`;
  const [cls, text] = TUNNEL_STATE[t.state] || ["", t.state];
  return `<span class="badge ${cls} has-dot" title="${esc(t.error || "")}">${text}</span><div class="cell-sub">${esc(t.since?.replace("T", " ").slice(5, 16) || "")}</div>`;
}

// 把这台 GPU 接入平台的注册命令：在新服务器上直接执行即可，不需要提前下载任何文件。
// 命令里的 sudo -E 会带上当前 SSH 会话的代理环境变量（GPU 要靠它访问平台下载 Agent）；
// 隧道参数、旧共享账号（默认取执行 sudo 的账号）都由脚本自己取回 / 推断。
function installInfo(token) {
  const base = location.origin;
  return {
    cmd: `curl -fsSL ${base}/agent/install.sh | sudo -E bash -s -- --platform-url ${base} --token ${token}`,
    notes: [
      "用以前大家共用的账号 SSH 登录到这台新 GPU 服务器，直接执行上面这条命令即可，不需要提前下载任何文件。",
      "这个 SSH 会话要能访问公网（已连上你的网络代理），命令里的 sudo -E 会把代理环境变量带给安装脚本。",
      "旧共享账号默认取执行命令的账号；要指定其他账号，在命令末尾追加 --legacy-user 账号名。",
      "安装会自动下载 Agent 和 chisel，随后等待公网服务器经校园网 WebVPN 建立隧道（最多 3 分钟）。重复执行会覆盖安装，账号和数据不受影响。",
    ],
  };
}

function registerCell(s) {
  if (!s.token) return `<span class="cell-sub">先重置 Token</span>`;
  return `<button class="btn small" data-install-server="${s.id}">${icon("term")}注册命令</button>`;
}

function showInstallDialog(s) {
  const { cmd, notes } = installInfo(s.token);
  $("#install-title").textContent = `注册命令 · ${s.name}`;
  $("#install-cmd").textContent = cmd;
  $("#install-notes").innerHTML = notes.map((n) => `<li>${esc(n)}</li>`).join("");
  const btn = $("#install-copy");
  btn.textContent = "复制命令";
  // 弹窗盖住了页面上的提示条，反馈直接显示在按钮上
  btn.onclick = async () => {
    btn.textContent = (await copyText(cmd, true)) ? "已复制 ✓" : "复制失败，请手动选中命令";
    setTimeout(() => (btn.textContent = "复制命令"), 2000);
  };
  $("#install-dialog").showModal();
}
$("#install-close").onclick = () => $("#install-dialog").close();

// 添加 / 编辑服务器（同一个弹窗）
const serverDialog = $("#server-dialog");
const serverForm = $("#server-form");
let editingServer = null;
function openServerDialog(s) {
  editingServer = s || null;
  serverForm.reset();
  $("#server-error").textContent = "";
  $("#server-dialog-title").textContent = s ? "编辑服务器" : "添加服务器";
  if (s) { serverForm.name.value = s.name; serverForm.host.value = s.host; }
  serverDialog.showModal();
}
$("#add-server").onclick = () => openServerDialog(null);
serverForm.addEventListener("submit", async (e) => {
  if (e.submitter?.value === "cancel") return;
  e.preventDefault();
  const body = { name: serverForm.name.value.trim(), host: serverForm.host.value.trim() };
  $("#server-submit").disabled = true;
  try {
    const r = editingServer
      ? await api(`/api/admin/servers/${editingServer.id}`, { method: "PATCH", body })
      : await api("/api/admin/servers", { method: "POST", body });
    serverDialog.close();
    await loadAdmin();
    if (!editingServer) {  // 新添加的服务器：直接弹出它的注册命令
      toast("已添加，请在新服务器上执行注册命令");
      showInstallDialog(r.server);
    } else {
      toast("已保存");
    }
  } catch (err) {
    $("#server-error").textContent = err.message;
  } finally {
    $("#server-submit").disabled = false;
  }
});

function renderAdminUsers(users) {
  $("#admin-users").innerHTML = `<div class="tbl-row head mem-grid"><span>姓名</span><span>Linux 用户名</span><span class="c-center">UID</span><span class="c-center">密码</span><span class="c-center">公钥</span><span class="c-center">管理员</span><span class="c-center">启用</span></div>
    ${users.map((u) => `<div class="tbl-row mem-grid" data-member="${esc(`${u.name} ${u.linux_username || ""}`.toLowerCase())}">
      <div class="who">${avatarHtml(u.name, u.avatar_url, u.id)}${esc(u.name)}</div>
      <div>${u.linux_username ? `<code class="inline-code">${esc(u.linux_username)}</code>` : `<span class="badge">未设置</span>`}
        <button class="btn ghost icon small" data-rename="${u.id}" title="修改用户名">${icon("edit")}</button></div>
      <span class="c-center mono muted">${u.uid}</span>
      <span class="c-center">${u.has_password ? `<span style="color:var(--ok)">${icon("check")}</span>` : `<span class="muted">-</span>`}</span>
      <span class="c-center">${u.key_count ? u.key_count : `<span class="muted">未配置</span>`}</span>
      <span class="c-center"><label class="switch"><input type="checkbox" data-admin="${u.id}" ${u.is_admin ? "checked" : ""}><i></i></label></span>
      <span class="c-center"><label class="switch"><input type="checkbox" data-enabled="${u.id}" ${u.enabled ? "checked" : ""}><i></i></label></span></div>`).join("")}
    <div class="card-foot">停用成员后，各服务器上的账号会在 1 分钟内被锁定（家目录和数据保留）。修改用户名会在各服务器上把账号改名并移动家目录。</div>`;
  applyMemberFilter();
  const patch = async (id, body) => {
    try { await api(`/api/admin/users/${id}`, { method: "PATCH", body }); toast("已更新"); } catch (err) { toast(err.message); }
    loadAdmin();
  };
  $$("[data-admin]").forEach((c) => (c.onchange = () => patch(c.dataset.admin, { is_admin: c.checked })));
  $$("[data-enabled]").forEach((c) => (c.onchange = () => patch(c.dataset.enabled, { enabled: c.checked })));
  $$("[data-rename]").forEach((b) => (b.onclick = () => {
    const u = users.find((x) => x.id === Number(b.dataset.rename));
    const name = prompt("新的 Linux 用户名", u.linux_username || "");
    if (name && name.trim()) patch(u.id, { linux_username: name.trim() });
  }));
}

function applyMemberFilter() {
  const q = $("#member-search").value.trim().toLowerCase();
  $$("[data-member]").forEach((r) => r.classList.toggle("hidden", !r.dataset.member.includes(q)));
}
$("#member-search").addEventListener("input", applyMemberFilter);

let migFilter = "all";
function renderAdminMigrations(migrations) {
  const n = (g) => migrations.filter((t) => migGroup(t) === g).length;
  const counts = { all: migrations.length, active: n("active"), done: n("done"), other: n("other") };
  $("#mig-filter").innerHTML = [["all", "全部"], ["active", "进行中"], ["done", "已完成"], ["other", "失败 / 取消"]]
    .map(([k, l]) => `<button data-f="${k}" class="${migFilter === k ? "active" : ""}">${l} ${counts[k]}</button>`).join("");
  const list = migrations.filter((t) => migFilter === "all" || migGroup(t) === migFilter);
  $("#admin-migrations").innerHTML = list.length
    ? list.map((t) => taskHtml(t, true)).join("")
    : `<p class="muted">${migrations.length ? "没有符合条件的记录。" : "暂无迁移记录。"}</p>`;
}
$("#mig-filter").addEventListener("click", (e) => {
  const b = e.target.closest("[data-f]");
  if (!b) return;
  migFilter = b.dataset.f;
  renderAdminMigrations(state.adminMigrations || []);
});

$("#rules-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  try {
    await api("/api/admin/settings", {
      method: "PUT",
      body: {
        lab_name: f.lab_name.value,
        max_reservation_days: f.max_reservation_days.value,
        max_advance_days: f.max_advance_days.value,
        max_gpu_sharers: f.max_gpu_sharers.value,
        reminder_hour: f.reminder_hour.value,
        auto_renew_before_hours: f.auto_renew_before_hours.value,
        auto_renew_days: f.auto_renew_days.value,
        migration_requires_approval: f.migration_requires_approval.checked ? "1" : "0",
        require_reservation_to_login: f.require_reservation_to_login.checked ? "1" : "0",
        restrict_to_admin_tenant: f.restrict_to_admin_tenant.checked ? "1" : "0",
        refresh_interval_seconds: f.refresh_interval_seconds.value,
      },
    });
    state.config = await api("/api/config");
    applyLabName();
    setRulesDirty(false);
    toast("已保存");
  } catch (err) {
    toast(err.message);
  }
});

$("#welcome-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  try {
    await api("/api/admin/settings", { method: "PUT", body: { welcome_title: f.welcome_title.value, welcome_markdown: f.welcome_markdown.value, welcome_links: f.welcome_links.value, welcome_chat_ids: f.welcome_chat_ids.value } });
    welcomeDirty = false;
    toast("已保存");
  } catch (err) {
    toast(err.message);
  }
});
$("#welcome-test").onclick = async () => {
  try { await api("/api/admin/welcome/test", { method: "POST" }); toast("已发送，请查看飞书"); } catch (err) { toast(err.message); }
};

// ---------------- 启动 ----------------

// 平台名称在管理页可改：同步到标题、顶栏和登录页
function applyLabName() {
  document.title = `${state.config.lab_name} GPU 管理平台`;
  $("#brand-name").textContent = $("#login-title").textContent = state.config.lab_name;
}

async function boot() {
  state.config = await api("/api/config");
  applyLabName();
  { const [y, m] = state.config.today.split("-").map(Number); state.month = { y, m }; }
  try {
    state.me = await api("/api/me", { allow401: true });
  } catch {
    state.me = null;
  }
  if (!state.me) {
    $("#dev-login").classList.toggle("hidden", !state.config.dev_login);
    $("#login-feishu").classList.toggle("hidden", !state.config.feishu_enabled);
    if (state.config.feishu_enabled) initQrLogin();
    $("#login-unavailable").classList.toggle("hidden", state.config.feishu_enabled || state.config.dev_login);
    return showLogin();
  }
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(location.hostname);
  $("#http-banner").classList.toggle("hidden", location.protocol === "https:" || local);
  $("#me-name").textContent = state.me.name;
  $("#me-avatar-box").innerHTML = avatarHtml(state.me.name, state.me.avatar_url, state.me.id);
  $$(".admin-only").forEach((el) => el.classList.toggle("hidden", !state.me.is_admin));
  renderBanner();
  let tab = "calendar";
  try { tab = localStorage.getItem("lab-tab") || tab; } catch {}
  if (tab === "admin" && !state.me.is_admin) tab = "calendar";
  await loadCalendar();
  if (tab !== "calendar") switchTab(tab);
  else refreshMigrations();
  let later = false;
  try { later = sessionStorage.getItem("lab-onboard-later") === "1"; } catch {}
  if (!state.me.onboarded && !later && !(await autoFinishOnboarding())) openOnboarding();
  scheduleRefresh();
}

// “GPU 日历”和“管理”两页按管理员设置的间隔自动刷新数据（不是刷新网页）。
// 页面在后台、有弹窗打开、正在看悬浮提示、折叠动画进行中时跳过这一轮。
state.lastToggle = 0;
function scheduleRefresh() {
  const secs = Math.max(3, Number(state.config?.refresh_interval) || 10);
  setTimeout(async () => {
    try {
      if (!document.hidden && !$("dialog[open]") && !tipAnchor && !$(".menu:not(.hidden)") && Date.now() - state.lastToggle > 600) {
        const tab = activeTab();
        if (tab === "calendar") await loadCalendar(true);
        else if (tab === "admin") await refreshAdmin();
      }
    } finally {
      scheduleRefresh();
    }
  }, secs * 1000);
}

boot();
