/* AutoTrader Watch - the dashboard.
 *
 * No framework and no build step: the bot publishes this page without node,
 * and every dependency is one more thing that can be down on a phone.
 *
 * Everything here reads docs/data.json, which the bot rewrites after every
 * check. Anything the bot can compute once is computed there (see
 * autotrader/insight.py), because only the bot has the whole history.
 */
'use strict';

const VIEWS = [
  { id: 'feed', label: 'Feed' },
  { id: 'listings', label: 'Listings' },
  { id: 'market', label: 'Market' },
  { id: 'searches', label: 'Searches' },
  { id: 'status', label: 'Status' },
];

const KIND = {
  new:        { label: 'New',        group: 'New to the market',  flag: 'new',    rule: 'Appeared on the site' },
  price_drop: { label: 'Price drop', group: 'Price drops',        flag: 'drop',   rule: 'Asking price came down' },
  price_rise: { label: 'Price rise', group: 'Price rises',        flag: 'rise',   rule: 'Asking price went up' },
  priced:     { label: 'Now priced', group: 'Now priced',         flag: 'priced', rule: 'Call-for-price car named a figure' },
  removed:    { label: 'Gone',       group: 'Gone from the site', flag: 'gone',   rule: 'No longer on the site' },
  relisted:   { label: 'Back',       group: 'Back on the market', flag: 'back',   rule: 'Listed again after going' },
  qualified:  { label: 'In range',   group: 'Back inside your rules', flag: 'back',
                rule: 'A rule of yours stopped hiding it' },
  photos:     { label: 'Photos',     group: 'Can be looked at now', flag: 'new',
                rule: 'First photos on a listing that had none' },
  seller:     { label: 'Seller',     group: 'Changed hands', flag: 'back',
                rule: 'Moved between a private seller and a dealer' },
};
// "qualified" ranks ahead of "new": a car crossing back into your rules is
// announced once, while a new listing will still be there tomorrow.
const KIND_ORDER = ['price_drop', 'qualified', 'new', 'priced', 'price_rise',
                    'relisted', 'seller', 'photos', 'removed'];

/* No sort ranks a missing value as the worst value: a "call for price" car is
   not the most expensive one. Rows with nothing to rank are set apart and
   labelled instead (see ranked()). */
const SORTS = [
  { id: 'newest',   label: 'Newest first',       absent: 'no first-seen date',
    get: l => { const t = Date.parse(l.first_seen); return Number.isFinite(t) ? -t : null; } },
  { id: 'price',    label: 'Asking, low first',  absent: 'no asking price',
    get: l => l.price ?? null },
  { id: 'priced',   label: 'Asking, high first', absent: 'no asking price',
    get: l => l.price == null ? null : -l.price },
  // No "price per 1,000 km" sort: it ranks by odometer with a dollar sign on
  // it. A "best value" sort would rank by comparables.pct, which only makes
  // sense once most cars carry one.
  { id: 'year',     label: 'Newest year',        absent: 'no model year',
    get: l => l.year ? -l.year : null },
  { id: 'km',       label: 'Lowest odometer',    absent: 'no odometer reading',
    get: l => l.mileage_km ?? null },
  { id: 'distance', label: 'Closest',            absent: 'no distance from you',
    get: l => l.distance_km ?? null },
  { id: 'days',     label: 'Longest listed',     absent: 'no first-seen date',
    get: l => l.days_listed == null ? null : -l.days_listed },
];

/* Split, then sort. A row whose value the sort cannot read sits outside the
   ranking, not at its bottom. That tail is ordered newest-first, which makes
   no claim about the metric that could not be read. */
function ranked(rows, sort) {
  const has = [], absent = [];
  for (const l of rows) (sort.get(l) == null ? absent : has).push(l);
  has.sort((a, b) => {
    const x = sort.get(a), y = sort.get(b);
    return x === y ? 0 : (x < y ? -1 : 1);
  });
  absent.sort((a, b) => (Date.parse(b.first_seen) || 0) - (Date.parse(a.first_seen) || 0));
  return { has, absent };
}

/* Everything this page keeps in the browser. Keys are namespaced by the
   site's path: every project site of a GitHub account shares one origin.
   Until the owner chooses to keep this device unlocked, nothing outlives
   the tab - it goes to sessionStorage. */
const NS = `atw:${location.pathname.replace(/[^/]*$/, '')}:`;
const store = {
  persist: true,
  get(k, d) {
    try {
      const v = sessionStorage.getItem(NS + k) ?? localStorage.getItem(NS + k);
      return v === null ? d : JSON.parse(v);
    } catch { return d; }
  },
  set(k, v) {
    try {
      (store.persist ? localStorage : sessionStorage).setItem(NS + k, JSON.stringify(v));
    } catch { /* storage blocked */ }
  },
  forget() {
    for (const area of [localStorage, sessionStorage]) {
      try {
        for (const k of Object.keys(area)) if (k.startsWith(NS)) area.removeItem(k);
      } catch { /* storage blocked */ }
    }
  },
};
// Unprefixed keys from before the namespace: only this page ever wrote them.
try {
  for (const k of ['seenIds', 'marks', 'lastSeen', 'lastTrust', 'welcomed', 'vaultKey']) {
    localStorage.removeItem(k);
  }
} catch { /* storage blocked */ }

const app = {
  data: null,
  view: 'feed',
  offline: false,
  loadError: null,
  q: '',
  sort: 'newest',
  chip: 'all',
  search: 'all',
  showHidden: false,
  // Feed slice: everything that happened, or only what reached a phone. Not
  // persisted, like every filter here, so each visit starts unfiltered.
  feedOnly: 'all',
  lastSeen: store.get('lastSeen', null),
  seenIds: new Set(store.get('seenIds', [])),
  freshIds: new Set(),
  draft: null,
};

/* ------------------------------------------------------------------ vault
   The published site carries only ciphertext: data.enc, and photos under
   hashed names. lock.json says how to turn a passphrase into the key
   (PBKDF2-SHA256), and holds a check value that proves a key right. The
   format is autotrader/vault.py's, read here with WebCrypto. A site without
   lock.json is a local, unencrypted build and reads data.json as it is. */
const vault = { lock: null, enc: null, name: null, photos: new Map() };
const te = new TextEncoder();
const fromB64 = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));
const toB64 = bytes => btoa(String.fromCharCode(...new Uint8Array(bytes)));
const hex = buf => [...new Uint8Array(buf)]
  .map(b => b.toString(16).padStart(2, '0')).join('');
const VAULT_KEY = 'vaultKey';

const hmacKey = raw => crypto.subtle.importKey(
  'raw', raw, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);

async function deriveMaster(passphrase, lock) {
  const base = await crypto.subtle.importKey(
    'raw', te.encode(passphrase), 'PBKDF2', false, ['deriveBits']);
  return new Uint8Array(await crypto.subtle.deriveBits(
    { name: 'PBKDF2', hash: 'SHA-256', salt: fromB64(lock.kdf.salt),
      iterations: lock.kdf.iterations }, base, 256));
}

async function unsealBytes(key, buf, name) {
  const b = new Uint8Array(buf);
  if (b.length < 33 || b[0] !== 0x41 || b[1] !== 0x54 || b[2] !== 0x57 || b[3] !== 1) {
    throw new Error('not a vault file');
  }
  let body = new Uint8Array(await crypto.subtle.decrypt(
    { name: 'AES-GCM', iv: b.slice(5, 17), additionalData: te.encode(name) },
    key, b.slice(17)));
  // Padded to a size bucket: a length prefix, the data, then zeros.
  if (b[4] & 2) {
    const used = new DataView(body.buffer).getUint32(0);
    if (used > body.length - 4) throw new Error('damaged padding');
    body = body.slice(4, 4 + used);
  }
  if (!(b[4] & 1)) return body.buffer;
  const stream = new Blob([body]).stream().pipeThrough(new DecompressionStream('gzip'));
  return new Response(stream).arrayBuffer();
}

async function sealBytes(key, bytes, name) {
  const nonce = crypto.getRandomValues(new Uint8Array(12));
  const ct = new Uint8Array(await crypto.subtle.encrypt(
    { name: 'AES-GCM', iv: nonce, additionalData: te.encode(name) }, key, bytes));
  const out = new Uint8Array(17 + ct.length);
  out.set([0x41, 0x54, 0x57, 1, 0]);
  out.set(nonce, 5);
  out.set(ct, 17);
  return out;
}

/* Opens the vault with a master key, or throws. Nothing is kept unless the
   check value decrypts, so a wrong passphrase leaves no trace. */
async function openVault(master) {
  const m = await hmacKey(master);
  const encRaw = await crypto.subtle.sign('HMAC', m, te.encode('autotrader-vault/enc'));
  const nameRaw = await crypto.subtle.sign('HMAC', m, te.encode('autotrader-vault/name'));
  const enc = await crypto.subtle.importKey(
    'raw', encRaw, 'AES-GCM', false, ['encrypt', 'decrypt']);
  const check = await unsealBytes(enc, fromB64(vault.lock.check), 'check');
  if (new TextDecoder().decode(check) !== 'autotrader-vault-check') throw new Error('wrong key');
  vault.enc = enc;
  vault.name = await hmacKey(nameRaw);
}

async function fetchLock() {
  try {
    const res = await fetch('lock.json', { cache: 'no-cache' });
    if (res.ok) return await res.json();
    if (res.status === 404) return null;
  } catch { /* offline: fall back to the worker's copy */ }
  try {
    const hit = await caches.match('lock.json');
    if (hit) return await hit.json();
  } catch { /* no cache API */ }
  return null;
}

/* The key saved on this device, if it still opens this vault. A new
   passphrase means a new salt, and the saved key simply stops working. */
async function savedKey() {
  store.persist = false;
  const saved = store.get(VAULT_KEY, null);
  if (!saved || saved.salt !== vault.lock.kdf.salt) return false;
  try { await openVault(fromB64(saved.key)); store.persist = true; return true; }
  catch { store.forget(); return false; }
}

async function unlockWith(passphrase, keep) {
  const master = await deriveMaster(passphrase, vault.lock);
  await openVault(master);
  store.persist = keep;
  if (keep) store.set(VAULT_KEY, { salt: vault.lock.kdf.salt, key: toB64(master) });
}

/* Lock forgets everything this page stored, and tells its other open tabs
   to lock too. */
function lockAgain() {
  store.forget();
  try {
    localStorage.setItem(NS + 'locked', String(Date.now()));
    localStorage.removeItem(NS + 'locked');
  } catch { /* storage blocked */ }
  location.reload();
}
window.addEventListener('storage', e => {
  if (vault.enc && e.key === NS + 'locked' && e.newValue) location.reload();
});

/* data.json, or its encrypted twin. Returns the parsed payload and whether
   the service worker answered from its cache. */
async function fetchData(init) {
  const file = vault.lock ? 'data.enc' : 'data.json';
  let res;
  try { res = await fetch(file, init); }
  catch (err) {
    const hit = await caches.match(file).catch(() => null);
    if (!hit) throw err;
    res = hit;
    res.fromCache = true;
  }
  if (!res.ok) throw new Error(`${file} responded ${res.status}`);
  const cached = res.fromCache || res.headers.get('X-From-Cache') === '1';
  if (!vault.lock) return { data: await res.json(), cached };
  const plain = await unsealBytes(vault.enc, await res.arrayBuffer(), 'data.json');
  return { data: JSON.parse(new TextDecoder().decode(plain)), cached };
}

/* A photo's address. Ours are stored as thumbs/<hmac of the name>.bin and
   decrypted into a blob: URL; anything else is used as it is. */
function photoUrl(src) {
  if (!vault.lock || !String(src).startsWith('thumbs/')) return Promise.resolve(src);
  if (!vault.photos.has(src)) {
    vault.photos.set(src, (async () => {
      const file = src.slice('thumbs/'.length);
      const sig = await crypto.subtle.sign('HMAC', vault.name, te.encode(`photo/${file}`));
      const stored = `thumbs/${hex(sig).slice(0, 32)}.bin`;
      const res = await fetch(stored);
      if (!res.ok) throw new Error(`${res.status}`);
      const plain = await unsealBytes(vault.enc, await res.arrayBuffer(), stored);
      const type = /\.webp$/i.test(file) ? 'image/webp'
                 : /\.png$/i.test(file) ? 'image/png' : 'image/jpeg';
      return URL.createObjectURL(new Blob([plain], { type }));
    })());
    vault.photos.get(src).catch(() => vault.photos.delete(src));
  }
  return vault.photos.get(src);
}

/* Decrypting every photo on the page up front is wasted work on a phone, so
   an encrypted photo is only fetched once it is near the screen. */
const nearScreen = 'IntersectionObserver' in window
  ? new IntersectionObserver(entries => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      nearScreen.unobserve(e.target);
      loadPhoto(e.target);
    }
  }, { rootMargin: '600px 0px' })
  : null;

function loadPhoto(img) {
  photoUrl(img.dataset.src).then(
    url => { img.src = url; },
    () => img.dispatchEvent(new Event('error')));
}

function setPhoto(img, src, eager) {
  if (!vault.lock) { img.src = src; return; }
  img.dataset.src = src;
  if (eager || !nearScreen) loadPhoto(img);
  else nearScreen.observe(img);
}

/* ----------------------------------------------------------------- format */
const money = n => (n === null || n === undefined || n === '') ? '—'
  : '$' + Math.round(n).toLocaleString('en-CA');
const signed = n => (n > 0 ? '+' : '−') + '$' + Math.abs(Math.round(n)).toLocaleString('en-CA');
const daysListed = d => d === 0 ? 'listed today'
  : d === 1 ? 'listed yesterday' : `${d} days listed`;
const km = n => (n === null || n === undefined) ? null : num(n);
/* Every number a person reads, grouped the same way. The locale is explicit
   because a bare toLocaleString() follows the browser's, not the page's. */
const num = n => (n === null || n === undefined || n === '') ? '—'
  : Math.round(n).toLocaleString('en-CA');
/* The one pluraliser, so every count is grouped the same way. */
const plural = (n, word) => `${num(n)} ${word}${n === 1 ? '' : 's'}`;

function when(iso) {
  const t = Date.parse(iso);
  if (!t) return '';
  const mins = (Date.now() - t) / 60000;
  if (mins < 1) return 'just now';
  if (mins < 60) return `${Math.round(mins)}m ago`;
  if (mins < 48 * 60) return `${Math.round(mins / 60)}h ago`;
  return new Date(t).toLocaleDateString('en-CA', { month: 'short', day: 'numeric' });
}
function stamp(iso) {
  const t = Date.parse(iso);
  return t ? new Date(t).toLocaleString('en-CA',
    { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false }) : '—';
}
/* A placeholder that is a car, rather than a grey rectangle or the title
   printed a second time under the title. */
const CAR_GLYPH = `<svg width="34" height="22" viewBox="0 0 34 22" fill="none" aria-hidden="true">
  <path d="M3 15h28M6 15l2.4-7A3 3 0 0111.3 6h11.4a3 3 0 012.9 2l2.4 7" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="10" cy="17.5" r="2.4" stroke="currentColor" stroke-width="1.6"/>
  <circle cx="24" cy="17.5" r="2.4" stroke="currentColor" stroke-width="1.6"/></svg>`;

const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* A schedule interval given in minutes, as people say it: "2 hours", not
   "120 minutes". Used wherever the schedule is described. */
function every(minutes) {
  const m = Number(minutes) || 0;
  if (m < 60) return `${m} minutes`;
  if (m % 60) return `${Math.round(m / 6) / 10} hours`;
  const h = m / 60;
  return h === 1 ? 'hour' : `${h} hours`;
}

/* A duration in hours, in one shape everywhere it is printed. */
function hours(n) {
  const h = Number(n) || 0;
  if (h < 1) return `${Math.round(h * 60)} minutes`;
  const rounded = h < 10 ? Math.round(h * 10) / 10 : Math.round(h);
  return `${rounded} hour${rounded === 1 ? '' : 's'}`;
}

/* The name of one coverage slot, from the schedule's real interval. */
function slotWord(cov, one_only) {
  const mins = (cov && cov.expected_interval_minutes) || 30;
  const one = mins === 30 ? 'half-hour'
    : mins === 60 ? 'hour'
    : mins % 60 === 0 ? `${mins / 60}-hour slot`
    : `${mins}-minute slot`;
  return one_only === false ? one : one + 's';
}

/* One name for the ratio, and one gate on the comparison, shared by the card
   and the sheet. The bot publishes a percentage only when the cohort is big
   enough, so a figure is always worth printing, and silence on a card means
   no cohort; the sheet says why. */
