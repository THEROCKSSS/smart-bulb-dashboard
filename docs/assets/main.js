(function () {
  "use strict";

  var toggle = document.querySelector(".rail__toggle");
  var rail = document.querySelector(".rail");
  if (toggle && rail) {
    toggle.addEventListener("click", function () {
      var open = rail.classList.toggle("is-open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
    document.addEventListener("click", function (e) {
      if (rail.classList.contains("is-open") && !rail.contains(e.target) && e.target !== toggle) {
        rail.classList.remove("is-open");
        toggle.setAttribute("aria-expanded", "false");
      }
    });
  }

  function flashCopied(btn) {
    var original = btn.textContent;
    btn.textContent = "Copied";
    btn.setAttribute("data-copied", "true");
    setTimeout(function () {
      btn.textContent = original;
      btn.removeAttribute("data-copied");
    }, 1600);
  }

  document.addEventListener("click", function (e) {
    var changelogBtn = e.target.closest(".copy-changelog");
    if (changelogBtn) {
      var tmpl = document.getElementById(changelogBtn.dataset.target);
      if (!tmpl) return;
      navigator.clipboard.writeText(tmpl.content.textContent.trim()).then(function () {
        flashCopied(changelogBtn);
      });
      return;
    }

    var btn = e.target.closest(".copy-btn");
    if (!btn) return;
    var pre = btn.closest("pre");
    var codeEl = pre && pre.querySelector("code");
    if (!codeEl) return;
    navigator.clipboard.writeText(codeEl.textContent).then(function () {
      flashCopied(btn);
    });
  });
})();
