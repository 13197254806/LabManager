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
let tipAnchor = null, tipShowTimer, tipHideTimer;
function tipHideNow() { clearTimeout(tipShowTimer); clearTimeout(tipHideTimer); tipAnchor = null; popHide(tipEl); }
function tipHideLater(ms = 250) { clearTimeout(tipShowTimer); clearTimeout(tipHideTimer); tipHideTimer = setTimeout(tipHideNow, ms); }
function tipShow(anchor) {
  const text = anchor.dataset.tip;
  if (!text) return;
  tipAnchor = anchor;
  tipEl.textContent = text;
  popShow(tipEl);
  const r = anchor.getBoundingClientRect(), t = tipEl.getBoundingClientRect();
  const left = Math.min(Math.max(8, r.left + r.width / 2 - t.width / 2), window.innerWidth - t.width - 8);
  // 默认显示在元素下方，放不下就放上方；中间留 0 间隙，鼠标能直接移上去
  const top = r.bottom + t.height + 8 <= window.innerHeight ? r.bottom : Math.max(8, r.top - t.height);
  tipEl.style.left = `${left}px`;
  tipEl.style.top = `${top}px`;
}
document.addEventListener("mouseover", (e) => {
  if (tipEl.contains(e.target)) return;
  const a = e.target.closest?.("[title], [data-tip]");
  if (!a) return tipAnchor && tipHideLater();
  if (a.hasAttribute("title")) {  // 把 title 摘下来，避免浏览器再弹自带的提示
    if (a.title) a.dataset.tip = a.title;
    a.removeAttribute("title");
  }
  if (!a.dataset.tip) return;
  if (a === tipAnchor) return clearTimeout(tipHideTimer);
  clearTimeout(tipHideTimer); clearTimeout(tipShowTimer);
  tipShowTimer = setTimeout(() => tipShow(a), tipAnchor ? 0 : 300);
});
document.addEventListener("mouseout", (e) => {
  if (e.relatedTarget && (tipEl.contains(e.relatedTarget) || e.target.closest?.("[data-tip]")?.contains(e.relatedTarget))) return;
  if (e.target.closest?.("[data-tip]") || tipEl.contains(e.target)) tipHideLater();
});
tipEl.addEventListener("mouseenter", () => clearTimeout(tipHideTimer));
tipEl.addEventListener("mouseleave", () => tipHideLater());
document.addEventListener("pointerdown", (e) => { if (!tipEl.contains(e.target)) tipHideNow(); }, true);
window.addEventListener("scroll", tipHideNow, true);
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
  start: null,
  days: 14,
  cal: null,
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

async function loadCalendar() {
  try {
    state.cal = await api(`/api/calendar?start=${state.start}&days=${state.days}`);
    renderCalendar();
  } catch (err) {
    toast(err.message);
  }
}

