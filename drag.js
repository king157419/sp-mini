// 指针拖拽，两个窗口共用，替代 HTML5 拖放。
// WebView2 里的 HTML5 拖放走 Windows 的 OLE 拖拽循环：起步要等，跟手的图一卡一卡。
// 这里自己做：按下后移动超过 4px 才算拖；跟手的是一份浮起来的拷贝，每帧只改 transform；
// 放到哪儿用 elementFromPoint 往上找最近一个登记过的放置区。
//
//   Drag.source(el, e => payload | null)   // 返回 null 表示这次不让拖
//   Drag.zone(el, { over(p, pt), leave(p), drop(p, pt) })
//   Drag.active                             // 按住了或正在拖（轮询、重画都该等一等）
const Drag = (() => {
  const zones = new WeakMap();
  const SKIP = ".ck, .check, .rs, input, button, [contenteditable='true']";
  let s = null;

  function source(el, payload) {
    el.addEventListener("pointerdown", e => {
      if (e.button !== 0 || s || e.target.closest(SKIP)) return;
      const p = payload(e);
      if (!p) return;
      s = { el, p, id: e.pointerId, x0: e.clientX, y0: e.clientY, x: e.clientX, y: e.clientY, on: false, zone: null, raf: 0, f: null };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
      window.addEventListener("pointercancel", cancel);
      window.addEventListener("keydown", esc, true);
      window.addEventListener("blur", cancel);
    });
  }

  function zone(el, handlers) { zones.set(el, handlers); }

  function begin() {
    const el = s.el, r = el.getBoundingClientRect();
    const f = el.cloneNode(true);
    f.classList.remove("new", "pop", "drop", "dragging");
    f.classList.add("floating");
    Object.assign(f.style, {
      position: "fixed", left: r.left + "px", top: r.top + "px", width: r.width + "px", height: r.height + "px",
      margin: "0", pointerEvents: "none", zIndex: "1000", willChange: "transform",
    });
    el.after(f);                                   // 放在原处旁边，原来的样式规则照样生效
    s.f = f; s.on = true;
    el.classList.add("dragging");
    document.body.classList.add("dragging");
    try { el.setPointerCapture(s.id); } catch (_) {}
    el.addEventListener("lostpointercapture", cancel, { once: true });   // 元素被移走等意外情况
    s.raf = requestAnimationFrame(frame);
  }

  function frame() {
    if (!s || !s.on) return;
    s.f.style.transform = `translate3d(${s.x - s.x0}px, ${s.y - s.y0}px, 0)`;
    const hit = document.elementFromPoint(s.x, s.y);
    const z = zoneOf(hit);
    if (z !== s.zone) {
      if (s.zone) { s.zone.classList.remove("drop"); zones.get(s.zone).leave?.(s.p); }
      s.zone = z;
      if (z) z.classList.add("drop");
    }
    if (z) zones.get(z).over?.(s.p, { clientX: s.x, clientY: s.y });
    autoscroll(hit);
    s.raf = requestAnimationFrame(frame);
  }

  function zoneOf(n) {
    while (n && !zones.has(n)) n = n.parentElement;
    return n;
  }

  // 拖到可滚动区域的上下边缘，自动滚。
  function autoscroll(hit) {
    const sc = hit && hit.closest("[data-scroll]");
    if (!sc) return;
    const r = sc.getBoundingClientRect(), E = 48;
    let v = 0;
    if (s.y < r.top + E) v = s.y - (r.top + E);
    else if (s.y > r.bottom - E) v = s.y - (r.bottom - E);
    if (v) sc.scrollTop += Math.max(-E, Math.min(E, v)) * 0.35;
  }

  function move(e) {
    if (!s || e.pointerId !== s.id) return;
    s.x = e.clientX; s.y = e.clientY;
    if (!s.on && Math.hypot(s.x - s.x0, s.y - s.y0) > 4) begin();
  }

  function finish() {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    window.removeEventListener("pointercancel", cancel);
    window.removeEventListener("keydown", esc, true);
    window.removeEventListener("blur", cancel);
    const st = s; s = null;
    if (st.on) {
      cancelAnimationFrame(st.raf);
      st.f.remove();
      st.el.classList.remove("dragging");
      document.body.classList.remove("dragging");
      if (st.zone) { st.zone.classList.remove("drop"); zones.get(st.zone).leave?.(st.p); }
    }
    return st;
  }

  function up(e) {
    if (!s || e.pointerId !== s.id) return;
    // 松手那一刻的位置可能和上一帧不同，按松手处重新找一次放置区（拷贝不挡鼠标）。
    const z = s.on ? zoneOf(document.elementFromPoint(e.clientX, e.clientY)) : null;
    const st = finish();
    if (!st.on) return;                            // 只是点了一下，交给 click
    swallowClick();
    if (z) zones.get(z).drop?.(st.p, { clientX: e.clientX, clientY: e.clientY });
  }
  function cancel() { if (s) finish(); }
  function esc(e) { if (e.key === "Escape" && s) { e.stopPropagation(); finish(); } }

  // 拖完松手时浏览器还会补发一个 click，别让它触发「点空白处加任务」。
  function swallowClick() {
    const stop = e => { e.stopPropagation(); e.preventDefault(); };
    window.addEventListener("click", stop, { capture: true, once: true });
    setTimeout(() => window.removeEventListener("click", stop, true), 0);
  }

  return { source, zone, get active() { return !!s; } };
})();
