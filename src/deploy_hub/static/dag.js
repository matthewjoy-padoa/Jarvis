// Formulaires « Lancer un DAG » : confirmation qui reprend le DAG et la stack choisis ; la prod demande de taper PROD.
document.addEventListener("htmx:confirm", (e) => {
  const form = e.target.closest && e.target.closest("[data-dag-form]");
  if (!form || e.target !== form) return;
  e.preventDefault();
  const dag = form.elements.dag_id.value, env = form.elements.padoa_env.value;
  if (env === "prod") {
    const typed = window.prompt(`Jarvis : lancement de ${dag} sur la PROD.\nTape PROD pour confirmer.`);
    if (typed === null) return;
    form.setAttribute("hx-headers", JSON.stringify({ "HX-Prompt": typed }));
  } else {
    if (!window.confirm(`Jarvis : je lance ${dag} sur ${env} ?`)) return;
    form.removeAttribute("hx-headers");
  }
  e.detail.issueRequest(true);
});
// La stack prod se voit : la ligne passe en alerte quand on la choisit.
document.addEventListener("change", (e) => {
  if (e.target.name === "padoa_env") e.target.closest("[data-dag-form]")?.classList.toggle("is-prod", e.target.value === "prod");
});
