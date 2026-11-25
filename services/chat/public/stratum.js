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

// Sign-in errors. Chainlit's own message depends on the HTTP status text, which HTTP/2 does
// not send, so behind the HTTPS proxy a wrong password showed nothing. Show our own message
// whenever the sign-in request is refused.
(function () {
  if (!window.fetch) return;
  var original = window.fetch;
  function show(lines) {
    var form = document.querySelector("form");
    if (!form) return;
    var box = document.getElementById("st-login-error");
    if (!box) {
      box = document.createElement("div");
      box.id = "st-login-error";
      box.setAttribute("role", "alert");
      form.appendChild(box);
    }
    box.textContent = "";
    lines.forEach(function (text, i) {
      var p = document.createElement(i ? "small" : "strong");
      p.textContent = text;
      box.appendChild(p);
    });
  }
  function clear() {
    var box = document.getElementById("st-login-error");
    if (box) box.remove();
  }
  window.fetch = function (input, init) {
    var url = typeof input === "string" ? input : (input && input.url) || "";
    var method = ((init && init.method) || (input && input.method) || "GET").toUpperCase();
    var isLogin = method === "POST" && /\/login(\?|$)/.test(url);
    if (isLogin) clear();
    var result = original.apply(this, arguments);
    if (isLogin) {
      result.then(function (res) {
        if (res.status === 401 || res.status === 403) {
          show(["Incorrect email or password.", "Sign-in pauses for 15 minutes after 8 failed attempts on an account."]);
        } else if (!res.ok) {
          show(["Sign-in is unavailable right now.", "Try again in a moment."]);
        }
      }, function () {
        show(["Could not reach the server.", "Check your connection and try again."]);
      });
    }
    return result;
  };
})();

// Tell phone browsers which colour to use for their own bars, and keep it in step with the
// light/dark toggle.
(function () {
  function sync() {
    var dark = document.documentElement.classList.contains("dark");
    var meta = document.querySelector('meta[name="theme-color"]');
    if (!meta) {
      meta = document.createElement("meta");
      meta.name = "theme-color";
      document.head.appendChild(meta);
    }
    var colour = dark ? "#0b0e17" : "#f3f6fd";
    if (meta.content !== colour) meta.content = colour;
  }
  sync();
  new MutationObserver(sync).observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
})();
