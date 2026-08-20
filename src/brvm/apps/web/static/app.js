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
    .then((bars) => {
      if (!bars || bars.length === 0) {
        if (empty) empty.textContent = "No historical data available.";
        return;
      }
      if (empty) empty.remove();
      const chart = LightweightCharts.createChart(el, {
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
      chart.timeScale().fitContent();
      window.addEventListener("resize", () =>
        chart.applyOptions({ width: el.clientWidth })
      );
    })
    .catch((e) => {
      if (empty) empty.textContent = "Could not load chart: " + e;
    });
})();
