// Palette de commandes « Demander à Jarvis » : ⌘K (ou Ctrl+K, ou « / » hors d'un champ) ouvre une recherche sur toute l'app.
// Le serveur répond (/palette?q=, branché par htmx sur le champ) ; ici : ouverture, clavier, sélection et éléments récents.
// ↑↓ ou Tab : choisir · Entrée : ouvrir · ⌘Entrée : nouvel onglet · Échap : fermer (sans fermer la fenêtre de ticket dessous).
(function () {
  const palette = document.getElementById("palette");
  if (!palette) return;
  const input = document.getElementById("palette-input"), results = document.getElementById("palette-results");
  const RECENTS = "jarvis-palette-recents", MAX_RECENTS = 6;
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

  // Récents : gardés dans le navigateur, montrés quand le champ est vide.
  function readRecents() {
    try { const list = JSON.parse(localStorage.getItem(RECENTS) || "[]"); return Array.isArray(list) ? list : []; } catch (e) { return []; }
  }
  function remember(item) {
    if (item.dataset.palAction) return;
    const entry = { title: item.dataset.palTitle, url: item.getAttribute("href"), ticket: item.hasAttribute("data-ticket"), blank: item.target === "_blank" };
    const list = [entry].concat(readRecents().filter((r) => r.url !== entry.url)).slice(0, MAX_RECENTS);
    try { localStorage.setItem(RECENTS, JSON.stringify(list)); } catch (e) {}
  }
  function showRecents() {
    const list = readRecents().filter((r) => r && typeof r.url === "string" && /^(\/(?!\/)|https:\/\/)/.test(r.url) && r.title);
    if (!list.length) return;
    const section = document.createElement("section"), title = document.createElement("h3");
    section.className = "pal-group";
    title.className = "pal-group-title";
    title.textContent = "Récents";
    section.append(title);
    list.forEach((r) => {
      const a = document.createElement("a"), icon = document.createElement("span"), main = document.createElement("span"), text = document.createElement("span");
      a.className = "pal-item";
      a.href = r.url;
      a.setAttribute("role", "option");
      a.dataset.palItem = "";
      a.dataset.palTitle = r.title;
      if (r.ticket) a.dataset.ticket = "";
      if (r.blank) { a.target = "_blank"; a.rel = "noopener"; }
      icon.className = "pal-icon k-recent";
      icon.textContent = "↺";
      main.className = "pal-main";
      text.className = "pal-title";
      text.textContent = r.title;
      main.append(text);
      a.append(icon, main);
      section.append(a);
    });
    results.prepend(section);
  }

  function open() {
    if (!palette.hidden) { input.select(); return; }
    lastFocus = document.activeElement;
    palette.hidden = false;
    document.documentElement.classList.add("palette-open");
    input.value = "";
    input.focus();
    if (window.htmx) htmx.trigger(input, "palette-load");
  }
  function close() {
    if (palette.hidden) return;
    palette.hidden = true;
    document.documentElement.classList.remove("palette-open");
    if (lastFocus && document.contains(lastFocus)) lastFocus.focus();
  }

  function activate(item, newTab) {
    if (!item) return;
    if (item.dataset.palAction === "theme") { document.getElementById("theme-toggle").click(); return; }
    remember(item);
    if (newTab) { window.open(item.href, "_blank", "noopener"); return; }
    if (item.hasAttribute("data-ticket")) close();  // la fenêtre de ticket (drawer.js) prend le relais du clic
    item.click();
  }

  results.addEventListener("htmx:afterSwap", () => {
    if (!input.value.trim()) showRecents();
    const all = items();
    all.forEach((item, n) => { item.id = "pal-item-" + n; });
    results.scrollTop = 0;
    select(all[0]);
  });
  results.addEventListener("mousemove", (e) => {
    const item = e.target.closest("[data-pal-item]");
    if (item && item !== selected) select(item);
  });
  palette.addEventListener("click", (e) => {
    if (e.target.closest("[data-palette-close]")) { close(); return; }
    const fill = e.target.closest("[data-pal-fill]");
    if (fill) {
      const word = fill.dataset.palFill, start = word === ">" ? "" : input.value.replace(/\s+$/, "");
      input.value = word.length === 1 ? (start ? start + " " : "") + word : (start ? start + " " : "") + word + " ";
      input.focus();
      htmx.trigger(input, "palette-load");
      return;
    }
    const item = e.target.closest("[data-pal-item]");
    if (!item || e.button !== 0) return;
    if (item.dataset.palAction) { e.preventDefault(); activate(item); return; }
    remember(item);
    if (item.hasAttribute("data-ticket") && !(e.metaKey || e.ctrlKey || e.shiftKey)) close();
  });

  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || (e.key === "Tab" && !e.shiftKey)) { e.preventDefault(); move(1); }
    else if (e.key === "ArrowUp" || (e.key === "Tab" && e.shiftKey)) { e.preventDefault(); move(-1); }
    else if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); activate(selected, e.metaKey || e.ctrlKey); }
  });

  // En phase de capture : Échap ferme la palette seule, pas la fenêtre de ticket qui peut être ouverte dessous.
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !palette.hidden) { e.stopImmediatePropagation(); e.preventDefault(); close(); return; }
    if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "k") { e.preventDefault(); open(); return; }
    const field = e.target.closest && e.target.closest("input, textarea, select, [contenteditable]");
    if (e.key === "/" && !field && !e.metaKey && !e.ctrlKey && !e.altKey) { e.preventDefault(); open(); }
  }, true);
  document.addEventListener("click", (e) => { if (e.target.closest("[data-palette-open]")) open(); });
})();
