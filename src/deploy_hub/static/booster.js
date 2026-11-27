// Booster gagné dans InteCG (jeu de cartes de l'équipe) : quand un DAG ou un agent lancé ici en rapporte un, sa
// notification (clé « intecg:… », relevée par notify.js) fait tomber un sachet au milieu de l'écran, avec un bouton
// qui ouvre le jeu. Se ferme seul, au clic à côté ou avec Échap. Sans mouvement si le Mac en demande moins (style.css).
(function () {
  const SHOWN_MS = 9000;
  let box, timer;
  function close() {
    clearTimeout(timer);
    if (box) box.remove();
    box = null;
  }
  function show(note) {
    close();
    box = document.createElement("div");
    box.className = "booster-won";
    box.innerHTML = '<div class="booster-won-card" role="status">' +
      '<div class="booster-pack" aria-hidden="true"><span class="booster-rings"></span>' + "<i></i>".repeat(8) + "</div>" +
      '<p class="booster-won-title"></p><p class="booster-won-detail muted"></p>' +
      '<form method="post" action="/outils/intecg" target="intecg"><button type="submit">Ouvrir InteCG</button></form>' +
      '<button type="button" class="booster-won-close secondary" aria-label="Fermer">✕</button></div>';
    box.querySelector(".booster-won-title").textContent = note.title || "InteCG · booster gagné";
    box.querySelector(".booster-won-detail").textContent = note.detail || "";
    box.addEventListener("click", (e) => { if (!e.target.closest(".booster-won-card") || e.target.closest(".booster-won-close, form")) setTimeout(close, 0); });
    document.body.append(box);
    timer = setTimeout(close, SHOWN_MS);
  }
  document.addEventListener("jarvis:notifs", (e) => {
    const won = (e.detail || []).filter((n) => String(n.key).startsWith("intecg:"));
    if (won.length) show(won[won.length - 1]);
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
  // Après un lancement (DAG, agent), on n'attend pas la minute du relevé : le booster arrive en quelques secondes.
  document.addEventListener("htmx:afterRequest", (e) => {
    const sent = e.detail && e.detail.requestConfig;
    if (sent && sent.verb === "post" && /^\/(actions|agents)\//.test(sent.path)) {
      [4000, 12000, 30000].forEach((ms) => setTimeout(() => document.dispatchEvent(new Event("jarvis:poll-notifs")), ms));
    }
  });
  window.jarvisBoosterWon = show;  // aperçu à la main : jarvisBoosterWon({title: "…", detail: "…"})
})();
