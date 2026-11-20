// Filtres et tri côté navigateur pour chaque bloc marqué [data-filterable].
// - Tableau : les <th data-filter="clé"> donnent une liste déroulante (valeurs de la colonne) ; avec data-multi, la cellule
//   porte data-values="a|b" (plusieurs valeurs : une carte de deux équipes répond aux deux) ; un clic sur un <th> trie,
//   sauf [data-nosort]. Une cellule peut porter data-sort (ex. date ISO) pour trier sur autre chose que son texte.
// - Liste de cartes : data-items (sélecteur des cartes) et data-filters="Libellé:attribut,…" (attributs data-* des cartes),
//   data-sorts="Libellé:attribut,…" pour un menu de tri. Un attribut « a|b » porte plusieurs valeurs (ex. responsables).
// - Lignes ou cartes avec data-group (todo, in_progress, ko, other, done) : filtre « Groupe » ajouté automatiquement.
// - Graphiques : un élément [data-filter-set="clé=valeur;clé=valeur"] applique ces filtres au clic (re-clic : retire) ;
//   avec [data-filter-href], il ouvre cette page, filtres passés dans l'ancre (#clé=valeur&clé=valeur).
// - Recherche : sans accents ni majuscules, tous les mots doivent être présents ; elle lit aussi le groupe (« terminé »),
//   la colonne du kanban et les attributs data-* de la carte.
// - Éléments [data-out] (hors de la vue par défaut : retours terminés, actions non actives) : masqués, mais dès qu'une
//   recherche ou un filtre en trouve, un bouton propose de les afficher (data-out-label sur le bloc : comment les nommer).
//   S'il n'y a de résultat que parmi eux, ils sont affichés d'office.
// - Filtre « Client » : présélectionné sur le client du menu en haut (body[data-current-client]), sauf ancre de filtres.
// - Chaque filtre accepte plusieurs valeurs (cases à cocher) : un élément passe s'il a l'une d'elles ; les filtres entre eux
//   se cumulent. Dans une ancre ou un data-filter-set, « a|b » coche les deux.
// - Bloc [data-fold-filters] (listes de la fiche client) : la barre montre la recherche et un bouton « Filtres » (nombre de
//   filtres actifs) ; les menus déroulants et le tri sont dans un panneau qu'il ouvre. Tous les filtres restent là.
// - Mémoire : recherche, filtres et tri de chaque bloc sont gardés dans ce navigateur (localStorage), par page, et remis
//   au retour sur la page. Le filtre « Client » n'est pas gardé (il suit le client du menu) ; une ancre de filtres
//   (#clé=valeur) l'emporte sur ce qui était gardé ; « Effacer les filtres » vide aussi la mémoire (le tri reste).
// - Choix de vue : un clic sur une pastille [data-view="page.clé:valeur"] (Ouverts / Tous, Liste / Kanban, période…) le
//   note dans le cookie jarvis_vues ; le serveur s'en sert quand l'adresse ne porte aucun choix de vue (main.py, _views).
(function () {
  const collator = new Intl.Collator("fr", { numeric: true, sensitivity: "base" });
  const text = (el) => (el ? el.textContent.replace(/\s+/g, " ").trim() : "");
  const GROUPS = { todo: "À faire", in_progress: "En cours", ko: "KO", other: "À classer", done: "Terminé" };
  const blocks = []; // { keys: {clé: <select>}, apply, bar }
  const fold = (s) => s.toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");
  const isClient = (k) => k.toLowerCase() === "client"; // « client » ou en-tête « Client »
  const fromAnchor = location.hash.includes("="); // arrivée par un graphique d'une autre page : ses filtres seuls comptent

  // Filtres gardés d'une visite à l'autre : { q: recherche, f: {clé: "a|b"}, sort } par page et par bloc (son rang parmi
  // les blocs filtrables de la page : la liste et le kanban d'un même onglet partagent donc leurs filtres).
  const memoryKey = (block) => `jarvis.filtres:${location.pathname}:${[...document.querySelectorAll("[data-filterable]")].indexOf(block)}`;
  const recall = (key) => { try { return JSON.parse(localStorage.getItem(key)) || {}; } catch (e) { return {}; } };
  function remember(entry) {
    const saved = recall(entry.memory); // un filtre ou un tri que ce bloc n'a pas (autre présentation de la page) reste gardé
    const f = { ...saved.f };
    Object.entries(entry.keys).forEach(([k, s]) => { if (s.value && !isClient(k)) f[k] = s.value; else delete f[k]; });
    const state = { q: entry.search.value, f, sort: (entry.sort ? entry.sort.value : saved.sort) || "" };
    try {
      if (state.q || state.sort || Object.keys(f).length) localStorage.setItem(entry.memory, JSON.stringify(state));
      else localStorage.removeItem(entry.memory);
    } catch (e) {} // navigation privée, stockage plein : les filtres marchent, sans mémoire
  }
  function restore(entry) {
    const saved = fromAnchor ? {} : recall(entry.memory);
    entry.search.value = saved.q || "";
    Object.entries(entry.keys).forEach(([k, s]) => { // seulement les valeurs encore proposées (la donnée a pu changer)
      if (!isClient(k)) s.value = (saved.f?.[k] || "").split("|").filter((v) => s.options.some((o) => o.value === v)).join("|");
    });
    if (entry.sort && saved.sort && [...entry.sort.options].some((o) => o.value === saved.sort)) {
      entry.sort.value = saved.sort;
      entry.sort.dispatchEvent(new Event("change"));
    }
  }

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

  // Filtre à choix multiple : un menu (details) de cases à cocher. options : liste de [valeur, libellé].
  // Le contrôle renvoyé garde l'interface d'un <select> pour le reste du script : value (valeurs cochées jointes par « | »,
  // "" : aucune), options ; plus add(valeur) et matches(valeurs de l'élément).
  function multiSelect(label, options, onChange) {
    const wrap = document.createElement("details");
    wrap.className = "filter multi";
    const summary = document.createElement("summary");
    const list = document.createElement("div");
    list.className = "multi-list";
    const all = document.createElement("button");
    all.type = "button";
    all.className = "multi-all";
    all.textContent = "Tous";
    list.append(all);
    wrap.append(summary, list);
    const chosen = new Set();
    const boxes = new Map(); // valeur -> [case, libellé]
    const sync = () => {
      boxes.forEach(([box], v) => (box.checked = chosen.has(v)));
      const names = [...chosen].map((v) => boxes.get(v)[1]);
      summary.textContent = !names.length ? `${label} : tous` : names.length <= 2 ? `${label} : ${names.join(", ")}` : `${label} : ${names.length} choisis`;
      summary.title = names.join(", ");
      wrap.classList.toggle("is-on", names.length > 0);
    };
    const ctl = {
      get options() { return [...boxes.keys()].map((value) => ({ value })); },
      get value() { return [...chosen].join("|"); },
      set value(v) { chosen.clear(); (v || "").split("|").filter(Boolean).forEach((x) => { ctl.add(x); chosen.add(x); }); sync(); }, // « a|b » : les deux ; inconnue : ajoutée
      matches: (values) => !chosen.size || values.some((v) => chosen.has(v)),
      add(value, name = value) {
        if (boxes.has(value)) return;
        const row = document.createElement("label");
        const box = document.createElement("input");
        box.type = "checkbox";
        box.addEventListener("change", () => { box.checked ? chosen.add(value) : chosen.delete(value); sync(); onChange(); });
        row.append(box, ` ${name}`);
        list.append(row);
        boxes.set(value, [box, name]);
      },
    };
    all.addEventListener("click", () => { ctl.value = ""; onChange(); });
    options.forEach(([value, name]) => ctl.add(value, name));
    sync();
    return [wrap, ctl];
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
    bar.append(search);
    // Menus repliés : un bouton « Filtres » ouvre le panneau qui les porte ; sinon ils vont dans la barre.
    let panel = bar, toggle = null;
    if (block.hasAttribute("data-fold-filters")) {
      panel = document.createElement("div");
      panel.className = "filter-panel";
      panel.hidden = true;
      toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "secondary filters-toggle";
      toggle.setAttribute("aria-expanded", "false");
      toggle.textContent = "Filtres";
      toggle.addEventListener("click", () => {
        panel.hidden = !panel.hidden;
        toggle.setAttribute("aria-expanded", String(!panel.hidden));
      });
      bar.append(toggle);
    }
    block.before(bar);
    return { bar, panel, toggle, search, count, reset, more, outLabel: block.dataset.outLabel || "masqué(s)", memory: memoryKey(block) };
  }

  function groupSelect(items, apply) {
    const present = new Set(items.map((it) => it.dataset.group).filter(Boolean));
    if (present.size < 2) return null;
    return multiSelect("Groupe", Object.entries(GROUPS).filter(([k]) => present.has(k)), apply);
  }

  function register(tools, keys, apply, sort = null) { // sort : le <select> de tri du bloc, s'il en a un
    const { bar, panel, toggle, search, count, reset, more } = tools;
    const entry = { ...tools, keys, apply, sort, out: null, shownOut: false }; // out : choix fait au bouton (null : aucun) ; shownOut : état affiché
    restore(entry);
    sort?.addEventListener("change", () => remember(entry));
    reset.addEventListener("click", () => {
      search.value = "";
      Object.values(keys).forEach((s) => (s.value = ""));
      apply();
    });
    more.addEventListener("click", () => { entry.out = !entry.shownOut; apply(); });
    bar.append(reset, more, count);
    if (toggle) {
      bar.append(panel);
      toggle.hidden = !panel.children.length; // rien à filtrer : la recherche suffit
    }
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
    if (entry.toggle) {
      const used = Object.values(entry.keys).filter((s) => s.value).length;
      entry.toggle.textContent = used ? `Filtres · ${used}` : "Filtres";
      entry.toggle.classList.toggle("is-on", used > 0);
    }
    const total = out ? items.length : items.filter((el) => !el.hasAttribute("data-out")).length;
    entry.count.textContent = `${visible.size} / ${total}`;
    remember(entry);
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
        const [wrap, s] = multiSelect(text(th), values.map((v) => [v, v]), apply);
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
        && filters.every((f) => f.s.matches(f.cellValues(r)))
        && (!keys.groupe || keys.groupe.matches([r.dataset.group])));
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
      const [wrap, s] = multiSelect(label, values.map((v) => [v, v]), apply);
      return { attr, s, wrap, valuesOf };
    }).filter((f) => f.s.options.length > 1); // une seule valeur : rien à filtrer
    filters.forEach((f) => { panel.append(f.wrap); keys[f.attr] = f.s; });
    const group = groupSelect(items, apply);
    if (group) { panel.append(group[0]); keys.groupe = group[1]; }
    const sorts = pairs(block.dataset.sorts);
    let sort = null;
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
      sort = s;
    }
    const entry = register(tools, keys, apply, sort);

    function apply() {
      const found = searcher(search);
      show(entry, items, (it) => found(it)
        && filters.every((f) => f.s.matches(f.valuesOf(it)))
        && (!keys.groupe || keys.groupe.matches([it.dataset.group])));
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

  // Applique {clé: valeur} au premier bloc qui a toutes ces clés. Renvoie false si aucun bloc ne convient.
  function applyFilters(wanted, { toggle = false, scroll = true } = {}) {
    const entry = blocks.find((b) => Object.keys(wanted).every((k) => k in b.keys));
    if (!entry) return false;
    const already = Object.entries(wanted).every(([k, v]) => entry.keys[k].value === v);
    // Clic sur un graphique de la page (toggle) : le client choisi reste ; ancre venue d'une autre page : elle seule compte.
    Object.entries(entry.keys).forEach(([k, s]) => { if (!(toggle && isClient(k) && !(k in wanted))) s.value = ""; });
    if (!(toggle && already)) Object.entries(wanted).forEach(([k, v]) => (entry.keys[k].value = v));
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

  // Choix de vue gardés : les « page.clé:valeur » des pastilles actives de la barre, puis celui de la pastille cliquée
  // (il gagne), notés dans le cookie (un an, comme le client en cours) avant que la page ne change. Toute la barre, car
  // on a pu arriver par un lien direct qui montre autre chose que ce qui était gardé.
  function keepView(pill) {
    const kept = new Map((document.cookie.match(/(?:^|; )jarvis_vues=([^;]*)/)?.[1] || "").split("|").filter((p) => p.includes(":")).map((p) => p.split(":")));
    const bar = pill.closest(".view-bar, .deadline-bar") || pill.parentElement;
    [...bar.querySelectorAll("a.active[data-view]"), pill].forEach((a) => kept.set(...a.dataset.view.split(":")));
    document.cookie = `jarvis_vues=${[...kept].map((p) => p.join(":")).join("|")}; path=/; max-age=31536000; samesite=lax`;
  }

  const keepClickedView = (e) => { const view = e.target.closest("a[data-view]"); if (view) keepView(view); };
  document.addEventListener("auxclick", keepClickedView); // clic molette : l'onglet ouvert doit montrer la vue cliquée

  // Menu de filtre ouvert : un clic ailleurs ou Échap le ferme (un seul ouvert à la fois).
  const closeMenus = (keep) => document.querySelectorAll("details.filter.multi[open]").forEach((d) => { if (!d.contains(keep)) d.open = false; });
  document.addEventListener("click", (e) => {
    closeMenus(e.target);
    keepClickedView(e);
    const el = e.target.closest("[data-filter-set]");
    if (el) { e.preventDefault(); activate(el); }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeMenus(null);
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