const PER_KM = 'per 1,000 km';

function comparableSays(cmp, l) {
  if (!cmp) return null;
  if (cmp.pct !== undefined) {
    const under = cmp.pct < 0, n = Math.round(Math.abs(cmp.pct));
    return {
      tone: under ? 'drop' : '',
      badge: `${n}% ${under ? 'under' : 'over'} the median of ${num(cmp.sample)}`,
      sentence: `${n}% ${under ? 'under' : 'over'} the median ${money(cmp.median)} `
        + `of ${plural(cmp.sample, 'comparable')} — ${esc(cmp.cohort || 'cars like it')}, `
        + `each within a third of this car's odometer.`,
    };
  }
  if (cmp.rank !== undefined) {
    return {
      tone: '',
      badge: `${ordinal(cmp.rank)} cheapest of ${num(cmp.of)}`,
      sentence: esc(stop(cmp.why_not)),
    };
  }
  return { tone: '', badge: null, sentence: cmp.why_not ? esc(stop(cmp.why_not)) : null };
}

/* A label and the sentence under it, without the stutter: the label is
   dropped when the sentence already starts with it. */
function sentence(text, label = '') {
  const body = String(text || '').trim();
  if (!body) return label;
  if (!label) return body;
  return body.toLowerCase().startsWith(label.toLowerCase())
    ? body : `${label}: ${body}`;
}

/* One full stop, added only when the sentence does not already end in one. */
const stop = t => {
  const body = String(t || '').trim();
  return !body ? '' : /[.!?\u2026]$/.test(body) ? body : body + '.';
};

const ordinal = n => (n % 100 >= 10 && n % 100 <= 20) ? `${n}th`
  : `${n}${ ({ 1: 'st', 2: 'nd', 3: 'rd' })[n % 10] || 'th' }`;

/* The bot's name for a car, as published (listing.name_of owns the rule).
   The fallback is only for a row the bot never named; it does not try to
   re-derive the rule. */
function carName(l) {
  const named = String(l.title || '').trim();
  if (named) return named;
  const built = [l.year, l.make, l.model].filter(Boolean).join(' ').trim();
  return built || 'Listing';
}
/* Titles arrive pipe-delimited ("Model 4dr Sdn|CERTIFIED|ONE OWNER"): the
   first segment is the car, the rest is the dealer's pitch. */
function carExtras(l) {
  return String(l.title || '').split('|').slice(1)
    .map(s => s.trim()).filter(s => s && s.length < 40);
}

/* ----------------------------------------------------------------- helpers */
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html !== undefined) n.innerHTML = html;
  return n;
};
const live = () => (app.data?.listings || []).filter(l => l.status === 'active');
const DAY = 86400000;
const arrivedRecently = l => Date.parse(l.first_seen) > Date.now() - DAY;
const visible = () => live().filter(l => !l.filtered);
const byId = id => (app.data?.listings || []).find(l => String(l.id) === String(id));

function lastEventOf(l) {
  const h = l.price_history || [];
  if (l.status === 'gone') return 'removed';
  if (h.length >= 2) {
    const a = h[h.length - 2]?.price, b = h[h.length - 1]?.price;
    if (a && b && b !== a) return b < a ? 'price_drop' : 'price_rise';
  }
  if (app.data && Date.parse(l.first_seen) > Date.now() - 36e5 * 24) return 'new';
  return null;
}
function priceMove(l) {
  const h = (l.price_history || []).filter(p => p.price);
  if (h.length < 2) return null;
  const was = h[h.length - 2].price, now = h[h.length - 1].price;
  return now === was ? null : { was, now, delta: now - was };
}

/* ----------------------------------------------------------------- trust */

/* The coverage percentage, or null while the window is too short to support
   one. Every place that shows or reasons about the percentage asks this, so
   the header and the Status tile always agree. */
function coveragePct(cov) {
  if (!cov || cov.too_short) return null;
  return (cov.pct === undefined || cov.pct === null) ? null : cov.pct;
}

/* Was that coverage the schedule's doing, or somebody's? Checks started by a
   push or by hand say nothing about whether the schedule works.

   Returns null when the payload has no trigger record to answer from. */
function whoKeptTime(cov) {
  if (!cov || cov.slots_scheduled === undefined || cov.too_short) return null;
  const all = cov.slots_covered ?? 0;
  const mine = cov.slots_scheduled ?? 0;
  if (!all) return null;
  // "None of it scheduled" and "no record of what started these" differ: a
  // missing field must not be reported as a measurement.
  const kinds = Object.keys(cov.by_trigger || {});
  if (!mine && kinds.length && kinds.every(k => k === 'unattributed')) {
    return { level: 'unknown', mine, all };
  }
  if (!mine) return { level: 'none', mine, all };
  if (mine < all) return { level: 'some', mine, all };
  return { level: 'all', mine, all };
}

/* What started the checks, in the words of the thing that started them. */
const TRIGGER_WORDS = {
  schedule: 'the schedule',
  repository_dispatch: 'an outside timer',
  workflow_dispatch: 'a dispatch by hand',
  push: 'a push to the repository',
  manual: 'a run by hand',
  unattributed: 'runs recorded before the bot noted what started them',
};
/* The same things, short enough to be a tile's value. A value is a noun
   phrase; the sentence goes underneath it. */
const TRIGGER_NAMES = {
  schedule: 'GitHub\u2019s schedule',
  repository_dispatch: 'An outside timer',
  workflow_dispatch: 'Hand dispatches',
  push: 'Your pushes',
  manual: 'Runs by hand',
  unattributed: 'Not recorded',
};
function triggerName(key) {
  const k = String(key || '');
  if (k.startsWith('repository_dispatch:')) return k.slice('repository_dispatch:'.length);
  return TRIGGER_NAMES[k] || k;
}
/* A repository_dispatch may name its caller ("repository_dispatch:laptop").
   The name is the useful half: it says which timer to look at when it stops. */
function triggerWord(key) {
  const k = String(key || '');
  if (k.startsWith('repository_dispatch:')) {
    return `${k.slice('repository_dispatch:'.length)} (an outside timer)`;
  }
  return TRIGGER_WORDS[k] || k;
}

function trustState() {
  const d = app.data;
  if (!d) return { state: 'ok', text: 'Loading…' };
  // The last run that read the site. A firing that stood down because a
  // check had just happened is recorded too, but it read nothing.
  const run = lastCheck(d);
  // The last firing answers a different question: "did the last attempt go
  // wrong". A run whose every search failed is not a check, so only this
  // record can carry that failure.
  const firing = d.last_run || run;

  // Offline outranks everything else: an old check with no signal is not a
  // broken bot, and the two want opposite actions.
  if (app.offline) {
    const age = when(run.at || d.generated_at);
    return {
      state: 'stale',
      text: `Offline · ${age}`,
      alarm: {
        level: 'warn',
        text: `You are offline. This is the copy your phone saved, and the `
            + `last check in it ran ${age}. Nothing here has been re-read `
            + `since - cars may have sold or changed price.`,
        detail: `Saved copy published ${stamp(d.generated_at)}.`,
      },
    };
  }
  const cov = d.coverage || {};
  // Age of the last check, not of the file: the file can be rewritten
  // without a check, and the question is about the site.
  const ageMin = (Date.now() - Date.parse(run.at || d.generated_at)) / 60000;
  const expected = cov.expected_interval_minutes || 30;
  const failing = firing.ok === false || (firing.errors || []).length > 0;
  // The same threshold the bot alarms on (health.silent_after_hours), so the
  // page and the alert share one definition of "too long ago".
  const quietAfterMin = (cov.silent_after_hours || 0) * 60 || expected * 3;
  const stale = ageMin > quietAfterMin;
  // Coverage this poor means cars can arrive and go between checks, which is
  // worth an amber light even when the most recent check was a minute ago.
  const pct = coveragePct(cov);
  const thin = pct !== null && pct < 50;

  // Nothing has ever run: a fresh install, not a fault. No alarm either;
  // every view's empty state already explains the first run.
  if (!run.at) {
    return { state: 'stale', text: 'Not checked yet' };
  }

  if (failing) {
    return {
      state: 'bad',
      text: `Last check failed ${when(firing.at)}`,
      alarm: {
        level: 'bad',
        text: 'The last check did not finish cleanly, so what you are looking at may be out of date.',
        // The bot's own words, verbatim, so monospace is right here.
        mono: true,
        detail: (firing.errors || [])[0] || (firing.invariants || [])[0] || '',
      },
    };
  }
  if (stale) {
    return {
      state: 'stale',
      text: `Checked ${when(run.at)}`,
      alarm: {
        level: 'warn',
        text: `No check has landed for ${hours(ageMin / 60)}, against one `
            + `expected every ${every(expected)}. Cars may have come and gone `
            + `since.`,
        // slots_covered, not the run count: the percentage is a share of
        // slots, and the count beside it has to be too.
        detail: pct === null ? ''
          : `Coverage over the last ${hours(cov.window_hours)}: ${pct}% — ${cov.slots_covered ?? cov.successful} of ${cov.expected} ${slotWord(cov)} had a check.`,
      },
    };
  }
  if (thin) {
    return {
      state: 'stale',
      text: `Checked ${when(run.at)}`,
      alarm: {
        level: 'warn',
        // slots_covered, to agree with the Status card and the strip.
        text: `Only ${pct}% of the last ${hours(cov.window_hours)} were watched — ${cov.slots_covered ?? cov.successful} of ${cov.expected} ${slotWord(cov)} had a check. A car can be listed and sold between checks at this rate.`,
        detail: cov.longest_gap_minutes
          ? `Longest gap: ${(cov.longest_gap_minutes / 60).toFixed(1)} hours.` : '',
      },
    };
  }
  return { state: 'ok', text: `Checked ${when(run.at)}` };
}

/* A newer build is cached and will run on the next load. Say so once. */
let versionBannerUp = false;
function newVersionReady() {
  if (versionBannerUp) return;
  versionBannerUp = true;
  const bar = el('div', 'newver');
  bar.setAttribute('role', 'status');
  const text = el('span', '', 'A newer version of this page is ready.');
  const btn = el('button', 'btn', 'Reload');
  btn.type = 'button';
  btn.addEventListener('click', () => location.reload());
  bar.appendChild(text);
  bar.appendChild(btn);
  document.body.appendChild(bar);
}

/* ------------------------------------------------------------------ clock

   The strip under the masthead: when the last check ran, how often checks
   happen, and how long until the next is due.

   The countdown targets the moment a check is due (last check plus the
   interval), which GitHub's scheduler does not guarantee. Past that moment
   it says "due" and counts upwards rather than freezing at zero, so a late
   schedule is visible as late. */

function countdownWords(ms) {
  // Under five minutes, show seconds so the countdown visibly moves.
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 300) {
    const m = Math.floor(s / 60);
    return `${m}:${String(s % 60).padStart(2, '0')}`;
  }
  const mins = Math.round(s / 60);
  if (mins < 60) return `${mins} min`;
  const h = Math.floor(mins / 60);
  const rest = mins % 60;
  return rest ? `${h}h ${rest}m` : `${h}h`;
}

function clockState() {
  const d = app.data;
  if (!d) return null;
  const run = lastCheck(d);
  // Keep the ISO string for when() and stamp(): they parse it themselves,
  // and a number would parse to NaN and render blank.
  const iso = run.at || '';
  const at = Date.parse(iso);
  const cov = d.coverage || {};
  const mins = Number(cov.expected_interval_minutes) || 0;
  if (!at) {
    return { last: 'Not checked yet', every: mins ? `every ${every(mins)}` : '—',
             next: 'first one pending', state: 'off', fill: 0 };
  }
  const evry = mins ? `every ${every(mins)}` : 'when it is asked to';
  if (!mins) {
    return { last: when(iso), every: evry, next: 'no set schedule',
             state: 'off', fill: 0, at, iso };
  }
  const due = at + mins * 60000;
  const left = due - Date.now();
  const elapsed = Date.now() - at;
  return {
    at,
    iso,
    last: when(iso),
    every: evry,
    next: left > 0 ? `in ${countdownWords(left)}`
        : left > -60000 ? 'due now'
        : `due ${countdownWords(-left)} ago`,
    state: left > 0 ? 'ok' : 'due',
    // Past due the rail stays full rather than wrapping round, because a bar
    // that restarts reads as a check having happened.
    fill: Math.max(0, Math.min(1, elapsed / (mins * 60000))),
  };
}

function renderClock() {
  const c = clockState();
  const host = document.getElementById('clock');
  if (!host) return;
  // The strip is in the markup from the first paint, showing dashes, so it
  // never shifts the page when data lands. If data.json never loads, the
  // dashes stay.
  if (!c) return;
  host.dataset.state = c.state;
  const last = document.getElementById('clock-last');
  last.textContent = c.last;
  // The exact stamp on hover: "3h ago" suits a glance, not a quote.
  last.title = c.iso ? stamp(c.iso) : '';
  document.getElementById('clock-every').textContent = c.every;
  document.getElementById('clock-next').textContent = c.next;
  document.getElementById('clock-fill').style.width = `${Math.round(c.fill * 100)}%`;
}

/* The countdown has to re-read what it counts from, or a tab left open would
   raise a false "due" alarm off the copy loaded at boot.

   So the strip pulls a fresh copy in the background: only while the tab is
   visible, at most once a minute, re-rendering only when `generated_at`
   moved (a re-render costs the scroll position), and never under an open
   listing sheet; the new data waits until it closes. */
let clockTimer = null;
let lastFetchAt = 0;

async function refreshData() {
  if (document.hidden) return;
  if (Date.now() - lastFetchAt < 60000) return;
  lastFetchAt = Date.now();
  try {
    const { data: fresh, cached } = await fetchData({ cache: 'no-store' });
    // Offline is re-decided on every refresh: the worker marks a cached
    // answer, and a live one means the network is back.
    const wasOffline = app.offline;
    app.offline = cached;
    if (!fresh || (fresh.generated_at === app.data?.generated_at
                   && app.offline === wasOffline)) return;
    app.data = fresh;
    if (document.getElementById('sheet')?.dataset.open === '1') {
      renderClock();          // the strip is outside the sheet; the rest waits
      return;
    }
    render();
  } catch { /* offline, or the file is mid-write - the next tick tries again */ }
}

function startClock() {
  const tick = () => {
    if (document.hidden) return;
    renderClock();
    // Only worth asking once the countdown is close to running out.
    const c = clockState();
    if (c && (c.state === 'due' || c.fill > 0.75)) refreshData();
  };
  if (clockTimer) clearInterval(clockTimer);
  clockTimer = setInterval(tick, 1000);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) return;
    renderClock();
    refreshData();
  });
  tick();
}

function renderTrust() {
  const t = trustState();
  store.set('lastTrust', { state: t.state, text: t.text, alarm: t.alarm || null });
  const wrap = document.getElementById('trust');
  wrap.dataset.state = t.state;
  document.getElementById('trust-text').textContent = t.text;
  const cov = app.data?.coverage;
  const pct = coveragePct(cov);
  const kept = whoKeptTime(cov);
  document.getElementById('trust-cov').innerHTML =
    !cov ? ''
    : pct === null ? '· measuring'
    // A percentage the schedule did not earn is never shown on its own.
    : kept && kept.level === 'unknown'
      ? `· <b class="num">${pct}%</b> covered, source not recorded`
    : kept && kept.level === 'none'
      ? `· <b class="num">${pct}%</b> covered, <b>none of it scheduled</b>`
    : kept && kept.level === 'some'
      ? `· <b class="num">${pct}%</b> covered, ${num(kept.mine)} of `
        + `${num(kept.all)} scheduled`
    : `· <b class="num">${pct}%</b> covered`;

  const alarm = document.getElementById('alarm');
  if (t.alarm) {
    alarm.hidden = false;
    alarm.dataset.level = t.alarm.level;
    document.getElementById('alarm-text').textContent = t.alarm.text;
    const detail = document.getElementById('alarm-detail');
    // Monospace for machine output, prose for prose.
    detail.innerHTML = !t.alarm.detail ? ''
      : t.alarm.mono ? `<code>${esc(t.alarm.detail)}</code>`
      : esc(t.alarm.detail);
  } else {
    alarm.hidden = true;
  }
}