function renderCalendar() {
  const { cal } = state;
  const days = cal.days;
  const end = addDays(cal.start, days - 1);
  $("#range-label").textContent = `${cal.start.slice(5).replace("-", "/")} – ${end.slice(5).replace("-", "/")}`;
  const root = $("#calendar");
  root.style.setProperty("--days", days);

  const dates = Array.from({ length: days }, (_, i) => addDays(cal.start, i));
  const cellClass = (d) => {
    const wd = parseDate(d).getDay();
    return [wd === 0 || wd === 6 ? "weekend" : "", d === cal.today ? "today" : "", d < cal.today ? "past" : ""].join(" ");
  };

  let html = `<div class="cal-row cal-head"><div class="cal-label muted small">服务器 / 显卡</div><div class="cal-track">`;
  dates.forEach((d, i) => {
    const dt = parseDate(d);
    html += `<div class="cal-cell ${cellClass(d)}" style="grid-column:${i + 1}"><b>${dt.getMonth() + 1}/${dt.getDate()}</b>周${WEEK[dt.getDay()]}</div>`;
  });
  html += `</div></div>`;

  if (!cal.servers.length) {
    html += `<div class="empty">还没有 GPU 服务器。${state.me.is_admin ? "请在「管理」页添加服务器并安装 Agent。" : "请联系管理员添加。"}</div>`;
  }

  const byGpu = {};
  cal.reservations.forEach((r) => (byGpu[r.gpu_id] ||= []).push(r));

  for (const s of cal.servers) {
    const seen = s.last_seen_at ? `最近上报 ${s.last_seen_at.replace("T", " ").slice(5, 16)}` : "Agent 未连接";
    html += `<div class="cal-row cal-server"><div class="cal-label" title="${esc(`${s.name} ${s.host}\n${seen}`)}">
      <span class="srv-title"><span class="dot ${s.online ? "on" : ""}"></span><span class="server-name">${esc(s.name)}</span></span>
      <span class="muted small srv-host">${esc(s.host)}</span></div>
      <div class="cal-track"><div class="muted small srv-desc">${esc(s.description)}${s.online ? "" : `　<span style="color:var(--warn)">${esc(seen)}</span>`}</div></div></div>`;
    if (!s.gpus.length) {
      html += `<div class="cal-row"><div class="cal-label muted small">等待 Agent 上报显卡…</div><div class="cal-track"></div></div>`;
    }
    for (const g of s.gpus) {
      const util = g.utilization ?? 0;
      const stat = s.online && g.memory_total != null
        ? `<span class="meter" title="利用率 ${util}%"><i style="width:${util}%"></i></span>${util}% · ${gb(g.memory_used)}/${gb(g.memory_total)}G`
        : `<span>离线</span>`;
      const procs = g.processes.map((p) => `${p.user || "?"}(pid ${p.pid}, ${gb(p.used_memory)}G)`).join("\n");
      const alert = g.alerts.length
        ? `<span class="alert" title="今天未预约这张卡、却在上面运行程序的用户：\n${esc(procs)}">⚠ 未预约使用：${esc(g.alerts.join(", "))}</span>`
        : "";
      html += `<div class="cal-row"><div class="cal-label" title="${esc(procs || "无进程")}">
        <span class="gpu-title"><b>GPU${g.index}</b> · ${esc(g.name.replace(/^NVIDIA /, ""))}</span>
        <span class="gpu-stat">${stat}</span>${alert}</div><div class="cal-track">`;
      dates.forEach((d, i) => {
        html += `<div class="cal-cell ${cellClass(d)}" style="grid-column:${i + 1}" data-server="${s.id}" data-gpu="${g.id}" data-date="${d}"></div>`;
      });
      for (const r of byGpu[g.id] || []) {
        const c1 = Math.max(0, diffDays(cal.start, r.start_date)) + 1;
        const c2 = Math.min(days - 1, diffDays(cal.start, r.end_date)) + 1;
        const mine = r.user_id === state.me.id;
        html += `<div class="bar ${mine ? "mine" : ""}" style="grid-column:${c1}/${c2 + 1};--h:${hue(r.user_id)}" data-res="${r.id}"
          title="${esc(`${r.user_name}（${r.linux_username || "-"}）\n${r.start_date} ~ ${r.end_date}${r.auto_renew ? "（自动续约）" : ""}\n${r.note}`)}">
          <span>${r.auto_renew ? "🔁 " : ""}${esc(r.user_name)}</span>${r.note ? `<small>${esc(r.note)}</small>` : ""}</div>`;
      }
      html += `</div></div>`;
    }
  }
  root.innerHTML = html;
}

$("#calendar").addEventListener("click", (e) => {
  const bar = e.target.closest(".bar");
  if (bar) return openDetail(state.cal.reservations.find((r) => r.id === Number(bar.dataset.res)));
  const cell = e.target.closest(".cal-cell[data-gpu]");
  if (cell && !cell.classList.contains("past")) {
    openBooking({ serverId: Number(cell.dataset.server), gpuId: Number(cell.dataset.gpu), date: cell.dataset.date });
  }
});

$("#prev").onclick = () => { state.start = addDays(state.start, -state.days); loadCalendar(); };
$("#next").onclick = () => { state.start = addDays(state.start, state.days); loadCalendar(); };
$("#go-today").onclick = () => { state.start = state.config.today; loadCalendar(); };
$("#days").onchange = (e) => { state.days = Number(e.target.value); loadCalendar(); };
$("#new-booking").onclick = () => openBooking({});

// ---------------- 预约弹窗 ----------------

const bookingDialog = $("#booking-dialog");
const bookingForm = $("#booking-form");

function busyMap(start, end) {
  // 根据当前已加载的日历数据标出冲突（只是提示，最终以服务端校验为准）
  const m = {};
  for (const r of state.cal?.reservations || []) {
    if (r.start_date <= end && r.end_date >= start) m[r.gpu_id] = r.user_name;
  }
  return m;
}

