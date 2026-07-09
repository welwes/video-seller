(function () {
  "use strict";

  var body = document.body;
  var subUrl = body.getAttribute("data-suburl") || "";
  var shopName = body.getAttribute("data-shop") || "";

  if (subUrl) {
    var enc = encodeURIComponent(subUrl);
    var b64 = "";
    try {
      b64 = window.btoa(subUrl);
    } catch (e) {
      b64 = "";
    }
    var deepLinks = {
      happ: "happ://add/" + enc,
      streisand: "streisand://import/" + enc,
      shadowrocket: b64 ? "sub://" + b64 : "",
      v2raytun: "v2raytun://import/" + enc,
      hiddify: "hiddify://import/" + enc + "#" + encodeURIComponent(shopName),
      flclash: "clash://install-config?url=" + enc,
      foxray: b64
        ? "foxray://yiguo.dev/sub/add/?url=" + b64 + "&name=" + encodeURIComponent(shopName)
        : ""
    };
    var linkEls = document.querySelectorAll("[data-deeplink]");
    for (var i = 0; i < linkEls.length; i++) {
      var key = linkEls[i].getAttribute("data-deeplink");
      if (deepLinks[key]) {
        linkEls[i].setAttribute("href", deepLinks[key]);
      }
    }
  }

  function legacyCopy(text) {
    var area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.left = "-9999px";
    document.body.appendChild(area);
    area.select();
    try {
      document.execCommand("copy");
    } catch (e) {
      /* nothing else we can do */
    }
    document.body.removeChild(area);
  }

  function flashCopied(btn) {
    if (btn.getAttribute("data-flashing")) return;
    var original = btn.textContent;
    btn.setAttribute("data-flashing", "1");
    btn.classList.add("copied");
    btn.textContent = "Скопировано";
    window.setTimeout(function () {
      btn.textContent = original;
      btn.classList.remove("copied");
      btn.removeAttribute("data-flashing");
    }, 1600);
  }

  function copyText(text, btn) {
    if (!text) return;
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(
        function () { flashCopied(btn); },
        function () { legacyCopy(text); flashCopied(btn); }
      );
    } else {
      legacyCopy(text);
      flashCopied(btn);
    }
  }

  var copyBtns = document.querySelectorAll("[data-copy]");
  for (var c = 0; c < copyBtns.length; c++) {
    (function (btn) {
      btn.addEventListener("click", function () {
        copyText(btn.getAttribute("data-copy") || subUrl, btn);
      });
    })(copyBtns[c]);
  }

  var linkInput = document.getElementById("subUrl");
  if (linkInput) {
    linkInput.addEventListener("focus", function () {
      linkInput.select();
    });
  }

  var qrToggle = document.getElementById("qrToggle");
  var qrBox = document.getElementById("qrBox");
  if (qrToggle && qrBox) {
    qrToggle.addEventListener("click", function () {
      var opened = !qrBox.classList.contains("hidden");
      qrBox.classList.toggle("hidden", opened);
      qrToggle.setAttribute("aria-expanded", String(!opened));
      qrToggle.textContent = opened ? "Показать QR-код" : "Скрыть QR-код";
    });
  }

  var tabs = document.querySelectorAll(".tab[data-tab]");
  var panels = document.querySelectorAll(".tab-panel[data-panel]");

  function selectTab(name) {
    var found = false;
    for (var p = 0; p < panels.length; p++) {
      var match = panels[p].getAttribute("data-panel") === name;
      panels[p].classList.toggle("active", match);
      if (match) found = true;
    }
    if (!found) return;
    for (var t = 0; t < tabs.length; t++) {
      var active = tabs[t].getAttribute("data-tab") === name;
      tabs[t].classList.toggle("active", active);
      tabs[t].setAttribute("aria-selected", String(active));
    }
  }

  for (var t = 0; t < tabs.length; t++) {
    (function (tab) {
      tab.addEventListener("click", function () {
        selectTab(tab.getAttribute("data-tab"));
      });
    })(tabs[t]);
  }

  function detectPlatform() {
    var ua = navigator.userAgent || "";
    if (/iPhone|iPad|iPod/i.test(ua)) return "ios";
    if (/Android/i.test(ua)) return "android";
    if (/Macintosh|Mac OS X/i.test(ua)) {
      return navigator.maxTouchPoints > 1 ? "ios" : "macos";
    }
    return "windows";
  }

  if (tabs.length) {
    selectTab(detectPlatform());
  }
})();