/* ----------------------------------------------------------------- tabs */
function renderTabs() {
  const host = document.getElementById('tabs');
  const unread = app.data ? feedEvents().filter(e => isUnread(e)).length : 0;
  host.innerHTML = '';
  for (const v of VIEWS) {
    const b = el('button', 'tab');
    b.type = 'button';
    b.dataset.viewLink = v.id;
    b.setAttribute('role', 'link');
    if (app.view === v.id) b.setAttribute('aria-current', 'page');
    let n = '';
    if (v.id === 'feed' && unread) n = `<span class="tab__n num" data-unread="1">${unread}</span>`;
    else if (v.id === 'listings' && app.data) n = `<span class="tab__n num">${visible().length}</span>`;
    else if (v.id === 'searches' && app.data) n = `<span class="tab__n num">${(app.data.searches || []).length}</span>`;
    else n = '<span class="tab__n num"></span>';
    b.innerHTML = `<span>${v.label}</span>${n}`;
    b.addEventListener('click', () => go(v.id));
    host.appendChild(b);
  }
}

function go(view, opts = {}) {
  app.view = view;
  if (!opts.silent) location.hash = `#/${view}`;
  for (const s of document.querySelectorAll('.view')) s.hidden = s.dataset.view !== view;
  renderTabs();
  render();
  if (opts.focus !== false) document.getElementById('main').focus({ preventScroll: true });
  window.scrollTo({ top: 0, behavior: 'instant' in window ? 'instant' : 'auto' });
}

/* ----------------------------------------------------------------- feed */
/* The last run that read the site: d.last_check, or, when a payload lacks
   it, the newest run that actually loaded a search. d.last_run is the last
   resort because a run that read nothing reports empty figures. */
function lastCheck(d) {
  if (d.last_check) return d.last_check;
  for (const run of (d.runs || [])) {
    if (run.skipped) continue;
    const ran = Number(run.searches_run) || 0;
    const failed = Number(run.searches_failed) || 0;
    if (ran ? failed < ran : run.ok) return run;
  }
  return d.last_run || {};
}

function feedEvents() {
  // Array-checked, not just truthy: a malformed payload may carry an object
  // here, and the page must still render.
  const rows = app.data?.events;
  return Array.isArray(rows) ? rows : [];
}
const isUnread = e => !app.lastSeen || e.at > app.lastSeen;

/* "a, b and c", not "a and b and c". */
function andList(items) {
  const parts = items.filter(Boolean);
  if (parts.length <= 1) return parts[0] || '';
  return parts.slice(0, -1).join(', ') + ' and ' + parts[parts.length - 1];
}

/* Shown once, on a browser that has never opened this before, and never
   again once dismissed. */
function welcome() {
  const d = app.data;
  const box = el('section', 'state measure');
  box.style.marginBottom = 'var(--s6)';
  const searches = (d.searches || []).map(s => s.name);
  const cov = d.coverage || {};
  box.innerHTML = `
    <h2>This is watching ${searches.length} search${searches.length === 1 ? '' : 'es'} on autotrader.ca</h2>
    <p>${esc(andList(searches) || 'nothing yet')} — ${visible().length} cars live
       right now, re-read about every ${every(cov.expected_interval_minutes || 30)}.</p>
    <p><b>Alerts</b> go to ${(d.notify?.active || []).join(', ') || 'nowhere yet — no channel is switched on'}.
       <b>Feed</b> is what changed since you last looked. <b>Status</b> says whether
       the bot itself is healthy, and the dot beside the title up there says it at a glance.</p>
    <p>Add a search by pasting its link on the Searches tab.</p>
    <p class="only-keyboard">On a keyboard: <kbd>1</kbd>–<kbd>5</kbd> for the
       tabs, <kbd>/</kbd> to find a car, <kbd>?</kbd> for the rest.</p>`;
  const b = el('button', 'btn btn--primary', 'Got it');
  b.type = 'button';
  b.addEventListener('click', () => {
    store.set('welcomed', true);
    app.firstVisit = false;
    renderFeed();
    document.getElementById('main').focus({ preventScroll: true });
  });
  box.appendChild(b);
  return box;
}

function renderFeed() {
  const host = document.querySelector('[data-view="feed"]');
  host.innerHTML = '';
  const head = el('div', 'view__head');
  head.classList.add('measure');
  head.innerHTML = `<h1 id="feed-h">What changed</h1>
    <p>Everything that has happened to a car you are watching, newest first — including
       cars your rules hide, which the run counters never counted. A row you were
       told about says nothing extra; a row that stayed quiet says why.</p>`;
  host.appendChild(head);
  if (app.firstVisit && !store.get('welcomed', false)) {
    host.appendChild(welcome());
  }

  if (app.firstVisit && store.get('welcomed', false)) {
    const p = el('p', 'why measure');
    p.innerHTML = `<b>First visit.</b> Everything already on the site is recorded as a
      starting point rather than announced at you. From now on this shows only what
      has changed since you last looked.`;
    host.appendChild(p);
  }

  const everything = feedEvents();
  // What happened, and what you were told about, are different lists: a car
  // your rules hide still changes, and the page records it. The two are one
  // click apart, with both counts on screen.
  const wasSent = e => e.delivery?.state === 'sent';
  const sentCount = everything.filter(wasSent).length;
  const events = app.feedOnly === 'sent' ? everything.filter(wasSent) : everything;
  if (!everything.length) {
    // Three different nothings: no check has run; a check found no cars at
    // all; cars are watched and none has changed yet.
    const noCarsAtAll = !(app.data.listings || []).length;
    host.appendChild(emptyState(
      !app.data.last_run ? 'Nothing has been checked yet'
        : noCarsAtAll ? 'No cars found yet'
        : 'Nothing has changed yet',
      !app.data.last_run
        ? 'The first check has not run. When it does, everything already on the site is recorded as a starting point — you will hear about what changes after that, not about the back catalogue.'
        : noCarsAtAll
        ? 'The searches have not turned up a car yet. That is either a narrow search or a new one — the Searches tab says which, and how many listings each one read last time.'
        : 'Every car the searches found was already there when the bot started watching. This fills up as prices move and cars come and go.'));
    return;
  }

  if (sentCount < everything.length) {
    const chips = el('div', 'chips');
    chips.setAttribute('role', 'group');
    chips.setAttribute('aria-label', 'Which changes to show');
    for (const [id, label, n] of [['all', 'Everything that changed', everything.length],
                                  ['sent', 'Sent to your phone', sentCount]]) {
      const c = el('button', 'chip');
      c.type = 'button';
      c.setAttribute('aria-pressed', app.feedOnly === id ? 'true' : 'false');
      c.innerHTML = `${label}<span class="n num">${num(n)}</span>`;
      c.addEventListener('click', () => { app.feedOnly = id; renderFeed(); });
      chips.appendChild(c);
    }
    host.appendChild(chips);
  }
  if (app.feedOnly === 'sent' && !events.length) {
    host.appendChild(emptyState('Nothing was sent',
      'Everything that changed was on a car your rules hide, or was a move too '
      + 'small to be worth a notification. Switch back to see all of it.'));
    return;
  }

  const unread = events.filter(isUnread);
  if (unread.length) {
    const b = el('div', 'bar');
    const btn = el('button', 'btn btn--primary', `Mark ${unread.length} as seen`);
    btn.type = 'button';
    btn.addEventListener('click', () => {
      app.lastSeen = new Date().toISOString();
      store.set('lastSeen', app.lastSeen);
      renderTabs(); renderFeed();
    });
    b.appendChild(btn);
    host.appendChild(b);
  }

  const groups = new Map();
  for (const e of events) {
    if (!groups.has(e.kind)) groups.set(e.kind, []);
    groups.get(e.kind).push(e);
  }
  for (const kind of KIND_ORDER) {
    const rows = groups.get(kind);
    if (!rows || !rows.length) continue;
    const sec = el('section', 'section measure');
    const n = rows.filter(isUnread).length;
    sec.innerHTML = `<div class="section__head">
        <h2>${KIND[kind].group}</h2>
        <span class="count num">${n === rows.length ? `${rows.length} new to you`
          : n ? `${rows.length} · ${n} new to you` : rows.length}</span>
      </div>`;
    const list = el('ul', 'feed');
    let markerDone = false;
    // Cars your rules keep come first within the group, then the hidden ones,
    // so the cap of 60 never cuts a car you can buy for a hidden one.
    const ordered = [...rows.filter(e => !e.filtered), ...rows.filter(e => e.filtered)];
    for (const e of ordered.slice(0, 60)) {
      if (!markerDone && !isUnread(e) && rows.some(isUnread)) {
        const m = el('li'); m.innerHTML = `<div class="marker">Seen before this</div>`;
        list.appendChild(m); markerDone = true;
      }
      list.appendChild(eventRow(e));
    }
    if (ordered.length > 60) {
      // Not "older": what falls off the end is the hidden cars, whatever
      // their age.
      const cut = rows.length - 60;
      const allHidden = ordered.slice(60).every(e => e.filtered);
      const more = el('li', 'note',
        allHidden ? `and ${num(cut)} more your rules hide`
                  : `and ${num(cut)} more`);
      more.style.padding = 'var(--s3) var(--s4)';
      list.appendChild(more);
    }
    sec.appendChild(list);
    host.appendChild(sec);
  }
}

function eventRow(e) {
  const li = el('li');
  const b = el('button', 'ev' + (isUnread(e) ? ' ev--unread' : ''));
  b.type = 'button';
  b.dataset.kind = e.kind;

  let fig = '', sub = [];
  if (e.kind === 'price_drop' || e.kind === 'price_rise') {
    const cls = e.kind === 'price_drop' ? 'drop' : 'rise';
    fig = `<span class="ev__fig num ${cls}">${signed(e.delta)}</span>`;
    sub.push(`<span class="ev__was num">${money(e.old_price)}</span>`);
    sub.push(`<span class="num">${money(e.new_price)}</span>`);
  } else if (e.kind === 'priced') {
    fig = `<span class="ev__fig num">${money(e.new_price)}</span>`;
    sub.push('was call for price');
  } else if (e.price) {
    fig = `<span class="ev__fig num">${money(e.price)}</span>`;
  } else if (e.kind === 'new') {
    // A car with no price is a dealer withholding one; say so.
    fig = '<span class="ev__fig">Call for price</span>';
  }
  // A delivery note only when the delivery was not the ordinary one, so the
  // unusual rows stand out. The Feed's heading says what silence means.
  if (e.filtered) sub.push(`<span>hidden — ${esc(e.filter_reason || 'a rule of yours')}</span>`);
  else if (e.delivery?.state === 'queued') sub.push('<span>queued, not sent yet</span>');
  else if (e.delivery?.state === 'quiet') sub.push(`<span>${esc(e.delivery.text)}</span>`);
  else if (e.delivery?.state === 'none') sub.push('<span class="drop">no delivery record</span>');

  const name = carName(e);
  // Named by its own content, with the kind word in a visually hidden span
  // rather than an aria-label, so the accessible name matches the visible
  // text (WCAG 2.5.3, Label in Name).
  b.innerHTML =
    `<span class="sr">${KIND[e.kind].label}.</span>
     <time class="ev__when" datetime="${esc(e.at)}">${when(e.at)}</time>
     <span class="ev__title">${esc(name)}</span>${fig}
     <span class="ev__sub">${sub.join('')}</span>`;
  // A photo, because similar titles are hard to tell apart at a glance. The
  // box has a fixed size, so nothing moves when the picture arrives.
  const car = byId(e.listing_id);
  if (car) b.prepend(shot(car, 'ev__shot'));
  b.addEventListener('click', () => openSheet(e.listing_id));
  li.appendChild(b);
  return li;
}

/* ----------------------------------------------------------------- listings */
function listingPool() {
  let rows = (app.data?.listings || []);
  if (app.search !== 'all') rows = rows.filter(l => l.search_id === app.search);

  const chip = app.chip;
  if (chip === 'all') rows = rows.filter(l => l.status === 'active' && (app.showHidden || !l.filtered));
  else if (chip === 'drops') rows = rows.filter(l => l.status === 'active' && priceMove(l)?.delta < 0);
  else if (chip === 'new') rows = rows.filter(l => l.status === 'active' && !l.filtered && arrivedRecently(l));
  else if (chip === 'unpriced') rows = rows.filter(l => l.status === 'active' && l.unpriced && !l.filtered);
  else if (chip === 'gone') rows = rows.filter(l => l.status === 'gone');
  else if (chip === 'hidden') rows = rows.filter(l => l.status === 'active' && l.filtered);
  else if (chip === 'private') rows = rows.filter(l => l.status === 'active'
    && !l.filtered && l.seller_type === 'private');
  else if (chip === 'mine') rows = rows.filter(l => marks.of(l.id).shortlisted);
  else if (chip === 'dropped') rows = rows.filter(l => marks.of(l.id).dismissed);

  // A car marked "not interested" is kept under its own chip, just out of
  // the lists you scroll.
  if (chip !== 'dropped') rows = rows.filter(l => !marks.of(l.id).dismissed);

  const q = app.q.trim().toLowerCase();
  if (q) {
    rows = rows.filter(l => [l.title, l.location, l.color, l.trim, l.seller, l.model]
      .filter(Boolean).join(' ').toLowerCase().includes(q));
  }
  const sort = SORTS.find(s => s.id === app.sort) || SORTS[0];
  return { sort, ...ranked(rows, sort) };
}

function renderListings() {
  const host = document.querySelector('[data-view="listings"]');
  host.innerHTML = '';
  const head = el('div', 'view__head');
  head.innerHTML = `<h1 id="listings-h">Listings</h1>
    <p>Every car the searches have turned up. Cars your rules hide are kept and
       explained rather than dropped — the number you can click on beats the number
       that quietly omits.</p>`;
  host.appendChild(head);

  const bar = el('div', 'bar');
  bar.innerHTML = `
    <label class="field">
      <span class="sr">Filter listings</span>
      <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true" style="flex:none;color:var(--text-3)">
        <circle cx="6" cy="6" r="4.5" fill="none" stroke="currentColor" stroke-width="1.4"/>
        <path d="M9.5 9.5L13 13" stroke="currentColor" stroke-width="1.4"/></svg>
      <input type="search" id="q" placeholder="Colour, city, trim, seller…" value="${esc(app.q)}">
    </label>
    <label class="sr" for="sort">Sort by</label>
    <select id="sort">${SORTS.map(s =>
      `<option value="${s.id}"${s.id === app.sort ? ' selected' : ''}>${s.label}</option>`).join('')}</select>
    <label class="sr" for="search-pick">Search</label>
    <select id="search-pick">
      <option value="all">All searches</option>
      ${(app.data.searches || []).map(s =>
        `<option value="${esc(s.id)}"${s.id === app.search ? ' selected' : ''}>${esc(s.name)}</option>`).join('')}
    </select>`;
  host.appendChild(bar);

  const kept = l => !marks.of(l.id).dismissed;
  // Counts are within the selected search, so each chip matches its grid.
  const mine = l => app.search === 'all' || l.search_id === app.search;
  const counts = {
    // Whatever the Live chip will actually show, "Include hidden" included.
    all: live().filter(l => mine(l) && (app.showHidden || !l.filtered)
                            && kept(l)).length,
    private: live().filter(l => mine(l) && !l.filtered && kept(l)
      && l.seller_type === 'private').length,
    mine: (app.data.listings || []).filter(l => mine(l) && marks.of(l.id).shortlisted).length,
    dropped: (app.data.listings || []).filter(l => mine(l) && marks.of(l.id).dismissed).length,
    drops: live().filter(l => mine(l) && priceMove(l)?.delta < 0).length,
    new: live().filter(l => mine(l) && !l.filtered && arrivedRecently(l)).length,
    unpriced: live().filter(l => mine(l) && l.unpriced && !l.filtered).length,
    hidden: live().filter(l => mine(l) && l.filtered).length,
    gone: (app.data.listings || []).filter(l => mine(l) && l.status === 'gone').length,
  };
  const chips = el('div', 'chips');
  chips.setAttribute('role', 'group');
  chips.setAttribute('aria-label', 'Filter by state');
  for (const [id, label] of CHIPS) {
    // A chip that would show nothing is hidden, unless it is the active one.
    // "Live" always stays: it is the way back from every other chip.
    if (id !== 'all' && !counts[id] && app.chip !== id) continue;
    const c = el('button', 'chip');
    c.type = 'button';
    c.setAttribute('aria-pressed', app.chip === id ? 'true' : 'false');
    c.innerHTML = `${label}<span class="n num">${counts[id] ?? 0}</span>`;
    c.addEventListener('click', () => { app.chip = id; renderListings(); });
    chips.appendChild(c);
  }
  if (app.chip === 'all' && counts.hidden) {
    const btn = el('button', 'chip');
    btn.type = 'button';
    btn.setAttribute('aria-pressed', String(app.showHidden));
    // No count: this is a switch, and its effect is the Live count changing.
    btn.textContent = app.showHidden ? 'Hidden cars included' : 'Include hidden';
    btn.addEventListener('click', () => { app.showHidden = !app.showHidden; renderListings(); });
    chips.appendChild(btn);
  }
  host.appendChild(chips);

  // The page carries at most dashboard.max_listings cars, and the ones left
  // out are the oldest hidden ones. Say so rather than show short counts.
  const short = app.data.health?.left_out || 0;
  if (short && app.search === 'all') {
    host.appendChild(el('p', 'note',
      `${num(short)} more ${short === 1 ? 'car is' : 'cars are'} stored than this `
      + `page carries, so the counts above stop there. They are the oldest cars `
      + `your rules hide. Raise <code class="mono">dashboard.max_listings</code> `
      + `to bring them back.`));
  }

  const pool = listingPool();
  const total = pool.has.length + pool.absent.length;
  if (!total) {
    host.appendChild(noResults());
    return;
  }

  const grid = el('div', 'grid');
  let drawn = 0;
  for (const l of pool.has.slice(0, 300)) { grid.appendChild(card(l)); drawn++; }
  if (pool.absent.length && drawn < 300) {
    // Label the tail by what is missing ("no asking price"), not "unsortable".
    const split = el('p', 'grid__split');
    split.textContent = `${plural(pool.absent.length, 'car')} with ${pool.sort.absent}`;
    grid.appendChild(split);
    for (const l of pool.absent.slice(0, 300 - drawn)) { grid.appendChild(card(l)); drawn++; }
  }
  host.appendChild(grid);
  if (total > drawn) {
    host.appendChild(el('p', 'note', `Showing the first ${num(drawn)} of ${num(total)}.`));
  }
  bar.querySelector('#q').addEventListener('input', e => {
    app.q = e.target.value;
    const keep = document.activeElement === e.target;
    renderListings();
    if (keep) { const i = document.getElementById('q'); i.focus(); i.setSelectionRange(i.value.length, i.value.length); }
  });
  bar.querySelector('#sort').addEventListener('change', e => { app.sort = e.target.value; renderListings(); });
  bar.querySelector('#search-pick').addEventListener('change', e => { app.search = e.target.value; renderListings(); });
}

