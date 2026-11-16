// Service worker des notifications du Mac (Web Push) : reçoit les notifications poussées par Jarvis même sans onglet
// ouvert, les affiche, et ouvre leur lien au clic. Servi par la route /sw.js (portée : tout le site). Voir sources/web_push.py.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("push", (e) => {
  let n = { title: "Jarvis" };
  try { n = e.data ? e.data.json() : n; } catch (err) { n = { title: "Jarvis", body: e.data ? e.data.text() : "" }; }
  e.waitUntil(self.registration.showNotification(n.title || "Jarvis", {
    body: n.body || "",
    tag: n.tag || undefined,
    requireInteraction: !!n.urgent,
    icon: "/static/favicon.svg",
    data: { link: n.link || "/notifications" },
  }));
});

// Clic : un onglet Jarvis déjà ouvert va sur le lien (lien interne) ; sinon un nouvel onglet l'ouvre.
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = new URL(e.notification.data && e.notification.data.link || "/", self.location.origin).href;
  const internal = url.startsWith(self.location.origin);
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
    const tab = internal && list.find((c) => c.url.startsWith(self.location.origin) && "navigate" in c);
    return tab ? tab.navigate(url).then((c) => (c || tab).focus()).catch(() => self.clients.openWindow(url)) : self.clients.openWindow(url);
  }));
});
