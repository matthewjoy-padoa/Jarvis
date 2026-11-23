// Enregistrements ratés. Une action htmx qui écrit (POST : note tapée, rappel, épingle, tâche, lancement…) et échoue
// — Jarvis injoignable, ou réponse en erreur — ne remplace rien : sans ce script, on croit que c'est fait. Le message
// va dans la zone de résultat de l'action (hx-target qui ne contient pas l'élément : .dag-out, #…-out), sinon juste après
// le formulaire ou le bouton. Notes et to-do lists : « Non enregistré », court, dans leur état commun ([data-save-scope] →
// [data-save-status], voir notes.js), la raison au survol.
// Il disparaît au prochain essai. Les lectures (GET : panneaux, rafraîchissements en fond) restent silencieuses.
(function () {
  const writes = (e) => (e.detail.requestConfig?.verb || "get").toLowerCase() !== "get";
  // Où écrire le message de cette action : [conteneur, avant quel nœud (null : à la fin), zone de résultat à vider ?].
  function spot(elt, target) {
    if (target && target.isConnected && target !== elt && !target.contains(elt)) return [target, null, true];
    const after = elt.closest("form") || elt;
    return [after.parentNode, after.nextSibling, false];
  }
  function clear(elt, target) {
    const [where] = spot(elt, target);
    where?.querySelectorAll(":scope > .request-error").forEach((p) => p.remove());
  }
  function reason(xhr) {
    if (!xhr || !xhr.status) return "Non pris en compte : Jarvis ne répond pas. Réessaie dans un instant.";
    let detail = "";
    try { detail = JSON.parse(xhr.responseText).detail; } catch (err) { /* réponse HTML ou vide */ }
    return typeof detail === "string" && detail
      ? `Non pris en compte : ${detail}.`.replace(/\.\.$/, ".")
      : `Non pris en compte : Jarvis a répondu par une erreur (${xhr.status}). Recharge la page.`;
  }
  document.addEventListener("htmx:beforeRequest", (e) => {
    if (writes(e)) clear(e.detail.elt, e.detail.target);
  });
  document.addEventListener("htmx:afterRequest", (e) => {
    if (e.detail.successful || !writes(e) || !e.detail.elt?.isConnected) return;
    const status = e.detail.elt.closest("[data-save-scope]")?.querySelector("[data-save-status]");
    if (status) {
      status.className = "save-status ko";
      status.textContent = "Non enregistré";
      status.title = reason(e.detail.xhr);
      return;
    }
    const [where, before, output] = spot(e.detail.elt, e.detail.target);
    if (!where) return;
    if (output) where.replaceChildren();  // zone de résultat : le message remplace l'ancien résultat
    else where.querySelectorAll(":scope > .request-error").forEach((p) => p.remove());
    const p = document.createElement("p");
    p.className = "result ko request-error";
    p.setAttribute("role", "alert");
    p.textContent = reason(e.detail.xhr);
    where.insertBefore(p, before);
  });
})();