/* The states a car can be in, in the order they are offered. The chip row and
   the empty state both read this one list. */
const CHIPS = [['all', 'Live'], ['new', 'New'], ['drops', 'Price drops'],
               ['private', 'Private sellers'], ['mine', 'Shortlisted'],
               ['unpriced', 'Call for price'], ['hidden', 'Hidden by a rule'],
               ['gone', 'Gone'], ['dropped', 'Not interested']];
const chipLabel = id => (CHIPS.find(c => c[0] === id) || [, id])[1];

/* Zero results, and specifically why, offering back every control that is
   narrowing the list rather than only the first one found. */
function noResults() {
  const s = el('div', 'state');
  const search = (app.data.searches || []).find(x => x.id === app.search);
  const narrowing = [];
  if (app.q) {
    narrowing.push({
      what: `the text \u201c${esc(app.q)}\u201d`,
      label: 'Clear the text filter',
      clear: () => { app.q = ''; },
    });
  }
  if (app.chip !== 'all') {
    narrowing.push({
      what: `\u201c${esc(chipLabel(app.chip))}\u201d`,
      label: 'Show every live car',
      clear: () => { app.chip = 'all'; },
    });
  }
  if (app.search !== 'all') {
    narrowing.push({
      what: esc(search?.name || 'one search'),
      label: 'Show all searches',
      clear: () => { app.search = 'all'; },
    });
  }

  const offer = choice => {
    const b = el('button', 'btn', choice.label);
    b.type = 'button';
    b.addEventListener('click', () => { choice.clear(); renderListings(); });
    s.appendChild(b);
  };

  // Nothing is narrowed, but "Live" is itself a filter: an empty Live list
  // can mean no cars, every car hidden, or every car gone, and each needs a
  // different next step.
  if (!narrowing.length) {
    const all = app.data.listings || [];
    const hidden = all.filter(l => l.status === 'active' && l.filtered);
    const gone = all.filter(l => l.status === 'gone');
    if (!all.length) {
      s.innerHTML = `<h2>No cars yet</h2>
        <p>The searches have not turned up a car. The Searches tab says how many
           listings each one read last time it ran.</p>`;
      return s;
    }
    if (hidden.length) {
      s.innerHTML = `<h2>Every car found is hidden by one of your rules</h2>
        <p>The searches are working — they are holding
           ${hidden.length} car${hidden.length === 1 ? '' : 's'}, and your
           rules hide all of them. Nothing is lost: each one says which rule
           turned it away, and loosening that rule brings them straight back.</p>`;
      offer({
        what: 'your rules',
        label: `Show the ${hidden.length} hidden`,
        clear: () => { app.chip = 'hidden'; },
      });
      return s;
    }
    if (gone.length) {
      s.innerHTML = `<h2>Every car it was watching has left the market</h2>
        <p>${gone.length} listing${gone.length === 1 ? ' has' : 's have'} come
           down and nothing new has arrived yet. They are kept with their last
           price rather than deleted.</p>`;
      offer({
        what: 'the live list',
        label: `Show the ${gone.length} gone`,
        clear: () => { app.chip = 'gone'; },
      });
      return s;
    }
    s.innerHTML = `<h2>No cars yet</h2>
      <p>The searches have not turned up a car. The Searches tab says how many
         listings each one read last time it ran.</p>`;
    return s;
  }

  if (narrowing.length === 1) {
    const only = narrowing[0];
    if (app.q) {
      s.innerHTML = `<h2>Nothing matches ${only.what}</h2>
        <p>The text filter is applied to the title, city, colour, trim and seller.</p>`;
    } else if (app.chip === 'hidden') {
      // Not an emptiness with somewhere to go back to: it is good news.
      s.innerHTML = `<h2>Nothing is hidden right now</h2>
        <p>Every car the searches found passed your rules.</p>`;
      return s;
    } else if (app.chip !== 'all') {
      s.innerHTML = `<h2>No car is ${esc(chipLabel(app.chip).toLowerCase())}</h2>
        <p>Every other car the searches hold is still on the Live chip.</p>`;
    } else {
      const why = search?.health?.shut_out;
      s.innerHTML = `<h2>${esc(search?.name || 'This search')} has nothing to show</h2>
        <p>${why ? `It read the site fine and every car was turned away: ${esc(why)}.`
                 : 'It has not kept any cars yet.'}</p>`;
    }
    offer(only);
    return s;
  }

  // More than one: name them all and offer every way out.
  s.innerHTML = `<h2>Nothing is all of these at once</h2>
    <p>No car is ${andList(narrowing.map(n => n.what))}. Each of these is
       narrowing the list on its own.</p>`;
  for (const choice of narrowing) offer(choice);
  return s;
}

/* The shortcut list, shown by "?" and mentioned in the welcome box. Kept
   short enough never to need scrolling. */
const SHORTCUTS = [
  ['1 – 5', 'Feed, Listings, Market, Searches, Status'],
  ['/', 'Find a car by colour, city, trim or seller'],
  ['Esc', 'Close a car, or clear what you typed'],
  ['?', 'This list'],
];

function showShortcuts() {
  let box = document.getElementById('shortcuts');
  if (box) { box.remove(); return; }         // pressed twice: put it away
  box = el('div', 'shortcuts');
  box.id = 'shortcuts';
  box.innerHTML = '<dl>' + SHORTCUTS.map(([key, what]) =>
    `<dt><kbd>${esc(key)}</kbd></dt><dd>${esc(what)}</dd>`).join('') + '</dl>';
  const close = el('button', 'btn', 'Close');
  close.type = 'button';
  close.addEventListener('click', () => box.remove());
  box.appendChild(close);
  document.body.appendChild(box);
  close.focus();
}

// h2, not h3: these sit directly under the view's h1, and a skipped heading
// level breaks the outline for screen readers.
function emptyState(title, body) {
  const s = el('div', 'state');
  s.innerHTML = `<h2>${esc(title)}</h2><p>${esc(body)}</p>`;
  return s;
}

// Reset per render: how many photos are worth blocking on. Six covers the
// first screen at every width this is designed for.
let eagerSlots = 0;

function shot(l, cls) {
  const box = el('div', cls || 'card__shot');
  // Our copy first. The seller's CDN is only a fallback: it is unreachable
  // offline and drops the picture once the car is delisted.
  const src = l.thumb || (l.images || [])[0];
  // Say why there is no photo: the seller published none, it is not copied
  // yet, the car is hidden (hidden cars get no copy), or it failed to load.
  const fallback = (failedToLoad) => {
    const has = (l.images || []).length;
    // A copy that exists but will not load is this device's problem (often
    // being offline), not a copy that is missing.
    const why = (failedToLoad && l.thumb) ? 'photo not loaded'
              : l.filtered ? 'not kept for hidden cars'
              : has ? 'photo not copied yet'
              : 'no photo';
    box.innerHTML = `<div class="shot__fallback">${CAR_GLYPH}
        <span>${why}</span>
      </div>`;
  };
  if (!src) { fallback(); return box; }
  const img = new Image();
  // The first screenful loads eagerly so it is not a grey box on arrival;
  // everything below stays lazy.
  const eager = eagerSlots > 0;
  if (eager) eagerSlots -= 1;
  img.loading = eager ? 'eager' : 'lazy';
  img.fetchPriority = eager ? 'high' : 'low';
  img.decoding = 'async';
  img.width = 400; img.height = 300;
  img.alt = '';
  img.addEventListener('error', () => fallback(true), { once: true });
  setPhoto(img, src, eager);
  box.appendChild(img);
  return box;
}

const sellerWord = kind => kind === 'private' ? 'private seller'
                        : kind === 'dealer' ? 'dealer' : '';

function card(l) {
  const b = el('button', 'card');
  b.type = 'button';
  if (l.status === 'gone') b.classList.add('card--gone');
  if (l.filtered) b.classList.add('card--hidden');
  if (app.freshIds.has(String(l.id))) b.classList.add('is-fresh');

  const move = priceMove(l);
  const kind = lastEventOf(l);
  const cmp = app.data.comparables?.[String(l.id)];

  if (!l.filtered) b.appendChild(shot(l));

  const body = el('div', 'card__body');
  let flag = '';
  if (kind) flag = `<span class="flag flag--${KIND[kind].flag}">${KIND[kind].label}</span>`;

  // An unpriced car is headed by its name, with "Call for price" on the line
  // under it, so the largest text on the card says what the car is.
  const headline = l.unpriced
    ? `<b>${esc(carName(l))}</b>`
    : `<b class="num">${money(l.price)}</b>` +
      (move && move.delta < 0
        ? `<span class="card__was num">${money(move.was)}</span><span class="card__delta num drop">${signed(move.delta)}</span>`
        : move && move.delta > 0
        ? `<span class="card__was num">${money(move.was)}</span><span class="card__delta num rise">${signed(move.delta)}</span>`
        : '');
  const subline = l.unpriced ? 'Call for price' : esc(carName(l));

  const facts = [];
  if (l.mileage_km) facts.push(`<span class="num">${km(l.mileage_km)}<u> km</u></span>`);
  // money(), so every dollar figure is formatted the same way.
  if (l.per_1000km) facts.push(`<span class="num">${money(l.per_1000km)}<u> ${PER_KM}</u></span>`);
  // "0 km away" reads as a missing value, not as "this one is in your city".
  if (l.distance_km !== undefined && l.distance_km !== null) {
    facts.push(l.distance_km < 1
      ? '<span>right here</span>'
      : `<span class="num">${km(l.distance_km)}<u> km away</u></span>`);
  }
  if (l.location) facts.push(`<span>${esc(l.location)}</span>`);

  const foot = [];
  const says = comparableSays(cmp, l);
  if (says?.badge) foot.push(`<span class="${says.tone}">${says.badge}</span>`);
  if (l.days_listed !== undefined) foot.push(`<span class="num">${daysListed(l.days_listed)}</span>`);
  if (l.photo_count) foot.push(`<span class="num">${l.photo_count} photo${
    l.photo_count === 1 ? '' : 's'}</span>`);
  // Dealer or private goes on this line, not among the facts, which would
  // overflow the card's width.
  if (l.seller_type) foot.push(`<span>${esc(sellerWord(l.seller_type))}</span>`);
  const yourMarks = marks.of(l.id);
  // Muting is said in words: a greyed card does not say alerts are off.
  if (yourMarks.muted) foot.push('<span>muted — no alerts</span>');
  if (yourMarks.dismissed) foot.push('<span>not interested</span>');

  // Your marks and note show on the card itself, not only in the sheet.
  const mine = yourMarks;
  if (mine.shortlisted) b.classList.add('card--mine');
  if (mine.dismissed) b.classList.add('card--dropped');
  if (mine.muted) b.classList.add('card--muted');

  body.innerHTML =
    `${flag}
     <div class="card__price">${headline}</div>
     <div class="card__title">${subline}</div>
     <div class="facts">${facts.join('')}</div>` +
    (l.filtered ? `<p class="rule">Hidden: ${esc(l.filter_reason || 'a rule of yours')}</p>` : '') +
    (mine.note ? `<p class="yours">${esc(mine.note)}</p>` : '') +
    (foot.length ? `<div class="card__foot">${foot.join('')}</div>` : '');
  b.appendChild(body);
  b.setAttribute('aria-label',
    `${carName(l)}, ${l.unpriced ? 'call for price' : money(l.price)}` +
    (l.mileage_km ? `, ${km(l.mileage_km)} kilometres` : '') +
    (l.filtered ? `, hidden: ${l.filter_reason || 'a rule'}` : '') +
    (mine.shortlisted ? ', on your shortlist' : '') +
    (mine.muted ? ', muted' : '') +
    (mine.dismissed ? ', dismissed' : '') +
    (mine.note ? `. Your note: ${mine.note}` : ''));
  b.addEventListener('click', () => openSheet(l.id));
  return b;
}

/* ----------------------------------------------------------------- market */
// Text as a sentence: a capital first letter and a closing full stop.
function sentence(text) {
  const t = String(text || '').trim();
  if (!t) return '';
  return t.charAt(0).toUpperCase() + t.slice(1) + (/[.!?]$/.test(t) ? '' : '.');
}

/* A few prices, written out, where a median would say too little. */
function priceList(prices) {
  const all = (prices || []).map(money);
  if (!all.length) return '—';
  if (all.length === 1) return all[0];
  return all.slice(0, -1).join(', ') + ' and ' + all[all.length - 1];
}

/* Getting the alerts onto a phone in as few steps as possible.
 *
 * The QR code is the topic's address, drawn at publish time by
 * autotrader/qr.py, so nobody has to type a long topic name. The copy does
 * not promise the ntfy app will open: that is the phone's decision, and the
 * address works either way.
 */
function onYourPhone(notify) {
  const box = el('div', 'phone');
  const url = notify.ntfy_url || '';
  const topic = notify.ntfy_topic || '';
  box.innerHTML =
    (notify.ntfy_qr ? `<div class="phone__qr">${notify.ntfy_qr}</div>` : '') +
    `<div class="phone__how">
       <h3 class="phone__h">Get these on your phone</h3>
       <ol class="phone__steps">
         <li>Install <b>ntfy</b> - it is free and needs no account:
           <a href="https://apps.apple.com/app/ntfy/id1625396347" rel="noopener">iOS</a> ·
           <a href="https://play.google.com/store/apps/details?id=io.heckel.ntfy" rel="noopener">Android</a> ·
           <a href="https://f-droid.org/packages/io.heckel.ntfy/" rel="noopener">F-Droid</a></li>
         <li>Point the phone's camera at the code${notify.ntfy_qr ? '' : ' below'}, or
           <a href="${esc(url)}" rel="noopener">open this link on it</a>.</li>
         <li>Subscribe. Every alert this page describes arrives there from then on.</li>
       </ol>
       <p class="phone__topic">Or type the topic in by hand:
         <code class="mono">${esc(topic)}</code>
         <button type="button" class="btn btn--small" data-copy="${esc(topic)}">Copy</button></p>
       <p class="note phone__warn">Anyone who knows this topic can read these alerts. It is
         long and random, and it appears only here, behind your passphrase - keep it
         to yourself.</p>
     </div>`;
  const copy = box.querySelector('[data-copy]');
  if (copy) {
    copy.addEventListener('click', async () => {
      const said = copy.textContent;
      try {
        await navigator.clipboard.writeText(copy.dataset.copy);
        copy.textContent = 'Copied';
      } catch {
        // No clipboard access: select the text instead, which always works,
        // rather than silently doing nothing.
        const range = document.createRange();
        range.selectNodeContents(box.querySelector('code'));
        const sel = getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        copy.textContent = 'Selected';
      }
      setTimeout(() => { copy.textContent = said; }, 1600);
    });
  }
  return box;
}

