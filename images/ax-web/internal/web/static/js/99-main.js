/*
 * Oficina de agentes · boot: shell, router, first snapshot, live stream.
 */
(() => {
  "use strict";
  const O = window.Oficina;

  // Fenced code blocks of rendered Markdown get a copy button. It copies
  // the raw code (the block shows invisible characters as ⟦U+XXXX⟧
  // markers) and warns when that raw code carries such characters.
  O.mdCodeDecorator = (pre, node) => {
    const raw = node && typeof node.text === "string" ? node.text : pre.textContent;
    pre.classList.add("has-copy");
    pre.appendChild(O.h("button", {
      type: "button", class: "pre-copy", "aria-label": "Copiar el código", title: "Copiar",
      on: { click: () => O.ui.copyText(raw, "Código copiado") },
    }, O.icon("copy", { size: 15 })));
  };

  function boot() {
    O.shell.init();
    O.router.start();
    O.store.load();
    O.stream.start();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
