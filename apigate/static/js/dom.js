// Tiny DOM toolkit. Everything goes through textContent / createElement, never innerHTML.
export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === 'value' || k === 'checked' || k === 'disabled' || k === 'selected' || k === 'indeterminate') el[k] = v;
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  append(el, children);
  return el;
}
export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
export function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
export function mount(el, ...children) { clear(el); return append(el, children); }

export function toast(msg, bad = false) {
  let box = document.getElementById('toasts');
  if (!box) { box = h('div', { id: 'toasts' }); document.body.append(box); }
  const t = h('div', { class: 'toast' + (bad ? ' bad' : ''), role: bad ? 'alert' : 'status' }, msg);
  box.append(t);
  setTimeout(() => t.remove(), bad ? 7000 : 3500);
}
export function fail(e) { toast(e && e.message ? e.message : String(e), true); }

export function modal(title, body, actions = []) {
  return new Promise((resolve) => {
    const close = (v) => { bg.remove(); resolve(v); };
    const bg = h('div', { class: 'modal-bg', onclick: (e) => { if (e.target === bg) close(null); } },
      h('div', { class: 'modal', role: 'dialog', 'aria-modal': 'true' },
        h('h2', {}, title), body,
        h('div', { class: 'row', style: null }, h('div', { class: 'grow' }),
          ...actions.map((a) => h('button', { class: a.class || '', onclick: () => close(a.value) }, a.label)))));
    document.body.append(bg);
    const first = bg.querySelector('input,textarea,select,button.primary');
    if (first) first.focus();
  });
}
export async function askText(title, label, { value = '', type = 'text', ok = 'OK' } = {}) {
  const input = h('input', { type, value, autocomplete: 'off' });
  const r = await modal(title, h('label', { class: 'field' }, label, input), [{ label: 'Cancel', value: null }, { label: ok, value: true, class: 'primary' }]);
  return r ? input.value : null;
}
export async function confirmBox(title, text, ok = 'Confirm') {
  return !!(await modal(title, h('p', {}, text), [{ label: 'Cancel', value: false }, { label: ok, value: true, class: 'danger' }]));
}

export const fmt = {
  credits(n, d = 4) { return Number(n ?? 0).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: n && Math.abs(n) < 100 ? 2 : 0 }); },
  int(n) { return Number(n ?? 0).toLocaleString(); },
  time(ts) { return ts ? new Date(ts * 1000).toLocaleString() : '—'; },
  ago(ts) {
    if (!ts) return 'never';
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 90) return 'just now'; if (s < 5400) return Math.round(s / 60) + ' min ago';
    if (s < 129600) return Math.round(s / 3600) + ' h ago'; return Math.round(s / 86400) + ' d ago';
  },
};

export function field(label, input) { return h('label', { class: 'field' }, label, input); }
export function badge(text, kind = '') { return h('span', { class: 'badge ' + kind }, text); }
export function bar(used, limit) {
  const r = limit > 0 ? Math.min(used / limit, 1) : 0;
  const el = h('div', { class: 'bar' + (r >= 0.9 ? ' hot' : r >= 0.7 ? ' warn' : ''), title: `${used} / ${limit}` }, h('div', {}));
  el.firstChild.style.width = (r * 100).toFixed(1) + '%';
  return el;
}
export function table(headers, rows, empty = 'Nothing here yet.') {
  if (!rows.length) return h('p', { class: 'muted' }, empty);
  return h('div', { class: 'tablewrap' }, h('table', {},
    h('thead', {}, h('tr', {}, headers.map((x) => h('th', {}, x)))),
    h('tbody', {}, rows.map((r) => h('tr', {}, r.map((c) => h('td', {}, c)))))));
}
export async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast('Copied'); } catch { toast('Select the text and copy it manually', true); }
}

// ---- small, safe markdown renderer (headings, lists, code, bold/italic, links) ----
export function renderMarkdown(src) {
  const root = h('div', {});
  const parts = src.split(/```/);
  parts.forEach((part, i) => {
    if (i % 2 === 1) {
      const nl = part.indexOf('\n');
      const code = nl >= 0 ? part.slice(nl + 1) : part;
      const pre = h('pre', {}, h('code', {}, code.replace(/\n$/, '')));
      pre.append(h('button', { class: 'tiny', onclick: () => copyText(code) }, 'Copy'));
      root.append(pre);
      return;
    }
    let list = null, listType = '';
    const flush = () => { list = null; listType = ''; };
    for (const line of part.split('\n')) {
      let m;
      if ((m = line.match(/^(#{1,3})\s+(.*)$/))) { flush(); root.append(h('h' + m[1].length, {}, inline(m[2]))); }
      else if ((m = line.match(/^\s*[-*]\s+(.*)$/))) { if (listType !== 'ul') { list = h('ul', {}); root.append(list); listType = 'ul'; } list.append(h('li', {}, inline(m[1]))); }
      else if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) { if (listType !== 'ol') { list = h('ol', {}); root.append(list); listType = 'ol'; } list.append(h('li', {}, inline(m[1]))); }
      else if (line.trim()) { flush(); root.append(h('p', {}, inline(line))); }
      else flush();
    }
  });
  return root;
}
function inline(text) {
  const out = [];
  const re = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\*[^*\n]+\*)|(\[[^\]\n]+\]\(https?:\/\/[^\s)]+\))/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const t = m[0];
    if (m[1]) out.push(h('code', { class: 'inline' }, t.slice(1, -1)));
    else if (m[2]) out.push(h('strong', {}, t.slice(2, -2)));
    else if (m[3]) out.push(h('em', {}, t.slice(1, -1)));
    else { const mm = t.match(/^\[([^\]]+)\]\((.+)\)$/); out.push(h('a', { href: mm[2], target: '_blank', rel: 'noopener noreferrer nofollow' }, mm[1])); }
    last = m.index + t.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}
