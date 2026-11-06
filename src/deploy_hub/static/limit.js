// Listes longues : seuls les data-limit premiers éléments sont visibles ; « Voir plus » en affiche autant à chaque clic,
// « Voir moins » revient au début. S'applique aussi aux fragments chargés par htmx (Quoi de neuf).
(function () {
  function setup(list) {
    if (list.dataset.limitReady) return;
    list.dataset.limitReady = "1";
    const step = Number(list.dataset.limit) || 5;
    const items = [...list.children].filter((el) => !el.classList.contains("muted"));
    if (items.length <= step) return;
    let shown = step;
    const bar = document.createElement("div");
    bar.className = "more-bar";
    const more = document.createElement("button");
    more.type = "button";
    more.className = "secondary";
    const less = more.cloneNode();
    less.textContent = "Voir moins";
    bar.append(more, less);
    list.after(bar);
    function render() {
      items.forEach((el, i) => el.classList.toggle("is-collapsed", i >= shown));
      const rest = items.length - shown;
      more.hidden = rest <= 0;
      more.textContent = `Voir plus (${Math.min(step, rest)} sur ${rest} restant${rest > 1 ? "s" : ""})`;
      less.hidden = shown <= step;
    }
    more.addEventListener("click", () => { shown += step; render(); });
    less.addEventListener("click", () => { shown = step; render(); list.scrollIntoView({ block: "nearest", behavior: "smooth" }); });
    render();
  }
  const run = (root) => root.querySelectorAll("[data-limit]").forEach(setup);
  document.addEventListener("DOMContentLoaded", () => run(document));
  document.addEventListener("htmx:afterSwap", (e) => run(e.target));
})();
