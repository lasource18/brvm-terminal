// Search box + chart bootstrapper.
// Depends on the global `LightweightCharts` (loaded per-page from CDN when
// the Chart tab is active). HTMX handles the actual dropdown swap.

(function () {
  // --- Enter-in-search: navigate to the first result if any. ---
  const input = document.getElementById("search-input");
  const results = document.getElementById("search-results");
  if (input && results) {
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        const first = results.querySelector("a[href]");
        if (first) {
          e.preventDefault();
          window.location.href = first.getAttribute("href");
        }
      } else if (e.key === "Escape") {
        input.value = "";
        results.innerHTML = "";
        input.blur();
      }
    });
    // Hide the dropdown when the input loses focus (small delay so clicks land).
    input.addEventListener("blur", () => {
      setTimeout(() => { results.innerHTML = ""; }, 150);
    });
  }

  // --- Chart bootstrapper (only present on the security Chart tab). ---
  const el = document.getElementById("chart");
  if (!el) return;
  const ticker = el.dataset.ticker;
  const empty = document.getElementById("chart-empty");
  if (!ticker || typeof LightweightCharts === "undefined") return;

  fetch(`/api/history/${encodeURIComponent(ticker)}`)
    .then((r) => r.json())
    .then((payload) => {
      const kind = payload && payload.kind;
      const bars = payload && payload.bars;
      if (!bars || bars.length === 0) {
        if (empty) empty.textContent = empty.dataset.empty || "No historical data available.";
        return;
      }
      if (empty) empty.remove();
      // Lightweight Charts formats its own axis and crosshair dates, so
      // without this the month ticks read "Jul/Aug/Sep" on a French page.
      // `<html lang>` is set from the resolved locale in base.html.
      const pageLang = document.documentElement.lang === "fr" ? "fr-FR" : "en-US";
      const chart = LightweightCharts.createChart(el, {
        localization: { locale: pageLang },
        layout: {
          background: { color: "#12181f" },
          textColor: "#d6e2ee",
          fontFamily: "JetBrains Mono, Menlo, monospace",
          fontSize: 11,
        },
        grid: {
          vertLines: { color: "#1c2530" },
          horzLines: { color: "#1c2530" },
        },
        rightPriceScale: { borderColor: "#1c2530" },
        timeScale: { borderColor: "#1c2530", timeVisible: false },
        crosshair: { mode: 1 },
      });

      if (kind === "index") {
        // Indices only have a daily level (no OHLC/volume) — line series.
        const line = chart.addLineSeries({
          color: "#7cd992",
          lineWidth: 2,
        });
        line.setData(bars.map((b) => ({ time: b.time, value: b.close })));
      } else {
        const candles = chart.addCandlestickSeries({
          upColor: "#7cd992",
          downColor: "#ff6b6b",
          borderUpColor: "#7cd992",
          borderDownColor: "#ff6b6b",
          wickUpColor: "#7cd992",
          wickDownColor: "#ff6b6b",
        });
        candles.setData(
          bars.map((b) => ({
            time: b.time,
            open: b.open ?? b.close,
            high: b.high ?? b.close,
            low: b.low ?? b.close,
            close: b.close,
          }))
        );
        const volume = chart.addHistogramSeries({
          priceFormat: { type: "volume" },
          priceScaleId: "",
          color: "#3f5871",
        });
        chart.priceScale("").applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
        volume.setData(bars.map((b) => ({ time: b.time, value: b.volume || 0 })));
      }
      chart.timeScale().fitContent();
      window.addEventListener("resize", () =>
        chart.applyOptions({ width: el.clientWidth })
      );
    })
    .catch((e) => {
      if (empty) empty.textContent = "Could not load chart: " + e;
    });
})();

// --- PWA shell + Web Push (PR-AA) ---------------------------------------
// The worker is registered on every page so the app is installable from
// anywhere; the subscribe UI only exists on /alerts (#push-panel).
(function () {
  if (!("serviceWorker" in navigator)) return;
  navigator.serviceWorker.register("/sw.js").catch(function () {});

  var panel = document.getElementById("push-panel");
  if (!panel || panel.dataset.enabled !== "true") return;
  var status = document.getElementById("push-status");
  var btnOn = document.getElementById("push-enable");
  var btnOff = document.getElementById("push-disable");
  var devices = document.getElementById("push-devices");
  var say = function (key) { if (status) status.textContent = panel.dataset["s" + key] || ""; };

  // iOS Safari only exposes PushManager to a page opened from the Home
  // Screen; in the plain browser the API is simply absent, so the hint
  // is the only useful thing to show.
  var isIOS = /iP(hone|ad|od)/.test(navigator.userAgent) ||
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  var standalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  if (!("PushManager" in window) || !("Notification" in window)) {
    say(isIOS && !standalone ? "Ios" : "Unsupported");
    return;
  }

  function keyBytes(b64url) {
    var pad = "=".repeat((4 - (b64url.length % 4)) % 4);
    var raw = atob((b64url + pad).replace(/-/g, "+").replace(/_/g, "/"));
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }

  function render(sub) {
    if (Notification.permission === "denied") {
      say("Denied"); btnOn.hidden = true; btnOff.hidden = true; return;
    }
    say(sub ? "On" : "Off");
    btnOn.hidden = !!sub;
    btnOff.hidden = !sub;
  }

  function post(method, body) {
    return fetch("/api/push/subscribe", {
      method: method,
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify(body),
    }).then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); });
  }

  navigator.serviceWorker.ready.then(function (reg) {
    reg.pushManager.getSubscription().then(render);

    btnOn.addEventListener("click", function () {
      btnOn.disabled = true;
      Notification.requestPermission().then(function (perm) {
        if (perm !== "granted") { render(null); btnOn.disabled = false; return; }
        return reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: keyBytes(panel.dataset.publicKey),
        }).then(function (sub) {
          return post("POST", sub.toJSON()).then(function (res) {
            if (devices) devices.textContent = res.devices;
            render(sub);
          });
        });
      }).catch(function () { say("Error"); }).then(function () { btnOn.disabled = false; });
    });

    btnOff.addEventListener("click", function () {
      btnOff.disabled = true;
      reg.pushManager.getSubscription().then(function (sub) {
        if (!sub) { render(null); return; }
        var endpoint = sub.endpoint;
        return sub.unsubscribe().then(function () {
          return post("DELETE", { endpoint: endpoint }).then(function (res) {
            if (devices) devices.textContent = res.devices;
          });
        }).then(function () { render(null); });
      }).catch(function () { say("Error"); }).then(function () { btnOff.disabled = false; });
    });
  });
})();
