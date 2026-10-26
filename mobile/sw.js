var CACHE = 'feinao-m-v1';
var ASSETS = ['index.html','style.css','app.js','config.js','icon.svg','manifest.webmanifest'];
self.addEventListener('install', function (e) {
  e.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(ASSETS); }).then(function () { return self.skipWaiting(); }));
});
self.addEventListener('activate', function (e) {
  e.waitUntil(caches.keys().then(function (ks) {
    return Promise.all(ks.filter(function (k) { return k !== CACHE; }).map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});
self.addEventListener('fetch', function (e) {
  var url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.pathname.indexOf('/api/') >= 0) return;
  e.respondWith(caches.match(e.request).then(function (hit) {
    return hit || fetch(e.request).then(function (r) {
      var cp = r.clone(); caches.open(CACHE).then(function (c) { c.put(e.request, cp); }); return r;
    }).catch(function () { return caches.match('index.html'); }));
  }));
});