/* Every table on this page, inside its own horizontal scroller, so a wide
   table can never spill invisibly past a narrow column. */
function table(html) {
  const wrap = el('div', 'tblwrap');
  const t = el('table', 'tbl');
  t.innerHTML = html;
  wrap.appendChild(t);
  return wrap;
}

/* The trim buckets are the handful of words that move a price, plus "base"
   for everything else. "base" is labelled "Other", not "no trim": most of
   those cars name a trim, just not one this groups on. */
const TRIM_NAMES = { cs: 'CS', lci: 'LCI', competition: 'Competition',
                    touring: 'Touring', carbon: 'Carbon' };

/* How old the watch is, in words: "started today" rather than "0 days old". */
function age(days) {
  const n = Number(days) || 0;
  if (n < 1) return 'the watch started today';
  if (n === 1) return 'the watch is a day old';
  return `the watch is ${plural(n, 'day')} old`;
}

function trimLabel(name) {
  if (name === 'base' || !name) return 'Other';
  // Known trims keep their own spelling ("CS", not "Cs").
  return TRIM_NAMES[name] || name.charAt(0).toUpperCase() + name.slice(1);
}

function renderMarket() {
  const host = document.querySelector('[data-view="market"]');
  host.innerHTML = '';
  const m = app.data.market || {};

  // With no cars, one empty state rather than a skeleton of zero tiles.
  const carsHere = (app.data.listings || []).length;
  if (!carsHere) {
    const head = el('div', 'view__head measure');
    head.innerHTML = '<h1 id="market-h">The market</h1>';
    host.appendChild(head);
    host.appendChild(emptyState(
      app.data.last_check ? 'No cars to compare yet' : 'Nothing measured yet',
      app.data.last_check
        ? 'The searches have not turned up a car, so there is no market to '
          + 'describe. This page fills in on its own: medians once six '
          + 'comparable cars are being watched, and how fast things sell '
          + 'once the bot has seen some of them leave.'
        : 'This page is about what a hundred cars say together - what the '
          + 'median asks, how long they sit, how often a price actually '
          + 'moves. None of that can be said from a standing start, so it '
          + 'stays empty until the first check has run and then fills in as '
          + 'the watch gets older.'));
    return;
  }

  const head = el('div', 'view__head measure');
  // Listings counts what you can see; the market is the whole market. The
  // heading spells out the difference between the two numbers.
  const hiddenHere = (app.data.listings || [])
    .filter(l => l.status === 'active' && l.filtered).length;
  head.innerHTML = `<h1 id="market-h">The market</h1>
    <p>What the cars say together, rather than what one says. The tiles count
       all ${m.live ?? 0} listings the searches returned${hiddenHere
         ? `, the ${hiddenHere} your rules hide included` : ''};
       the per-model figures below count only the ${(m.live ?? 0) - hiddenHere}
       you could actually buy, because a median of the cars a rule rejects is
       a market you are not shopping in. Everything carries how many cars it
       is drawn from.</p>`;
  host.appendChild(head);

  // The window first, because it decides how much of the rest to believe.
  if (m.window) {
    const w = el('p', 'why measure');
    w.innerHTML = m.window.thin
      ? `<b>Read this as a snapshot.</b> ${esc(m.window.note)}`
      : `<b>${m.window.cars_with_two_prices}</b> cars have been priced more than
         once over ${m.window.watching_days} days of watching.`;
    host.appendChild(w);
  }

  const v = m.velocity || {}, d = m.discounting || {};
  const st = m.still_listed_days || {}, lt = m.listed_days || {};
  const days = n => `${n}<small style="display:inline"> day${n === 1 ? '' : 's'}</small>`;
  // Some of these are bounded by how long the bot has been watching rather
  // than by the market, and are labelled so.
  const sinceWatch = v.window_is_the_watch;
  const stats = el('dl', 'stats');
  stats.innerHTML = `
    <div class="stat"><dt>${sinceWatch ? 'Arrived since watching began' : 'Arrived this week'}</dt>
      <dd class="num">${v.arrived_7d ?? '—'}</dd>
      ${sinceWatch ? `<dd class="stat__note">${age(st.watching_days)}, so that is
        all of them rather than this week's</dd>` : ''}</div>
    <div class="stat"><dt>${sinceWatch ? 'Left since watching began' : 'Left this week'}</dt>
      <dd class="num">${v.left_7d ?? '—'}</dd></div>
    <div class="stat"><dt>Still listed, median</dt>
      <dd class="num">${st.median == null ? '—'
        : (st.censored && !st.median) ? '—'
        : (st.censored ? '<small style="display:inline">at least </small>' : '') + days(st.median)}</dd>
      <dd class="stat__note">${
        st.median == null ? 'nothing to measure yet'
        : (st.censored && !st.median)
          ? 'every car here arrived after the watch on it started, so none of '
            + 'them has a measurable age yet'
        : st.censored ? 'nothing has been watched longer than this'
        : `longest ${days(st.longest ?? 0)}`}</dd></div>
    <div class="stat"><dt>Cars discounted</dt><dd class="num">${d.cars ?? 0}</dd>
      <dd class="stat__note">${d.total ? money(d.total) + ' off in total' : 'none yet'}</dd></div>
    <div class="stat"><dt>Listed before coming down</dt>
      <dd class="num">${lt.median == null ? '—' : days(lt.median)}</dd>
      <dd class="stat__note">from ${lt.n ?? 0} that came down${lt.biased_short
        ? ' — only short-lived ones can finish inside a watch this young' : ''}</dd></div>
    <div class="stat"><dt>Listed right now</dt><dd class="num">${m.live ?? 0}</dd>
      <dd class="stat__note">${hiddenHere
        ? `${(m.live ?? 0) - hiddenHere} live · ${hiddenHere} hidden · `
        : ''}${m.gone ?? 0} gone and kept</dd></div>`;
  host.appendChild(stats);
  if (lt.note) {
    host.appendChild(el('p', 'note measure',
      '“Listed before coming down” is ' + lt.note + '.'));
  }

  // One section per model: prices and trims only mean something within a
  // model, never averaged across different ones.
  for (const row of Object.values(m.by_model || {})) {
    const sec = el('section', 'section measure');
    const count = row.n
      ? `${row.n} to buy${row.hidden ? ` · ${row.hidden} hidden` : ''}`
      : `none to buy · ${row.hidden} hidden`;
    sec.innerHTML = `<div class="section__head">
        <h2>${esc(row.label)}</h2><span class="count num">${count}</span></div>`;

    if (!row.n) {
      sec.appendChild(el('p', 'note', `Every ${esc(row.label)} the searches found is `
        + `outside your rules. The Listings tab says which rule, car by car.`));
      host.appendChild(sec);
      continue;
    }
    sec.appendChild(el('p', 'note', row.thin
      ? `${priceList(row.prices)} — ${row.n} car${row.n === 1 ? '' : 's'}, which is `
        + `too few for a median. The asking prices themselves are above.`
      : `Median ${money(row.median)}, ${money(row.low)} to ${money(row.high)}, `
        + `from ${row.n} cars.`));

    const years = Object.entries(row.by_year || {});
    const fat = years.filter(([, y]) => !y.thin);
    // Years with enough cars get a bar, scaled against the whole model so a
    // single year does not fill the width; years with too few get their
    // prices listed. Each block has its own subhead so the two cannot read
    // as one table.
    const thin = years.filter(([, y]) => y.thin);
    if (fat.length) {
      sec.appendChild(subhead('By year', 'the line is the full range, the '
        + 'block the middle half, the tick the median'));
      sec.appendChild(rangeChart(fat, modelRange(row)));
    }
    if (thin.length) {
      sec.appendChild(subhead(
        fat.length ? 'The other years' : 'By year',
        `fewer than ${MIN_FOR_A_YEAR_MEDIAN} cars each, so the asking prices `
        + `themselves rather than a median`));
      sec.appendChild(table(`<thead><tr><th>Year</th><th class="r">Cars</th>
          <th class="r">Asking</th></tr></thead><tbody>` +
        thin.map(([year, y]) =>
          `<tr><td>${esc(year)}</td><td class="r num">${y.n}</td>
            <td class="r num">${priceList(y.prices)}</td></tr>`).join('') + '</tbody>'));
    }

    const trims = Object.entries(row.by_trim || {}).filter(([, t]) => t.n > 1);
    if (trims.length > 1) {
      const trimTable = table(`<thead><tr><th>Trim</th><th class="r">Cars</th>
          <th class="r">Asking</th></tr></thead><tbody>` +
        trims.map(([name, r]) =>
          `<tr><td>${esc(trimLabel(name))}</td><td class="r num">${r.n}</td>
            <td class="r num">${r.thin ? priceList(r.prices) : money(r.median)}</td>
            </tr>`).join('') + '</tbody>');
      sec.appendChild(subhead('By trim',
        `a median where there are ${MIN_FOR_A_YEAR_MEDIAN} or more of one, `
        + `the prices themselves below that`));
      sec.appendChild(trimTable);
      sec.appendChild(el('p', 'note',
        '"Other" is every trim that is not one of the words that move a '
        + 'price.'));
    }
    host.appendChild(sec);
  }

  // Whether the deal score means anything, said either way.
  const check = app.data.score_check;
  if (check) {
    const sec = el('section', 'section measure');
    sec.innerHTML = `<div class="section__head"><h2>Is the deal score worth anything?</h2></div>
      <p class="note" style="margin-top:0">${esc(sentence(check.verdict))}</p>`;
    if (check.cheap_rate !== undefined) {
      sec.appendChild(table(`<thead><tr><th>Called</th><th class="r">Cars</th>
          <th class="r">Later cut the price</th></tr></thead><tbody>
        <tr><td>cheap for its kind</td><td class="r num">${check.called_cheap}</td>
          <td class="r num">${check.cheap_rate}%</td></tr>
        <tr><td>dear for its kind</td><td class="r num">${check.called_dear}</td>
          <td class="r num">${check.dear_rate}%</td></tr></tbody>`));
    }
    host.appendChild(sec);
  }

  const out = el('section', 'section measure');
  out.innerHTML = `<div class="section__head"><h2>Take it with you</h2></div>`;
  const bar = el('div', 'bar');
  for (const [label, make] of [['Listings as CSV', csvOfListings],
                               ['Everything as JSON', () => JSON.stringify(app.data, null, 1)]]) {
    const b = el('button', 'btn', label);
    b.type = 'button';
    b.addEventListener('click', () => download(label.includes('CSV') ? 'listings.csv' : 'autotrader.json', make()));
    bar.appendChild(b);
  }
  out.appendChild(bar);
  host.appendChild(out);
}

/* The asking prices of every car of this model, however few. The chart's
   axis comes from here rather than from the rows it draws. */
function modelRange(row) {
  const prices = (row.prices || []).filter(p => typeof p === 'number');
  const ends = [row.low, row.high, ...prices].filter(p => typeof p === 'number');
  const years = Object.values(row.by_year || {});
  for (const y of years) {
    for (const p of [y.low, y.high, ...(y.prices || [])])
      if (typeof p === 'number') ends.push(p);
  }
  return ends.length ? { min: Math.min(...ends), max: Math.max(...ends) } : null;
}

/* Below this many cars of a year, the prices are listed rather than reduced
   to a median. Mirrors MIN_FOR_A_MEDIAN in autotrader/insight.py, which
   decides it; this copy only feeds the page's wording. */
const MIN_FOR_A_YEAR_MEDIAN = 5;

/* A label over a block, so two blocks stacked cannot read as one. */
function subhead(title, note) {
  const h = el('div', 'subhead');
  h.innerHTML = `<h3>${esc(title)}</h3>${note ? `<span>${esc(note)}</span>` : ''}`;
  return h;
}

/* One row per year: the range as a bar, the median as a tick. A box plot
   without the jargon, and it degrades to a table on a phone. */
function rangeChart(years, axis) {
  const all = years.flatMap(([, r]) => [r.low, r.high]);
  const min = Math.min(axis?.min ?? Infinity, ...all);
  const max = Math.max(axis?.max ?? -Infinity, ...all);
  const span = (max - min) || 1;
  const wrap = el('div');
  for (const [year, r] of years) {
    const row = el('div');
    row.style.cssText = 'display:grid;grid-template-columns:48px 1fr auto;gap:var(--s3);align-items:center;padding:var(--s1) 0';
    const at = v => ((v - min) / span) * 100;
    // Full range as a hairline, middle half as the solid bar, median as the
    // tick, so one outlier cannot squash every other year into a smudge.
    const q1 = r.q1 ?? r.low, q3 = r.q3 ?? r.high;
    row.innerHTML = `
      <span class="num" style="font-size:var(--t-small)">${esc(year)}</span>
      <span style="position:relative;height:16px;display:block">
        <span style="position:absolute;left:${at(r.low)}%;width:${Math.max(at(r.high) - at(r.low), 0.4)}%;top:7px;height:1px;background:var(--line-strong)"></span>
        <span style="position:absolute;left:${at(q1)}%;width:${Math.max(at(q3) - at(q1), 0.8)}%;top:5px;height:5px;border-radius:2px;background:var(--surface-3)"></span>
        <span style="position:absolute;left:${at(r.median)}%;top:1px;width:2px;height:14px;background:var(--text)"></span>
      </span>
      <span class="num" style="font-size:var(--t-small)">${money(r.median)}
        <u style="text-decoration:none;color:var(--text-3);font-size:var(--t-micro)"> n=${r.n}</u></span>`;
    row.setAttribute('role', 'img');
    row.setAttribute('aria-label',
      `${year}: ${r.n} cars, ${money(r.low)} to ${money(r.high)}, ` +
      `middle half ${money(q1)} to ${money(q3)}, median ${money(r.median)}`);
    wrap.appendChild(row);
  }
  // The axis ends, so the bars can be read against something. Spaced below
  // as well as above so whatever follows does not read as another row.
  const ends = el('div', 'facts');
  ends.style.cssText = ('justify-content:space-between;'
    + 'margin:var(--s2) 0 var(--s5)');
  ends.innerHTML = `<span class="num">${money(min)}</span>`
    + `<span class="num">${money(max)}</span>`;
  wrap.appendChild(ends);
  return wrap;
}

function csvOfListings() {
  const cols = ['id', 'year', 'make', 'model', 'trim', 'price', 'mileage_km',
                'per_1000km', 'distance_km', 'days_listed', 'location',
                'province', 'seller', 'status', 'filtered', 'filter_reason',
                'first_seen', 'last_seen', 'url'];
  const cell = v => {
    const text = v === null || v === undefined ? '' : String(v);
    return /[",\n]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text;
  };
  return [cols.join(',')].concat(
    (app.data.listings || []).map(l => cols.map(c => cell(l[c])).join(','))
  ).join('\n');
}

function download(name, text) {
  const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/* ----------------------------------------------------------------- searches */
function renderSearches() {
  const host = document.querySelector('[data-view="searches"]');
  host.innerHTML = '';
  const head = el('div', 'view__head');
  head.innerHTML = `<h1 id="searches-h">Searches</h1>
    <p>What the bot is looking at, and the rules applied to what it finds. Editing a
       rule shows what it would keep before you commit to it.</p>`;
  host.appendChild(head);

  host.appendChild(recentChanges());

  for (const s of (app.data.searches || [])) {
    const sec = el('section', 'section');
    const h = s.health || {};
    const bad = h.consecutive_failures > 0;
    const shutOut = h.shut_out;
    sec.innerHTML = `<div class="section__head">
        <h2 class="name">${esc(s.name)}</h2>
        <span class="count num">${s.counts?.active ?? 0} live · ${s.counts?.filtered ?? 0} hidden</span>
      </div>`;

    const kv = el('dl', 'kv');
    const area = s.area?.text;
    // The model and area the bot enforces itself are shown, so every rule
    // applied is visible somewhere on the page.
    const models = (s.rules?.filters?.models || []);
    kv.innerHTML =
      `<dt>Watching</dt><dd>${esc(searchWords(s))}
         · <a href="${esc(s.url)}" rel="noopener" target="_blank">open on autotrader.ca</a></dd>` +
      (models.length ? `<dt>Model</dt><dd>${esc(models.join(', '))} <span class="note" style="margin:0">— a backstop. The site does honour the model, and the bot reads only the results it declares, so this rule has nothing left to turn away</span></dd>` : '') +
      (area ? `<dt>Area</dt><dd>${esc(area)} <span class="note" style="margin:0">— enforced here, because the site ignores it</span></dd>` : '') +
      `<dt>Last read</dt><dd>${h.last_ok
          ? `${stamp(h.last_ok)} · ${h.last_count || 0} listing${(h.last_count || 0) === 1 ? '' : 's'}`
            + (h.not_this_car
               ? ` · ${num(h.not_this_car)} of them a different car, discarded`
               : '')
          : 'never — this search has not been read yet'}</dd>` +
      (bad ? `<dt>Trouble</dt><dd class="err">${h.consecutive_failures} failure${
        h.consecutive_failures === 1 ? '' : 's'} in a row — ${esc(h.last_error || '')}</dd>` : '') +
      (shutOut ? `<dt>Note</dt><dd class="warnt">Reads fine, keeps nothing: ${esc(shutOut)}</dd>` : '');
    sec.appendChild(kv);
    sec.appendChild(rulesEditor(s));
    if ((app.data.searches || []).length > 1) {
      const stop = el('div', 'bar');
      stop.style.marginTop = 'var(--s3)';
      stop.appendChild(askButton('Stop watching this search', 'Remove a search',
        [{ action: 'remove-search', search: s.id }]));
      sec.appendChild(stop);
    }
    host.appendChild(sec);
  }

  host.appendChild(pasteALink());
}

/* What became of the changes sent from this page. A refused change is
   otherwise invisible: the file is consumed either way. */
function recentChanges() {
  const frag = document.createDocumentFragment();
  const changes = (app.data.changes || []).slice(0, 3);
  if (!changes.length) return frag;
  const sec = el('section', 'section');
  sec.innerHTML = '<div class="section__head"><h2>Your recent changes</h2></div>';
  const list = el('ul', 'limits');
  for (const c of changes) {
    const li = el('li');
    li.innerHTML = `<b class="${c.ok ? '' : 'err'}">${c.ok ? 'Applied' : 'Refused'}</b>
      ${esc(when(c.at))} — `;
    li.appendChild(document.createTextNode(String(c.text || '').split('\n')
      .filter(Boolean).join(' ')));
    list.appendChild(li);
  }
  sec.appendChild(list);
  frag.appendChild(sec);
  return frag;
}

/* The search as a sentence, from the parts of the link the bot understood,
   so the reader can see it was read correctly. */
function searchWords(s) {
  const m = s.summary || {};
  const bits = [[m.make, m.model].filter(Boolean).join(' ') || 'any car'];
  if (m.year_min && m.year_max) bits.push(`${m.year_min}–${m.year_max}`);
  else if (m.year_min) bits.push(`${m.year_min} or newer`);
  else if (m.year_max) bits.push(`up to ${m.year_max}`);
  else bits.push('any year');
  if (m.price_max) bits.push(`under ${money(m.price_max)}`);
  if (m.mileage_max) bits.push(`under ${km(m.mileage_max)} km`);
  for (const chip of (m.chips || [])) bits.push(chip);
  return bits.join(' · ');
}

function rulesEditor(s) {
  const box = el('div');
  box.style.marginTop = 'var(--s4)';
  const f = { ...(s.rules?.filters || {}) };
  const id = s.id.replace(/[^a-z0-9]/gi, '');
  const box_ = [
    ['mx', 'Max asking', f.max_price, 'no ceiling'],
    ['y0', 'From year', f.min_year, 'any'],
    ['y1', 'To year', f.max_year, 'any'],
    ['km', 'Within km', f.max_distance_km, 'anywhere'],
  ];
  box.innerHTML = `
    <div class="bar">${box_.map(([k, label, value, hint]) => `
      <label class="labelled"><span>${label}</span>
        <span class="field"><input type="number" inputmode="numeric" id="${k}-${id}"
          placeholder="${hint}" value="${value ?? ''}"></span></label>`).join('')}
    </div>
    <p class="why" id="pv-${id}"></p>`;
  const ask = el('div', 'bar');
  ask.style.marginTop = 'var(--s3)';
  box.appendChild(ask);

  const preview = () => {
    const v = k => {
      const n = box.querySelector('#' + k + '-' + id).value.trim();
      return n === '' ? null : Number(n);
    };
    const rule = { max_price: v('mx'), min_year: v('y0'), max_year: v('y1'), max_distance_km: v('km') };
    const pool = (app.data.listings || []).filter(l => l.status === 'active' && l.search_id === s.id);
    // A car hidden by a rule this editor does not show stays hidden. The
    // preview re-runs only its four boxes; `filter_rule` is the bot's own
    // verdict, named by config key. A car with no recorded rule counts as
    // held too, the safe direction.
    const elsewhere = l => l.filtered && !(l.filter_rule in rule);
    const held = pool.filter(elsewhere);
    const kept = pool.filter(l => {
      if (elsewhere(l)) return false;
      if (rule.max_price !== null && l.price !== null && l.price > rule.max_price) return false;
      if (rule.min_year !== null && l.year && l.year < rule.min_year) return false;
      if (rule.max_year !== null && l.year && l.year > rule.max_year) return false;
      if (rule.max_distance_km !== null && l.distance_km !== null &&
          l.distance_km !== undefined && l.distance_km > rule.max_distance_km) return false;
      return true;
    });
    const changed = JSON.stringify(rule) !== JSON.stringify({
      max_price: f.max_price ?? null, min_year: f.min_year ?? null,
      max_year: f.max_year ?? null, max_distance_km: f.max_distance_km ?? null });
    box.querySelector('#pv-' + id).innerHTML = pool.length
      ? `<b>${num(kept.length)}</b> of the ${num(pool.length)} cars this search currently holds would pass`
        + (changed ? ' under the rule above.' : ' under the rule as saved.')
        + (held.length ? ` ${plural(held.length, 'car')} the bot currently hides `
            + `${held.length === 1 ? 'is' : 'are'} not counted here: this preview `
            + 'can only re-run the four rules above.' : '')
      : 'This search is not holding any cars to test the rule against.';

    ask.innerHTML = '';
    if (!changed) return;
    const instructions = Object.entries(rule)
      .filter(([k, v]) => v !== (f[k] ?? null))
      .map(([k, v]) => ({ action: 'set-rule', search: s.id, rule: k, value: v }));
    if (!instructions.length) return;
    ask.appendChild(askButton(
      `Apply this to ${s.name}`,
      `Change the rules on ${s.name}`, instructions,
      `Rules for ${s.name}. On today's ${num(pool.length)} cars this would keep ${num(kept.length)}.`));
    ask.appendChild(el('span', 'note', CHANGE_NOTE));
  };
  box.addEventListener('input', preview);
  preview();
  return box;
}

