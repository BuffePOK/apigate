import { h, mount, fail, toast, field, table, badge, bar, fmt, modal, askText, confirmBox, copyText } from './dom.js';
import { get, post, put, patch, del } from './api.js';

const TABS = [['overview', 'Overview'], ['providers', 'Providers & keys'], ['models', 'Models'], ['limits', 'Limits'], ['routing', 'Routing & prices'], ['users', 'Users'], ['apikeys', 'API keys'], ['audit', 'Audit log']];
const METRICS = ['rps', 'rpm', 'rph', 'rpd', 'rpw', 'tps', 'tpm', 'tph', 'tpd', 'tpw', 'conc'];
const METRIC_HELP = { rps: 'requests / second', rpm: 'requests / minute', rph: 'requests / hour', rpd: 'requests / day', rpw: 'requests / week', tps: 'tokens / second', tpm: 'tokens / minute', tph: 'tokens / hour', tpd: 'tokens / day', tpw: 'tokens / week', conc: 'in-flight requests' };

export async function renderAdmin(page, arg) {
  const tab = (arg || 'overview').split('/')[0];
  const body = h('div', {});
  mount(page, h('div', { class: 'tabs' }, TABS.map(([id, l]) => h('a', { href: '#/admin/' + id, class: tab === id ? 'on' : '' }, l))), body);
  const fn = { overview, providers, models, limits, routing, users, apikeys, audit }[tab] || overview;
  await fn(body);
}

// re-run `fn` every `ms` while `el` is still on screen
function live(el, fn, ms) {
  const t = setInterval(() => { if (!el.isConnected) clearInterval(t); else fn().catch(() => {}); }, ms);
}
const act = (fn, after) => async (...a) => { try { await fn(...a); if (after) await after(); } catch (e) { fail(e); } };

function showKey(key, title = 'New API key') {
  return modal(title, h('div', { class: 'stack' }, h('p', {}, 'Copy it now — it will never be shown again.'), h('div', { class: 'keybox' }, key)),
    [{ label: 'Copy', value: 'copy' }, { label: 'Done', value: 'done', class: 'primary' }]).then((v) => { if (v === 'copy') copyText(key); });
}

// ------------------------------------------------------------------- overview
async function overview(body) {
  const draw = async () => {
    const o = await get('/api/admin/overview');
    const keys = o.providers.flatMap((p) => p.keys);
    const healthy = keys.filter((k) => k.enabled && k.status === 'ok' && !k.cooldown_s).length;
    mount(body,
      h('div', { class: 'grid c4' },
        ...[['Users', o.totals.users], ['Requests (24 h)', fmt.int(o.totals.requests_24h)], ['Credits outstanding', fmt.credits(o.totals.credits_outstanding, 2)],
          ['Provider keys healthy', `${healthy} / ${keys.length}`]].map(([l, v]) => h('div', { class: 'card stat' }, h('b', {}, v), h('span', {}, l)))),
      h('div', { class: 'card' }, h('h2', {}, 'Usage, last 24 hours'),
        table(['Provider', 'Model', 'Requests', 'Tokens in', 'Tokens out', 'Credits'], o.usage_24h.map((r) => [r.provider, r.model, fmt.int(r.requests), fmt.int(r.prompt_tokens), fmt.int(r.completion_tokens), fmt.credits(r.cost_credits, 4)]), 'No traffic yet.')),
      h('div', { class: 'card' }, h('h2', {}, 'Key health'),
        table(['Provider', 'Key', 'State', 'Models', 'Load'], o.providers.flatMap((p) => p.keys.map((k) => [p.name, k.label + ' ' + k.fingerprint, keyState(k), k.models, loadCell(k)])), 'No provider keys yet. Add some under Providers & keys.')));
  };
  await draw();
  live(body, draw, 5000);
}
function keyState(k) {
  if (!k.enabled) return badge('disabled');
  if (k.status !== 'ok') return h('span', {}, badge('auth failed', 'bad'), ' ', h('span', { class: 'small muted' }, k.note || ''));
  if (k.cooldown_s) return badge(`cooling ${k.cooldown_s}s`, 'warn');
  const mc = Object.keys(k.model_cooldowns || {}).length;
  return mc ? badge(`${mc} model(s) cooling`, 'warn') : badge('ok', 'ok');
}
function loadCell(k) {
  const top = (k.limits || []).filter((l) => l.limit > 0).map((l) => ({ ...l, r: l.used / l.limit })).sort((a, b) => b.r - a.r).slice(0, 3);
  if (!top.length) return h('span', { class: 'muted small' }, 'no limits set');
  return h('div', { class: 'limits' }, ...top.flatMap((l) => [h('span', { class: 'muted' }, `${l.scope} ${l.metric}`), bar(l.used, l.limit), h('span', { class: 'mono' }, `${fmt.int(l.used)}/${fmt.int(l.limit)}`)]));
}

