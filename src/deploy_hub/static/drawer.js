// Fenêtre de détail des tickets : un lien [data-ticket] s'ouvre dans une fenêtre centrée (htmx) au lieu de changer de page.
// Toute la carte (ou ligne) [data-ticket-card] est cliquable : elle ouvre le lien [data-ticket] qu'elle contient, sauf si le
// clic vise un autre élément interactif (lien, bouton, bloc dépliable, champ) ou sélectionne du texte.
// Ctrl/Cmd-clic : nouvel onglet. Échap, la croix ou le fond ferment la fenêtre.
(function () {
  let drawer, body, lastFocus;
  function build() {
    drawer = document.createElement("div");
    drawer.className = "drawer modal";
    drawer.hidden = true;
    drawer.innerHTML = '<div class="drawer-backdrop" data-close></div>' +
      '<aside class="drawer-panel" role="dialog" aria-modal="true" aria-label="Détail du ticket" tabindex="-1">' +
      '<button type="button" class="drawer-close secondary" data-close aria-label="Fermer">✕</button><div class="drawer-body"></div></aside>';
    document.body.append(drawer);
    body = drawer.querySelector(".drawer-body");
    drawer.addEventListener("click", (e) => { if (e.target.closest("[data-close]")) close(); });
  }
  function open(url) {
    if (!drawer) build();
    lastFocus = document.activeElement;
    body.innerHTML = '<p class="muted">Chargement…</p>';
    drawer.hidden = false;
    document.documentElement.classList.add("drawer-open");
    // Le focus va à l'élément [autofocus] du ticket s'il y en a un (bloc agent demandé par la palette), sinon au panneau.
    htmx.ajax("GET", url, { target: body, swap: "innerHTML" })
      .then(() => (body.querySelector("[autofocus]") || drawer.querySelector(".drawer-panel")).focus());
  }
  function close() {
    if (!drawer || drawer.hidden) return;
    drawer.hidden = true;
    document.documentElement.classList.remove("drawer-open");
    if (lastFocus) lastFocus.focus();
  }
  // Un bloc <details> fermé prend toute la largeur de la carte : seul son bouton (summary) lui appartient.
  // Déplié, son contenu (texte du retour, brief de l'agent) reste à lui : on y lit, on y sélectionne, on y écrit.
  const INTERACTIVE = "a, button, summary, input, select, textarea, label, form, details[open] > :not(summary)";
  document.addEventListener("click", (e) => {
    if (e.button !== 0) return;
    let a = e.target.closest("a[data-ticket]");
    if (!a) {
      const card = e.target.closest("[data-ticket-card]");
      if (!card || e.target.closest(INTERACTIVE) || String(window.getSelection())) return;
      a = card.querySelector("a[data-ticket]");
      if (!a) return;
      if (e.metaKey || e.ctrlKey) { window.open(a.href, "_blank", "noopener"); return; }
    }
    if (e.metaKey || e.ctrlKey || e.shiftKey || !window.htmx) return;
    e.preventDefault();
    open(a.getAttribute("href"));
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
})();
