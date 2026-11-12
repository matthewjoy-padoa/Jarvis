// Console Jarvis : un terminal en bas de l'écran. On tape une commande, le serveur (/console) répond en lignes de texte.
// Une action (lancer un DAG, rafraîchir) demande « (o/N) » : sur « o », la route d'action existante est appelée ici.
// Entrée : exécuter · ↑↓ : historique · Tab : compléter · Ctrl+L : effacer · Ctrl+C : annuler · Échap : fermer.
// Ouverture : bouton flottant [data-console-open], touche « > » hors d'un champ (palette.js), Ctrl+` (comme VS Code).
(function () {
  const term = document.getElementById("term");
  if (!term) return;
  const out = document.getElementById("term-out"), screen = document.getElementById("term-screen");
  const form = document.getElementById("term-form"), input = document.getElementById("term-input");
  const promptEl = document.getElementById("term-prompt"), clientEl = document.getElementById("term-client");
  const COMMANDS = (term.dataset.commands || "").split(" ").filter(Boolean);
  const HISTORY = "jarvis-console-history", SCROLLBACK = "jarvis-console-scrollback", OPEN = "jarvis-console-open";
  const MAX_HISTORY = 50, MAX_LINES = 400;
  let pending = null, cursor = -1, draft = "", lastFocus = null, busy = false;

  const store = (area, key, value) => { try { area.setItem(key, value); } catch (e) {} };
  const load = (area, key) => { try { return area.getItem(key); } catch (e) { return null; } };
  const history = () => { try { return JSON.parse(load(localStorage, HISTORY) || "[]"); } catch (e) { return []; } };

  function line(text, cls) {  // ligne écrite par le navigateur : toujours en textContent
    const el = document.createElement("div");
    el.className = "t-line " + (cls || "");
    el.textContent = text || "\u00a0";
    out.append(el);
  }
  function echo(text) {
    const el = document.createElement("div"), p = document.createElement("span");
    el.className = "t-line t-cmd";
    p.className = "t-p";
    p.textContent = promptEl.textContent + " $ ";
    el.append(p, document.createTextNode(text));
    out.append(el);
  }
  function save() {
    while (out.children.length > MAX_LINES) out.firstElementChild.remove();
    store(sessionStorage, SCROLLBACK, out.innerHTML);  // lignes du serveur déjà échappées, celles du navigateur en textContent
    screen.scrollTop = screen.scrollHeight;
  }
  function banner() {
    ["      _                  _", "     | | __ _ _ ____   _(_)___", "  _  | |/ _` | '__\\ \\ / / / __|",
     " | |_| | (_| | |   \\ V /| \\__ \\", "  \\___/ \\__,_|_|    \\_/ |_|___/"].forEach((l) => line(l, "t-banner"));
    line("");
    line("Console Jarvis · client en cours : " + clientEl.textContent + " · « aide » pour les commandes", "t-dim");
    line("");
  }
  function setPrompt(prompt) {
    promptEl.textContent = prompt;
    clientEl.textContent = prompt.replace(/^jarvis:/, "").replace(/^~$/, "aucun client");
  }

  function open() {
    store(sessionStorage, OPEN, "1");  // la console reste ouverte d'une page à l'autre, comme un terminal
    if (term.hidden) {
      lastFocus = document.activeElement;
      term.hidden = false;
      document.documentElement.classList.add("term-open");
      const saved = load(sessionStorage, SCROLLBACK);
      if (saved) out.innerHTML = saved; else banner();
      save();
    }
    input.focus();
  }
  function close() {
    store(sessionStorage, OPEN, "");
    if (term.hidden) return;
    term.hidden = true;
    document.documentElement.classList.remove("term-open");
    if (lastFocus && document.contains(lastFocus)) lastFocus.focus();
  }

  async function confirmAnswer(answer) {
    const action = pending;
    pending = null;
    form.classList.remove("is-confirm");
    if (!/^(o|oui|y|yes)$/i.test(answer.trim())) { line("Annulé.", "t-dim"); return; }
    line("En cours…", "t-dim");
    try {
      const r = await fetch(action.url, { method: "POST", body: new URLSearchParams(action.data), headers: { "HX-Request": "true" } });
      const box = document.createElement("div");
      box.innerHTML = await r.text();  // fragment _action_result.html du serveur
      box.querySelectorAll("svg").forEach((svg) => svg.remove());  // l'icône « Simulation » n'est pas du texte
      const result = box.querySelector(".result");
      line(result ? result.textContent.replace(/\s+/g, " ").trim() : `Réponse ${r.status}`, (result && result.classList.contains("ok") ? "t-ok" : "t-ko") + " t-wrap");
      if (r.headers.get("HX-Refresh") === "true") { line("Rechargement de la page…", "t-dim"); save(); setTimeout(() => location.reload(), 1200); }
    } catch (e) {
      line("Jarvis ne répond pas : " + e.message, "t-ko");
    }
  }

  async function execute(text) {
    const r = await fetch("/console", { method: "POST", body: new URLSearchParams({ ligne: text }) });
    if (!r.ok) { line(`Erreur ${r.status}`, "t-ko"); return; }
    const res = await r.json();
    if (res.clear) out.replaceChildren();
    if (res.history) {
      const list = history(), start = Math.max(0, list.length - 20);
      list.slice(start).forEach((h, i) => line(String(start + i + 1).padStart(4) + "  " + h, "t-dim"));
    }
    out.insertAdjacentHTML("beforeend", res.lines.join(""));
    const switched = res.prompt !== promptEl.textContent;
    setPrompt(res.prompt);
    if (switched) { save(); location.reload(); return; }  // nouveau client en cours : la page le suit, la console reste ouverte
    if (res.confirm) {
      pending = res.confirm;
      line(res.confirm.question, "t-warn t-wrap");
      form.classList.add("is-confirm");
    }
    if (res.navigate) {
      save();
      if (res.ticket) {  // la fenêtre de ticket (drawer.js) passe au-dessus de la console
        const a = document.createElement("a");
        a.href = res.navigate;
        a.dataset.ticket = "";
        document.body.append(a);
        a.click();
        a.remove();
      } else {
        location.href = res.navigate;
      }
    }
    if (res.close) close();
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (busy) return;
    const text = input.value;
    input.value = "";
    cursor = -1;
    echo(text);
    busy = true;
    try {
      if (pending) await confirmAnswer(text);
      else if (text.trim()) {
        const list = history().filter((h) => h !== text.trim());
        list.push(text.trim());
        store(localStorage, HISTORY, JSON.stringify(list.slice(-MAX_HISTORY)));
        await execute(text);
      }
    } catch (err) {
      line("Jarvis ne répond pas : " + err.message, "t-ko");
    } finally {
      busy = false;
      save();
      input.focus();
    }
  });

  input.addEventListener("keydown", (e) => {
    const list = history();
    if (e.key === "ArrowUp" || e.key === "ArrowDown") {
      if (!list.length || pending) return;
      e.preventDefault();
      if (cursor === -1) draft = input.value;
      cursor = e.key === "ArrowUp" ? Math.min(cursor + 1, list.length - 1) : cursor - 1;
      input.value = cursor < 0 ? (cursor = -1, draft) : list[list.length - 1 - cursor];
    } else if (e.key === "Tab") {
      e.preventDefault();
      const word = input.value.trimStart();
      if (word.includes(" ")) return;
      const matches = COMMANDS.filter((c) => c.startsWith(word.toLowerCase()));
      if (matches.length === 1) input.value = matches[0] + " ";
      else if (matches.length > 1) { echo(input.value); line(matches.join("   "), "t-dim"); save(); }
    } else if (e.key === "l" && e.ctrlKey) {
      e.preventDefault();
      out.replaceChildren();
      save();
    } else if (e.key === "c" && e.ctrlKey && !window.getSelection().toString()) {
      e.preventDefault();
      echo(input.value + "^C");
      input.value = "";
      if (pending) { pending = null; form.classList.remove("is-confirm"); line("Annulé.", "t-dim"); }
      save();
    } else if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      close();
    }
  });

  screen.addEventListener("click", (e) => {  // un clic dans l'écran rend la main au clavier, sauf sur un lien ou une sélection
    if (!e.target.closest("a") && !window.getSelection().toString()) input.focus();
  });
  // Pastilles à la macOS : rouge ferme, jaune réduit à la barre de titre (un clic sur la barre la rouvre), verte agrandit.
  const SIZE = "jarvis-console-size";
  function size(state) {
    term.classList.toggle("is-minimized", state === "min");
    term.classList.toggle("is-maximized", state === "max");
    term.querySelector("[data-term-maximize]").setAttribute("aria-label", state === "max" ? "Remettre la console à sa taille" : "Agrandir la console");
    store(sessionStorage, SIZE, state || "");
    if (state !== "min") (document.getElementById("shell-pane")?.hidden === false ? null : input.focus());
  }
  const sizeNow = () => term.classList.contains("is-minimized") ? "min" : term.classList.contains("is-maximized") ? "max" : "";
  term.querySelector("[data-term-close]").addEventListener("click", (e) => { e.stopPropagation(); close(); });
  term.querySelector("[data-term-minimize]").addEventListener("click", (e) => { e.stopPropagation(); size(sizeNow() === "min" ? "" : "min"); });
  term.querySelector("[data-term-maximize]").addEventListener("click", (e) => { e.stopPropagation(); size(sizeNow() === "max" ? "" : "max"); });
  term.querySelector(".term-bar").addEventListener("click", (e) => {  // réduite : un clic sur la barre la rouvre
    if (sizeNow() === "min" && !e.target.closest("button")) size("");
  });
  size(load(sessionStorage, SIZE) || "");
  document.addEventListener("click", (e) => { if (e.target.closest("[data-console-open]")) open(); });
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey && e.key === "`") { e.preventDefault(); term.hidden ? open() : close(); }
  });
  window.jarvisConsole = { open, close };
  if (load(sessionStorage, OPEN)) open();
})();
