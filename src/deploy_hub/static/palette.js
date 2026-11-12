// Palette de commandes « Demander à Jarvis » : ⌘K (ou Ctrl+K, ou « / » hors d'un champ) ouvre une recherche sur toute l'app.
// Console : le bouton >_ (ou « > » hors d'un champ) l'ouvre en mode commande : >dag, >agent, >rafraichir, >aide.
// Le serveur répond (/palette?q=, branché par htmx sur le champ) et porte le comportement des éléments (lien, hx-get, hx-post) ;
// ici : ouverture, clavier, sélection, récents et journal des lancements.
// ↑↓ ou Tab : choisir · Entrée : comme un clic · ⌘Entrée : nouvel onglet · Échap : fermer (sans fermer la fenêtre de ticket dessous).
(function () {
  const palette = document.getElementById("palette");
  if (!palette) return;
  const input = document.getElementById("palette-input"), results = document.getElementById("palette-results");
  const out = document.getElementById("palette-out");
  const RECENTS = "jarvis-palette-recents", MAX_RECENTS = 6, JOURNAL = "jarvis-console-journal", MAX_JOURNAL = 8;
  const mac = /Mac|iPhone|iPad/.test(navigator.platform);
  let lastFocus = null, selected = null;

  document.querySelectorAll("[data-palette-key]").forEach((kbd) => {
    kbd.textContent = kbd.dataset.paletteKey === "enter" ? (mac ? "⌘↵" : "Ctrl ↵") : (mac ? "⌘K" : "Ctrl K");
  });

  const items = () => Array.from(results.querySelectorAll("[data-pal-item]"));
  function select(item, scroll) {
    if (selected) selected.classList.remove("is-selected");
    selected = item || null;
    if (!selected) { input.removeAttribute("aria-activedescendant"); return; }
    selected.classList.add("is-selected");
    input.setAttribute("aria-activedescendant", selected.id);
    if (scroll) selected.scrollIntoView({ block: "nearest" });
  }
  function move(step) {
    const all = items();
    if (!all.length) return;
    const at = all.indexOf(selected);
    select(all[(at + step + all.length) % all.length], true);
  }

  const read = (key) => { try { const list = JSON.parse(localStorage.getItem(key) || "[]"); return Array.isArray(list) ? list : []; } catch (e) { return []; } };
  const write = (key, list) => { try { localStorage.setItem(key, JSON.stringify(list)); } catch (e) {} };
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  // Groupe construit dans le navigateur (récents, journal), en tête des résultats ; textes posés en textContent.
  function prependGroup(title, entries, render) {
    if (!entries.length) return;
    const section = el("section", "pal-group");
    section.append(el("h3", "pal-group-title", title), ...entries.map(render));
    results.prepend(section);
  }

  // Récents : gardés dans le navigateur, montrés quand le champ est vide. Les commandes (hx-*, thème, verbes) n'y vont pas.
  function remember(item) {
    if (item.dataset.palAction || item.dataset.palFillInput || item.matches("[hx-get], [hx-post]")) return;
    const entry = { title: item.dataset.palTitle, url: item.getAttribute("href"), ticket: item.hasAttribute("data-ticket"), blank: item.target === "_blank" };
    write(RECENTS, [entry].concat(read(RECENTS).filter((r) => r.url !== entry.url)).slice(0, MAX_RECENTS));
  }
  const showRecents = () => prependGroup("Récents",
    read(RECENTS).filter((r) => r && typeof r.url === "string" && /^(\/(?!\/)|https:\/\/)/.test(r.url) && r.title), (r) => {
      const a = el("a", "pal-item"), main = el("span", "pal-main");
      a.href = r.url;
      a.setAttribute("role", "option");
      a.dataset.palItem = "";
      a.dataset.palTitle = r.title;
      if (r.ticket) a.dataset.ticket = "";
      if (r.blank) { a.target = "_blank"; a.rel = "noopener"; }
      main.append(el("span", "pal-title", r.title));
      a.append(el("span", "pal-icon k-recent", "↺"), main);
      return a;
    });

  // Journal de la console : ce qui a été lancé et la réponse, montré quand le champ ne contient que « > ».
  function record(label, target) {
    const result = target.querySelector(".result");
    if (!result) return;
    const at = new Date().toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
    write(JOURNAL, [{ label, message: result.innerText.trim(), ok: result.classList.contains("ok"), at }].concat(read(JOURNAL)).slice(0, MAX_JOURNAL));
  }
  const showJournal = () => prependGroup("Journal de la console", read(JOURNAL).filter((j) => j && j.label && j.message), (j) => {
    const line = el("p", "pal-log " + (j.ok ? "ok" : "ko"));
    line.append(el("span", "pal-log-at", j.at), el("code", "", j.label), el("span", "", j.message));
    return line;
  });

  const consoleMode = () => palette.classList.toggle("is-console", input.value.trimStart().startsWith(">"));
  function query(text) {
    input.value = text;
    consoleMode();
    input.focus();
    htmx.trigger(input, "palette-load");
  }
  function open(prefix) {
    if (!palette.hidden && !prefix) { input.select(); return; }
    if (palette.hidden) {
      lastFocus = document.activeElement;
      palette.hidden = false;
      document.documentElement.classList.add("palette-open");
      out.replaceChildren();
    }
    if (window.htmx) query(prefix || "");
  }
  function close() {
    if (palette.hidden) return;
    palette.hidden = true;
    document.documentElement.classList.remove("palette-open");
    if (lastFocus && document.contains(lastFocus)) lastFocus.focus();
  }

  palette.addEventListener("htmx:afterSwap", (e) => {
    const target = e.detail.target;
    if (target !== results) {  // réponse d'un lancement (DAG, rafraîchir) : au journal, sous le libellé porté par l'élément qui l'a demandé
      const log = e.detail.requestConfig?.elt?.closest?.("[data-pal-log]");
      if (log) record(log.dataset.palLog, target);
      return;
    }
    if (!input.value.trim()) showRecents();
    if (input.value.trim() === ">") showJournal();
    const all = items();
    all.forEach((item, n) => { item.id = "pal-item-" + n; });
    results.scrollTop = 0;
    select(all[0]);
    results.querySelector("[autofocus]")?.focus();  // formulaire de lancement : le bouton « Lancer » prend la main
  });
  results.addEventListener("mousemove", (e) => {
    const item = e.target.closest("[data-pal-item]");
    if (item && item !== selected) select(item);
  });
  palette.addEventListener("click", (e) => {
    if (e.target.closest("[data-palette-close]")) { close(); return; }
    if (e.target.closest("[data-pal-back]")) { query(input.value); return; }
    const preset = e.target.closest("[data-pal-fill-input]");  // verbe de console, dans la liste ou au pied
    if (preset) { e.preventDefault(); query(preset.dataset.palFillInput); return; }
    const hint = e.target.closest("[data-pal-fill]");  // mot compris, ajouté à la requête
    if (hint) {
      const word = hint.dataset.palFill, start = word === ">" ? "" : input.value.replace(/\s+$/, "");
      query((start ? start + " " : "") + word + (word.length === 1 ? "" : " "));
      return;
    }
    const item = e.target.closest("[data-pal-item]");
    if (!item || e.button !== 0) return;
    if (item.dataset.palAction === "theme") { e.preventDefault(); document.getElementById("theme-toggle").click(); return; }
    remember(item);
    if (item.hasAttribute("data-ticket") && !(e.metaKey || e.ctrlKey || e.shiftKey)) close();  // drawer.js prend le relais du clic
  });

  input.addEventListener("input", consoleMode);
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || (e.key === "Tab" && !e.shiftKey)) { e.preventDefault(); move(1); }
    else if (e.key === "ArrowUp" || (e.key === "Tab" && e.shiftKey)) { e.preventDefault(); move(-1); }
    else if (e.key === "Enter" && !e.isComposing && selected) {
      e.preventDefault();
      const link = !selected.dataset.palAction && !selected.dataset.palFillInput && !selected.matches("[hx-get], [hx-post]");
      if ((e.metaKey || e.ctrlKey) && link) { remember(selected); window.open(selected.href, "_blank", "noopener"); }
      else selected.click();
    }
  });

  // En phase de capture : Échap ferme la palette seule, pas la fenêtre de ticket qui peut être ouverte dessous.
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !palette.hidden) { e.stopImmediatePropagation(); e.preventDefault(); close(); return; }
    if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "k") { e.preventDefault(); open(); return; }
    const field = e.target.closest && e.target.closest("input, textarea, select, [contenteditable]");
    if (field || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "/") { e.preventDefault(); open(); }
    else if (e.key === ">") { e.preventDefault(); open(">"); }
  }, true);
  document.addEventListener("click", (e) => {
    const button = e.target.closest("[data-palette-open]");
    if (button) open(button.dataset.paletteOpen);
  });
})();
