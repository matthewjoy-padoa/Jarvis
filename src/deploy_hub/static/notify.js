// Notifications : panneau de la cloche, et notifications du Mac.
// - Web Push (static/sw.js) : Jarvis pousse lui-même chaque notification, reçue même onglet fermé. Le navigateur
//   s'abonne dès que la permission est donnée ; l'abonnement est renvoyé au serveur à chaque chargement.
// - Sinon (navigateur sans push) : API Notification tant qu'un onglet est ouvert.
// Interroge /notifications/nouvelles toutes les 60 s pour la cloche ; la première fois, ne notifie rien.
(function () {
  const KEY = "deploy-hub-last-notif";
  let pushActive = false; // vrai : le service worker affiche les notifications, l'onglet ne le fait pas en double

  const toBytes = (b64) => Uint8Array.from(atob(b64.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - b64.length % 4) % 4)), (c) => c.charCodeAt(0));
  const sameKey = (buf, bytes) => !!buf && buf.byteLength === bytes.length && new Uint8Array(buf).every((b, i) => b === bytes[i]);

  async function subscribePush() {
    if (!("serviceWorker" in navigator) || !("PushManager" in window) || Notification.permission !== "granted") return false;
    try {
      const reg = await navigator.serviceWorker.register("/sw.js");
      await navigator.serviceWorker.ready;
      const key = toBytes((await (await fetch("/notifications/push/cle")).json()).key);
      let sub = await reg.pushManager.getSubscription();
      if (sub && !sameKey(sub.options.applicationServerKey, key)) { await sub.unsubscribe(); sub = null; } // clé du serveur changée
      if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
      const r = await fetch("/notifications/push/abonnement", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(sub) });
      pushActive = r.ok;
    } catch (e) {
      console.warn("Notifications du Mac par Web Push indisponibles :", e);
      pushActive = false;
    }
    return pushActive;
  }
  const get = () => { try { return localStorage.getItem(KEY); } catch (e) { return null; } };
  const set = (v) => { try { localStorage.setItem(KEY, String(v)); } catch (e) {} };

  // Panneau : ouvert au clic sur la cloche, fermé en cliquant ailleurs ou avec Échap.
  document.addEventListener("click", (e) => {
    const panel = document.getElementById("notif-panel"), bell = document.querySelector(".bell");
    if (!panel || !bell) return;
    if (bell.contains(e.target)) {
      panel.hidden = !panel.hidden;
      bell.setAttribute("aria-expanded", String(!panel.hidden));
    } else if (!panel.contains(e.target)) {
      panel.hidden = true;
      bell.setAttribute("aria-expanded", "false");
    }
  });
  // Clic sur une notification : le lien s'ouvre normalement ; la notification est marquée comme lue en arrière-plan
  // (sendBeacon survit au changement de page). Ne pas mettre hx-post sur le lien : htmx y bloquerait la navigation.
  document.addEventListener("click", (e) => {
    const link = e.target.closest && e.target.closest("a[data-read-id]");
    if (!link) return;
    const data = new FormData();
    data.append("id", link.dataset.readId);
    if (!(navigator.sendBeacon && navigator.sendBeacon("/notifications/lu", data))) {
      fetch("/notifications/lu", { method: "POST", body: data, keepalive: true });
    }
    link.closest(".notif")?.classList.remove("unread");
    if (link.target === "_blank") htmx.trigger(document.body, "notifs-changed");
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { const p = document.getElementById("notif-panel"); if (p) p.hidden = true; } });

  // Réglages → « Notifications du Mac » : état de ce navigateur et bouton d'activation. Dans le panneau de la cloche,
  // un simple lien vers ce réglage tant que la permission n'a pas été demandée.
  const STATES = {
    granted: ["activées", "g-done", ""],
    denied: ["bloquées", "g-ko", "Ce navigateur les a refusées pour Jarvis : autorise les notifications de ce site dans ses réglages (cadenas de la barre d'adresse), puis recharge la page."],
    default: ["à activer", "", ""],
    none: ["indisponibles", "g-ko", "Ce navigateur ne sait pas afficher de notifications."],
  };
  function showState(root) {
    const permission = "Notification" in window ? Notification.permission : "none";
    root.querySelectorAll("[data-notifs-todo]").forEach((el) => { el.hidden = permission !== "default"; });
    root.querySelectorAll("[data-browser-notifs]").forEach((box) => {
      let [label, group, help] = STATES[permission];
      if (permission === "granted" && !pushActive) { label = "activées, onglet ouvert"; help = "Sans Web Push dans ce navigateur, elles n'arrivent que tant qu'un onglet Jarvis est ouvert."; }
      const badge = box.querySelector("[data-notifs-state]"), text = box.querySelector("[data-notifs-help]"), btn = box.querySelector("[data-enable-browser-notifs]");
      badge.textContent = label;
      badge.className = "badge" + (group ? " " + group : "");
      text.textContent = help;
      text.hidden = !help;
      btn.hidden = permission !== "default";
    });
  }
  document.addEventListener("click", (e) => {
    const btn = e.target.closest && e.target.closest("[data-enable-browser-notifs]");
    if (btn && "Notification" in window) Notification.requestPermission().then(subscribePush).then(() => showState(document));
  });
  document.addEventListener("htmx:afterSwap", (e) => { if (e.target.querySelectorAll) showState(e.target); });

  async function poll() {
    const last = get();
    let items = [];
    try { items = await (await fetch("/notifications/nouvelles?apres=" + (last || 0))).json(); } catch (e) { return; }
    if (!items.length) return;
    set(Math.max(...items.map((n) => n.id)));
    if (last === null) return; // premier passage : on mémorise sans notifier
    htmx.trigger(document.body, "notifs-changed");
    document.dispatchEvent(new CustomEvent("jarvis:notifs", { detail: items })); // booster.js : booster InteCG gagné
    if (pushActive || !("Notification" in window) || Notification.permission !== "granted") return;
    items.slice(-5).forEach((n) => {
      const note = new Notification(n.title, { body: n.detail, tag: n.key, requireInteraction: n.level === "urgent" });
      note.onclick = () => { window.focus(); if (n.link) location.href = n.link; note.close(); };
    });
  }
  document.addEventListener("jarvis:poll-notifs", poll); // relevé demandé sans attendre la minute (booster.js)
  document.addEventListener("DOMContentLoaded", () => { showState(document); subscribePush().then(() => showState(document)); poll(); setInterval(poll, 60000); });
})();
