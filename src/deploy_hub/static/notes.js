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
  // To-do list (_todo.html) : chaque action renvoie la liste, qui se remplace elle-même. La même liste peut être affichée
  // deux fois (ici et sur la fiche) : l'autre copie reçoit la même réponse. Si l'enregistrement échoue (Jarvis injoignable,
  // erreur), la case revient comme avant et la liste le dit, au lieu de laisser croire que c'est fait.
  const todoBox = (elt) => elt?.closest?.(".todo-box");
  document.addEventListener("htmx:beforeRequest", (e) => {
    const box = todoBox(e.detail.elt);
    if (!box) return;
    box.querySelector(".todo-error")?.remove();
    document.querySelectorAll(".todo-box").forEach((other) => {
      if (other !== box && other.dataset.todoSlug === box.dataset.todoSlug) other.dataset.todoStale = "1";
    });
  });
  document.addEventListener("htmx:afterRequest", (e) => {
    // Repérée par son adresse : une fois la liste remplacée, htmx envoie cet événement au premier parent encore présent.
    if (!(e.detail.pathInfo?.requestPath || "").startsWith("/perso/todo")) return;
    const stale = document.querySelectorAll(".todo-box[data-todo-stale]");
    if (e.detail.successful) {
      if (!stale.length) return;
      const tpl = document.createElement("template");
      tpl.innerHTML = e.detail.xhr.responseText;
      const fresh = tpl.content.querySelector(".todo-box");
      if (!fresh) return;
      fresh.querySelectorAll("[autofocus]").forEach((el) => el.removeAttribute("autofocus"));  // le curseur reste où il est
      stale.forEach((other) => {
        const copy = fresh.cloneNode(true);
        other.replaceWith(copy);
        htmx.process(copy);
      });
      return;
    }
    stale.forEach((other) => delete other.dataset.todoStale);
    const box = todoBox(e.detail.elt);  // pas remplacée : l'élément cliqué est toujours là
    if (!box) return;
    if (e.detail.elt.matches("input[type=checkbox]")) e.detail.elt.checked = !e.detail.elt.checked;
    const error = document.createElement("p");
    error.className = "result ko todo-error";
    error.setAttribute("role", "alert");
    error.textContent = e.detail.xhr && e.detail.xhr.status ? "Pas enregistré : Jarvis a répondu par une erreur. Recharge la page."
      : "Pas enregistré : Jarvis ne répond pas. Réessaie dans un instant.";
    box.appendChild(error);
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
