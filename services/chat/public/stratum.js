// First visit only: start in the dark theme. The theme toggle still works afterwards.
(function () {
  try {
    if (!localStorage.getItem("vite-ui-theme")) {
      localStorage.setItem("vite-ui-theme", "dark");
      document.documentElement.classList.remove("light");
      document.documentElement.classList.add("dark");
    }
  } catch (e) {}
})();

// When an answer arrives, Chainlit opens the side panel with every cited PDF stacked under
// one title, which makes it look as if a passage came from the wrong document. Close that
// stacked view; the panel still opens normally (one document, right page) when a chip is clicked.
(function () {
  var closing = false;
  new MutationObserver(function () {
    var content = document.getElementById("side-view-content");
    if (!content || closing || content.children.length < 2) return;
    var back = document.querySelector("#side-view-title button");
    if (!back) return;
    closing = true;
    back.click();
    setTimeout(function () { closing = false; }, 300);
  }).observe(document.documentElement, { childList: true, subtree: true });
})();