/* Paste a link and see what it would watch, before committing to it. The
   page is static and cannot write to the repository, so it reads the link
   back and offers the change as a GitHub link (see "the ask" below). */
function pasteALink() {
  const sec = el('section', 'section');
  sec.innerHTML = `<div class="section__head"><h2>Add a search</h2></div>
    <p class="note" style="margin-top:0">Set up the search you want on autotrader.ca, then
       paste the address of the results page here.</p>
    <div class="bar">
      <label class="field" style="flex:1 1 320px"><span class="sr">Search link</span>
        <input type="url" id="paste" placeholder="https://www.autotrader.ca/cars/…"
          spellcheck="false" autocomplete="off"></label>
    </div>
    <div id="paste-out"></div>`;

  const out = sec.querySelector('#paste-out');
  sec.querySelector('#paste').addEventListener('input', e => {
    const raw = e.target.value.trim();
    if (!raw) { out.innerHTML = ''; return; }
    let url;
    try { url = new URL(raw); } catch {
      out.innerHTML = `<p class="why err">That is not a web address yet — it should
        start with <span class="mono">https://</span>.</p>`;
      return;
    }
    if (!/autotrader\.ca$/i.test(url.hostname.replace(/^www\./, ''))) {
      out.innerHTML = `<p class="why err">That is a link to
        <b>${esc(url.hostname)}</b>, not autotrader.ca.</p>`;
      return;
    }
    const q = url.searchParams;
    const bits = [];
    const seg = url.pathname.split('/').filter(Boolean);
    if (seg[0] === 'cars' && seg[1]) bits.push(seg.slice(1, 3).join(' ').toUpperCase());
    const yr = q.get('yRng');
    if (yr) bits.push(yr.replace('%2C', ',').replace(',', '–'));
    const pr = q.get('pRng');
    if (pr) bits.push('price ' + pr.replace(',', '–'));
    if (q.get('loc')) bits.push('near ' + q.get('loc'));
    out.innerHTML = `
      <p class="why"><b>Reads as:</b> ${bits.length ? esc(bits.join(' · ')) : 'every car on that page'}.
        The bot re-reads the link itself on every check, so anything it did not
        understand here is still applied by the site.</p>`;
    const bar = el('div', 'bar');
    bar.appendChild(askButton('Add this search', 'Add a search',
      [{ action: 'add-search', url: raw }]));
    bar.appendChild(el('span', 'note', CHANGE_NOTE));
    out.appendChild(bar);
  });
  return sec;
}

