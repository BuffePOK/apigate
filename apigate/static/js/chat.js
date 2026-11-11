import { h, mount, clear, fail, toast, renderMarkdown, confirmBox, askText } from './dom.js';
import { get, post, patch, del, stream } from './api.js';
import { refreshUser } from './main.js';

export async function renderChat(page, convId) {
  const [{ models }, { conversations }] = await Promise.all([get('/api/models'), get('/api/conversations')]);
  let convs = conversations, current = null, abort = null, busy = false;

  const list = h('div', { class: 'convs' });
  const msgs = h('div', { class: 'msgs' });
  const inner = h('div', { class: 'inner' });
  msgs.append(inner);
  const modelSel = h('select', { 'aria-label': 'Model' });
  const byProv = {};
  models.forEach((m) => { (byProv[m.owned_by] ||= []).push(m); });
  Object.keys(byProv).sort().forEach((p) => modelSel.append(h('optgroup', { label: p }, byProv[p].map((m) => h('option', { value: m.id }, m.id.startsWith(p + '/') ? m.id.slice(p.length + 1) : m.id)))));
  if (!models.length) modelSel.append(h('option', { value: '' }, 'No models available yet'));
  const temp = h('input', { type: 'number', min: 0, max: 2, step: 0.1, placeholder: 'temp', 'aria-label': 'Temperature', style: null, class: 'small' });
  temp.style.width = '5rem';
  const sys = h('input', { type: 'text', placeholder: 'System prompt (optional)', class: 'grow', maxlength: 20000, 'aria-label': 'System prompt' });
  const input = h('textarea', { rows: 1, placeholder: 'Message… (Enter to send, Shift+Enter for a new line)', maxlength: 200000, 'aria-label': 'Message' });
  const send = h('button', { class: 'primary' }, 'Send');

  function drawList() {
    mount(list, convs.map((c) => h('a', { class: 'conv' + (current && c.id === current.id ? ' on' : ''), href: '#/chat/' + c.id },
      h('span', { title: c.title }, c.title),
      h('button', { title: 'Delete', 'aria-label': 'Delete conversation', onclick: async (e) => {
        e.preventDefault(); e.stopPropagation();
        if (!(await confirmBox('Delete conversation?', c.title, 'Delete'))) return;
        await del('/api/conversations/' + c.id); convs = convs.filter((x) => x.id !== c.id);
        if (current && current.id === c.id) location.hash = '#/chat'; else drawList();
      } }, '✕'))));
  }
  function bubble(role, text, model) {
    const body = h('div', { class: 'body' });
    if (role === 'user') body.textContent = text; else body.append(renderMarkdown(text));
    const el = h('div', { class: 'msg ' + role }, h('div', { class: 'who' }, role === 'user' ? 'You' : (model || 'Assistant')), body);
    inner.append(el);
    return body;
  }
  const scroll = () => { msgs.scrollTop = msgs.scrollHeight; };

  async function open(id) {
    clear(inner);
    if (!id) {
      current = null; sys.value = '';
      inner.append(h('div', { class: 'empty' }, h('h2', {}, 'What can I help with?'), h('p', {}, 'Pick a model above and start typing. Usage is billed to the same credit balance as your API keys.')));
      drawList(); return;
    }
    const d = await get('/api/conversations/' + id);
    current = d.conversation; sys.value = current.system_prompt || '';
    if (current.model && [...modelSel.options].some((o) => o.value === current.model)) modelSel.value = current.model;
    d.messages.forEach((m) => bubble(m.role, m.content, m.model));
    drawList(); scroll();
  }

  async function submit() {
    const text = input.value.trim();
    if (!text || busy) return;
    if (!modelSel.value) { toast('No model selected', true); return; }
    busy = true; send.textContent = 'Stop'; input.value = ''; input.style.height = 'auto';
    try {
      if (!current) {
        const { id } = await post('/api/conversations', { model: modelSel.value, system_prompt: sys.value || null });
        history.replaceState(null, '', '#/chat/' + id);
        const c = { id, title: 'New chat', model: modelSel.value, updated_at: Date.now() / 1000 };
        current = c; convs.unshift(c); clear(inner);
      } else if ((sys.value || null) !== (current.system_prompt || null)) {
        await patch('/api/conversations/' + current.id, { system_prompt: sys.value || null }); current.system_prompt = sys.value || null;
      }
      bubble('user', text);
      const out = bubble('assistant', '', modelSel.value); out.classList.add('cursor');
      scroll();
      abort = new AbortController();
      let acc = '', pending = false;
      const paint = () => { pending = false; mount(out, renderMarkdown(acc)); scroll(); };
      const req = { content: text, model: modelSel.value };
      if (temp.value !== '') req.temperature = Number(temp.value);
      try {
        for await (const ev of stream(`/api/conversations/${current.id}/messages`, req, abort.signal)) {
          if (ev.error) { toast(ev.error, true); break; }
          if (ev.delta) { acc += ev.delta; if (!pending) { pending = true; requestAnimationFrame(paint); } }
        }
      } catch (e) { if (e.name !== 'AbortError') throw e; }
      out.classList.remove('cursor'); paint();
      get('/api/conversations').then((r) => { convs = r.conversations; drawList(); });
      setTimeout(refreshUser, 500);
    } catch (e) { fail(e); const last = inner.lastChild; if (last && last.classList.contains('assistant') && !last.querySelector('.body').textContent) last.remove(); }
    finally { busy = false; abort = null; send.textContent = 'Send'; input.focus(); }
  }

  send.addEventListener('click', () => (busy ? abort && abort.abort() : submit()));
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); } });
  input.addEventListener('input', () => { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 220) + 'px'; });

  mount(page.parentElement === null ? page : page, h('div', { class: 'chat' },
    h('aside', { class: 'side' },
      h('div', { style: null, class: 'row' }, h('button', { class: 'primary grow', onclick: () => { location.hash = '#/chat'; } }, '+ New chat')),
      list),
    h('section', { class: 'main' },
      h('div', { class: 'head' }, modelSel, temp, sys),
      msgs,
      h('div', { class: 'composer' }, h('div', { class: 'inner' }, input, send)))));
  list.parentElement.firstChild.style.padding = '.6rem';
  await open(convId || null);
  input.focus();
}
