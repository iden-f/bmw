/* Offline copy of the last published check.
 *
 * BUILD is rewritten on every publish from a hash of the page's files; a
 * changed sw.js is what makes a browser install the new version.
 *
 *   - Data is network-first. A cached answer carries X-From-Cache: 1 so the
 *     page can say it is offline instead of passing old data off as new.
 *   - The shell is cache-first; BUILD keeps it current.
 *   - Photos are cache-first in a cache that outlives builds: a photo's bytes
 *     never change.
 *
 * On a private site everything cached here is ciphertext. Nothing decrypted
 * is ever written to a cache.
 */
const BUILD = 'dbe1cc632142';
const SHELL = `atw-shell-${BUILD}`;
const DATA = `atw-data-${BUILD}`;
const PHOTOS = 'atw-photos';
const FILES = ['./', './index.html', './app.js', './manifest.webmanifest',
               './icon.svg'];
const DATA_FILES = ['./lock.json', './data.enc', './data.json'];
const isData = path => /\/(lock\.json|data\.enc|data\.json)$/.test(path);

// cache: 'reload' so the HTTP cache cannot hand a new worker old bytes.
const fresh = path => new Request(path, { cache: 'reload' });

self.addEventListener('install', event => {
  event.waitUntil(Promise.all([
    caches.open(SHELL).then(cache => cache.addAll(FILES.map(fresh))),
    // Best effort, one file at a time: a site has either data.enc or
    // data.json, and a missing one must not fail the install.
    caches.open(DATA).then(cache => Promise.all(
      DATA_FILES.map(f => cache.add(fresh(f)).catch(() => null)))),
  ]).then(() => self.skipWaiting()));
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys
        .filter(k => k !== SHELL && k !== DATA && k !== PHOTOS)
        .map(k => caches.delete(k))))
      .then(() => self.clients.claim()));
});

async function data(request) {
  try {
    const response = await fetch(request);
    if (response && response.ok) {
      const cache = await caches.open(DATA);
      cache.put(request, response.clone());
    }
    return response;
  } catch (err) {
    const hit = await caches.match(request);
    if (hit) {
      const headers = new Headers(hit.headers);
      headers.set('X-From-Cache', '1');
      return new Response(await hit.blob(), {
        status: hit.status, statusText: hit.statusText, headers,
      });
    }
    return new Response(JSON.stringify({ offline: true }), {
      status: 503,
      headers: { 'Content-Type': 'application/json', 'X-From-Cache': '0' },
    });
  }
}

async function photo(request) {
  const hit = await caches.match(request, { cacheName: PHOTOS });
  if (hit) return hit;
  try {
    const response = await fetch(request);
    if (response && response.ok) {
      const cache = await caches.open(PHOTOS);
      cache.put(request, response.clone());
    }
    return response;
  } catch (err) {
    // The page draws its own fallback, and knows it is offline.
    return new Response('', { status: 504, headers: { 'X-From-Cache': '0' } });
  }
}

async function shell(request) {
  const hit = await caches.match(request, { cacheName: SHELL });
  if (hit) return hit;
  try {
    return await fetch(request);
  } catch (err) {
    return new Response(
      'This page is not available offline yet. Open it once with a connection.',
      { status: 503, headers: { 'Content-Type': 'text/plain' } });
  }
}

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== location.origin) return;
  if (isData(url.pathname)) {
    event.respondWith(data(event.request));
    return;
  }
  if (url.pathname.includes('/thumbs/')) {
    event.respondWith(photo(event.request));
    return;
  }
  event.respondWith(shell(event.request));
});

self.addEventListener('message', event => {
  if (event.data && event.data.type === 'skip-waiting') self.skipWaiting();
});
