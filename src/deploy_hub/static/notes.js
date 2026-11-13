// Bloc-notes flottant : bouton [data-notes-open] (ou touche « n » hors d'un champ) ; Échap ou la croix ferment.
// Le contenu est relu à chaque ouverture (/perso/notes) ; chaque zone s'enregistre seule (htmx, comme sur la fiche).
(function () {
  const panel = document.getElementById("notes"), body = document.getElementById("notes-body");
  const fab = document.querySelector("[data-notes-open]");
  if (!panel || !body || !fab) return;
  function open() {
    panel.hidden = false;
    fab.setAttribute("aria-expanded", "true");
    document.documentElement.classList.add("notes-open");
    body.addEventListener("htmx:afterSwap", () => { const first = body.querySelector("textarea"); if (first) first.focus(); }, { once: true });
    htmx.trigger(body, "notes-open");
  }
  function close() {
    panel.hidden = true;
    fab.setAttribute("aria-expanded", "false");
    document.documentElement.classList.remove("notes-open");
    fab.focus();
  }
  document.addEventListener("click", (e) => {
    if (e.target.closest("[data-notes-open]")) panel.hidden ? open() : close();
    else if (e.target.closest("[data-notes-close]")) close();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !panel.hidden && panel.contains(e.target)) { e.stopPropagation(); close(); return; }
    const field = e.target.closest && e.target.closest("input, textarea, select, [contenteditable]");
    if (field || e.metaKey || e.ctrlKey || e.altKey || e.key !== "n") return;
    if (document.documentElement.matches(".palette-open, .drawer-open, .term-open")) return;
    e.preventDefault();
    panel.hidden ? open() : close();
  });
  // La même note peut être affichée deux fois (ici et sur la fiche du client) : ce qui est tapé dans l'une est recopié dans l'autre.
  document.addEventListener("input", (e) => {
    const url = e.target.matches && e.target.matches("textarea[hx-post^='/perso/note']") && e.target.getAttribute("hx-post");
    if (!url) return;
    document.querySelectorAll("textarea[hx-post]").forEach((other) => {
      if (other !== e.target && other.getAttribute("hx-post") === url) other.value = e.target.value;
    });
  });
})();