function renderGpuPicks(selected = []) {
  const s = state.cal.servers.find((x) => x.id === Number(bookingForm.server.value));
  const busy = busyMap(bookingForm.start_date.value, bookingForm.end_date.value || bookingForm.start_date.value);
  $("#booking-gpus").innerHTML = (s?.gpus || [])
    .map((g) => {
      const b = busy[g.id];
      return `<label class="${b ? "busy" : ""}" title="${b ? `已被 ${esc(b)} 预约` : ""}">
        <input type="checkbox" name="gpu" value="${g.id}" ${selected.includes(g.id) && !b ? "checked" : ""} ${b ? "disabled" : ""}>
        GPU${g.index}${b ? ` · ${esc(b)}` : ""}</label>`;
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
  $("#booking-rule").textContent = `规则：单次最长 ${state.config.max_reservation_days} 天，最多提前 ${state.config.max_advance_days} 天；按自然日计算，开始当天 00:00 开放登录，结束日 24:00 关闭。`;
  if (state.me.is_admin) {
    const users = state.users.length ? state.users : (state.users = await api("/api/admin/users"));
    $("#booking-user").innerHTML = users
      .filter((u) => u.linux_username && u.enabled)
      .map((u) => `<option value="${u.id}" ${u.id === state.me.id ? "selected" : ""}>${esc(u.name)}（${esc(u.linux_username)}）</option>`)
      .join("");
  }
  renderGpuPicks(gpuId ? [gpuId] : []);
  bookingDialog.showModal();
}

bookingForm.server.onchange = () => renderGpuPicks();
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
    start_date: bookingForm.start_date.value,
    end_date: bookingForm.end_date.value,
    note: bookingForm.note.value,
    auto_renew: bookingForm.auto_renew.checked,
  };
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
  $("#detail-title").textContent = `${r.server_name} · GPU${r.gpu_index}`;
  $("#detail-info").innerHTML = `${esc(r.user_name)}（${esc(r.linux_username || "-")}）<br>${r.start_date} ~ ${r.end_date}`
    + `${r.auto_renew ? `<br>🔁 已开启自动续约${r.renew_count ? `（已续约 ${r.renew_count} 次）` : ""}` : ""}${r.note ? `<br>${esc(r.note)}` : ""}`;
  $("#detail-error").textContent = "";
  $$(".editable", detailForm).forEach((el) => el.classList.toggle("hidden", !r.editable));
  $$(".renew-rule").forEach((el) => (el.textContent = renewRuleText()));
  detailForm.start_date.value = r.start_date;
  detailForm.end_date.value = r.end_date;
  detailForm.note.value = r.note;
  detailForm.auto_renew.checked = r.auto_renew;
  detailForm.start_date.disabled = r.start_date <= today && !state.me.is_admin;
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
    : "确定立即释放吗？今天起这张卡会让给别人，你将不能再使用它（若没有其他预约）。";
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

async function renderAccount() {
  const [me, servers, rs] = await Promise.all([api("/api/me"), api("/api/me/servers"), api("/api/me/reservations")]);
  state.me = me;
  state.myServers = servers;
  renderBanner();

  // 服务器账号
  const box = $("#linux-account");
  if (!me.linux_username) {
    box.innerHTML = `<p class="muted small">这是你在所有 GPU 服务器上的登录名，建议用姓名拼音，如 <code>zhangsan</code>。以后也可以修改。</p>
      <form id="username-form" class="group"><input name="username" placeholder="小写字母开头，如 zhangsan" required style="flex:1;width:auto">
      <button class="btn primary">保存</button></form>`;
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
        : !a ? `<span class="badge pending">同步中…</span>`
        : `<span class="badge ${a.state}" title="${esc(a.message)}">${ACCOUNT_STATE[a.state] || a.state}</span>`;
      const local = a && a.username !== me.linux_username ? `<div class="small warn-text">当前仍为 ${esc(a.username)}：${esc(a.message)}</div>`
        : a?.state === "error" ? `<div class="small" style="color:var(--danger)">${esc(a.message)}</div>` : "";
      return `<tr><td>${esc(s.name)}</td><td><code>ssh ${esc(a?.username || me.linux_username)}@${esc(s.host || s.name)}</code>${local}</td><td>${st}</td></tr>`;
    }).join("");
    box.innerHTML = `<div class="group" style="margin-bottom:8px">用户名 <code>${esc(me.linux_username)}</code>　UID <code>${me.uid}</code>
        <button class="btn small" id="rename-btn">修改</button></div>
      <p class="muted small">随时都可以登录服务器（需在实验室内网），不需要预约；预约只决定你能用哪些显卡。登录后执行 <code>lab-gpus</code> 查看今天分配给你的显卡。</p>
      ${servers.length ? `<div class="table-wrap"><table><tr><th>服务器</th><th>登录命令</th><th>账号状态</th></tr>${rows}</table></div>` : `<p class="muted">暂无服务器</p>`}`;
    $("#rename-btn").onclick = openRename;
  }

  // 密码
  $("#password-state").innerHTML = me.has_password
    ? `<p><span class="badge ok">已设置</span> 可以用密码登录</p>`
    : `<p><span class="badge">未设置</span> 目前只能用公钥登录</p>`;
  $("#password-clear").classList.toggle("hidden", !me.has_password);

  // 公钥
  $("#key-list").innerHTML = me.ssh_keys.length
    ? me.ssh_keys.map((k) => `<li><div><b>${esc(k.title)}</b><code>${esc(k.fingerprint)}</code></div>
        <button class="btn small danger" data-del-key="${k.id}">删除</button></li>`).join("")
    : `<li class="muted small">还没有公钥</li>`;

  // 预约
  $("#my-reservations").innerHTML = rs.length
    ? `<div class="table-wrap"><table><tr><th>服务器</th><th>显卡</th><th>日期</th><th>自动续约</th><th>备注</th><th></th></tr>
        ${rs.map((r) => `<tr><td>${esc(r.server_name)}</td><td>GPU${r.gpu_index}</td><td>${r.start_date} ~ ${r.end_date}</td>
          <td><label class="check"><input type="checkbox" data-renew="${r.id}" ${r.auto_renew ? "checked" : ""}>${r.renew_count ? `<span class="muted small">已续 ${r.renew_count} 次</span>` : ""}</label></td>
          <td>${esc(r.note)}</td><td><button class="btn small" data-open-res="${r.id}">续约 / 释放</button></td></tr>`).join("")}
      </table></div><p class="muted small">自动续约${esc(renewRuleText())}。与他人预约冲突时只延长到冲突前一天。</p>`
    : `<p class="muted">没有进行中或即将开始的预约。</p>`;
  $$("[data-open-res]").forEach((b) => (b.onclick = () => openDetail(rs.find((r) => r.id === Number(b.dataset.openRes)))));
  $$("[data-renew]").forEach((c) => (c.onchange = async () => {
    try {
      await api(`/api/reservations/${c.dataset.renew}`, { method: "PATCH", body: { auto_renew: c.checked } });
      toast(c.checked ? "已开启自动续约" : "已关闭自动续约");
    } catch (err) {
      c.checked = !c.checked;
      toast(err.message);
    }
  }));

  await refreshMigrations();
}

