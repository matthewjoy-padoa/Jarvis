// Filtres et tri côté navigateur pour chaque bloc marqué [data-filterable].
// - Tableau : les <th data-filter="clé"> donnent une liste déroulante (valeurs de la colonne) ; avec data-multi, la cellule
//   porte data-values="a|b" (plusieurs valeurs : une carte de deux équipes répond aux deux) ; un clic sur un <th> trie,
//   sauf [data-nosort]. Une cellule peut porter data-sort (ex. date ISO) pour trier sur autre chose que son texte.
// - Liste de cartes : data-items (sélecteur des cartes) et data-filters="Libellé:attribut,…" (attributs data-* des cartes),
//   data-sorts="Libellé:attribut,…" pour un menu de tri. Un attribut « a|b » porte plusieurs valeurs (ex. responsables).
// - Lignes ou cartes avec data-group (todo, in_progress, ko, done) : filtre « Groupe » ajouté automatiquement.
// - Graphiques : un élément [data-filter-set="clé=valeur;clé=valeur"] applique ces filtres au clic (re-clic : retire) ;
//   avec [data-filter-href], il ouvre cette page, filtres passés dans l'ancre (#clé=valeur&clé=valeur).
// - Recherche : sans accents ni majuscules, tous les mots doivent être présents ; elle lit aussi le groupe (« terminé »),
//   la colonne du kanban et les attributs data-* de la carte.
// - Éléments [data-out] (hors de la vue par défaut : retours terminés, actions non actives) : masqués, mais dès qu'une
//   recherche ou un filtre en trouve, un bouton propose de les afficher (data-out-label sur le bloc : comment les nommer).
//   S'il n'y a de résultat que parmi eux, ils sont affichés d'office.
// - Filtre « Client » : présélectionné sur le client du menu en haut (body[data-current-client]), sauf ancre de filtres.
// - Barre : la recherche et un bouton « Filtres » (nombre de filtres actifs) ; les listes déroulantes sont dans un
//   panneau replié qu'il ouvre, pour ne pas empiler cinq menus au-dessus de chaque liste.
(function () {
  const collator = new Intl.Collator("fr", { numeric: true, sensitivity: "base" });
  const text = (el) => (el ? el.textContent.replace(/\s+/g, " ").trim() : "");
  const GROUPS = { todo: "À faire", in_progress: "En cours", ko: "KO", done: "Terminé" };
  const blocks = []; // { keys: {clé: <select>}, apply, bar, panel }
  const fold = (s) => s.toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");
  const isClient = (k) => k.toLowerCase() === "client"; // « client » ou en-tête « Client »

  // Texte où chercher : ce qui est affiché, le groupe, la colonne du kanban, les attributs data-* (cartes et cellules).
  function haystack(el) {
    const extra = [GROUPS[el.dataset.group] || "", el.closest(".kb-col")?.getAttribute("aria-label") || "", ...Object.values(el.dataset)];
    el.querySelectorAll("[data-values]").forEach((c) => extra.push(c.dataset.values));
    return fold(`${text(el)} ${extra.join(" ")}`);
  }

  function compare(a, b) {
    if (a === b) return 0;
    if (a === "" || a === "—") return 1; // vides en dernier
    if (b === "" || b === "—") return -1;
    return collator.compare(a, b);
  }

  // options : liste de [valeur, libellé]
  function select(label, options, onChange) {
    const wrap = document.createElement("label");
    wrap.className = "filter";
    const s = document.createElement("select");
    s.append(new Option(`${label} : tous`, ""));
    options.forEach(([value, text]) => s.append(new Option(text, value)));
    s.addEventListener("change", onChange);
    wrap.append(s);
    return [wrap, s];
  }

  function toolbar(block, onChange) {
    const bar = document.createElement("div");
    bar.className = "toolbar";
    const search = document.createElement("input");
    search.type = "search";
    search.placeholder = "Rechercher…";
    search.addEventListener("input", onChange);
    const count = document.createElement("span");
    count.className = "muted count-label";
    const reset = document.createElement("button");
    reset.type = "button";
    reset.className = "secondary reset";
    reset.textContent = "Effacer les filtres";
    reset.hidden = true;
    const more = reset.cloneNode();
    more.className = "secondary more-out";
    const toggle = reset.cloneNode();
    toggle.className = "secondary filters-toggle";
    toggle.hidden = false;
    toggle.setAttribute("aria-expanded", "false");
    const panel = document.createElement("div");
    panel.className = "filter-panel";
    panel.hidden = true;
    toggle.addEventListener("click", () => {
      panel.hidden = !panel.hidden;
      toggle.setAttribute("aria-expanded", String(!panel.hidden));
    });
    bar.append(search, toggle);
    block.before(bar);
    return { bar, panel, toggle, search, count, reset, more, outLabel: block.dataset.outLabel || "masqué(s)" };
  }

  function groupSelect(items, apply) {
    const present = new Set(items.map((it) => it.dataset.group).filter(Boolean));
    if (present.size < 2) return null;
    return select("Groupe", Object.entries(GROUPS).filter(([k]) => present.has(k)), apply);
  }

  function register(tools, keys, apply) {
    const { bar, panel, toggle, search, count, reset, more } = tools;
    const entry = { ...tools, keys, apply, out: null, shownOut: false }; // out : choix fait au bouton (null : aucun) ; shownOut : état affiché
    reset.addEventListener("click", () => {
      search.value = "";
      Object.values(keys).forEach((s) => (s.value = ""));
      apply();
    });
    more.addEventListener("click", () => { entry.out = !entry.shownOut; apply(); });
    bar.append(reset, more, count, panel);
    toggle.hidden = !panel.children.length;  // rien à filtrer : la recherche suffit
    blocks.push(entry);
    return entry;
  }

  // Affiche les éléments qui répondent à matches(el) ; ceux hors de la vue par défaut ([data-out]) seulement sur demande.
  function show(entry, items, matches) {
    const active = !!entry.search.value || Object.values(entry.keys).some((s) => s.value);
    // Le client présélectionné ne compte pas : seuls une recherche ou un autre filtre font proposer les éléments masqués.
    const narrowed = !!entry.search.value || Object.entries(entry.keys).some(([k, s]) => s.value && !isClient(k));
    if (!narrowed) entry.out = null;
    const found = items.filter(matches);
    const inside = found.filter((el) => !el.hasAttribute("data-out"));
    const outside = found.length - inside.length;
    const out = entry.out ?? (narrowed && !inside.length && outside > 0); // sans choix fait : affichés s'il n'y a rien d'autre
    entry.shownOut = out;
    const visible = new Set(out ? found : inside);
    items.forEach((el) => (el.hidden = !visible.has(el)));
    entry.more.hidden = !(narrowed && outside);
    entry.more.textContent = out ? `${outside} ${entry.outLabel} affiché(s) · masquer` : `+ ${outside} ${entry.outLabel} · afficher`;
    entry.reset.hidden = !active;
    const used = Object.values(entry.keys).filter((s) => s.value).length;
    entry.toggle.textContent = used ? `Filtres · ${used}` : "Filtres";
    entry.toggle.classList.toggle("is-on", used > 0);
    const total = out ? items.length : items.filter((el) => !el.hasAttribute("data-out")).length;
    entry.count.textContent = `${visible.size} / ${total}`;
    markCharts();
  }

  function searcher(search) {
    const words = fold(search.value).split(/\s+/).filter(Boolean);
    return (el) => { const hay = words.length ? haystack(el) : ""; return words.every((w) => hay.includes(w)); };
  }

  function setupTable(table) {
    const headers = [...table.tHead.rows[0].cells];
    const body = table.tBodies[0];
    const rows = [...body.rows].filter((r) => r.cells.length === headers.length);
    if (rows.length < 2) return;
    const cellValue = (row, i) => row.cells[i].dataset.sort ?? text(row.cells[i]);
    const tools = toolbar(table, apply);
    const { panel, search } = tools;
    const keys = {};
    const filters = headers
      .map((th, i) => [th, i])
      .filter(([th]) => th.hasAttribute("data-filter"))
      .map(([th, i]) => {
        const multi = th.hasAttribute("data-multi");
        const cellValues = (r) => (multi ? (r.cells[i].dataset.values || "").split("|") : [text(r.cells[i])]);
        const values = [...new Set(rows.flatMap(cellValues))].filter((v) => v && v !== "—").sort(compare);
        const [wrap, s] = select(text(th), values.map((v) => [v, v]), apply);
        panel.append(wrap);
        keys[th.dataset.filter || text(th)] = s;
        return { i, s, cellValues };
      });
    const group = groupSelect(rows, apply);
    if (group) { panel.append(group[0]); keys.groupe = group[1]; }
    const entry = register(tools, keys, apply);

    function apply() {
      const found = searcher(search);
      show(entry, rows, (r) => found(r)
        && filters.every((f) => !f.s.value || f.cellValues(r).includes(f.s.value))
        && (!keys.groupe || !keys.groupe.value || r.dataset.group === keys.groupe.value));
    }

    headers.forEach((th, i) => {
      if (th.hasAttribute("data-nosort")) return;
      th.classList.add("sortable");
      th.addEventListener("click", () => {
        const dir = th.dataset.dir === "asc" ? "desc" : "asc";
        headers.forEach((h) => delete h.dataset.dir);
        th.dataset.dir = dir;
        rows.sort((a, b) => (dir === "asc" ? 1 : -1) * compare(cellValue(a, i), cellValue(b, i)));
        if (dir === "desc") rows.sort((a, b) => { // les vides restent en bas même en ordre décroissant
          const va = cellValue(a, i), vb = cellValue(b, i);
          return (va === "" || va === "—") - (vb === "" || vb === "—");
        });
        body.append(...rows);
      });
    });
    apply();
  }

  function pairs(spec) {
    return (spec || "").split(",").filter(Boolean).map((p) => p.split(":"));
  }

  function setupCards(block) {
    const items = [...block.querySelectorAll(block.dataset.items)];
    if (items.length < 2) return;
    const tools = toolbar(block, apply);
    const { panel, search } = tools;
    const keys = {};
    const filters = pairs(block.dataset.filters).map(([label, attr]) => {
      const valuesOf = (it) => (it.dataset[attr] || "").split("|"); // « a|b » : la carte répond aux deux valeurs
      const values = [...new Set(items.flatMap(valuesOf))].filter(Boolean).sort(compare);
      const [wrap, s] = select(label, values.map((v) => [v, v]), apply);
      return { attr, s, wrap, valuesOf };
    }).filter((f) => f.s.options.length > 2);
    filters.forEach((f) => { panel.append(f.wrap); keys[f.attr] = f.s; });
    const group = groupSelect(items, apply);
    if (group) { panel.append(group[0]); keys.groupe = group[1]; }
    const sorts = pairs(block.dataset.sorts);
    if (sorts.length) {
      const wrap = document.createElement("label");
      wrap.className = "filter";
      const s = document.createElement("select");
      s.innerHTML = `<option value="">Trier : ordre Notion</option>` + sorts.map(([l, a]) => `<option value="${a}">Trier : ${l}</option>`).join("");
      const original = [...items];
      s.addEventListener("change", () => {
        const order = s.value ? [...items].sort((a, b) => compare(a.dataset[s.value] || "", b.dataset[s.value] || "")) : original;
        block.append(...order);
      });
      wrap.append(s);
      panel.append(wrap);
    }
    const entry = register(tools, keys, apply);

    function apply() {
      const found = searcher(search);
      show(entry, items, (it) => found(it)
        && filters.every((f) => !f.s.value || f.valuesOf(it).includes(f.s.value))
        && (!keys.groupe || !keys.groupe.value || it.dataset.group === keys.groupe.value));
      block.querySelectorAll(".kb-col").forEach((col) => { // kanban : nombre de cartes visibles par colonne
        const n = col.querySelector(".kb-count");
        if (n) n.textContent = col.querySelectorAll(".kb-card:not([hidden])").length;
        col.hidden = !entry.shownOut && !col.querySelector(".kb-card:not([data-out])"); // colonne faite seulement de cartes masquées
      });
    }
    apply();
  }

  // --- Graphiques cliquables -------------------------------------------------------------------
  function parse(spec, sep) {
    return Object.fromEntries((spec || "").split(sep).filter(Boolean).map((p) => {
      const i = p.indexOf("=");
      return [decodeURIComponent(p.slice(0, i)), decodeURIComponent(p.slice(i + 1))];
    }));
  }

  function setOption(s, value) {
    if (value && ![...s.options].some((o) => o.value === value)) s.append(new Option(value, value));
    s.value = value;
  }

  // Applique {clé: valeur} au premier bloc qui a toutes ces clés. Renvoie false si aucun bloc ne convient.
  function applyFilters(wanted, { toggle = false, scroll = true } = {}) {
    const entry = blocks.find((b) => Object.keys(wanted).every((k) => k in b.keys));
    if (!entry) return false;
    const already = Object.entries(wanted).every(([k, v]) => entry.keys[k].value === v);
    // Clic sur un graphique de la page (toggle) : le client choisi reste ; ancre venue d'une autre page : elle seule compte.
    Object.entries(entry.keys).forEach(([k, s]) => { if (!(toggle && isClient(k) && !(k in wanted))) s.value = ""; });
    if (!(toggle && already)) Object.entries(wanted).forEach(([k, v]) => setOption(entry.keys[k], v));
    entry.apply();
    if (scroll) entry.bar.scrollIntoView({ behavior: "smooth", block: "start" });
    return true;
  }

  // Surligne l'élément de graphique qui correspond aux filtres en cours, estompe les autres.
  function markCharts() {
    document.querySelectorAll(".chart[data-filterable-chart]").forEach((chart) => {
      let any = false;
      chart.querySelectorAll("[data-filter-set]").forEach((el) => {
        const wanted = parse(el.dataset.filterSet, ";");
        const entry = blocks.find((b) => Object.keys(wanted).every((k) => k in b.keys));
        const on = !!entry && Object.entries(wanted).every(([k, v]) => entry.keys[k].value === v)
          && Object.entries(entry.keys).every(([k, s]) => k in wanted || isClient(k) || !s.value);
        el.classList.toggle("is-selected", on);
        any ||= on;
      });
      chart.classList.toggle("has-selection", any);
    });
  }

  function activate(el) {
    const wanted = parse(el.dataset.filterSet, ";");
    if (el.dataset.filterHref) {
      const hash = Object.entries(wanted).map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join("&");
      location.href = `${el.dataset.filterHref}#${hash}`;
    } else {
      applyFilters(wanted, { toggle: true });
    }
  }

  document.addEventListener("click", (e) => {
    const el = e.target.closest("[data-filter-set]");
    if (el) { e.preventDefault(); activate(el); }
  });
  document.addEventListener("keydown", (e) => {
    const el = e.target.closest && e.target.closest("[data-filter-set]");
    if (el && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); activate(el); }
  });

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("table[data-filterable]").forEach(setupTable);
    document.querySelectorAll("[data-filterable][data-items]").forEach(setupCards);
    // Client par défaut : celui du menu en haut, dans chaque filtre « Client » qui le propose (« tous » reste au choix).
    const current = document.body.dataset.currentClient;
    if (current) blocks.forEach((b) => {
      const s = Object.entries(b.keys).find(([k]) => isClient(k))?.[1];
      if (s && !s.value && [...s.options].some((o) => o.value === current)) { s.value = current; b.apply(); }
    });
    if (location.hash.length > 1) applyFilters(parse(location.hash.slice(1), "&"));
    markCharts();
  });
})();
