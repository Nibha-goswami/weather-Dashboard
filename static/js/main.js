// WeatherSphere AI — client-side behavior
// Handles: dark/light theme, live search suggestions, voice search,
// geolocation lookup, favorite toggling, share links, mobile nav.

(function () {
  "use strict";

  /* ---------------------------------------------------------- theme ---- */
  const root = document.documentElement;
  const themeBtn = document.getElementById("themeToggle");
  const savedTheme = localStorageSafe("get", "ws_theme") || "dark";
  root.setAttribute("data-theme", savedTheme);
  updateThemeIcon(savedTheme);

  if (themeBtn) {
    themeBtn.addEventListener("click", () => {
      const next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      updateThemeIcon(next);
      localStorageSafe("set", "ws_theme", next);
      fetch("/api/theme", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ theme: next }),
      }).catch(() => {});
    });
  }

  function updateThemeIcon(theme) {
    if (!themeBtn) return;
    themeBtn.innerHTML = theme === "dark"
      ? '<i class="fa-solid fa-moon"></i>'
      : '<i class="fa-solid fa-sun"></i>';
  }

  // Artifacts/sandboxed contexts can throw on localStorage access — guard it.
  function localStorageSafe(action, key, value) {
    try {
      if (action === "get") return window.localStorage.getItem(key);
      if (action === "set") window.localStorage.setItem(key, value);
    } catch (e) { return null; }
  }

  /* ------------------------------------------------------- burger nav --- */
  const burger = document.getElementById("navBurger");
  const navLinks = document.getElementById("navLinks");
  if (burger && navLinks) {
    burger.addEventListener("click", () => {
      navLinks.style.display = navLinks.style.display === "flex" ? "none" : "flex";
      navLinks.style.flexDirection = "column";
      navLinks.style.position = "absolute";
      navLinks.style.top = "64px";
      navLinks.style.left = "0";
      navLinks.style.right = "0";
      navLinks.style.background = "var(--card)";
      navLinks.style.padding = "16px 24px";
      navLinks.style.borderBottom = "1px solid var(--card-border)";
    });
  }

  /* --------------------------------------------------- search & suggest -- */
  function wireSearch(inputId, suggestId, formSubmitFn) {
    const input = document.getElementById(inputId);
    const box = document.getElementById(suggestId);
    if (!input || !box) return;
    let timer = null;

    input.addEventListener("input", () => {
      clearTimeout(timer);
      const q = input.value.trim();
      if (q.length < 2) { box.classList.remove("show"); box.innerHTML = ""; return; }
      timer = setTimeout(() => {
        fetch(`/api/search-suggestions?q=${encodeURIComponent(q)}`)
          .then((r) => r.json())
          .then((results) => {
            if (!results.length) { box.classList.remove("show"); box.innerHTML = ""; return; }
            box.innerHTML = results.map((r) =>
              `<div data-city="${escapeHtml(r.name)}">${escapeHtml(r.name)}${r.admin1 ? ", " + escapeHtml(r.admin1) : ""}${r.country ? " — " + escapeHtml(r.country) : ""}</div>`
            ).join("");
            box.classList.add("show");
            box.querySelectorAll("div").forEach((el) => {
              el.addEventListener("click", () => {
                window.location.href = "/weather/" + encodeURIComponent(el.dataset.city);
              });
            });
          })
          .catch(() => {});
      }, 250);
    });

    document.addEventListener("click", (e) => {
      if (!box.contains(e.target) && e.target !== input) {
        box.classList.remove("show");
      }
    });

    if (formSubmitFn) {
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && input.value.trim()) {
          e.preventDefault();
          formSubmitFn(input.value.trim());
        }
      });
    }
  }

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  function goToCity(city) {
    window.location.href = "/weather/" + encodeURIComponent(city);
  }

  wireSearch("navSearchInput", "navSuggestions", goToCity);
  wireSearch("heroSearchInput", "heroSuggestions", goToCity);

  const heroForm = document.getElementById("heroSearchForm");
  if (heroForm) {
    heroForm.addEventListener("submit", (e) => {
      e.preventDefault();
      const val = document.getElementById("heroSearchInput").value.trim();
      if (val) goToCity(val);
    });
  }

  /* -------------------------------------------------------- voice search -- */
  function wireVoice(btnId, inputId, autoSubmit) {
    const btn = document.getElementById(btnId);
    const input = document.getElementById(inputId);
    if (!btn || !input) return;
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) { btn.style.opacity = "0.4"; btn.title = "Voice search not supported in this browser"; return; }

    const recognition = new SpeechRecognition();
    recognition.lang = "en-US";
    recognition.interimResults = false;
    recognition.maxAlternatives = 1;

    btn.addEventListener("click", () => {
      btn.classList.add("listening");
      recognition.start();
    });
    recognition.onresult = (event) => {
      const text = event.results[0][0].transcript;
      input.value = text;
      if (autoSubmit) goToCity(text);
    };
    recognition.onend = () => btn.classList.remove("listening");
    recognition.onerror = () => btn.classList.remove("listening");
  }

  wireVoice("navVoiceBtn", "navSearchInput", true);
  wireVoice("heroVoiceBtn", "heroSearchInput", true);

  /* ------------------------------------------------------- geolocation --- */
  const geoBtn = document.getElementById("heroGeoBtn");
  if (geoBtn) {
    geoBtn.addEventListener("click", () => {
      if (!navigator.geolocation) { alert("Geolocation isn't supported in this browser."); return; }
      geoBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
      navigator.geolocation.getCurrentPosition(
        (pos) => {
          const { latitude, longitude } = pos.coords;
          // Reverse-resolve to a friendly name via a quick suggestion lookup,
          // falling back to raw coordinates if nothing resolves.
          fetch(`/api/geolocate?lat=${latitude}&lon=${longitude}`)
            .then((r) => r.json())
            .then(() => {
              window.location.href = `/weather/${latitude.toFixed(2)},${longitude.toFixed(2)}`;
            })
            .catch(() => { geoBtn.innerHTML = '<i class="fa-solid fa-location-crosshairs"></i>'; });
        },
        () => {
          geoBtn.innerHTML = '<i class="fa-solid fa-location-crosshairs"></i>';
          alert("Location access was denied. Please search manually.");
        }
      );
    });
  }

  /* --------------------------------------------------------- favorites --- */
  const favBtn = document.getElementById("favBtn");
  if (favBtn) {
    favBtn.addEventListener("click", () => {
      const isFav = favBtn.dataset.fav === "true";
      const payload = {
        city: favBtn.dataset.city,
        country: favBtn.dataset.country,
        lat: parseFloat(favBtn.dataset.lat),
        lon: parseFloat(favBtn.dataset.lon),
      };
      fetch("/api/favorites", {
        method: isFav ? "DELETE" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      })
        .then((r) => r.json())
        .then(() => {
          favBtn.dataset.fav = isFav ? "false" : "true";
          favBtn.classList.toggle("active");
          favBtn.querySelector("i").className = isFav ? "fa-regular fa-star" : "fa-solid fa-star";
        })
        .catch(() => {});
    });
  }

  /* -------------------------------------------------------------- share --- */
  const shareBtn = document.getElementById("shareBtn");
  if (shareBtn) {
    shareBtn.addEventListener("click", () => {
      const url = shareBtn.dataset.url;
      if (navigator.share) {
        navigator.share({ title: "WeatherSphere AI", url }).catch(() => {});
      } else {
        navigator.clipboard.writeText(url).then(() => {
          const original = shareBtn.innerHTML;
          shareBtn.innerHTML = '<i class="fa-solid fa-check"></i>';
          setTimeout(() => (shareBtn.innerHTML = original), 1500);
        }).catch(() => {});
      }
    });
  }

  /* --------------------------------------------------- auto data refresh -- */
  // Refresh the current weather page every 5 minutes so figures stay live.
  if (window.location.pathname.startsWith("/weather/")) {
    setTimeout(() => window.location.reload(), 5 * 60 * 1000);
  }
})();