// ------------------------------------------------------------------ providers
async function providers(body) {
  const typing = () => body.contains(document.activeElement) && ['INPUT', 'SELECT', 'TEXTAREA'].includes(document.activeElement.tagName);
  const draw = async () => {
    const o = await get('/api/admin/overview');
    mount(body, ...o.providers.map((p) => providerCard(p, o.strategies, draw)), addProviderCard(draw));
  };
  await draw();
  live(body, async () => { if (!typing()) await draw(); }, 8000);
}
function providerCard(p, strategies, reload) {
  const strat = h('select', {}, h('option', { value: '' }, `default (${p.effective_strategy})`), strategies.map((s) => h('option', { value: s, selected: p.strategy === s }, s)));
  const thr = h('input', { type: 'number', min: 0.05, max: 1, step: 0.05, value: p.fill_threshold ?? '', placeholder: String(p.effective_threshold), title: 'fill strategy: use a key up to this fraction of its limits before moving on' });
  thr.style.width = '5.5rem';
  const url = h('input', { type: 'text', value: p.base_url, maxlength: 300, class: 'grow mono' });
  const enabled = h('input', { type: 'checkbox', checked: p.enabled });
  const label = h('input', { type: 'text', placeholder: 'label (e.g. account-3)', maxlength: 80 });
  const secret = h('input', { type: 'password', placeholder: 'paste API key', autocomplete: 'off', spellcheck: 'false', maxlength: 1024 });
  const verify = h('input', { type: 'checkbox', checked: true });
  const note = h('div', { class: 'muted small' });
  return h('div', { class: 'card stack' },
    h('div', { class: 'row' }, h('h2', { class: 'grow' }, p.name, ' ', badge(p.kind), ' ', h('span', { class: 'muted small' }, `${p.models} model${p.models === 1 ? '' : 's'}`)),
      h('label', { class: 'check small' }, enabled, 'enabled')),
    h('div', { class: 'row' }, url,
      field('Strategy', strat), field('Fill threshold', thr),
      h('button', { onclick: act(() => patch('/api/admin/providers/' + p.name, { base_url: url.value, enabled: enabled.checked, strategy: strat.value || null, fill_threshold: thr.value === '' ? null : Number(thr.value) }).then(() => toast('Saved')), reload) }, 'Save'),
      h('button', { onclick: act(async () => { const r = await post('/api/admin/refresh-models?provider=' + p.name); const s = r.summary[p.name] || { keys_ok: 0, keys_failed: 0, models: 0 }; toast(`Models refreshed: ${s.keys_ok} key(s) ok, ${s.keys_failed} failed, ${s.models} models`); }, reload) }, 'Refresh models'),
      h('button', { class: 'danger', onclick: async () => { if (await confirmBox('Delete provider?', `"${p.name}" with all its keys, models and limit rules.`, 'Delete')) act(() => del('/api/admin/providers/' + p.name), reload)(); } }, 'Delete')),
    table(['Key', 'State', 'Models', 'Priority', 'Load', ''], p.keys.map((k) => [
      h('span', {}, k.label, ' ', h('code', { class: 'muted' }, k.fingerprint)), keyState(k), k.models,
      priorityInput(k, reload), loadCell(k),
      h('span', { class: 'row' },
        h('button', { class: 'tiny', onclick: act(() => patch('/api/admin/keys/' + k.id, { enabled: !k.enabled, reset_status: !k.enabled }), reload) }, k.enabled ? 'Disable' : 'Enable'),
        k.status !== 'ok' || k.cooldown_s ? h('button', { class: 'tiny', onclick: act(() => patch('/api/admin/keys/' + k.id, { reset_status: true }), reload) }, 'Reset') : null,
        h('button', { class: 'tiny danger', onclick: async () => { if (await confirmBox('Delete key?', `${k.label} ${k.fingerprint}`, 'Delete')) act(() => del('/api/admin/keys/' + k.id), reload)(); } }, 'Delete')),
    ]), 'No keys for this provider yet.'),
    h('div', { class: 'row' }, field('Label', label), field('API key', secret),
      h('label', { class: 'check small', title: 'Calls the provider once to check the key and list its models' }, verify, 'verify'),
      h('button', { class: 'primary', onclick: act(async () => {
        const r = await post(`/api/admin/providers/${p.name}/keys`, { label: label.value || 'key', secret: secret.value, verify: verify.checked });
        secret.value = ''; label.value = ''; toast(`Key added${r.models != null ? ` — ${r.models} models visible` : ''}`);
      }, reload) }, 'Add key')),
    note);
}
function priorityInput(k, reload) {
  const i = h('input', { type: 'number', min: 0, value: k.priority, title: 'lower = preferred (used by "fill" ordering)' });
  i.style.width = '5rem';
  i.addEventListener('change', act(() => patch('/api/admin/keys/' + k.id, { priority: Number(i.value) }), reload));
  return i;
}
function addProviderCard(reload) {
  const name = h('input', { type: 'text', placeholder: 'e.g. my-vllm', maxlength: 32 });
  const kind = h('select', {}, ['openai', 'gemini', 'anthropic'].map((k) => h('option', { value: k }, k === 'openai' ? 'openai (any OpenAI-compatible API)' : k)));
  const url = h('input', { type: 'text', placeholder: 'https://host/v1', maxlength: 300 });
  return h('div', { class: 'card stack' }, h('h3', {}, 'Add a custom provider'),
    h('p', { class: 'muted small' }, 'Any OpenAI-compatible endpoint (vLLM, Ollama, LM Studio, a regional provider…). http:// is only accepted for loopback addresses.'),
    h('div', { class: 'row' }, field('Name', name), field('Kind', kind), h('div', { class: 'grow' }, field('Base URL', url)),
      h('div', {}, h('div', { class: 'small' }, ' '), h('button', { onclick: act(() => post('/api/admin/providers', { name: name.value.trim(), kind: kind.value, base_url: url.value.trim() }).then(() => toast('Provider added')), reload) }, 'Add'))));
}

