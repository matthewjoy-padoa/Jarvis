// Filtres et tri côté navigateur pour chaque bloc marqué [data-filterable].
// - Tableau : les <th data-filter="clé"> donnent une liste déroulante (valeurs de la colonne) ; avec data-multi, la cellule
//   porte data-values="a|b" (plusieurs valeurs : une carte de deux équipes répond aux deux) ; un clic sur un <th> trie,
//   sauf [data-nosort]. Une cellule peut porter data-sort (ex. date ISO) pour trier sur autre chose que son texte.
// - Liste de cartes : data-items (sélecteur des cartes) et data-filters="Libellé:attribut,…" (attributs data-* des cartes),
//   data-sorts="Libellé:attribut,…" pour un menu de tri.
// - Lignes ou cartes avec data-group (todo, in_progress, ko, done) : filtre « Groupe » ajouté automatiquement.
// - Graphiques : un élément [data-filter-set="clé=valeur;clé=valeur"] applique ces filtres au clic (re-clic : retire) ;
//   avec [data-filter-href], il ouvre cette page, filtres passés dans l'ancre (#clé=valeur&clé=valeur).
(function () {
  const collator = new Intl.Collator("fr", { numeric: true, sensitivity: "base" });
  const text = (el) => (el ? el.textContent.replace(/\s+/g, " ").trim() : "");
  const GROUPS = { todo: "À faire", in_progress: "En cours", ko: "KO", done: "Terminé" };
  const blocks = []; // { keys: {clé: <select>}, apply, bar }

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
    bar.append(search);
    block.before(bar);
    return { bar, search, count, reset };
  }

  function groupSelect(items, apply) {
    const present = new Set(items.map((it) => it.dataset.group).filter(Boolean));
    if (present.size < 2) return null;
    return select("Groupe", Object.entries(GROUPS).filter(([k]) => present.has(k)), apply);
  }

  function register(bar, search, count, reset, keys, apply) {
    reset.addEventListener("click", () => {
      search.value = "";
      Object.values(keys).forEach((s) => (s.value = ""));
      apply();
    });
    bar.append(reset, count);
    const entry = { keys, bar, search, reset, apply };
    blocks.push(entry);
    return entry;
  }

  function finish(entry, shown, total) {
    const active = entry.search.value || Object.values(entry.keys).some((s) => s.value);
    entry.reset.hidden = !active;
    entry.bar.querySelector(".count-label").textContent = `${shown} / ${total}`;
    markCharts();
  }

  function setupTable(table) {
    const headers = [...table.tHead.rows[0].cells];
    const body = table.tBodies[0];
    const rows = [...body.rows].filter((r) => r.cells.length === headers.length);
    if (rows.length < 2) return;
    const cellValue = (row, i) => row.cells[i].dataset.sort ?? text(row.cells[i]);
    const { bar, search, count, reset } = toolbar(table, apply);
    const keys = {};
    const filters = headers
      .map((th, i) => [th, i])
      .filter(([th]) => th.hasAttribute("data-filter"))
      .map(([th, i]) => {
        const multi = th.hasAttribute("data-multi");
        const cellValues = (r) => (multi ? (r.cells[i].dataset.values || "").split("|") : [text(r.cells[i])]);
        const values = [...new Set(rows.flatMap(cellValues))].filter((v) => v && v !== "—").sort(compare);
        const [wrap, s] = select(text(th), values.map((v) => [v, v]), apply);
        bar.append(wrap);
        keys[th.dataset.filter || text(th)] = s;
        return { i, s, cellValues };
      });
    const group = groupSelect(rows, apply);
    if (group) { bar.append(group[0]); keys.groupe = group[1]; }
    const entry = register(bar, search, count, reset, keys, apply);

    function apply() {
      const q = search.value.toLowerCase();
      let shown = 0;
      for (const r of rows) {
        const ok = (!q || text(r).toLowerCase().includes(q))
          && filters.every((f) => !f.s.value || f.cellValues(r).includes(f.s.value))
          && (!keys.groupe || !keys.groupe.value || r.dataset.group === keys.groupe.value);
        r.hidden = !ok;
        shown += ok;
      }
      finish(entry, shown, rows.length);
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
    const { bar, search, count, reset } = toolbar(block, apply);
    const keys = {};
    const filters = pairs(block.dataset.filters).map(([label, attr]) => {
      const values = [...new Set(items.map((it) => it.dataset[attr] || ""))].filter(Boolean).sort(compare);
      const [wrap, s] = select(label, values.map((v) => [v, v]), apply);
      return { attr, s, wrap };
    }).filter((f) => f.s.options.length > 2);
    filters.forEach((f) => { bar.append(f.wrap); keys[f.attr] = f.s; });
    const group = groupSelect(items, apply);
    if (group) { bar.append(group[0]); keys.groupe = group[1]; }
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
      bar.append(wrap);
    }
    const entry = register(bar, search, count, reset, keys, apply);

    function apply() {
      const q = search.value.toLowerCase();
      let shown = 0;
      for (const it of items) {
        const ok = (!q || text(it).toLowerCase().includes(q))
          && filters.every((f) => !f.s.value || it.dataset[f.attr] === f.s.value)
          && (!keys.groupe || !keys.groupe.value || it.dataset.group === keys.groupe.value);
        it.hidden = !ok;
        shown += ok;
      }
      finish(entry, shown, items.length);
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
    Object.values(entry.keys).forEach((s) => (s.value = ""));
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
          && Object.entries(entry.keys).every(([k, s]) => k in wanted || !s.value);
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
    if (location.hash.length > 1) applyFilters(parse(location.hash.slice(1), "&"));
    markCharts();
  });
})();
