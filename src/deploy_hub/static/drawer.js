// Panneau latéral des tickets : un lien [data-ticket] s'ouvre dans le panneau (htmx) au lieu de changer de page.
// Ctrl/Cmd-clic ou clic du milieu : comportement normal (nouvel onglet). Échap, la croix ou le fond ferment le panneau.
(function () {
  let drawer, body, lastFocus;
  function build() {
    drawer = document.createElement("div");
    drawer.className = "drawer";
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
    htmx.ajax("GET", url, { target: body, swap: "innerHTML" }).then(() => drawer.querySelector(".drawer-panel").focus());
  }
  function close() {
    if (!drawer || drawer.hidden) return;
    drawer.hidden = true;
    document.documentElement.classList.remove("drawer-open");
    if (lastFocus) lastFocus.focus();
  }
  document.addEventListener("click", (e) => {
    const a = e.target.closest("a[data-ticket]");
    if (!a || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0 || !window.htmx) return;
    e.preventDefault();
    open(a.getAttribute("href"));
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
})();