/* ----------------------------------------------------------------- status */
function renderStatus() {
  const host = document.querySelector('[data-view="status"]');
  host.innerHTML = '';
  const d = app.data;
  // As in trustState: "Last good check", "Requests last check" and "Check
  // took" describe a check, and a firing that stood down is not one.
  const run = lastCheck(d);
  const firing = d.last_run || {};
  const cov = d.coverage || {};
  const h = d.health || {};

  const head = el('div', 'view__head');
  head.innerHTML = `<h1 id="status-h">Status</h1>
    <p>Whether the thing watching is working. The useful measure is not whether the
       last check passed — it is what share of the checks it was meant to make it made.</p>`;
  host.appendChild(head);

  // The colour goes with the number: no number, no colour.
  const covPct = coveragePct(cov);
  const covKept = whoKeptTime(cov);
  const covTone = covPct === null ? ''
    : covPct >= 80 ? 'good' : covPct >= 40 ? 'warn' : 'bad';
  const stats = el('dl', 'stats');
  stats.innerHTML = `
    <div class="stat${covKept && covKept.level !== 'all' ? ' stat--wide' : ''}"
         data-tone="${covKept && covKept.level === 'none' ? 'warn' : covTone}">
      <dt>Coverage, ${hours(cov.window_hours || 24)}</dt>
      <dd class="num">${covPct === null ? '—' : `${covPct}%`}</dd>
      <dd class="stat__note">${cov.too_short
        ? `measuring for ${hours(cov.window_hours)} so far, since the schedule `
          + `changed to one check every ${every(cov.expected_interval_minutes)}. `
          + `${cov.successful ?? 0} check${cov.successful === 1 ? '' : 's'} in that time.`
        : `${cov.slots_covered ?? cov.successful ?? 0} of ${cov.expected ?? 0} ${slotWord(cov)}`
          + `${cov.partial ? ` in the ${hours(cov.window_hours)} since the schedule changed` : ''}`
          // `> 0`, not truthy: a count of events is never negative, even if
          // a malformed payload says so.
          + `${cov.complained > 0 ? ` · ${cov.complained} of ${cov.successful} checks complained` : ''}`
        }</dd>
      ${covKept && covKept.level !== 'all' ? `<dd class="stat__note">${
        covKept.level === 'unknown'
          ? 'These checks were recorded before the bot noted what started '
            + 'them, so this cannot say how many the schedule filled.'
        : covKept.level === 'none'
          ? `<b>The schedule filled none of them.</b> Every check came from `
            + `${andList(Object.keys(cov.by_trigger || {})
                  .filter(k => k !== 'schedule' && k !== 'repository_dispatch')
                  .map(triggerWord))}.`
            // "Stop doing that" only applies to triggers the bot can name;
            // an unattributed run might have been the schedule.
            + ((cov.by_trigger || {}).unattributed
               ? ' The unattributed ones may have been the schedule — they'
                 + ' predate the bot recording what started a run.'
               : ' Stop doing that and this number goes to zero.')
          : `${num(covKept.mine)} of those ${num(covKept.all)} came from the `
            + `schedule; the rest from `
            + `${andList(Object.keys(cov.by_trigger || {})
                  .filter(k => k !== 'schedule' && k !== 'repository_dispatch')
                  .map(triggerWord))}.`
      }</dd>` : ''}</div>
    <div class="stat"><dt>Last good check</dt><dd>${when(run.at)}</dd>
      <dd class="stat__note">${stamp(run.at)}${
        // When the most recent firing was not this check, say what it was.
        firing.at && firing.at !== run.at
          ? ` \u00b7 ${firing.skipped
                ? 'a firing stood down'
                : (firing.ok === false ? 'a firing failed' : 'a firing ran')} ${when(firing.at)}`
          : ''}</dd></div>
    <div class="stat" data-tone="${cov.longest_gap_minutes > 180 ? 'warn' : ''}"><dt>Longest gap</dt>
      <dd class="num">${cov.longest_gap_minutes ? Math.round(cov.longest_gap_minutes / 60 * 10) / 10 : '—'}h</dd>
      <dd class="stat__note">between good checks</dd></div>
    <div class="stat"><dt>Requests last check</dt><dd class="num">${run.requests_made ?? '—'}</dd>
      <dd class="stat__note">budget ${h.budget?.limit ?? '—'}</dd></div>
    <div class="stat"><dt>Check took</dt><dd class="num">${
      run.duration_s == null ? '—' : Math.round(run.duration_s)}s</dd>
      <dd class="stat__note">${d.cost
        ? `${d.cost.checks} checks · ${d.cost.billed_minutes ?? d.cost.minutes} billed minutes in ${d.cost.window_hours}h`
          + (cov.stood_down ? ` · ${num(cov.stood_down)} firing${
              cov.stood_down === 1 ? '' : 's'} stood down` : '')
        : ''}</dd></div>
    ${d.budget ? `<div class="stat" data-tone="${
        d.budget.state === 'stop' ? 'bad' : d.budget.state === 'over' ? 'warn' : ''}">
      <dt>Allowance minutes this month</dt>
      <dd class="num">${num(d.budget.drawing_minutes ?? d.budget.used)}${
        (d.budget.drawing_minutes ?? 0) > 0
          ? `<small style="display:inline"> / ${num(d.budget.allowance)}</small>`
          : ''}</dd>
      <dd class="stat__note">${esc(d.budget.short || d.budget.text)}</dd></div>
    <div class="stat"><dt>Exempt minutes</dt>
      <dd class="num">${num(d.budget.exempt_minutes ?? 0)}</dd>
      <dd class="stat__note">${(d.budget.exempt_minutes ?? 0) > 0
        ? 'ran free' : 'none labelled yet \u2014 from here'} \u2014 ${
        esc(d.budget.why || 'reason not recorded')}</dd></div>
    <div class="stat" data-tone="${d.budget.can_still_run === false ? 'bad' : 'good'}">
      <dt>Can it still run</dt>
      <dd>${d.budget.can_still_run === false ? 'No' : 'Yes'}</dd>
      <dd class="stat__note">${d.budget.can_still_run === false
        ? 'the bot has stopped itself; delete BUDGET-STOP to start it again'
        : `nothing here is stopping it \u2014 ${plural(d.budget.days_to_reset ?? 0, 'day')} `
          + 'until the allowance refills'}</dd></div>` : ''}
    ${cov.timekeeper ? `<div class="stat" data-tone="${
        covKept && covKept.level === 'none' ? 'warn' : ''}">
      <dt>Keeping time</dt>
      <dd>${esc(triggerName(cov.timekeeper))}</dd>
      <dd class="stat__note">${andList(Object.entries(cov.slots_by_trigger || {})
        .map(([k, n]) => `${plural(n, slotWord(cov, false))} from ${esc(triggerWord(k))}`))
        || 'nothing has filled a slot yet'}</dd></div>` : ''}
    <div class="stat" data-tone="${h.accounted?.unexplained ? 'bad' : 'good'}"><dt>Unaccounted cars</dt>
      <dd class="num">${h.accounted?.unexplained ?? 0}</dd>
      <dd class="stat__note">${h.accounted?.delivered ?? 0} told, ${h.accounted?.quiet ?? 0} deliberately quiet</dd></div>`;
  host.appendChild(stats);

  // When the checks happened, not just how many: a percentage cannot tell a
  // schedule thin everywhere from one absent for hours at a stretch.
  if ((cov.slots || []).length) {
    const sec = el('section', 'section measure');
    sec.innerHTML = `<div class="section__head"><h2>When it checked</h2>
      <span class="count num">${cov.slots_covered ?? 0}/${cov.expected ?? 0}</span></div>`;
    const strip = el('div', 'slots');
    strip.setAttribute('role', 'img');
    strip.setAttribute('aria-label',
      `${cov.slots_covered ?? 0} of ${cov.expected ?? 0} ${slotWord(cov)} in the `
      + `last ${hours(cov.window_hours ?? 24)} had a check. `
      + `Longest gap ${Math.round((cov.longest_gap_minutes || 0) / 6) / 10} hours.`);
    const begin = Date.parse(cov.since);
    const step = (cov.expected_interval_minutes || 30) * 60000;
    cov.slots.forEach((v, i) => {
      const cell = el('i', 'slot');
      cell.dataset.state = v === 0 ? 'miss' : v === 1 ? 'ok' : 'warn';
      const at = new Date(begin + i * step);
      cell.title = `${at.toLocaleString('en-CA', { weekday: 'short', hour: '2-digit',
        minute: '2-digit' })} — ` + (v === 0 ? 'no check'
          : v === 1 ? 'checked' : 'checked, reported a problem');
      strip.appendChild(cell);
    });
    sec.appendChild(strip);
    const ends = el('div', 'slots__ends');
    ends.innerHTML = `<span>${when(cov.since)}</span><span>now</span>`;
    sec.appendChild(ends);
    sec.appendChild(el('p', 'note',
      `Each mark is ${every(cov.expected_interval_minutes || 30)}. `
      + `Longest gap ${hours((cov.longest_gap_minutes || 0) / 60)}.`));
    host.appendChild(sec);
  }

  if ((run.invariants || []).length) {
    const s = el('section', 'section');
    s.innerHTML = `<div class="section__head"><h2>Bookkeeping failures</h2></div>` +
      `<ul class="note" style="padding-left:var(--s4)">` +
      run.invariants.map(v => `<li class="err">${esc(v)}</li>`).join('') + `</ul>`;
    host.appendChild(s);
  }

  // Run history as one mark per check. A gap reads as a gap.
  const runs = (d.runs || []).slice().reverse();
  if (runs.length) {
    const s = el('section', 'section');
    s.innerHTML = `<div class="section__head"><h2>Recent checks</h2>
      <span class="count num">${runs.length} kept</span></div>`;
    const tl = el('div', 'timeline');
    tl.setAttribute('role', 'img');
    tl.setAttribute('aria-label',
      `${runs.filter(r => r.ok).length} of the last ${runs.length} checks succeeded`);
    let prev = null;
    for (const r of runs) {
      const t = Date.parse(r.at);
      if (prev && t - prev > (cov.expected_interval_minutes || 30) * 60000 * 2) {
        const g = el('i'); g.dataset.gap = '1'; tl.appendChild(g);
      }
      const i = el('i');
      i.dataset.ok = r.ok ? '1' : '0';
      i.style.height = Math.max(16, Math.min(100, (r.duration_s || 10) * 2)) + '%';
      i.title = `${stamp(r.at)} — ${r.ok ? 'ok' : 'failed'}, ${r.listings_seen ?? 0} listings, ${r.duration_s ?? '?'}s`;
      tl.appendChild(i);
      prev = t;
    }
    s.appendChild(tl);
    s.appendChild(el('p', 'note',
      `One mark per check, oldest first; height is how long it took. A dotted line is a gap longer than two intervals — a stretch with no check in it at all.`));
    host.appendChild(s);
  }

  // Parser ladder: which rungs scored, not just which one won.
  const strat = h.strategies || {};
  if (Object.keys(strat).length) {
    const s = el('section', 'section');
    s.innerHTML = `<div class="section__head"><h2>Parser ladder</h2></div>`;
    const ladderHtml = `<thead><tr><th>Search</th><th>Winner</th><th>Working</th><th class="r">Scores</th></tr></thead><tbody>` +
      Object.entries(strat).map(([, v]) => {
        const order = v.order || [];
        const lad = order.map(n => `<i data-on="${(v.working || []).includes(n) ? 1 : 0}" title="${esc(n)}"></i>`).join('');
        const scores = order.map(n => `${n} ${v.scores?.[n] ?? 0}`).join(' · ');
        return `<tr><td>${esc(v.name)}</td><td class="mono">${esc(v.winner || '—')}</td>
          <td><span class="ladder" role="img" aria-label="${(v.working || []).length} of ${v.of} strateg${v.of === 1 ? 'y' : 'ies'} working">${lad}</span></td>
          <td class="r mono" style="font-size:var(--t-micro)">${esc(scores)}</td></tr>`;
      }).join('') + `</tbody>`;
    s.appendChild(table(ladderHtml));
    if ((h.drift || []).length) {
      s.appendChild(el('p', 'note warnt', 'Shape drift: ' + esc(h.drift.join('; '))));
    }
    host.appendChild(s);
  }

  // Where alerts go, and whether they arrived.
  const s2 = el('section', 'section');
  s2.innerHTML = `<div class="section__head"><h2>Alerts</h2></div>`;
  const rows = Object.entries(d.channel_health || {});
  const active = (d.notify?.active || []);
  const channelsHtml = `<thead><tr><th>Channel</th><th>State</th><th>Last good</th></tr></thead><tbody>` +
    (active.length ? active.map(name => {
      const ch = (d.channel_health || {})[name] || {};
      const fails = ch.consecutive_failures || 0;
      return `<tr><td>${esc(d.channels?.[name]?.label || name)}</td>
        <td class="${fails ? 'err' : 'ok'}">${fails ? `${fails} failure${fails === 1 ? '' : 's'} in a row` : 'delivering'}</td>
        <td class="num">${ch.last_ok ? when(ch.last_ok) : '—'}</td></tr>`;
    }).join('') : `<tr><td colspan="3">No channel is switched on, so nothing is being sent.</td></tr>`) +
    // A channel switched off on purpose, with the reason given, so a
    // deliberate decision does not read as a fault.
    Object.entries(d.channels || {})
      .filter(([name, c]) => !c.active && c.disabled_reason)
      .map(([name, c]) => `<tr><td>${esc(c.label || name)}</td>
        <td colspan="2" class="note">${esc(sentence(c.disabled_reason, 'Switched off'))}</td></tr>`).join('') +
    `</tbody>`;
  s2.appendChild(table(channelsHtml));
  if (d.notify?.ntfy_url) s2.appendChild(onYourPhone(d.notify));
  host.appendChild(s2);

  // What this page cannot tell you: the bounds on every figure above, where
  // the reader is deciding whether to believe it. Built from the same values
  // as the tiles, so a line that stops applying stops appearing.
  const limits = [];
  if (d.budget?.blind_spot) limits.push(d.budget.blind_spot);
  const gap = cov.longest_gap_minutes;
  if (gap) {
    limits.push(`A car could have been listed and taken down inside the `
      + `longest gap between checks - ${hours(gap / 60)} - and nothing here `
      + `would ever have known about it. That is what the coverage figure is `
      + `for.`);
  }
  if (cov.partial) {
    limits.push(`Coverage is measured over ${hours(cov.window_hours)} rather `
      + `than a full day, because that is how long the current schedule has `
      + `been running. It is not yet a statement about a day.`);
  }
  const unattributed = (cov.by_trigger || {}).unattributed || 0;
  if (unattributed) {
    limits.push(unattributed === 1
      ? `One check on record carries no note of what started it, so it `
        + `counts towards coverage and not towards the schedule. It may have `
        + `been the schedule; this cannot say.`
      : `${num(unattributed)} of the checks on record carry no note of what `
        + `started them, so they count towards coverage and not towards the `
        + `schedule. They may have been the schedule; this cannot say.`);
  }
  limits.push('A listing coming down means the seller stopped advertising '
    + 'it. Whether it sold, and for what, is not on the site and is not '
    + 'guessed at here.');
  if (limits.length) {
    const s3 = el('section', 'section measure');
    s3.innerHTML = '<div class="section__head"><h2>What this page cannot '
      + 'tell you</h2></div>';
    const list = el('ul', 'limits');
    for (const line of limits) {
      const li = el('li');
      li.textContent = line;
      list.appendChild(li);
    }
    s3.appendChild(list);
    host.appendChild(s3);
  }
  void rows;
}

/* -------------------------------------------------------------- the ask
   A static page cannot write to the repository, and a phone should not carry
   a token. So a change opens GitHub's web editor with the file already filled
   in, and the next check applies it whole or refuses it whole. The file is
   sealed with the vault key and named by time alone, because the repository
   is public: neither the file nor its commit may say what the change is.
   An unencrypted page has no key to seal with, so it offers no change links. */
async function askUrl(title, instructions) {
  const repo = app.data?.repo;
  if (!repo || !vault.lock) return null;
  const now = new Date();
  const stamp = now.toISOString().replace(/[-:T]/g, '').slice(0, 14);
  // A one-time id and the time, so the bot can refuse a replayed change,
  // padded with spaces to a whole kilobyte so the file's size does not say
  // what kind of change it is.
  const raw = te.encode(JSON.stringify({
    id: hex(crypto.getRandomValues(new Uint8Array(16))),
    at: now.toISOString(),
    changes: instructions,
  }));
  const body = new Uint8Array(Math.ceil((raw.length + 1) / 1024) * 1024).fill(0x20);
  body.set(raw);
  const sealed = await sealBytes(vault.enc, body, 'control');
  const nonce = hex(crypto.getRandomValues(new Uint8Array(3)));
  return `https://github.com/${repo}/new/main`
       + `?filename=${encodeURIComponent(`control/${stamp}-${nonce}.enc`)}`
       + `&value=${encodeURIComponent(toB64(sealed))}`
       + `&message=${encodeURIComponent('Change from the dashboard')}`;
}

const CHANGE_NOTE = 'Opens GitHub with the change filled in. Commit it, and a '
  + 'workflow applies it and runs a check — usually inside a minute.';

/* Opens the change in a new tab. The tab is opened first, while the tap
   still counts as a user gesture, and pointed at the URL once it is sealed:
   popup blockers refuse a window opened after an await. */
function openAsk(title, instructions, prose) {
  if (!vault.lock || !app.data?.repo) return;
  const tab = window.open('about:blank', '_blank');
  askUrl(title, instructions, prose).then(url => {
    if (!url) { if (tab) tab.close(); return; }
    if (tab) { tab.opener = null; tab.location.href = url; }
    else window.open(url, '_blank', 'noopener');
  });
}

function askButton(label, title, instructions, prose) {
  if (!app.data?.repo || !vault.lock) {
    const dead = el('button', 'btn');
    dead.textContent = label;
    dead.type = 'button';
    dead.disabled = true;
    dead.title = vault.lock
      ? 'This page does not know which repository it belongs to.'
      : 'Changes are sent from the published, locked dashboard.';
    return dead;
  }
  const a = el('a', 'btn');
  a.textContent = label;
  a.target = '_blank'; a.rel = 'noopener';
  a.style.cssText = 'display:inline-flex;align-items:center;text-decoration:none';
  a.setAttribute('aria-disabled', 'true');
  askUrl(title, instructions, prose).then(url => {
    a.href = url;
    a.removeAttribute('aria-disabled');
  });
  return a;
}

/* Your marks on a car. Kept in the browser for instant feedback and in the
   repository so they survive a new device; the local copy wins. */
const marks = {
  all() { return store.get('marks', {}); },
  of(id) {
    const local = this.all()[String(id)] || {};
    const remote = (byId(id) || {}).you || {};
    return { ...remote, ...local };
  },
  set(id, patch) {
    const all = this.all();
    all[String(id)] = { ...(all[String(id)] || {}), ...patch };
    for (const [k, v] of Object.entries(patch)) if (!v) delete all[String(id)][k];
    store.set('marks', all);
  },
};

/* ----------------------------------------------------------------- sheet */
let lastFocus = null;

function openSheet(id) {
  const l = byId(id);
  const sheet = document.getElementById('sheet');
  const scrim = document.getElementById('scrim');
  const body = document.getElementById('sheet-body');
  lastFocus = document.activeElement;

  if (!l) {
    body.innerHTML = '';
    body.appendChild(emptyState('That listing is not in the published data',
      'It may have been dropped when the file was trimmed, or the link may be from an older alert.'));
  } else {
    document.getElementById('sheet-title').textContent = carName(l);
    body.innerHTML = '';
    body.appendChild(sheetBody(l));
  }
  sheet.hidden = false; scrim.hidden = false;
  requestAnimationFrame(() => { sheet.dataset.open = '1'; scrim.dataset.open = '1'; });
  document.body.style.overflow = 'hidden';
  document.getElementById('sheet-close').focus();
  if (l) history.replaceState(null, '', `#/listing/${encodeURIComponent(l.id)}`);
}

function closeSheet() {
  const sheet = document.getElementById('sheet');
  const scrim = document.getElementById('scrim');
  sheet.dataset.open = '0'; scrim.dataset.open = '0';
  document.body.style.overflow = '';
  const done = () => { sheet.hidden = true; scrim.hidden = true; };
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) done();
  else setTimeout(done, 180);
  history.replaceState(null, '', `#/${app.view}`);
  if (lastFocus && lastFocus.isConnected) lastFocus.focus();
}

