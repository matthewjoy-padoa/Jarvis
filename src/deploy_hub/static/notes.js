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
  // erreur), la case revient comme avant ; le message, lui, vient d'errors.js, comme pour toute action qui écrit.
  const todoBox = (elt) => elt?.closest?.(".todo-box");
  document.addEventListener("htmx:beforeRequest", (e) => {
    const box = todoBox(e.detail.elt);
    if (!box) return;
    document.querySelectorAll(".todo-box").forEach((other) => {
      if (other !== box && other.dataset.todoSlug === box.dataset.todoSlug) other.dataset.todoStale = "1";
    });
  });
  document.addEventListener("htmx:afterRequest", (e) => {
    // État d'enregistrement commun : un seul par zone (le panneau, le bloc « Mes notes » de la fiche), pour les notes et
    // les to-do lists qu'elle contient ([data-save-scope] → [data-save-status]) : « Enregistré », discret, qui s'efface
    // (CSS) ; les échecs y sont écrits par errors.js. Une liste remplacée n'est plus dans la page : htmx envoie alors cet
    // événement au premier parent encore présent (e.target), qui est dans la zone.
    const status = e.target.closest?.("[data-save-scope]")?.querySelector("[data-save-status]");
    if (status && e.detail.successful && (e.detail.requestConfig?.verb || "get").toLowerCase() !== "get") {
      status.className = "save-status";
      void status.offsetWidth;  // relance l'effacement à chaque enregistrement
      status.className = "save-status ok";
      status.textContent = "Enregistré";
      status.removeAttribute("title");
    }
    // To-do list, repérée par son adresse (sa copie dans l'autre zone, case remise après un échec).
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
    if (todoBox(e.detail.elt) && e.detail.elt.matches("input[type=checkbox]")) e.detail.elt.checked = !e.detail.elt.checked;
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
