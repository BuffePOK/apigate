import { h, mount, toast, fail, field } from './dom.js';
import { get, post, setCsrf } from './api.js';
import { fmt } from './dom.js';
import { renderChat } from './chat.js';
import { renderAccount } from './account.js';
import { renderAdmin } from './admin.js';

const root = document.getElementById('app');
export const state = { user: null, publicInfo: { signup_enabled: false } };

async function boot() {
  try { state.publicInfo = await get('/api/public'); } catch { /* ignore */ }
  try {
    const me = await get('/api/auth/me');
    state.user = me.user; setCsrf(me.csrf);
  } catch { state.user = null; }
  route();
}

window.addEventListener('apigate:logout', () => { if (state.user) { state.user = null; setCsrf(null); route(); } });
window.addEventListener('hashchange', route);

export async function refreshUser() {
  try { state.user = (await get('/api/auth/me')).user; const b = document.getElementById('balance'); if (b) b.textContent = balanceText(); } catch { /* ignore */ }
}
const balanceText = () => (state.user.unlimited ? '∞ credits' : fmt.credits(state.user.balance_credits) + ' credits');

function route() {
  if (!state.user) return renderLogin();
  const [, section = 'chat', ...rest] = location.hash.replace(/^#/, '').split('/');
  const page = h('div', { id: 'page' });
  const isChat = section === 'chat';
  mount(root, topbar(section), isChat ? page : h('main', { class: 'page' }, page));
  const arg = rest.join('/');
  const run = section === 'account' ? renderAccount : section === 'admin' && state.user.role === 'admin' ? renderAdmin : renderChat;
  Promise.resolve(run(page, arg)).catch(fail);
}

function topbar(section) {
  const link = (id, label) => h('a', { href: '#/' + id, class: section === id ? 'on' : '' }, label);
  return h('header', { class: 'top' },
    h('div', { class: 'brand' }, h('i', {}), 'apigate'),
    h('nav', { class: 'nav' }, link('chat', 'Chat'), link('account', 'Account & API keys'), state.user.role === 'admin' ? link('admin', 'Admin') : null),
    h('div', { class: 'grow' }),
    h('span', { class: 'pill', id: 'balance', title: 'Shared by the web chat and the API' }, balanceText()),
    h('span', { class: 'muted small' }, state.user.username),
    h('button', { class: 'tiny', onclick: async () => { try { await post('/api/auth/logout'); } catch { /* */ } state.user = null; setCsrf(null); route(); } }, 'Sign out'));
}

function renderLogin() {
  let signup = false;
  const err = h('div', { class: 'err', role: 'alert' });
  const user = h('input', { type: 'text', autocomplete: 'username', required: true, maxlength: 64 });
  const pass = h('input', { type: 'password', autocomplete: 'current-password', required: true, maxlength: 256 });
  const btn = h('button', { class: 'primary', type: 'submit' }, 'Sign in');
  const toggle = h('button', { class: 'link', type: 'button', onclick: () => { signup = !signup; btn.textContent = signup ? 'Create account' : 'Sign in'; toggle.textContent = signup ? 'I already have an account' : 'Create an account'; pass.autocomplete = signup ? 'new-password' : 'current-password'; } }, 'Create an account');
  const form = h('form', { class: 'card stack', onsubmit: async (e) => {
    e.preventDefault(); err.textContent = ''; btn.disabled = true;
    try {
      const r = await post(signup ? '/api/auth/signup' : '/api/auth/login', { username: user.value.trim(), password: pass.value });
      state.user = r.user; setCsrf(r.csrf); pass.value = ''; location.hash = '#/chat'; route();
    } catch (ex) { err.textContent = ex.message; } finally { btn.disabled = false; }
  } }, h('h1', {}, 'Sign in'), field('Username', user), field('Password', pass), err, btn,
  state.publicInfo.signup_enabled ? toggle : null);
  mount(root, h('div', { class: 'login' }, h('div', { class: 'brand', style: null }, h('i', {}), 'apigate'), h('p', { class: 'muted' }, 'One OpenAI-compatible API over many models.'), form));
  user.focus();
}

boot();
