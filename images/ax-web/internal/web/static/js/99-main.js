/*
 * Oficina de agentes · boot: shell, router, first snapshot, live stream.
 */
(() => {
  "use strict";
  const O = window.Oficina;

  // Fenced code blocks of rendered Markdown get a copy button.
  O.mdCodeDecorator = (pre) => {
    const code = pre.firstChild;
    pre.classList.add("has-copy");
    pre.appendChild(O.h("button", {
      type: "button", class: "pre-copy", "aria-label": "Copiar el código", title: "Copiar",
      on: {
        click: async () => {
          try {
            await navigator.clipboard.writeText(code ? code.textContent : "");
            O.toast("Código copiado", { kind: "ok", timeout: 1500 });
          } catch (e) {
            O.toast("El navegador no permitió copiar", { kind: "warn" });
          }
        },
      },
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
