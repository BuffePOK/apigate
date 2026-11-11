import { h, mount, fail, toast, field, table, badge, fmt, copyText, confirmBox, askText } from './dom.js';
import { get, post, del } from './api.js';
import { state } from './main.js';

export async function renderAccount(page) {
  const keysBox = h('div', {});
  const ledgerBox = h('div', {});
  const fresh = h('div', {});

  async function loadKeys() {
    const { keys } = await get('/api/me/api-keys');
    mount(keysBox, table(['Name', 'Key', 'Created', 'Last used', 'Status', ''], keys.map((k) => [
      k.name, h('code', {}, k.prefix + '…'), fmt.time(k.created_at), fmt.ago(k.last_used_at),
      k.revoked_at ? badge('revoked', 'bad') : (k.expires_at && k.expires_at < Date.now() / 1000 ? badge('expired', 'warn') : badge('active', 'ok')),
      k.revoked_at ? '' : h('button', { class: 'tiny danger', onclick: async () => { if (await confirmBox('Revoke this key?', `"${k.name}" will stop working immediately.`, 'Revoke')) { await del('/api/me/api-keys/' + k.id); loadKeys(); } } }, 'Revoke'),
    ]), 'You have no API keys yet.'));
  }
  let lastId = null, rows = [];
  async function loadLedger(more) {
    const r = await get('/api/me/ledger?limit=30' + (more && lastId ? '&before=' + lastId : ''));
    if (!more) rows = [];
    rows = rows.concat(r.entries); lastId = rows.length ? rows[rows.length - 1].id : null;
    mount(ledgerBox, table(['When', 'What', 'Model', 'Tokens in/out', 'Credits', 'Balance'], rows.map((e) => [
      fmt.time(e.ts),
      e.kind === 'usage' ? badge(e.source === 'chat' ? 'chat' : 'api') : badge(e.kind, e.delta_credits >= 0 ? 'ok' : 'warn'),
      e.model ? `${e.provider}/${e.model}` : (e.note || ''),
      e.kind === 'usage' ? `${fmt.int(e.prompt_tokens)} / ${fmt.int(e.completion_tokens)}` : '',
      h('span', { class: e.delta_credits < 0 ? '' : 'muted' }, (e.delta_credits > 0 ? '+' : '') + fmt.credits(e.delta_credits, 6)),
      fmt.credits(e.balance_credits, 4),
    ]), 'No activity yet.'), r.entries.length === 30 ? h('button', { onclick: () => loadLedger(true).catch(fail) }, 'Load more') : null);
  }

  const name = h('input', { type: 'text', maxlength: 80, placeholder: 'e.g. my laptop' });
  const days = h('input', { type: 'number', min: 1, max: 3650, placeholder: 'never' });
  const create = h('button', { class: 'primary', onclick: async () => {
    try {
      const r = await post('/api/me/api-keys', { name: name.value.trim(), expires_days: days.value ? Number(days.value) : null });
      name.value = '';
      mount(fresh, h('div', { class: 'callout stack' }, h('b', {}, 'Your new API key — copy it now, it will not be shown again.'),
        h('div', { class: 'keybox' }, r.key), h('button', { onclick: () => copyText(r.key) }, 'Copy')));
      loadKeys();
    } catch (e) { fail(e); }
  } }, 'Create key');

  const base = location.origin + '/v1';
  const pw = { cur: h('input', { type: 'password', autocomplete: 'current-password', maxlength: 256 }), nw: h('input', { type: 'password', autocomplete: 'new-password', maxlength: 256 }) };

  mount(page,
    h('div', { class: 'grid c2' },
      h('div', { class: 'card stack' }, h('h2', {}, 'Credits'),
        h('div', { class: 'stat' }, h('b', {}, state.user.unlimited ? '∞' : fmt.credits(state.user.balance_credits)), h('span', {}, 'credits — one balance shared by the web chat and every API key you own')),
        h('p', { class: 'muted small' }, 'Credits are added by an administrator.')),
      h('div', { class: 'card stack' }, h('h2', {}, 'Use the API'),
        h('p', { class: 'small muted' }, 'Any OpenAI-compatible client works. Base URL:'), h('div', { class: 'keybox' }, base),
        h('pre', { class: 'snippet' }, `curl ${base}/chat/completions \\\n  -H "Authorization: Bearer sk-gate-…" \\\n  -H "Content-Type: application/json" \\\n  -d '{"model":"MODEL_ID","messages":[{"role":"user","content":"Hello"}]}'`))),
    h('div', { class: 'card stack' }, h('h2', {}, 'API keys'), fresh,
      h('div', { class: 'row' }, field('Name', name), field('Expires in (days)', days), h('div', {}, h('div', { class: 'small' }, ' '), create)), keysBox),
    h('div', { class: 'card stack' }, h('h2', {}, 'Usage & credit history'), ledgerBox),
    h('div', { class: 'card stack' }, h('h2', {}, 'Change password'),
      h('div', { class: 'row' }, field('Current password', pw.cur), field('New password', pw.nw),
        h('div', {}, h('div', { class: 'small' }, ' '), h('button', { onclick: async () => {
          try { await post('/api/auth/password', { current: pw.cur.value, new: pw.nw.value }); pw.cur.value = pw.nw.value = ''; toast('Password changed. Other sessions were signed out.'); } catch (e) { fail(e); }
        } }, 'Change')))));
  await Promise.all([loadKeys(), loadLedger(false)]);
}