// --------------------------------------------------------------------- models
async function models(body) {
  const q = h('input', { type: 'search', placeholder: 'filter…' });
  const kindSel = h('select', {}, ['all', 'chat', 'embedding', 'image', 'audio', 'video', 'moderation', 'other'].map((k) => h('option', { value: k }, k)));
  const list = h('div', {});
  let data = [];
  const draw = () => {
    const f = q.value.toLowerCase();
    const rows = data.filter((m) => (kindSel.value === 'all' || m.kind === kindSel.value) && `${m.provider}/${m.id}`.toLowerCase().includes(f));
    mount(list, table(['Model', 'Kind', 'Keys', 'Context', 'Price in / out', 'Servable', 'Enabled', ''], rows.slice(0, 400).map((m) => [
      h('span', {}, h('span', { class: 'muted' }, m.provider + '/'), m.id),
      badge(m.kind), m.keys, m.context_window ? fmt.int(m.context_window) : '—', `${m.price_in} / ${m.price_out}`,
      m.enabled ? (m.servable ? badge('yes', 'ok') : badge('no usable key', 'warn')) : '',
      h('input', { type: 'checkbox', checked: m.enabled, disabled: !['chat', 'embedding'].includes(m.kind), title: ['chat', 'embedding'].includes(m.kind) ? '' : 'Only chat and embedding models can be routed', onchange: act((e) => patch(`/api/admin/models/${m.provider}/${m.id}`, { enabled: !m.enabled }), load) }),
      m.manual ? h('button', { class: 'tiny danger', onclick: act(() => del(`/api/admin/models/${m.provider}/${m.id}`), load) }, 'Remove') : '',
    ]), 'No models yet — add provider keys first.'), rows.length > 400 ? h('p', { class: 'muted small' }, `Showing 400 of ${rows.length}; narrow the filter.`) : null);
  };
  const load = async () => { data = (await get('/api/admin/models')).models; draw(); };
  q.addEventListener('input', draw); kindSel.addEventListener('change', draw);
  const mp = h('input', { type: 'text', placeholder: 'provider' }), mi = h('input', { type: 'text', placeholder: 'model id', class: 'mono' });
  mp.style.width = '9rem';
  mount(body, h('div', { class: 'card stack' },
    h('div', { class: 'row' }, h('h2', { class: 'grow' }, 'Models'), q, kindSel,
      h('button', { onclick: act(async () => { toast('Refreshing…'); await post('/api/admin/refresh-models'); }, load) }, 'Refresh all')),
    list,
    h('details', {}, h('summary', {}, 'Add a model that discovery does not list'),
      h('div', { class: 'row' }, mp, mi, h('button', { onclick: act(() => post('/api/admin/models', { provider: mp.value.trim(), model_id: mi.value.trim(), kind: 'chat' }), load) }, 'Add chat model')))));
  await load();
}

