// Formulaires « Lancer un DAG » : la confirmation montre l'aperçu d'air.py (ressources, run déjà en cours…) ;
// la prod demande de taper PROD. Un aperçu en échec (Airflow injoignable, DAG absent…) s'affiche et rien ne part.
async function dagPreview(dag, env) {
  const body = new URLSearchParams({ dag_id: dag, padoa_env: env });
  try {
    const res = await fetch("/actions/apercu", { method: "POST", body });
    return res.ok ? await res.json() : { ok: false, text: `Aperçu impossible (HTTP ${res.status}).` };
  } catch (err) {
    return { ok: false, text: `Aperçu impossible : ${err}` };
  }
}

document.addEventListener("htmx:confirm", async (e) => {
  const form = e.target.closest && e.target.closest("[data-dag-form]");
  if (!form || e.target !== form) return;
  e.preventDefault();
  const dag = form.elements.dag_id.value;  // liste de DAG, ou champ caché (ressource de la fiche, console)
  const env = form.elements.padoa_env.value;
  const button = form.querySelector("button[type=submit]");
  const label = button?.innerHTML;
  if (button) { button.disabled = true; button.textContent = "Aperçu…"; }  // air.py interroge Airflow : quelques secondes
  const preview = await dagPreview(dag, env);
  if (button) { button.disabled = false; button.innerHTML = label; }
  if (!preview.ok) {
    window.alert(`Jarvis : ${dag} ne peut pas être lancé sur ${env}.\n\n${preview.text}`);
    return;
  }
  if (env === "prod") {
    const typed = window.prompt(`Jarvis : lancement sur la PROD.\n\n${preview.text}\n\nTape PROD pour confirmer.`);
    if (typed === null) return;
    form.setAttribute("hx-headers", JSON.stringify({ "HX-Prompt": typed }));
  } else {
    if (!window.confirm(`Jarvis : je lance ce DAG ?\n\n${preview.text}`)) return;
    form.removeAttribute("hx-headers");
  }
  e.detail.issueRequest(true);
});
// La stack prod se voit : la ligne passe en alerte quand on la choisit.
document.addEventListener("change", (e) => {
  if (e.target.name === "padoa_env") e.target.closest("[data-dag-form]")?.classList.toggle("is-prod", e.target.value === "prod");
});
