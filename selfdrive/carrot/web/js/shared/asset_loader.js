"use strict";

(function initCarrotAssetLoader(global) {
  const scriptPromises = new Map();
  const stylePromises = new Map();

  function normalizeAssetUrl(url) {
    return String(url || "").trim();
  }

  function loadCarrotScriptOnce(url) {
    const src = normalizeAssetUrl(url);
    if (!src) return Promise.reject(new Error("missing script url"));
    if (scriptPromises.has(src)) return scriptPromises.get(src);

    const promise = new Promise((resolve, reject) => {
      const existing = Array.from(document.scripts || []).find((script) => script.src.endsWith(src));
      if (existing?.dataset.loaded === "1") {
        resolve(existing);
        return;
      }

      const script = existing || document.createElement("script");
      const finish = () => {
        script.dataset.loaded = "1";
        resolve(script);
      };
      const fail = () => {
        scriptPromises.delete(src);
        reject(new Error(`failed to load script: ${src}`));
      };

      script.addEventListener("load", finish, { once: true });
      script.addEventListener("error", fail, { once: true });
      if (!existing) {
        script.src = src;
        script.async = true;
        document.head.appendChild(script);
      }
    });

    scriptPromises.set(src, promise);
    return promise;
  }

  function loadCarrotStyleOnce(url) {
    const href = normalizeAssetUrl(url);
    if (!href) return Promise.reject(new Error("missing stylesheet url"));
    if (stylePromises.has(href)) return stylePromises.get(href);

    const promise = new Promise((resolve, reject) => {
      const existing = Array.from(document.styleSheets || [])
        .map((sheet) => sheet.ownerNode)
        .find((node) => node?.tagName === "LINK" && node.href?.endsWith(href));
      if (existing?.dataset.loaded === "1") {
        resolve(existing);
        return;
      }

      const link = existing || document.createElement("link");
      const finish = () => {
        link.dataset.loaded = "1";
        resolve(link);
      };
      const fail = () => {
        stylePromises.delete(href);
        reject(new Error(`failed to load stylesheet: ${href}`));
      };

      link.addEventListener("load", finish, { once: true });
      link.addEventListener("error", fail, { once: true });
      if (!existing) {
        link.rel = "stylesheet";
        link.href = href;
        document.head.appendChild(link);
      }
    });

    stylePromises.set(href, promise);
    return promise;
  }

  global.loadCarrotScriptOnce = loadCarrotScriptOnce;
  global.loadCarrotStyleOnce = loadCarrotStyleOnce;
  global.ensurePlyrAssets = function ensurePlyrAssets() {
    return Promise.all([
      loadCarrotStyleOnce("/css/vendor/plyr.css?v=3.7.8"),
      loadCarrotScriptOnce("/js/vendor/plyr.min.js?v=3.7.8"),
    ]);
  };
  global.ensureToolsQrVendor = function ensureToolsQrVendor() {
    return Promise.all([
      loadCarrotScriptOnce("/js/vendor/qrcode-generator.js?v=1.4.4"),
      loadCarrotScriptOnce("/js/vendor/jsQR.js?v=1.4.0"),
    ]);
  };
})(window);
