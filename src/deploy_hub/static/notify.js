// Notifications : panneau de la cloche, et notifications du Mac (API Notification du navigateur) tant qu'un onglet
// de l'app est ouvert. Interroge /notifications/nouvelles toutes les 60 s ; la première fois, ne notifie rien
// (on part des notifications déjà présentes).
(function () {
  const KEY = "deploy-hub-last-notif";
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

  // Bouton « Activer les notifications du Mac » dans le panneau, tant que la permission n'est pas donnée.
  document.addEventListener("htmx:afterSwap", (e) => {
    const btn = e.target.querySelector && e.target.querySelector("[data-enable-browser-notifs]");
    if (!btn || !("Notification" in window)) return;
    btn.hidden = Notification.permission !== "default";
    btn.addEventListener("click", () => Notification.requestPermission().then(() => { btn.hidden = true; }));
  });

  async function poll() {
    const last = get();
    let items = [];
    try { items = await (await fetch("/notifications/nouvelles?apres=" + (last || 0))).json(); } catch (e) { return; }
    if (!items.length) return;
    set(Math.max(...items.map((n) => n.id)));
    if (last === null) return; // premier passage : on mémorise sans notifier
    htmx.trigger(document.body, "notifs-changed");
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    items.slice(-5).forEach((n) => {
      const note = new Notification(n.title, { body: n.detail, tag: n.key, requireInteraction: n.level === "urgent" });
      note.onclick = () => { window.focus(); if (n.link) location.href = n.link; note.close(); };
    });
  }
  document.addEventListener("DOMContentLoaded", () => { poll(); setInterval(poll, 60000); });
})();
