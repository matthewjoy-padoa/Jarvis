// Onglet « Shell VM » de la console : un vrai terminal (xterm.js) relié au shell de la VM par WebSocket (/terminal/ws).
// Présent seulement si Jarvis a JARVIS_SHELL=1 (la page porte alors data-shell-token). xterm.js se charge à la première
// ouverture. La session vit tant que la page est ouverte : changer de page la ferme (tmux sur la VM pour la garder).
(function () {
  const term = document.getElementById("term"), pane = document.getElementById("shell-pane");
  if (!term || !pane || !term.dataset.shellToken) return;
  const screen = document.getElementById("term-screen"), input = document.getElementById("term-input");
  const CDN = "https://cdn.jsdelivr.net/npm/", TAB = "jarvis-console-tab";
  let xterm = null, fit = null, ws = null, loading = null;

  function load(tag, attrs) {
    return new Promise((resolve, reject) => {
      const el = Object.assign(document.createElement(tag), attrs, { onload: resolve, onerror: () => reject(new Error(attrs.src || attrs.href)) });
      document.head.append(el);
    });
  }
  function libraries() {
    loading = loading || Promise.all([
      load("link", { rel: "stylesheet", href: CDN + "@xterm/xterm@5.5.0/css/xterm.css" }),
      load("script", { src: CDN + "@xterm/xterm@5.5.0/lib/xterm.js" }).then(() => load("script", { src: CDN + "@xterm/addon-fit@0.10.0/lib/addon-fit.js" })),
    ]);
    return loading;
  }

  const send = (event) => { if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(event)); };
  function resize() {
    if (!xterm || pane.hidden) return;
    fit.fit();
    send({ type: "resize", cols: xterm.cols, rows: xterm.rows });
  }

  function connect() {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${scheme}://${location.host}/terminal/ws?token=${encodeURIComponent(term.dataset.shellToken)}`);
    ws.binaryType = "arraybuffer";
    ws.onopen = () => resize();
    ws.onmessage = (e) => xterm.write(typeof e.data === "string" ? e.data : new Uint8Array(e.data));
    ws.onclose = (e) => {
      ws = null;
      xterm.write(`\r\n\x1b[33m[${e.reason || "Session fermée."} Appuie sur Entrée pour en ouvrir une nouvelle.]\x1b[0m\r\n`);
    };
  }

  async function show() {
    pane.hidden = false;
    screen.hidden = true;
    if (!xterm) {
      try {
        await libraries();
      } catch (e) {
        pane.textContent = "Impossible de charger xterm.js (" + e.message + ") : vérifie l'accès à cdn.jsdelivr.net.";
        return;
      }
      xterm = new window.Terminal({
        cursorBlink: true, fontSize: 13.5, scrollback: 5000, allowProposedApi: false,
        fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
        theme: { background: "#0b1222", foreground: "#d7def0", cursor: "#7ee2a0", selectionBackground: "#a9a3ff59" },
      });
      fit = new window.FitAddon.FitAddon();
      xterm.loadAddon(fit);
      xterm.open(pane);
      xterm.onData((data) => {
        if (!ws) { if (data === "\r") connect(); return; }  // session fermée : Entrée en rouvre une
        send({ type: "input", data });
      });
      new ResizeObserver(() => resize()).observe(pane);
      connect();
    }
    resize();
    xterm.focus();
  }
  function hide() {
    pane.hidden = true;
    screen.hidden = false;
    input.focus();
  }

  function select(tab) {
    term.querySelectorAll("[data-term-tab]").forEach((b) => {
      const on = b.dataset.termTab === tab;
      b.classList.toggle("is-active", on);
      b.setAttribute("aria-selected", on);
    });
    try { sessionStorage.setItem(TAB, tab); } catch (e) {}
    tab === "shell" ? show() : hide();
  }
  term.addEventListener("click", (e) => {
    const tab = e.target.closest("[data-term-tab]");
    if (tab) select(tab.dataset.termTab);
  });
  // Rouverte sur l'onglet Shell après un changement de page : nouvelle session (l'ancienne s'est fermée avec la page).
  new MutationObserver(() => {
    let last = "";
    try { last = sessionStorage.getItem(TAB) || ""; } catch (e) {}
    if (!term.hidden && last === "shell" && pane.hidden) select("shell");
  }).observe(term, { attributes: true, attributeFilter: ["hidden"] });
})();