function sheetBody(l) {
  const frag = document.createDocumentFragment();
  const move = priceMove(l);
  const cmp = app.data.comparables?.[String(l.id)];
  const says = comparableSays(cmp, l);

  const gal = el('div', 'gallery');
  // Only the photos we hold a copy of. The seller's CDN is unreachable
  // offline and drops photos once the car is delisted.
  const imgs = (l.thumbs && l.thumbs.length ? l.thumbs
                : [l.thumb].filter(Boolean)).slice(0, 8);
  const published = l.photo_count || 0;
  if (imgs.length) {
    for (const src of imgs) {
      const box = el('div', 'shotbox');
      const img = new Image();
      img.loading = 'lazy'; img.decoding = 'async'; img.alt = '';
      img.width = 400; img.height = 300;
      img.style.cssText = 'width:100%;height:100%;object-fit:cover;border-radius:var(--r-sm)';
      img.addEventListener('error', () => {
        box.innerHTML = `<div class="shot__fallback">${CAR_GLYPH}<span>photo not loaded</span></div>`;
      }, { once: true });
      setPhoto(img, src, true);
      box.appendChild(img);
      gal.appendChild(box);
    }
    gal.setAttribute('role', 'group');
    gal.setAttribute('aria-label',
      `${imgs.length} photo${imgs.length === 1 ? '' : 's'}`);
    frag.appendChild(gal);
    if (published > imgs.length) {
      const note = el('p', 'note');
      note.style.margin = '0 0 var(--s4)';
      note.innerHTML = `${imgs.length} of ${published} photos kept here. `
        + `<a href="${esc(l.url || '#')}" rel="noopener" target="_blank">See them all on autotrader.ca</a>`;
      frag.appendChild(note);
    }
  } else {
    const box = el('div', 'shotbox');
    box.style.cssText = 'width:100%;aspect-ratio:4/3;border-radius:var(--r-sm);margin-bottom:var(--s4)';
    box.innerHTML = `<div class="shot__fallback">${CAR_GLYPH}<b>${esc(carName(l))}</b><span>${l.filtered ? 'photos not kept for hidden cars' : 'no photo published'}</span></div>`;
    frag.appendChild(box);
  }

  const price = el('div');
  price.style.marginBottom = 'var(--s4)';
  price.innerHTML =
    `<div style="display:flex;align-items:baseline;gap:var(--s3);flex-wrap:wrap">
      <span class="num" style="font-size:var(--t-display);font-weight:560;letter-spacing:-.02em">
        ${l.unpriced ? 'Call for price' : money(l.price)}</span>
      ${move ? `<span class="card__was num">${money(move.was)}</span>
        <span class="num ${move.delta < 0 ? 'drop' : 'rise'}" style="font-weight:520">${signed(move.delta)}</span>` : ''}
    </div>` +
    (says?.sentence ? `<p class="note note--cmp">${says.sentence}</p>` : '');
  frag.appendChild(price);

  // Why you are seeing this, or why you did not hear about it.
  const why = el('p', 'why');
  if (l.filtered) {
    why.innerHTML = `<b>You were not told about this.</b> It is hidden by a rule on
      ${esc(l.search_name || 'this search')}: ${esc(l.filter_reason || 'a rule of yours')}.
      It is kept and tracked so a change to it is never silently lost.`;
  } else if (l.notified_at) {
    why.innerHTML = `<b>You were told about this</b> at ${stamp(l.notified_at)}, via the
      channels switched on at the time.`;
  } else if (l.quiet_reason) {
    why.innerHTML = `<b>Deliberately quiet.</b> ${esc(l.quiet_reason)}.`;
  } else {
    why.innerHTML = `<b>No delivery record.</b> That is a fault rather than a decision, and
      the next check's bookkeeping test should fail on it.`;
  }
  frag.appendChild(why);

  const hist = (l.price_history || []).filter(p => p.price);
  if (hist.length >= 2) {
    const s = el('section', 'section');
    s.innerHTML = `<div class="section__head"><h2>Asking price</h2>
      <span class="count num">${hist.length} observation${hist.length === 1 ? '' : 's'}</span></div>`;
    s.appendChild(sparkline(hist));
    host_append(s, frag);
  }

  const spec = el('section', 'section');
  spec.innerHTML = `<div class="section__head"><h2>Specification</h2></div>`;
  const kv = el('dl', 'kv');
  const pairs = [
    ['Year', l.year], ['Odometer', l.mileage_km ? `${km(l.mileage_km)} km` : null],
    // This row explains its own blank: most causes are about the ratio,
    // not the car.
    [`Asking ${PER_KM}`, l.per_1000km ? money(l.per_1000km)
      : l.per_1000km_why ? `not shown — ${l.per_1000km_why}` : null],
    ['Distance', (l.distance_km ?? null) === null ? null
      : l.distance_km < 1 ? `in ${esc(l.distance_from || 'your area')}`
      : `${km(l.distance_km)} km from ${esc(l.distance_from || 'home')}`],
    ['On the market', l.days_listed === undefined ? null : daysListed(l.days_listed)], ['Colour', l.color], ['Body', l.body],
    ['Transmission', l.transmission], ['Drivetrain', l.drivetrain], ['Fuel', l.fuel],
    ['Location', [l.location, l.province].filter(Boolean).join(', ')],
    ['Seller', l.seller], ['Watched by', l.search_name],
  ].filter(([, v]) => v !== null && v !== undefined && v !== '');
  kv.innerHTML = pairs.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');
  spec.appendChild(kv);
  const extras = carExtras(l);
  if (extras.length) {
    spec.appendChild(el('p', 'note', 'Dealer copy: ' + extras.join(' · ')));
  }
  host_append(spec, frag);

  const events = (app.data.events || []).filter(e => String(e.listing_id) === String(l.id));
  if (events.length) {
    const s = el('section', 'section');
    s.innerHTML = `<div class="section__head"><h2>Its life so far</h2>
      <span class="count num">${events.length}</span></div>`;
    const ul = el('ul', 'life');
    for (const e of events) {
      const li = el('li');
      let what = KIND[e.kind]?.rule || e.kind;
      if (e.kind === 'price_drop' || e.kind === 'price_rise') {
        what = `${money(e.old_price)} → ${money(e.new_price)} <span class="num ${e.kind === 'price_drop' ? 'drop' : 'rise'}">${signed(e.delta)}</span>`;
      }
      // For a sent alert the sentence is built here, where the date
      // formatter lives; delivery.text would carry a raw ISO timestamp.
      const told = e.delivery?.state === 'sent' && e.delivery.at
        ? `Told you ${stamp(e.delivery.at)}`
        : (e.delivery?.text || '');
      li.innerHTML = `<time datetime="${esc(e.at)}">${stamp(e.at)}</time>
        <span>${what}<br><span class="note" style="margin:0">${esc(told)}</span></span>`;
      ul.appendChild(li);
    }
    s.appendChild(ul);
    host_append(s, frag);
  }

  const yours = el('div', 'bar');
  yours.style.marginTop = 'var(--s6)';
  const mine = marks.of(l.id);
  const name = carName(l);
  for (const [key, on, off, action, undo] of [
    ['shortlisted', 'On your shortlist', 'Shortlist', 'shortlist', 'unshortlist'],
    ['muted', 'Muted', 'Mute this car', 'mute-listing', 'unmute-listing'],
    ['dismissed', 'Dismissed', 'Not interested', 'dismiss', 'undismiss'],
  ]) {
    const active = !!mine[key];
    const b = el('button', 'chip', active ? on : off);
    b.type = 'button';
    b.setAttribute('aria-pressed', String(active));
    b.addEventListener('click', () => {
      marks.set(l.id, { [key]: !active });
      // Instant here, permanent there: the tap lands now, and the change
      // file makes it survive a new device.
      openAsk(
        `${active ? undo : action}: ${name}`,
        [{ action: active ? undo : action, listing: String(l.id) }],
        `${active ? undo : action} for ${name} — opened from the dashboard.`);
      openSheet(l.id);
    });
    yours.appendChild(b);
  }
  frag.appendChild(yours);

  // A free-text note to yourself, shown on the card as well.
  const noteBox = el('div', 'note-edit');
  noteBox.style.marginTop = 'var(--s3)';
  const field = el('textarea', 'field');
  field.rows = 2;
  field.maxLength = 400;
  field.placeholder = 'A note to yourself about this car…';
  field.value = mine.note || '';
  field.id = `note-${l.id}`;
  const label = el('label', 'note-edit__label', mine.note ? 'Your note' : 'Add a note');
  label.setAttribute('for', field.id);
  const save = el('button', 'chip', 'Save note');
  save.type = 'button';
  save.disabled = true;
  field.addEventListener('input', () => {
    save.disabled = field.value.trim() === (mine.note || '').trim();
  });
  save.addEventListener('click', () => {
    const text = field.value.trim().slice(0, 400);
    marks.set(l.id, { note: text });
    openAsk(
      text ? `Note on ${name}` : `Clear the note on ${name}`,
      [{ action: 'note', listing: String(l.id), text }],
      `A note on ${name} — opened from the dashboard.`);
    openSheet(l.id);
  });
  noteBox.appendChild(label);
  noteBox.appendChild(field);
  const noteBar = el('div', 'bar');
  noteBar.style.marginTop = 'var(--s2)';
  noteBar.appendChild(save);
  noteBox.appendChild(noteBar);
  frag.appendChild(noteBox);

  const go = el('div', 'bar');
  go.style.marginTop = 'var(--s3)';
  const a = el('a', 'btn btn--primary', 'Open on autotrader.ca');
  a.href = l.url; a.rel = 'noopener'; a.target = '_blank';
  a.style.cssText = 'display:inline-flex;align-items:center;text-decoration:none';
  go.appendChild(a);
  frag.appendChild(go);
  return frag;
}
function host_append(section, frag) { frag.appendChild(section); }

/* A price history needs no chart library: a few points, and the only
   question is which way it went. */
function sparkline(hist) {
  const w = 100, h = 44, pad = 3;
  const prices = hist.map(p => p.price);
  const min = Math.min(...prices), max = Math.max(...prices);
  const span = (max - min) || 1;
  const pts = prices.map((p, i) => {
    const x = pad + (i / Math.max(1, prices.length - 1)) * (w - pad * 2);
    const y = h - pad - ((p - min) / span) * (h - pad * 2);
    return [x, y];
  });
  const d = pts.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(' ');
  const area = `${d} L${pts[pts.length - 1][0].toFixed(1)} ${h} L${pts[0][0].toFixed(1)} ${h} Z`;
  const svg = el('div');
  svg.innerHTML =
    `<svg class="hist" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img"
       aria-label="Asking price from ${money(prices[0])} to ${money(prices[prices.length - 1])} over ${prices.length} observation${prices.length === 1 ? '' : 's'}">
      <path class="area" d="${area}"/><path d="${d}"/>
      <circle cx="${pts[pts.length - 1][0].toFixed(1)}" cy="${pts[pts.length - 1][1].toFixed(1)}" r="1.8"/>
    </svg>
    <div class="facts" style="justify-content:space-between;margin-top:var(--s2)">
      <span class="num">${money(min)} low</span><span class="num">${money(max)} high</span>
    </div>`;
  return svg;
}

/* ----------------------------------------------------------------- boot */
function skeleton() {
  const remembered = store.get('lastTrust', null);
  if (remembered) {
    // The pill only, never the alarm: a stale pill is corrected a moment
    // later, but a stale alarm would be a false claim. renderTrust() raises
    // the alarm once there is data to raise it from.
    document.getElementById('trust').dataset.state = remembered.state;
    document.getElementById('trust-text').textContent = remembered.text;
  }
  const host = document.querySelector('[data-view="feed"]');
  host.hidden = false;
  host.innerHTML = `<div class="view__head measure"><h1 id="feed-h">What changed</h1>
    <p>Everything that has happened to a car you are watching, newest first — including
       cars your rules hide, which the run counters never counted.</p></div>`;
  const sec = el('section', 'section measure');
  sec.innerHTML = `<div class="section__head"><h2>&nbsp;</h2></div>`;
  const list = el('ul', 'feed');
  /* Real rows with the text taken out, not bars of a guessed height: same
     elements, classes and font metrics, so the height matches the real row
     and the page does not shift when the data lands. */
  const bar = n => '\u00a0'.repeat(n);
  for (let i = 0; i < 8; i++) {
    const li = el('li');
    li.innerHTML = `<div class="ev skel-row" aria-hidden="true">
        <time class="ev__when skel__text">${bar(6)}</time>
        <span class="ev__title skel__text">${bar(16 + (i % 4) * 4)}</span>
        <span class="ev__fig skel__text">${bar(8)}</span>
        <span class="ev__sub"><span class="skel__text">${bar(20 + (i % 3) * 5)}</span></span>
      </div>`;
    list.appendChild(li);
  }
  sec.appendChild(list);
  host.appendChild(sec);
}

function render() {
  if (!app.data) return;
  eagerSlots = 6;
  renderTrust();
  renderClock();
  if (app.view === 'feed') renderFeed();
  else if (app.view === 'listings') renderListings();
  else if (app.view === 'market') renderMarket();
  else if (app.view === 'searches') renderSearches();
  else if (app.view === 'status') renderStatus();
}

function route() {
  const h = location.hash.replace(/^#\/?/, '');
  const [what, arg] = h.split('/');
  if (what === 'listing' && arg) {
    const view = app.view || 'feed';
    go(view, { silent: true, focus: false });
    openSheet(decodeURIComponent(arg));
    return;
  }
  if (VIEWS.some(v => v.id === what)) go(what, { silent: true, focus: false });
  else go('feed', { silent: true, focus: false });
}

function showLock() {
  document.body.dataset.locked = '1';
  const form = document.getElementById('lock');
  const pass = document.getElementById('lock-pass');
  const go = document.getElementById('lock-go');
  const msg = document.getElementById('lock-msg');
  form.hidden = false;
  pass.focus();
  form.addEventListener('submit', async e => {
    e.preventDefault();
    go.disabled = true;
    go.textContent = 'Unlocking…';
    msg.textContent = '';
    try {
      await unlockWith(pass.value, document.getElementById('lock-keep').checked);
    } catch {
      msg.textContent = 'That passphrase does not open this dashboard.';
      go.disabled = false;
      go.textContent = 'Unlock';
      pass.select();
      return;
    }
    pass.value = '';
    form.hidden = true;
    delete document.body.dataset.locked;
    start();
  });
}

async function boot() {
  renderTabs();
  vault.lock = await fetchLock();
  if (vault.lock) {
    const lockNow = document.getElementById('lock-now');
    lockNow.addEventListener('click', lockAgain);
    if (!(await savedKey())) { showLock(); return; }
  }
  start();
}

async function start() {
  if (vault.lock) document.getElementById('lock-now').hidden = false;
  skeleton();
  try {
    // With a worker installed, an offline load still succeeds from its
    // cache, marked so the page can say it is offline.
    const { data, cached } = await fetchData({ cache: 'no-cache' });
    app.data = data;
    if (cached) app.offline = true;
  } catch (err) {
    if (!app.data) {
      app.loadError = err.message;
      // Take the pill down too: skeleton() restored it from the last visit,
      // and it must not reassure above a load failure.
      const pill = document.getElementById('trust');
      pill.dataset.state = 'bad';
      document.getElementById('trust-text').textContent = 'No data';
      document.querySelector('[data-view="feed"]').innerHTML = '';
      const s = emptyState('Could not load the data',
        `The page could not read its data: ${err.message}. On a fresh install nothing has been published yet — run a check and it will appear.`);
      document.querySelector('[data-view="feed"]').appendChild(s);
      renderTabs();
      return;
    }
  }

  if (app.lastSeen === null) {
    app.lastSeen = new Date().toISOString();
    store.set('lastSeen', app.lastSeen);
    app.firstVisit = true;
  }

  // Which ids are new to this browser, so a card animates once on arrival
  // rather than every time the list re-renders.
  const ids = (app.data.listings || []).map(l => String(l.id));
  for (const id of ids) if (!app.seenIds.has(id)) app.freshIds.add(id);
  app.seenIds = new Set(ids);
  store.set('seenIds', ids.slice(0, 1200));

  // A newer worker has taken over, so a newer page is cached for the next
  // load. Offered rather than applied, so code never changes mid-tap. Not on
  // a first visit, where the installing worker claims the page anyway.
  if ('serviceWorker' in navigator) {
    const hadOne = !!navigator.serviceWorker.controller;
    navigator.serviceWorker.addEventListener('controllerchange', () => {
      if (hadOne) newVersionReady();
    });
  }

  renderTabs();
  route();
  // After route(), which has drawn the first view: the strip is painted by
  // render(), and this only takes over the ticking from here on.
  startClock();
  window.addEventListener('hashchange', route);
  document.getElementById('sheet-close').addEventListener('click', closeSheet);
  document.getElementById('scrim').addEventListener('click', closeSheet);
  document.addEventListener('keydown', e => {
    // Four shortcuts, deliberately few: a view, find a car, the list, and
    // Escape. Never while typing: a "/" inside the search box is a slash.
    const typing = /^(input|textarea|select)$/i.test(
      (document.activeElement || {}).tagName || '');
    const sheetOpen = document.getElementById('sheet').dataset.open === '1';
    if (!typing && !sheetOpen && !e.metaKey && !e.ctrlKey && !e.altKey) {
      if (e.key >= '1' && e.key <= String(VIEWS.length)) {
        e.preventDefault();
        go(VIEWS[Number(e.key) - 1].id);
        return;
      }
      if (e.key === '/') {
        e.preventDefault();
        go('listings');
        // Focus after the hash settles: the hashchange from go() runs as a
        // separate task and moves focus back to <main>.
        setTimeout(() => {
          const box = document.getElementById('q');
          if (box) { box.focus(); box.select(); }
        }, 0);
        return;
      }
      if (e.key === '?') { e.preventDefault(); showShortcuts(); return; }
    }
    // Escape gets you out of whatever you are in, innermost first.
    if (e.key === 'Escape') {
      if (sheetOpen) { closeSheet(); return; }
      if (typing && document.activeElement.id === 'q' && app.q) {
        app.q = '';
        renderListings();
        return;
      }
    }
    if (e.key === 'Tab' && document.getElementById('sheet').dataset.open === '1') {
      const f = document.getElementById('sheet').querySelectorAll(
        'a[href],button,input,select,[tabindex]:not([tabindex="-1"])');
      if (!f.length) return;
      const first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });
  // Push the sheet back down with a thumb. Only downward, only from the top
  // of its own scroll, so it never fights the content inside it.
  const sheet = document.getElementById('sheet');
  let startY = null;
  sheet.addEventListener('touchstart', e => {
    startY = sheet.scrollTop <= 0 ? e.touches[0].clientY : null;
  }, { passive: true });
  sheet.addEventListener('touchmove', e => {
    if (startY === null) return;
    const dy = e.touches[0].clientY - startY;
    if (dy > 0 && innerWidth < 700) sheet.style.transform = `translateY(${dy}px)`;
  }, { passive: true });
  sheet.addEventListener('touchend', e => {
    if (startY === null) return;
    const dy = (e.changedTouches[0].clientY - startY);
    sheet.style.transform = '';
    startY = null;
    if (dy > 110 && innerWidth < 700) closeSheet();
  });

  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('sw.js').catch(() => { /* file:// or no https */ });
  }
}

boot();