$("#key-list").addEventListener("click", async (e) => {
  const id = e.target.dataset.delKey;
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
  return `<div class="task">
    <div class="task-head">
      <span class="badge ${t.status}">${TASK_STATE[t.status] || t.status}</span>
      ${admin ? `<b>${esc(t.user_name)}</b>` : ""}
      <b>${esc(t.server_name)}</b>
      ${t.kind === "profile"
        ? `<span>${esc(t.summary)}</span>`
        : `<span class="path">${esc(t.source)} → ${esc(t.dest_path || `~/${t.dest_name}`)}</span><span class="muted small">${MODE_NAMES[t.mode] || t.mode}</span>`}
      <span class="spacer"></span>${actions.join("")}
    </div>
    ${["running", "done"].includes(t.status) ? `<div class="progress ${pct == null ? "indeterminate" : ""}"><i style="width:${pct ?? 30}%"></i></div>` : ""}
    <div class="task-meta"><span>${t.status === "running" ? esc(t.phase) + "　" : ""}${detail}</span><span>${speed}</span></div>
    ${t.message ? `<div class="small ${t.status === "failed" ? "error" : "muted"}" style="margin-top:4px">${esc(t.message)}</div>` : ""}
  </div>`;
}

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
    chip.textContent = running ? `📦 迁移中${pct != null ? ` ${Math.floor(pct)}%` : "…"}` : `📦 ${active.length} 个迁移等待中`;
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
 *  上半部分：从旧共享账号（如 ubuntu）复制配置、软件、conda 环境；下半部分：迁移自己的文件 / 目录。 */
const CFG_GROUPS = {
  config: { title: "配置文件", hint: "默认全选，文件里的旧家目录路径会自动改成你的家目录", checked: true },
  software: { title: "用户级软件", hint: "", checked: false },
  cache: { title: "缓存", hint: "体积较大，需要时再选", checked: false },
  credential: { title: "共享账号的凭据", hint: "这些是大家共用的密钥 / 令牌，建议以后换成自己的；不选则不会复制", checked: false },
};
const MODE_NAMES = { move: "移动", copy: "复制", link: "原地保留" };

async function renderPicker(container, onSubmitted) {
  container.innerHTML = `<p class="muted">正在读取服务器上的内容…</p>`;
  let servers;
  try {
    servers = await api("/api/me/servers");
  } catch (err) {
    container.innerHTML = `<p class="error">${esc(err.message)}</p>`;
    return;
  }
  if (!state.me.linux_username) {
    container.innerHTML = `<p class="muted">请先设置 Linux 用户名。</p>`;
    return;
  }
  const usable = servers.filter((s) => s.online);
  if (!usable.length) {
    container.innerHTML = `<p class="muted">目前没有在线的服务器。</p>`;
    return;
  }
  container.innerHTML = `
    <label>服务器<select class="pk-server">${usable.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join("")}</select></label>
    <div class="pk-body"></div>
    <p class="pk-total small muted"></p>
    <p class="pk-warn small warn-text"></p>
    <p class="error pk-error"></p>
    <div class="actions"><button class="btn primary pk-submit">开始迁移</button></div>`;
  const sel = $(".pk-server", container);
  const cur = () => usable.find((x) => x.id === Number(sel.value));

  const render = () => {
    const s = cur();
    let html = "";
    for (const [li, lg] of s.legacy.entries()) {
      const groups = {};
      for (const c of lg.configs) (groups[c.group] ||= []).push(c);
      html += `<div class="pk-section" data-legacy="${li}">
        <h3>① 从共享账号 <code>${esc(lg.user)}</code> 复制环境与配置 <span class="muted small">（复制，原账号不受影响）</span></h3>`;
      for (const [g, meta] of Object.entries(CFG_GROUPS)) {
        if (!groups[g]) continue;
        html += `<div class="label">${meta.title}${meta.hint ? ` <span class="muted small">· ${meta.hint}</span>` : ""}</div><div class="cfg-grid">`;
        html += groups[g].map((c) => `<label class="check cfg ${g === "credential" ? "cred" : ""}"><input type="checkbox" data-cfg="${esc(c.key)}" ${meta.checked ? "checked" : ""}>
          <span>${esc(c.label)} <span class="muted small">${fmtBytes(c.size)}</span></span></label>`).join("");
        html += `</div>`;
      }
      for (const [ci, c] of lg.conda.entries()) {
        const isUser = c.name === ".conda";
        if (!isUser) {
          html += `<div class="label">Conda</div><div class="cfg-grid">
            <label class="check"><input type="checkbox" data-conda="${ci}" data-base="1" checked>
              <span>conda 本体 <code>~/${esc(c.name)}</code> <span class="muted small">${fmtBytes(c.base_size)} · 不含环境，之后自己 conda create</span></span></label></div>`;
        }
        if (c.envs.length) {
          html += `<details class="small" style="margin-top:6px"><summary>${isUser ? "<code>~/.conda/envs</code> 里" : `<code>~/${esc(c.name)}/envs</code> 里`}的已有环境（可选，复制后自动修正路径）</summary>
            <div class="cfg-grid" style="margin-top:6px">${c.envs.map((e) => `<label class="check"><input type="checkbox" data-conda="${ci}" data-env="${esc(e.name)}">
              <span>${esc(e.name)} <span class="muted small">${fmtBytes(e.size)}</span></span></label>`).join("")}</div></details>`;
        }
      }
      if (!lg.conda.length) html += `<p class="muted small">共享账号家目录里没有找到 conda。</p>`;
      html += `<p class="muted small">提示：apt 安装的软件（git、vim、tmux、gcc、CUDA 等）是全系统共享的，新账号直接可用，不需要迁移。
        建议只复制 conda 本体，需要的环境自己用 <code>conda create</code> 新建（GPU 服务器没有外网，需要先在自己的 SSH 会话里挂上代理）；确实不方便重建的旧环境，可以展开上面的列表一并复制。</p></div>`;
    }
    html += `<div class="pk-section"><h3>② 迁移我的文件和目录</h3>
      <p class="muted small">下面是共享账号家目录${s.legacy.length ? "" : "（未配置）"}和数据目录里<b>还没归属任何成员</b>的内容。请只勾选<b>自己的</b>：
      「移动」把文件从共享位置搬走（其他人就看不到了），「复制」保留原文件。迁移前请先停掉正在使用这些文件的程序。</p>
      <div class="cand-list pk-list">${s.candidates.length ? s.candidates.map((c, i) => {
        const taken = c.claimed_by || c.mine;
        const name = c.path.split("/").pop();
        const modes = c.in_home ? ["move", "copy"] : ["move", "copy", "link"];
        return `<div class="cand ${taken ? "disabled" : ""}">
          <input type="checkbox" data-i="${i}" ${taken ? "disabled" : ""}>
          <span class="path">${esc(c.path)}${c.is_dir === false ? "" : "/"}</span>
          <span class="cand-ctl">
            <select data-mode="${i}" ${taken ? "disabled" : ""}>${modes.map((m) => `<option value="${m}">${MODE_NAMES[m]}</option>`).join("")}</select>
            <input class="dest" data-dest="${i}" value="${esc(name)}" title="放到家目录下的名字" ${taken ? "disabled" : ""}>
          </span>
          <span class="meta">${c.size != null ? fmtBytes(c.size) : "大小统计中"} · 修改于 ${esc(c.mtime)} · 属主 ${esc(c.owner)}${c.same_fs ? "" : " · 与家目录不在同一磁盘"}
            ${c.claimed_by ? ` · 已被 ${esc(c.claimed_by)} 迁移` : ""}${c.mine ? " · 你已迁移过" : ""}</span>
        </div>`;
      }).join("") : `<div class="empty">没有可迁移的文件或目录。</div>`}</div></div>`;
    $(".pk-body", container).innerHTML = html;
    update();
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
    const dirs = $$(".pk-list input[type=checkbox]:checked", container).map((c) => {
      const i = Number(c.dataset.i);
      return { cand: s.candidates[i], mode: $(`[data-mode="${i}"]`, container).value, dest: $(`[data-dest="${i}"]`, container).value.trim() };
    });
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

  sel.onchange = render;
  container.onchange = update;
  container.oninput = (e) => { if (e.target.matches(".dest")) update(); };
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

$("#open-migrate").onclick = () => {
  $("#migrate-dialog").showModal();
  renderPicker($("#migrate-picker"), () => { $("#migrate-dialog").close(); });
};
$("#migrate-close").onclick = () => $("#migrate-dialog").close();

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
    body.innerHTML = `<h2>欢迎使用${esc(state.config.lab_name)}平台 👋</h2>
      <p>以后大家用<b>自己的账号</b>登录 GPU 服务器，不再共用 ubuntu 等公共账号。先给自己起一个 Linux 用户名（所有服务器通用，之后也可以修改）：</p>
      <input id="ob-username" placeholder="小写字母开头，如 zhangsan" value="${esc(me.linux_username || "")}">`;
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
      <p class="muted small">以前大家共用一个账号（如 ubuntu）。这里可以把共用账号里的 .bashrc、git 配置、需要的 conda 环境复制到你的新账号，并把你自己的项目目录迁移过来。</p>
      <div id="ob-picker"></div><div id="onboard-migrations"></div>`;
    renderPicker($("#ob-picker"), () => {
      $("#ob-picker").innerHTML = `<p>✅ 迁移已开始，在服务器后台进行。你可以直接关闭这个窗口，进度随时可以在「我的账号 → 文件迁移」或顶部的 📦 查看。</p>`;
    });
  } else {
    const running = state.migrations.some((t) => ["queued", "running", "pending_approval"].includes(t.status));
    body.innerHTML = `<h2>设置完成 🎉</h2>
      <ul>
        <li>在「GPU 日历」上点空白格子即可预约显卡，预约当天起可以使用所选显卡（没有预约也能登录服务器、迁移文件，只是看不到显卡）。</li>
        <li>登录命令：<code>ssh ${esc(me.linux_username)}@服务器地址</code>${me.has_password ? "，输入你刚设置的密码" : ""}。</li>
        ${running ? "<li>文件迁移正在后台进行，进度见顶部的 📦。</li>" : ""}
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
$("#onboard-later").onclick = () => {
  $("#onboard-dialog").close();
  try { sessionStorage.setItem("lab-onboard-later", "1"); } catch {}
};

// ---------------- 管理 ----------------

async function loadAdmin() {
  try {
    const [servers, users, settings, migrations, tunnel] = await Promise.all([
      api("/api/admin/servers"), api("/api/admin/users"), api("/api/admin/settings"), api("/api/admin/migrations"),
      api("/api/admin/tunnel").catch(() => ({ enabled: false })),
    ]);
    state.users = users;
    state.tunnelEnabled = tunnel.enabled;
    renderAdminServers(servers, tunnel);
    renderAdminUsers(users);
    $("#admin-migrations").innerHTML = migrations.length ? migrations.map((t) => taskHtml(t, true)).join("") : `<p class="muted">暂无迁移记录。</p>`;
    const f = $("#welcome-form");
    for (const k of ["welcome_title", "welcome_markdown", "welcome_links"]) f[k].value = settings[k];
    const r = $("#rules-form");
    r.auto_renew_before_hours.value = settings.auto_renew_before_hours;
    r.auto_renew_days.value = settings.auto_renew_days;
    r.migration_requires_approval.checked = settings.migration_requires_approval === "1";
  } catch (err) {
    toast(err.message);
  }
}

function renderAdminServers(servers, tunnel) {
  $("#admin-servers").innerHTML = webvpnLine(tunnel) + (servers.length
    ? `<div class="table-wrap"><table><tr><th>名称</th><th>地址</th><th>状态</th><th>显卡</th><th>Agent</th><th>校园网隧道</th><th>注册命令</th><th></th></tr>
      ${servers.map((s) => {
        const models = [...new Set(s.gpus.map((g) => g.name.replace(/^NVIDIA /, "")))].join(", ");
        return `<tr><td><b>${esc(s.name)}</b><div class="muted small">${esc(s.description)}</div></td><td>${esc(s.host)}</td>
          <td><span class="dot ${s.online ? "on" : ""}" style="display:inline-block"></span> ${s.online ? "在线" : "离线"}
            <div class="muted small">${s.last_seen_at ? s.last_seen_at.replace("T", " ") : "从未连接"}</div></td>
          <td>${s.gpus.length} × ${esc(models || "-")}</td>
          <td class="small">${esc(s.hostname || "-")}<div class="muted">${esc(s.agent_version || "")}</div></td>
          <td class="small">${tunnelCell(s.tunnel)}</td>
          <td class="small">${registerCell(s)}</td>
          <td style="white-space:nowrap">
            <button class="btn small" data-edit-server="${s.id}">编辑</button>
            <button class="btn small" data-token-server="${s.id}">重置 Token</button>
            <button class="btn small danger" data-del-server="${s.id}">删除</button></td></tr>`;
      }).join("")}</table></div>
      <p class="muted small">「注册命令」只有管理员能看到，用来把 GPU 服务器接入平台。接入过的服务器重装 / 换 token 时点「重置 Token」会生成新命令。</p>`
    : `<p class="muted">还没有服务器。添加后这里会给出一条注册命令，照着执行即可把 GPU 接入平台。</p>`);
  $$("[data-install-server]").forEach((b) => (b.onclick = () => showInstallDialog(servers.find((x) => x.id === Number(b.dataset.installServer)))));
  $$("[data-edit-server]").forEach((b) => (b.onclick = async () => {
    const s = servers.find((x) => x.id === Number(b.dataset.editServer));
    const name = prompt("名称", s.name); if (name === null) return;
    const host = prompt("内网地址", s.host); if (host === null) return;
    const description = prompt("备注", s.description); if (description === null) return;
    try { await api(`/api/admin/servers/${s.id}`, { method: "PATCH", body: { name, host, description } }); loadAdmin(); } catch (err) { toast(err.message); }
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
  return `<p class="small">公网服务器 → 学校 WebVPN：<span class="badge ${cls}">${esc(text)}</span>${w.error ? ` <span class="muted">${esc(w.error)}</span>` : ""}</p>`;
}
function tunnelCell(t) {
  if (!t) return `<span class="muted">-</span>`;
  const [cls, text] = TUNNEL_STATE[t.state] || ["", t.state];
  return `<span class="badge ${cls}" title="${esc(t.error || "")}">${text}</span><div class="muted">${esc(t.since?.replace("T", " ").slice(5, 16) || "")}</div>`;
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
  if (!s.token) return `<span class="muted">点「重置 Token」生成</span>`;
  return `<button class="btn small" data-install-server="${s.id}">查看命令</button>`;
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

$("#server-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  try {
    await api("/api/admin/servers", { method: "POST", body: { name: f.name.value.trim(), host: f.host.value, description: f.description.value } });
    f.reset();
    toast("已添加，请点该行「注册命令」里的「查看命令」");
    loadAdmin();
  } catch (err) {
    toast(err.message);
  }
});

function renderAdminUsers(users) {
  $("#admin-users").innerHTML = `<div class="table-wrap"><table><tr><th>姓名</th><th>Linux 用户名</th><th>UID</th><th>密码</th><th>公钥</th><th>管理员</th><th>启用</th></tr>
    ${users.map((u) => `<tr><td>${esc(u.name)}</td>
      <td>${u.linux_username ? `<code>${esc(u.linux_username)}</code>` : `<span class="badge">未设置</span>`}
        <button class="btn small ghost" data-rename="${u.id}">修改</button></td>
      <td>${u.uid}</td><td>${u.has_password ? "✓" : "-"}</td><td>${u.key_count ? u.key_count : `<span class="muted">未配置</span>`}</td>
      <td><input type="checkbox" data-admin="${u.id}" ${u.is_admin ? "checked" : ""} style="width:auto;min-height:0"></td>
      <td><input type="checkbox" data-enabled="${u.id}" ${u.enabled ? "checked" : ""} style="width:auto;min-height:0"></td></tr>`).join("")}
    </table></div>
    <p class="muted small">停用成员后，各服务器上的账号会在 1 分钟内被锁定（家目录和数据保留）。修改用户名会在各服务器上把账号改名并移动家目录。</p>`;
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

$("#rules-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  try {
    await api("/api/admin/settings", {
      method: "PUT",
      body: {
        auto_renew_before_hours: f.auto_renew_before_hours.value,
        auto_renew_days: f.auto_renew_days.value,
        migration_requires_approval: f.migration_requires_approval.checked ? "1" : "0",
      },
    });
    state.config = await api("/api/config");
    toast("已保存");
  } catch (err) {
    toast(err.message);
  }
});

$("#welcome-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  try {
    await api("/api/admin/settings", { method: "PUT", body: { welcome_title: f.welcome_title.value, welcome_markdown: f.welcome_markdown.value, welcome_links: f.welcome_links.value } });
    toast("已保存");
  } catch (err) {
    toast(err.message);
  }
});
$("#welcome-test").onclick = async () => {
  try { await api("/api/admin/welcome/test", { method: "POST" }); toast("已发送，请查看飞书"); } catch (err) { toast(err.message); }
};

// ---------------- 启动 ----------------

async function boot() {
  state.config = await api("/api/config");
  document.title = `${state.config.lab_name}平台`;
  $("#brand").textContent = $("#login-title").textContent = document.title;
  state.start = state.config.today;
  try {
    state.me = await api("/api/me", { allow401: true });
  } catch {
    state.me = null;
  }
  if (!state.me) {
    $("#dev-login").classList.toggle("hidden", !state.config.dev_login);
    $("#feishu-login").classList.toggle("hidden", !state.config.feishu_enabled);
    $("#login-unavailable").classList.toggle("hidden", state.config.feishu_enabled || state.config.dev_login);
    return showLogin();
  }
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(location.hostname);
  $("#http-banner").classList.toggle("hidden", location.protocol === "https:" || local);
  $("#me-name").textContent = state.me.name;
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
  if (!state.me.onboarded && !later) openOnboarding();
  // 每分钟刷新一次日历（GPU 利用率由 Agent 每分钟上报）
  setInterval(() => {
    if (!document.hidden && activeTab() === "calendar" && !$("dialog[open]")) loadCalendar();
  }, 60000);
}

boot();