// --------------------------------------------------------------------- limits
async function limits(body) {
  const draw = async () => {
    const [{ rules }, o] = await Promise.all([get('/api/admin/limits'), get('/api/admin/overview')]);
    const keyById = Object.fromEntries(o.providers.flatMap((p) => p.keys).map((k) => [k.id, k]));
    const prov = h('select', {}, o.providers.map((p) => h('option', { value: p.name }, p.name)));
    const keySel = h('select', {});
    const fillKeys = () => { mount(keySel, h('option', { value: '' }, 'every key of the provider'), (o.providers.find((p) => p.name === prov.value)?.keys || []).map((k) => h('option', { value: k.id }, `${k.label} ${k.fingerprint}`))); };
    prov.addEventListener('change', fillKeys); fillKeys();
    const pattern = h('input', { type: 'text', value: '*', class: 'mono', maxlength: 200 });
    const scope = h('select', {}, h('option', { value: 'model' }, 'each model separately'), h('option', { value: 'key' }, 'shared by all matching models'));
    const inputs = Object.fromEntries(METRICS.map((m) => { const i = h('input', { type: 'number', min: 0, placeholder: '∞', title: METRIC_HELP[m] }); i.style.width = '6.5rem'; return [m, i]; }));
    const note = h('input', { type: 'text', maxlength: 200, placeholder: 'note (optional)' });
    let editing = null;
    const save = h('button', { class: 'primary', onclick: act(async () => {
      const lim = {}; METRICS.forEach((m) => { if (inputs[m].value !== '') lim[m] = Number(inputs[m].value); });
      await put('/api/admin/limits', { id: editing, provider: prov.value, key_id: keySel.value ? Number(keySel.value) : null, pattern: pattern.value, scope: scope.value, limits: lim, note: note.value });
      toast('Rule saved');
    }, draw) }, 'Save rule');
    const edit = (r) => { editing = r.id; prov.value = r.provider; fillKeys(); keySel.value = r.key_id ?? ''; pattern.value = r.pattern; scope.value = r.scope; note.value = r.note || ''; METRICS.forEach((m) => { inputs[m].value = r.limits[m] ?? ''; }); save.textContent = `Update rule #${r.id}`; window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' }); };
    mount(body,
      h('div', { class: 'card stack' }, h('h2', {}, 'Limit rules'),
        h('p', { class: 'muted small' }, 'For each metric the most specific matching rule wins (a rule for one key beats a provider-wide rule; an exact model name beats a glob beats *). A value of 0 means “this key cannot serve this model”, so the model is skipped for that key. Empty = unlimited. Limits are enforced per key before a request is sent, and kept for a week across restarts.'),
        table(['#', 'Provider', 'Key', 'Model pattern', 'Scope', 'Limits', ''], rules.map((r) => [r.id, r.provider, r.key_id ? (keyById[r.key_id]?.label || '#' + r.key_id) : 'all', h('code', {}, r.pattern), r.scope === 'key' ? 'shared' : 'per model',
          h('span', { class: 'mono small' }, Object.entries(r.limits).map(([k, v]) => `${k}=${fmt.int(v)}`).join('  ')),
          h('span', { class: 'row' }, h('button', { class: 'tiny', onclick: () => edit(r) }, 'Edit'), h('button', { class: 'tiny danger', onclick: act(() => del('/api/admin/limits/' + r.id), draw) }, 'Delete'))]), 'No rules yet: nothing is limited on our side (upstream 429s still trigger cooldowns).')),
      h('div', { class: 'card stack' }, h('h3', {}, 'Add or edit a rule'),
        h('div', { class: 'row' }, field('Provider', prov), field('Key', keySel), field('Model pattern', pattern), field('Counter scope', scope)),
        h('div', { class: 'row' }, ...METRICS.map((m) => field(m, inputs[m]))), h('div', { class: 'row' }, h('div', { class: 'grow' }, note), save)));
  };
  await draw();
}

// -------------------------------------------------------------------- routing
async function routing(body) {
  const draw = async () => {
    const [{ settings, strategies }, { aliases }, { prices, default: dp }, o] = await Promise.all([get('/api/admin/settings'), get('/api/admin/aliases'), get('/api/admin/prices'), get('/api/admin/overview')]);
    const setting = (name, label, type = 'number', help = '') => {
      const el = type === 'bool' ? h('input', { type: 'checkbox', checked: !!settings[name] }) : type === 'strategy' ? h('select', {}, strategies.map((s) => h('option', { value: s, selected: settings[name] === s }, s))) : h('input', { type: 'number', step: 'any', min: 0, value: settings[name] });
      el.addEventListener('change', act(() => put('/api/admin/settings/' + name, { value: type === 'bool' ? el.checked : type === 'strategy' ? el.value : Number(el.value) }).then(() => toast('Saved'))));
      return h('label', { class: 'field' }, label, el, help ? h('span', { class: 'small' }, help) : null);
    };
    const an = h('input', { type: 'text', placeholder: 'alias, e.g. smart', maxlength: 64 });
    const at = h('textarea', { rows: 3, placeholder: 'provider/model, one per line', class: 'mono' });
    const am = h('select', {}, ['fallback', 'round_robin', 'random'].map((m) => h('option', { value: m }, m)));
    const pp = h('select', {}, h('option', { value: '*' }, 'any provider'), o.providers.map((p) => h('option', { value: p.name }, p.name)));
    const pg = h('input', { type: 'text', value: '*', class: 'mono' });
    const pi = h('input', { type: 'number', min: 0, step: 'any', placeholder: 'in' }), po = h('input', { type: 'number', min: 0, step: 'any', placeholder: 'out' });
    mount(body,
      h('div', { class: 'card stack' }, h('h2', {}, 'Routing & access'),
        h('div', { class: 'grid c4' },
          setting('default_strategy', 'Default key strategy', 'strategy', 'per-provider override on the Providers tab'),
          setting('fill_threshold', 'Default fill threshold', 'number', 'fill: stay on one key until this share of its limits'),
          setting('allow_signup', 'Allow self sign-up', 'bool', 'new accounts start with 0 credits'),
          setting('max_attempts', 'Max keys tried per request'),
          setting('max_queue_wait_s', 'Wait for a free slot (s)', 'number', 'instead of failing immediately with 429'),
          setting('assumed_output_tokens', 'Assumed output tokens', 'number', 'for token-limit reservations without max_tokens'),
          setting('default_max_tokens', 'Credit hold without max_tokens'))),
      h('div', { class: 'card stack' }, h('h2', {}, 'Aliases'),
        h('p', { class: 'muted small' }, 'A virtual model name that maps to several real models. fallback = try in order; round_robin / random = spread across them.'),
        table(['Alias', 'Mode', 'Targets', ''], aliases.map((a) => [h('code', {}, a.name), a.mode, h('span', { class: 'mono small' }, a.targets.join(', ')), h('button', { class: 'tiny danger', onclick: act(() => del('/api/admin/aliases/' + a.name), draw) }, 'Delete')]), 'No aliases.'),
        h('div', { class: 'row' }, field('Name', an), field('Mode', am), h('div', { class: 'grow' }, field('Targets', at)), h('div', {}, h('div', { class: 'small' }, ' '),
          h('button', { class: 'primary', onclick: act(() => put('/api/admin/aliases/' + an.value.trim(), { targets: at.value.split('\n').map((s) => s.trim()).filter(Boolean), mode: am.value }).then(() => toast('Alias saved')), draw) }, 'Save alias')))),
      h('div', { class: 'card stack' }, h('h2', {}, 'Prices (credits per 1M tokens)'),
        h('p', { class: 'muted small' }, `Default: ${dp.price_in} in / ${dp.price_out} out. Most specific rule wins. These are placeholders until you decide what a credit is worth.`),
        table(['Provider', 'Model pattern', 'In', 'Out', ''], prices.map((p) => [p.provider, h('code', {}, p.pattern), p.price_in, p.price_out, h('button', { class: 'tiny danger', onclick: act(() => del('/api/admin/prices/' + p.id), draw) }, 'Delete')]), 'Using the defaults everywhere.'),
        h('div', { class: 'row' }, field('Provider', pp), field('Model pattern', pg), field('In', pi), field('Out', po), h('div', {}, h('div', { class: 'small' }, ' '),
          h('button', { class: 'primary', onclick: act(() => put('/api/admin/prices', { provider: pp.value, pattern: pg.value, price_in: Number(pi.value), price_out: Number(po.value) }).then(() => toast('Price saved')), draw) }, 'Save price')))));
  };
  await draw();
}

// ---------------------------------------------------------------------- users
async function users(body) {
  const draw = async () => {
    const { users: us } = await get('/api/admin/users');
    const un = h('input', { type: 'text', maxlength: 64, autocomplete: 'off' }), up = h('input', { type: 'password', autocomplete: 'new-password', maxlength: 256 });
    const ur = h('select', {}, h('option', { value: 'user' }, 'user'), h('option', { value: 'admin' }, 'admin'));
    const uc = h('input', { type: 'number', min: 0, step: 'any', value: 0 }); uc.style.width = '7rem';
    mount(body,
      h('div', { class: 'card stack' }, h('h2', {}, 'Users'),
        table(['User', 'Role', 'Status', 'Credits', 'Last login', ''], us.map((u) => [
          u.username, badge(u.role, u.role === 'admin' ? 'warn' : ''), u.active ? badge('active', 'ok') : badge('disabled', 'bad'),
          u.unlimited ? badge('unlimited') : fmt.credits(u.balance_credits, 6), fmt.ago(u.last_login_at),
          h('span', { class: 'row' },
            h('button', { class: 'tiny', onclick: () => grant(u, draw) }, 'Credits…'),
            h('button', { class: 'tiny', onclick: () => issueKey(u) }, 'New API key…'),
            h('button', { class: 'tiny', onclick: () => history(u) }, 'History'),
            h('button', { class: 'tiny', onclick: act(() => patch('/api/admin/users/' + u.id, { unlimited: !u.unlimited }), draw) }, u.unlimited ? 'Meter' : 'Unlimited'),
            h('button', { class: 'tiny', onclick: async () => { const p = await askText('Reset password', `New password for ${u.username} (all their sessions are signed out)`, { type: 'password' }); if (p) act(() => patch('/api/admin/users/' + u.id, { password: p }).then(() => toast('Password reset')))(); } }, 'Password…'),
            h('button', { class: 'tiny ' + (u.active ? 'danger' : ''), onclick: act(() => patch('/api/admin/users/' + u.id, { active: !u.active }), draw) }, u.active ? 'Disable' : 'Enable')),
        ]))),
      h('div', { class: 'card stack' }, h('h3', {}, 'Create a user'),
        h('div', { class: 'row' }, field('Username', un), field('Password (min 12)', up), field('Role', ur), field('Initial credits', uc),
          h('div', {}, h('div', { class: 'small' }, ' '), h('button', { class: 'primary', onclick: act(() => post('/api/admin/users', { username: un.value.trim(), password: up.value, role: ur.value, initial_credits: Number(uc.value) || 0 }).then(() => toast('User created')), draw) }, 'Create')))));
  };
  await draw();
}
async function grant(u, reload) {
  const amt = h('input', { type: 'number', step: 'any', value: 10 }), note = h('input', { type: 'text', maxlength: 300, placeholder: 'reason (optional)' });
  const r = await modal(`Credits for ${u.username}`, h('div', { class: 'stack' }, h('p', { class: 'muted small' }, `Balance: ${fmt.credits(u.balance_credits, 6)}. Use a negative number to take credits away.`), field('Credits to add', amt), field('Note', note)),
    [{ label: 'Cancel', value: null }, { label: 'Apply', value: true, class: 'primary' }]);
  if (r) act(async () => { const x = await post(`/api/admin/users/${u.id}/credits`, { credits: Number(amt.value), note: note.value }); toast(`New balance: ${fmt.credits(x.balance_credits, 6)}`); }, reload)();
}
async function issueKey(u) {
  const name = h('input', { type: 'text', maxlength: 80, value: 'issued by admin' }), rpm = h('input', { type: 'number', min: 1, placeholder: 'default' }), models = h('input', { type: 'text', placeholder: 'e.g. gemini/*, deepseek/deepseek-chat (blank = all)' });
  const r = await modal(`API key for ${u.username}`, h('div', { class: 'stack' }, field('Name', name), field('Requests / minute cap', rpm), field('Allowed models (comma-separated globs)', models)),
    [{ label: 'Cancel', value: null }, { label: 'Create', value: true, class: 'primary' }]);
  if (!r) return;
  try {
    const allowed = models.value.split(',').map((s) => s.trim()).filter(Boolean);
    const x = await post(`/api/admin/users/${u.id}/api-keys`, { name: name.value, rpm: rpm.value ? Number(rpm.value) : null, allowed_models: allowed.length ? allowed : null });
    await showKey(x.key, `API key for ${u.username}`);
  } catch (e) { fail(e); }
}
async function history(u) {
  const { entries } = await get(`/api/admin/users/${u.id}/ledger?limit=100`);
  await modal(`Ledger — ${u.username}`, table(['When', 'Kind', 'What', 'Δ credits', 'Balance'], entries.map((e) => [fmt.time(e.ts), e.kind, e.model ? `${e.provider}/${e.model} (${e.source})` : e.note || '', fmt.credits(e.delta_credits, 6), fmt.credits(e.balance_credits, 4)]), 'Nothing yet.'), [{ label: 'Close', value: true, class: 'primary' }]);
}

// ------------------------------------------------------------------- api keys
async function apikeys(body) {
  const draw = async () => {
    const { keys } = await get('/api/admin/api-keys');
    mount(body, h('div', { class: 'card stack' }, h('h2', {}, 'All API keys'),
      table(['User', 'Name', 'Key', 'Created', 'Last used', 'Limits', 'Status', ''], keys.map((k) => [
        k.username, k.name, h('code', {}, k.prefix + '…'), fmt.time(k.created_at), fmt.ago(k.last_used_at),
        h('span', { class: 'small muted' }, [k.rpm ? `${k.rpm} rpm` : '', k.allowed_models ? k.allowed_models.join(', ') : ''].filter(Boolean).join(' · ') || '—'),
        k.revoked_at ? badge('revoked', 'bad') : badge('active', 'ok'),
        k.revoked_at ? '' : h('button', { class: 'tiny danger', onclick: async () => { if (await confirmBox('Revoke key?', `${k.username} / ${k.name}`, 'Revoke')) act(() => del('/api/admin/api-keys/' + k.id), draw)(); } }, 'Revoke'),
      ]), 'No API keys yet.')));
  };
  await draw();
}

// ----------------------------------------------------------------------- audit
async function audit(body) {
  const { entries } = await get('/api/admin/audit?limit=300');
  mount(body, h('div', { class: 'card stack' }, h('h2', {}, 'Audit log'),
    table(['When', 'Who', 'Action', 'Target', 'IP', 'Detail'], entries.map((e) => [fmt.time(e.ts), e.actor || 'cli', h('code', {}, e.action), e.target, e.ip, h('span', { class: 'small muted' }, e.detail)]), 'Empty.')));
}
