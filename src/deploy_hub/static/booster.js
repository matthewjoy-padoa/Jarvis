// Booster gagné dans InteCG (jeu de cartes de l'équipe) : quand un DAG ou un agent lancé ici en rapporte un, sa
// notification (clé « intecg:… », relevée par notify.js) fait tomber un sachet au milieu de l'écran, avec un bouton
// qui ouvre le jeu. Se ferme seul, au clic à côté ou avec Échap. Sans mouvement si le Mac en demande moins (style.css).
// Le sachet est celui du jeu, repris tel quel (intecg : src/components/BoosterPack.tsx) : film métallisé violet → orange,
// contour encre, soudures crantées, formes pop, logo InteCG en autocollant. Le garder accordé au jeu s'il y change.
(function () {
  const SHOWN_MS = 9000;
  const INK = "#14121c", CREAM = "#fffbe9", FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif";
  // Dents de la soudure du sachet, en haut (direction -1) et en bas (1).
  const crimp = (y, direction) => "M5 " + y + " " + Array.from({ length: 21 }, (_, i) => `L${11 + i * 12} ${y + direction * 7} L${17 + i * 12} ${y}`).join(" ");
  const PACK = `<svg class="booster-pack" viewBox="0 0 262 380" aria-hidden="true">
    <defs>
      <linearGradient id="jv-booster-foil" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#3b1fa8"/><stop offset="0.35" stop-color="#c632b4"/><stop offset="0.6" stop-color="#ff5d6c"/><stop offset="1" stop-color="#ffb84d"/></linearGradient>
      <linearGradient id="jv-booster-sheen" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#fff" stop-opacity="0"/><stop offset="0.5" stop-color="#fff" stop-opacity="0.55"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>
      <pattern id="jv-booster-dots" width="10" height="10" patternUnits="userSpaceOnUse"><circle cx="5" cy="5" r="2.2" fill="${INK}" opacity="0.28"/></pattern>
      <clipPath id="jv-booster-body"><rect x="5" y="44" width="252" height="300"/></clipPath>
    </defs>
    <path d="${crimp(344, 1)} L257 344 L257 44 L5 44 Z" fill="url(#jv-booster-foil)" stroke="${INK}" stroke-width="4" stroke-linejoin="round"/>
    <g clip-path="url(#jv-booster-body)">
      <circle cx="20" cy="70" r="90" fill="url(#jv-booster-dots)"/><circle cx="250" cy="330" r="110" fill="url(#jv-booster-dots)"/>
      <rect class="booster-sheen" x="-120" y="30" width="90" height="330" fill="url(#jv-booster-sheen)" transform="skewX(-18)"/>
      <path d="M30 300q7-16 14 0t14 0 14 0" fill="none" stroke="#ffe14d" stroke-width="6" stroke-linecap="round"/>
      <path d="M196 92l9-17 9 17 9-17" fill="none" stroke="#5df2c2" stroke-width="5.5" stroke-linecap="round" stroke-linejoin="round"/>
      <path d="M212 270l16 28h-32z" fill="#ffe14d" stroke="${INK}" stroke-width="3" stroke-linejoin="round"/>
      <circle cx="52" cy="104" r="12" fill="none" stroke="#5df2c2" stroke-width="6"/>
    </g>
    <path d="M5 330h252" stroke="${INK}" stroke-width="2" stroke-dasharray="2 6" opacity="0.5"/>
    <g transform="translate(131 190) rotate(-8)">
      <circle cx="6" cy="8" r="70" fill="${INK}"/><circle r="70" fill="${CREAM}" stroke="${INK}" stroke-width="5"/>
      <text x="0" y="-4" text-anchor="middle" font-family="${FONT}" font-size="34" font-weight="900" fill="${INK}">Inte<tspan fill="#c632b4">CG</tspan></text>
      <text x="0" y="26" text-anchor="middle" font-family="${FONT}" font-size="13" font-weight="800" letter-spacing="3" fill="${INK}">5 CARTES</text>
    </g>
    <path d="${crimp(10, -1)} L257 10 L257 44 L5 44 Z" fill="url(#jv-booster-foil)" stroke="${INK}" stroke-width="4" stroke-linejoin="round"/>
    <path d="M5 44h252" stroke="${CREAM}" stroke-width="2.5" stroke-dasharray="7 5"/>
    <text x="131" y="33" text-anchor="middle" font-family="${FONT}" font-size="12" font-weight="800" letter-spacing="2.5" fill="${CREAM}">✂ DÉCHIRER ICI</text>
  </svg>`;
  let box, timer;
  function close() {
    clearTimeout(timer);
    if (box) box.remove();
    box = null;
  }
  function show(note) {
    close();
    box = document.createElement("div");
    box.className = "booster-won";
    box.innerHTML = '<div class="booster-won-card" role="status">' +
      '<div class="booster-pack-wrap"><span class="booster-burst" aria-hidden="true"></span>' + PACK + "</div>" +
      '<p class="booster-won-title"></p><p class="booster-won-detail muted"></p>' +
      '<form method="post" action="/outils/intecg" target="intecg"><button type="submit">Ouvrir InteCG</button></form>' +
      '<button type="button" class="booster-won-close secondary" aria-label="Fermer">✕</button></div>';
    box.querySelector(".booster-won-title").textContent = note.title || "InteCG · booster gagné";
    box.querySelector(".booster-won-detail").textContent = note.detail || "";
    box.addEventListener("click", (e) => { if (!e.target.closest(".booster-won-card") || e.target.closest(".booster-won-close, form")) setTimeout(close, 0); });
    document.body.append(box);
    timer = setTimeout(close, SHOWN_MS);
  }
  document.addEventListener("jarvis:notifs", (e) => {
    const won = (e.detail || []).filter((n) => String(n.key).startsWith("intecg:"));
    if (won.length) show(won[won.length - 1]);
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
  // Après un lancement (DAG, agent), on n'attend pas la minute du relevé : le booster arrive en quelques secondes.
  document.addEventListener("htmx:afterRequest", (e) => {
    const sent = e.detail && e.detail.requestConfig;
    if (sent && sent.verb === "post" && /^\/(actions|agents)\//.test(sent.path)) {
      [4000, 12000, 30000].forEach((ms) => setTimeout(() => document.dispatchEvent(new Event("jarvis:poll-notifs")), ms));
    }
  });
  window.jarvisBoosterWon = show;  // aperçu à la main : jarvisBoosterWon({title: "…", detail: "…"})
})();
