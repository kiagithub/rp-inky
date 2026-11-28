(() => {
  let savedPin = localStorage.getItem("rp_inky_pin") || "";
  let benchPollTimer = null;

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  function showToast(msg, isError = false) {
    const el = $("#toast");
    el.textContent = msg;
    el.style.borderColor = isError ? "#ef4444" : "#38bdf8";
    el.classList.remove("hidden");
    clearTimeout(el._timer);
    el._timer = setTimeout(() => el.classList.add("hidden"), 3800);
  }

  function esc(val) {
    const d = document.createElement("div");
    d.textContent = val == null ? "" : String(val);
    return d.innerHTML;
  }

  async function api(path, opts = {}) {
    const headers = Object.assign({ "X-Requested-With": "rp-inky" }, opts.headers || {});
    if (savedPin) headers["X-Pin"] = savedPin;
    if (opts.json !== undefined) {
      headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.json);
    }
    const resp = await fetch(path, Object.assign({}, opts, { headers }));
    if (resp.status === 401) {
      $("#pin-modal").classList.remove("hidden");
      throw new Error("PIN required");
    }
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.error || `HTTP ${resp.status}`);
    }
    return data;
  }

  // -------------------- Tabs & Auth --------------------
  let activeTab = "photos";
  $$(".tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$(".tab").forEach((b) => b.classList.remove("active"));
      $$(".tab-panel").forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      activeTab = btn.dataset.tab;
      $(`#tab-${activeTab}`).classList.add("active");
      if (activeTab === "vpn") loadVpnStatus();
      if (activeTab === "network") loadNetwork(false);
      if (activeTab === "monitor") loadMonitor();
      if (activeTab === "wol") loadWol();
    });
  });

  $("#pin-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    savedPin = $("#pin-input").value.trim();
    localStorage.setItem("rp_inky_pin", savedPin);
    $("#pin-modal").classList.add("hidden");
    refreshAll();
  });

  // -------------------- Tab 1: Photos & Cropper --------------------
  function refreshPreviewImage(photoName = "", view = "") {
    const params = new URLSearchParams({ t: String(Date.now()) });
    if (photoName) params.set("photo", photoName);
    if (view) params.set("view", view);
    $("#frame-preview-img").src = `/api/frame/preview?${params.toString()}`;
  }

  const photoInput = $("#photo-input");
  const cropX = $("#crop-x");
  const cropY = $("#crop-y");
  const cropImg = $("#crop-preview-img");

  function updateCropPreviewPos() {
    const px = Math.round(parseFloat(cropX.value) * 100);
    const py = Math.round(parseFloat(cropY.value) * 100);
    cropImg.style.objectPosition = `${px}% ${py}%`;
  }
  cropX.addEventListener("input", updateCropPreviewPos);
  cropY.addEventListener("input", updateCropPreviewPos);

  photoInput.addEventListener("change", () => {
    const files = photoInput.files;
    if (!files || !files.length) {
      $("#crop-box-wrap").classList.add("hidden");
      $("#file-drop-label").textContent = "Tap to select photo(s) from Camera Roll";
      return;
    }
    $("#file-drop-label").textContent =
      files.length === 1 ? `Selected: ${files[0].name}` : `Selected ${files.length} photos`;
    const first = files[0];
    if (first.type.startsWith("image/")) {
      cropImg.src = URL.createObjectURL(first);
      cropX.value = "0.5";
      cropY.value = "0.5";
      updateCropPreviewPos();
      $("#crop-box-wrap").classList.remove("hidden");
    } else {
      $("#crop-box-wrap").classList.add("hidden");
    }
  });

  $("#upload-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const files = photoInput.files;
    if (!files || !files.length) return;
    const fd = new FormData();
    for (const f of files) fd.append("photos", f);
    fd.append("center_x", cropX.value);
    fd.append("center_y", cropY.value);
    fd.append("location", $("#upload-location").value.trim());
    fd.append("date", $("#upload-date").value.trim());
    fd.append("show_now", $("#upload-show-now").checked ? "1" : "0");

    const btn = $("#btn-upload");
    btn.disabled = true;
    btn.textContent = "Uploading & Cropping…";
    try {
      const res = await api("/api/photos/upload", { method: "POST", body: fd });
      showToast(`Uploaded ${res.count} photo(s)!`);
      $("#upload-form").reset();
      $("#crop-box-wrap").classList.add("hidden");
      $("#file-drop-label").textContent = "Tap to select photo(s) from Camera Roll";
      await loadGallery();
      if (res.uploaded && res.uploaded.length) {
        refreshPreviewImage(res.uploaded[res.uploaded.length - 1].name);
      }
    } catch (err) {
      showToast(err.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = "Upload & Crop to 480×800";
    }
  });

  async function loadGallery() {
    const data = await api("/api/photos");
    $("#album-count").textContent = data.count;
    const dash = data.dashboard || {};
    if (dash.mode) {
      $("#dash-mode").value = dash.mode;
      $("#dash-sched-wrap").classList.toggle("hidden", dash.mode !== "schedule");
    }
    if (dash.schedule_hours) $("#dash-sched").value = dash.schedule_hours;
    if (dash.content) $("#dash-content").value = dash.content;
    if (dash.custom_text !== undefined) $("#dash-custom").value = dash.custom_text || "";

    const unpinBtn = $("#btn-unpin-photo");
    if (data.pinned_image) {
      unpinBtn.classList.remove("hidden");
      $("#frame-caption").textContent = `Pinned: ${data.pinned_image}`;
    } else if (data.last_view === "dashboard") {
      unpinBtn.classList.add("hidden");
      $("#frame-caption").textContent = `Showing: Weather & Daily Wisdom (mode: ${dash.mode || "alternate"})`;
    } else {
      unpinBtn.classList.add("hidden");
      $("#frame-caption").textContent = data.current_image
        ? `Showing: ${data.current_image} (mode: ${dash.mode || "photos"}, ${data.refresh_minutes}m)`
        : "No photo displayed yet";
    }

    const grid = $("#gallery-grid");
    if (!data.photos.length) {
      grid.innerHTML = `<p class="muted">No photos in album yet. Upload from your phone above!</p>`;
      return;
    }
    grid.innerHTML = data.photos
      .map((p) => {
        const badges = [
          p.is_current ? `<span class="pill pill-ok">On Screen</span>` : "",
          p.is_pinned ? `<span class="pill pill-neutral">Pinned</span>` : "",
        ]
          .filter(Boolean)
          .join(" ");
        return `
        <div class="photo-card">
          <img src="/api/photos/file/${encodeURIComponent(p.name)}?v=${p.modified_at}" loading="lazy" alt="${esc(p.name)}">
          <div class="photo-meta">
            <div class="photo-title" title="${esc(p.name)}">${esc(p.place || p.name)}</div>
            <div class="photo-sub">${esc(p.date_display || "")} ${badges}</div>
            <div class="photo-actions">
              <button class="btn btn-primary btn-sm" data-photo-act="show" data-name="${esc(p.name)}">Show</button>
              <button class="btn btn-ghost btn-sm" data-photo-act="pin" data-name="${esc(p.name)}">Pin</button>
              <button class="btn btn-ghost btn-sm" data-photo-act="edit" data-name="${esc(p.name)}" data-loc="${esc(p.sidecar_location || p.place)}" data-date="${esc(p.sidecar_date || p.date)}">Caption</button>
              <button class="btn btn-danger btn-sm" data-photo-act="delete" data-name="${esc(p.name)}">Delete</button>
            </div>
          </div>
        </div>`;
      })
      .join("");

    grid.querySelectorAll("button[data-photo-act]").forEach((btn) => {
      btn.addEventListener("click", () => handlePhotoAction(btn));
    });
  }

  async function handlePhotoAction(btn) {
    const act = btn.dataset.photoAct;
    const name = btn.dataset.name;
    if (act === "edit") {
      $("#meta-photo-name").value = name;
      $("#meta-location").value = btn.dataset.loc || "";
      $("#meta-date").value = btn.dataset.date || "";
      $("#meta-modal").classList.remove("hidden");
      return;
    }
    btn.disabled = true;
    try {
      if (act === "delete") {
        if (!confirm(`Delete ${name} from the photo frame?`)) return;
        await api("/api/photos/delete", { method: "POST", json: { name } });
        showToast(`Deleted ${name}`);
        await loadGallery();
        refreshPreviewImage();
        return;
      }
      const res = await api("/api/frame/action", { method: "POST", json: { action: act, name } });
      const panelMsg = res.signaled ? "(e-ink panel refreshing ~30s)" : "(preview updated; check inky-photoframe service)";
      showToast(act === "pin" ? `Pinned ${name} ${panelMsg}` : `Showing ${name} ${panelMsg}`);
      refreshPreviewImage();
      await loadGallery();
    } catch (err) {
      showToast(err.message, true);
    } finally {
      btn.disabled = false;
    }
  }

  $("#btn-show-dash").addEventListener("click", async () => {
    const btn = $("#btn-show-dash");
    btn.disabled = true;
    try {
      const res = await api("/api/frame/action", { method: "POST", json: { action: "dashboard" } });
      showToast(
        res.signaled
          ? "Sending Weather + Daily Quote to e-ink panel (~30s refresh)…"
          : "Preview updated (inky-photoframe service not signaled)"
      );
      refreshPreviewImage();
      await loadGallery();
    } catch (err) {
      showToast(err.message, true);
    } finally {
      btn.disabled = false;
    }
  });

  $("#btn-shuffle-wisdom").addEventListener("click", async () => {
    const btn = $("#btn-shuffle-wisdom");
    btn.disabled = true;
    try {
      const res = await api("/api/frame/action", { method: "POST", json: { action: "shuffle_wisdom" } });
      showToast(
        res.signaled
          ? "Picked new Fact/Quote & refreshing e-ink panel (~30s)…"
          : "Picked new Fact/Quote (inky-photoframe service not signaled)"
      );
      refreshPreviewImage();
      await loadGallery();
    } catch (err) {
      showToast(err.message, true);
    } finally {
      btn.disabled = false;
    }
  });

  $("#dash-mode").addEventListener("change", () => {
    $("#dash-sched-wrap").classList.toggle("hidden", $("#dash-mode").value !== "schedule");
  });

  $("#dash-mode-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const mode = $("#dash-mode").value;
      await api("/api/frame/dashboard", {
        method: "POST",
        json: {
          mode,
          schedule_hours: $("#dash-sched").value.trim(),
          content: $("#dash-content").value,
          custom_text: $("#dash-custom").value.trim(),
          refresh_now: true,
        },
      });
      showToast(`Saved settings ('${mode}') & signaled e-ink refresh (~30s)!`);
      refreshPreviewImage();
      await loadGallery();
    } catch (err) {
      showToast(err.message, true);
    }
  });

  $("#btn-next-photo").addEventListener("click", async () => {
    const btn = $("#btn-next-photo");
    btn.disabled = true;
    try {
      const res = await api("/api/frame/action", { method: "POST", json: { action: "next" } });
      showToast(
        res.signaled
          ? "Picked next photo & refreshing e-ink panel (~30s)…"
          : "Picked next photo (inky-photoframe service not signaled)"
      );
      refreshPreviewImage();
      await loadGallery();
    } catch (err) {
      showToast(err.message, true);
    } finally {
      btn.disabled = false;
    }
  });

  $("#btn-unpin-photo").addEventListener("click", async () => {
    try {
      await api("/api/frame/action", { method: "POST", json: { action: "unpin" } });
      showToast("Unpinned photo; rotation resumed.");
      await loadGallery();
    } catch (err) {
      showToast(err.message, true);
    }
  });

  $("#btn-refresh-gallery").addEventListener("click", () => {
    loadGallery();
    refreshPreviewImage();
  });

  $("#btn-close-meta").addEventListener("click", () => $("#meta-modal").classList.add("hidden"));
  $("#meta-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    await api("/api/photos/metadata", {
      method: "POST",
      json: {
        name: $("#meta-photo-name").value,
        location: $("#meta-location").value,
        date: $("#meta-date").value,
      },
    });
    $("#meta-modal").classList.add("hidden");
    showToast("Saved photo caption!");
    loadGallery();
    refreshPreviewImage($("#meta-photo-name").value);
  });

  // -------------------- Tab 2: UK VPN --------------------
  async function loadVpnStatus(refreshExit = false) {
    const s = await api(`/api/vpn/status${refreshExit ? "?refresh=1" : ""}`);
    const pill = $("#pill-vpn");
    const badge = $("#vpn-state-badge");

    if (s.connected) {
      const loc = s.exit_city ? `${s.exit_city}, ${s.exit_country}` : s.active_profile;
      pill.textContent = `VPN: ${s.active_profile} (${loc})`;
      pill.className = "pill pill-ok";
      badge.textContent = "CONNECTED";
      badge.className = "pill pill-ok";
    } else {
      pill.textContent = "VPN: DOWN";
      pill.className = "pill pill-bad";
      badge.textContent = "DISCONNECTED";
      badge.className = "pill pill-bad";
    }

    const hs = s.handshake_age_seconds != null ? `${s.handshake_age_seconds}s ago` : "—";
    const ap = s.hotspot || {};
    $("#vpn-kv").innerHTML = `
      <div class="kv-item"><span>Active Profile</span><strong>${esc(s.active_profile || "None")}</strong></div>
      <div class="kv-item"><span>Public Exit IP</span><strong>${esc(s.exit_ip || "—")} ${esc(s.exit_country ? `(${s.exit_city}, ${s.exit_country})` : "")}</strong></div>
      <div class="kv-item"><span>Tunnel (${esc(s.tunnel_interface || "wg-vpn")})</span><strong>${esc(s.tunnel_ip || "down")} • HS ${esc(hs)}</strong></div>
      <div class="kv-item"><span>Tunnel Traffic</span><strong>↓ ${esc(s.rx_human)} / ↑ ${esc(s.tx_human)}</strong></div>
      <div class="kv-item"><span>5GHz Hotspot (${esc(ap.ssid || "-")})</span><strong>${ap.clients || 0} client(s) ${ap.phy_rate ? `• ${esc(ap.phy_rate)}` : ""}</strong></div>
      <div class="kv-item"><span>500M LAN Gateway</span><strong>${esc(s.lan_gateway_ip || s.wan_ip || "—")}</strong></div>
    `;

    $("#bench-wan-iface").textContent = s.wan_interface || "eth0";
    $("#bench-vpn-iface").textContent = s.tunnel_interface || "wg-vpn";
    renderBenchmark(s.benchmark || {});

    const pList = $("#vpn-profiles-list");
    if (!s.profiles.length) {
      pList.innerHTML = `<p class="muted">No profiles imported yet. Upload Surfshark .conf / .ovpn files above.</p>`;
    } else {
      pList.innerHTML = s.profiles
        .map(
          (p) => `
        <div class="profile-card ${p.active ? "active" : ""}">
          <div>
            <strong>${esc(p.name)}</strong>
            <div class="muted small">${esc(p.protocol)} • ${esc(p.country || "?")}</div>
          </div>
          <button class="btn ${p.active ? "btn-ghost" : "btn-primary"} btn-sm" data-switch-prof="${esc(p.name)}">
            ${p.active ? "Active" : "Connect"}
          </button>
        </div>`
        )
        .join("");
      pList.querySelectorAll("button[data-switch-prof]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const prof = btn.dataset.switchProf;
          btn.disabled = true;
          btn.textContent = "Switching…";
          try {
            await api("/api/vpn/switch", { method: "POST", json: { profile: prof } });
            showToast(`Switched VPN to ${prof}!`);
            await loadVpnStatus(true);
          } catch (err) {
            showToast(err.message, true);
            await loadVpnStatus();
          }
        });
      });
    }
  }

  function renderBenchmark(b) {
    const btn = $("#btn-run-bench");
    if (b.running) {
      btn.disabled = true;
      btn.textContent = "Testing 25 MB…";
      $("#bench-meta").textContent = "Downloading test payload over WAN and Surfshark tunnel…";
      if (!benchPollTimer) {
        benchPollTimer = setInterval(async () => {
          const s = await api("/api/vpn/status");
          renderBenchmark(s.benchmark || {});
          if (!(s.benchmark || {}).running) {
            clearInterval(benchPollTimer);
            benchPollTimer = null;
          }
        }, 2000);
      }
      return;
    }
    btn.disabled = false;
    btn.textContent = "Run Speed Test";
    $("#bench-wan-val").textContent = b.wan_mbps != null ? `${b.wan_mbps} Mbps` : "—";
    $("#bench-vpn-val").textContent =
      b.vpn_mbps != null ? `${b.vpn_mbps} Mbps ${b.exit_location ? `[${b.exit_location}]` : ""}` : "—";
    $("#bench-meta").textContent = b.error ? `Error: ${b.error}` : b.updated_at ? `Last tested: ${b.updated_at}` : "";
  }

  $$("button[data-vpn-action]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const act = btn.dataset.vpnAction;
      btn.disabled = true;
      try {
        await api("/api/vpn/action", { method: "POST", json: { action: act } });
        showToast(`VPN ${act} completed.`);
        await loadVpnStatus(true);
      } catch (err) {
        showToast(err.message, true);
      } finally {
        btn.disabled = false;
      }
    });
  });

  $("#btn-run-bench").addEventListener("click", async () => {
    try {
      const b = await api("/api/vpn/benchmark", { method: "POST", json: { megabytes: 25 } });
      renderBenchmark(b);
    } catch (err) {
      showToast(err.message, true);
    }
  });

  $("#vpn-upload-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const files = $("#vpn-file-input").files;
    if (!files || !files.length) return;
    const fd = new FormData();
    for (const f of files) fd.append("profiles", f);
    try {
      await api("/api/vpn/upload", { method: "POST", body: fd });
      showToast(`Imported ${files.length} VPN profile(s)!`);
      $("#vpn-upload-form").reset();
      loadVpnStatus();
    } catch (err) {
      showToast(err.message, true);
    }
  });

  // -------------------- Tab 3: Watchdog & Uptime --------------------
  $("#mon-type").addEventListener("change", () => {
    $("#mon-port-wrap").classList.toggle("hidden", $("#mon-type").value !== "tcp");
  });

  async function loadMonitor() {
    const snap = await api("/api/monitor/status");
    const wanTarget = (snap.targets || []).find((t) => t.id === "isp-wan");
    if (wanTarget && wanTarget.up !== null) {
      const wanPill = $("#pill-wan");
      wanPill.textContent = wanTarget.up
        ? `ISP: ${wanTarget.latency_ms != null ? `${wanTarget.latency_ms} ms` : "UP"}`
        : "ISP: DOWN";
      wanPill.className = wanTarget.up ? "pill pill-ok" : "pill pill-bad";
    }

    const listEl = $("#monitor-list");
    listEl.innerHTML = (snap.targets || [])
      .map((t) => {
        const badgeCls = t.up === true ? "pill-ok" : t.up === false ? "pill-bad" : "pill-neutral";
        const badgeTxt = t.up === true ? `${t.latency_ms ?? 0} ms` : t.up === false ? "DOWN" : "PENDING";
        const hist = t.history || [];
        const padded = Array(Math.max(0, 40 - hist.length))
          .fill(null)
          .concat(hist.slice(-40));
        const bars = padded
          .map((h) => {
            if (!h) return `<span class="hb-bar"></span>`;
            const cls = h.up ? "up" : "down";
            const tip = `${h.t} — ${h.up ? `${h.ms} ms` : "DOWN"}`;
            return `<span class="hb-bar ${cls}" title="${esc(tip)}"></span>`;
          })
          .join("");
        const delBtn = t.builtin
          ? ""
          : `<button class="btn btn-danger btn-sm" data-del-mon="${esc(t.id)}">Remove</button>`;
        return `
        <div class="monitor-row">
          <div class="monitor-top">
            <div>
              <strong>${esc(t.name)}</strong>
              <span class="muted small">(${esc(t.type)}: ${esc(t.host)}${t.port ? `:${t.port}` : ""})</span>
            </div>
            <div class="btn-row">
              <span class="muted small">${t.uptime_pct != null ? `${t.uptime_pct}% uptime` : ""}</span>
              <span class="pill ${badgeCls}">${esc(badgeTxt)}</span>
              ${delBtn}
            </div>
          </div>
          <div class="heartbeat-bars">${bars}</div>
        </div>`;
      })
      .join("");

    listEl.querySelectorAll("button[data-del-mon]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        await api("/api/monitor/targets/delete", { method: "POST", json: { id: btn.dataset.delMon } });
        loadMonitor();
      });
    });

    const incEl = $("#incidents-list");
    if (snap.incidents && snap.incidents.length) {
      incEl.innerHTML = snap.incidents
        .map((inc) => `<div><strong>[${esc(inc.status.toUpperCase())}]</strong> ${esc(inc.summary)} <span class="muted">(${esc(inc.at)})</span></div>`)
        .join("");
    } else {
      incEl.textContent = "No outages recorded.";
    }

    const n = snap.notifications || {};
    if (n.ntfy) {
      $("#ntfy-enabled").checked = !!n.ntfy.enabled;
      $("#ntfy-server").value = n.ntfy.server || "https://ntfy.sh";
      $("#ntfy-topic").value = n.ntfy.topic || "";
    }
    if (n.telegram) {
      $("#tg-enabled").checked = !!n.telegram.enabled;
      $("#tg-chat").value = n.telegram.chat_id || "";
      $("#tg-token").placeholder = n.telegram.bot_token_masked
        ? `Saved (${n.telegram.bot_token_masked})`
        : "Bot Token";
    }
    if (n.pushover) {
      $("#po-enabled").checked = !!n.pushover.enabled;
      $("#po-user").value = n.pushover.user_key || "";
      $("#po-token").placeholder = n.pushover.app_token_masked ? "Saved (***)" : "App Token";
    }
    if (n.webhook) {
      $("#wh-enabled").checked = !!n.webhook.enabled;
      $("#wh-url").value = n.webhook.url || "";
    }
  }

  $("#btn-check-now").addEventListener("click", async () => {
    const btn = $("#btn-check-now");
    btn.disabled = true;
    btn.textContent = "Probing…";
    try {
      await api("/api/monitor/check", { method: "POST", json: {} });
      await loadMonitor();
      showToast("Completed health probes!");
    } catch (err) {
      showToast(err.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = "Probe All Now";
    }
  });

  $("#add-monitor-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/api/monitor/targets/add", {
        method: "POST",
        json: {
          name: $("#mon-name").value,
          type: $("#mon-type").value,
          host: $("#mon-host").value,
          port: parseInt($("#mon-port").value || "80", 10),
        },
      });
      $("#add-monitor-form").reset();
      $("#mon-port-wrap").classList.add("hidden");
      showToast("Added monitor target!");
      setTimeout(loadMonitor, 800);
    } catch (err) {
      showToast(err.message, true);
    }
  });

  $("#notif-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/api/monitor/notifications", {
        method: "POST",
        json: {
          ntfy: {
            enabled: $("#ntfy-enabled").checked,
            server: $("#ntfy-server").value,
            topic: $("#ntfy-topic").value,
          },
          telegram: {
            enabled: $("#tg-enabled").checked,
            bot_token: $("#tg-token").value,
            chat_id: $("#tg-chat").value,
          },
          pushover: {
            enabled: $("#po-enabled").checked,
            app_token: $("#po-token").value,
            user_key: $("#po-user").value,
          },
          webhook: {
            enabled: $("#wh-enabled").checked,
            url: $("#wh-url").value,
          },
        },
      });
      $("#tg-token").value = "";
      $("#po-token").value = "";
      showToast("Saved push notification settings!");
      loadMonitor();
    } catch (err) {
      showToast(err.message, true);
    }
  });

  $("#btn-test-alert").addEventListener("click", async () => {
    try {
      await api("/api/monitor/notifications/test", { method: "POST", json: {} });
      showToast("Test notification sent to your phone!");
    } catch (err) {
      showToast(err.message, true);
    }
  });

  // -------------------- Tab 4: Wake-on-LAN --------------------
  async function loadWol() {
    const data = await api("/api/wol/devices?ping=1");
    const el = $("#wol-list");
    if (!data.devices.length) {
      el.innerHTML = `<p class="muted">No saved Wake-on-LAN devices yet. Add one or scan LAN on the right.</p>`;
      return;
    }
    el.innerHTML = data.devices
      .map((d) => {
        const statusPill =
          d.awake === true
            ? `<span class="pill pill-ok">Awake</span>`
            : d.awake === false
            ? `<span class="pill pill-neutral">Asleep</span>`
            : "";
        return `
        <div class="wol-item">
          <div>
            <strong>${esc(d.name)}</strong> ${statusPill}
            <div class="muted small"><code>${esc(d.mac)}</code> ${d.ip ? `• ${esc(d.ip)}` : ""}</div>
          </div>
          <div class="btn-row">
            <button class="btn btn-primary btn-sm" data-wake-id="${esc(d.id)}" data-wake-name="${esc(d.name)}">⚡ Wake</button>
            <button class="btn btn-danger btn-sm" data-del-wol="${esc(d.id)}">✕</button>
          </div>
        </div>`;
      })
      .join("");

    el.querySelectorAll("button[data-wake-id]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          const res = await api("/api/wol/wake", { method: "POST", json: { id: btn.dataset.wakeId } });
          showToast(`Magic packet sent to ${btn.dataset.wakeName} (${res.woken})!`);
        } catch (err) {
          showToast(err.message, true);
        }
      });
    });

    el.querySelectorAll("button[data-del-wol]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        await api("/api/wol/delete", { method: "POST", json: { id: btn.dataset.delWol } });
        loadWol();
      });
    });
  }

  $("#wol-add-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/api/wol/add", {
        method: "POST",
        json: {
          name: $("#wol-name").value,
          mac: $("#wol-mac").value,
          ip: $("#wol-ip").value,
        },
      });
      $("#wol-add-form").reset();
      showToast("Saved Wake-on-LAN device!");
      loadWol();
    } catch (err) {
      showToast(err.message, true);
    }
  });

  $("#btn-refresh-wol").addEventListener("click", loadWol);
  $("#btn-scan-lan").addEventListener("click", async () => {
    const list = $("#lan-scan-list");
    list.textContent = "Scanning ARP neighbors…";
    try {
      const data = await api("/api/wol/scan");
      if (!data.neighbors.length) {
        list.textContent = "No ARP neighbors found yet.";
        return;
      }
      list.innerHTML = data.neighbors
        .map(
          (n) => `
        <div class="wol-item">
          <div><strong>${esc(n.ip)}</strong> — <code>${esc(n.mac)}</code></div>
          <button type="button" class="btn btn-ghost btn-sm" data-pick-ip="${esc(n.ip)}" data-pick-mac="${esc(n.mac)}">Use</button>
        </div>`
        )
        .join("");
      list.querySelectorAll("button[data-pick-mac]").forEach((btn) => {
        btn.addEventListener("click", () => {
          $("#wol-mac").value = btn.dataset.pickMac;
          $("#wol-ip").value = btn.dataset.pickIp;
          if (!$("#wol-name").value) $("#wol-name").value = `Device ${btn.dataset.pickIp}`;
        });
      });
    } catch (err) {
      list.textContent = err.message;
    }
  });

  // -------------------- Tab 3: Network & Devices --------------------
  let lastNetworkData = null;

  function formatDuration(sec) {
    if (sec == null || sec < 0) return "";
    if (sec < 60) return `${sec}s`;
    const mins = Math.floor(sec / 60);
    if (mins < 60) return `${mins}m`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ${mins % 60}m`;
    return `${Math.floor(hrs / 24)}d ${hrs % 24}h`;
  }

  function renderDeviceRow(d) {
    const typePill = `<span class="pill pill-neutral">${esc(d.category_label)}</span>`;
    const newPill = d.is_new ? `<span class="pill pill-warn" title="New device not yet acknowledged">NEW</span>` : "";
    const privPill = d.is_private_mac ? `<span class="pill pill-neutral" title="Phone/tablet using randomized MAC">Private MAC</span>` : "";
    const sigPart = d.signal_dbm != null ? `<span>📶 ${d.signal_dbm} dBm</span>` : "";
    const connPart = d.connected_seconds != null ? `<span>⏱ ${formatDuration(d.connected_seconds)}</span>` : "";

    let bwHtml = "";
    if (d.bandwidth_available) {
      bwHtml = `
        <div class="device-bw">
          <div class="bw-rates">
            <span class="bw-down" title="Download speed">↓ ${esc(d.download_rate_human)}</span>
            <span class="bw-up" title="Upload speed">↑ ${esc(d.upload_rate_human)}</span>
          </div>
          <div class="bw-totals">Total: ↓ ${esc(d.download_human)} • ↑ ${esc(d.upload_human)}</div>
        </div>`;
    } else {
      bwHtml = `
        <div class="device-bw">
          <span class="bw-na" title="${esc(d.bandwidth_note || "Direct via main router")}">Direct via main router</span>
          <span class="bw-totals">Bandwidth N/A from Pi</span>
        </div>`;
    }

    const canWatch = Boolean(d.ip) && !d.is_self;
    const canWol = Boolean(d.mac) && !d.is_self && !d.is_gateway;
    const rowClasses = ["device-row", d.network === "hotspot" ? "is-hotspot" : "", d.is_new ? "is-new" : ""]
      .filter(Boolean)
      .join(" ");

    return `
      <div class="${rowClasses}">
        <div class="device-main">
          <div class="device-icon">${esc(d.icon || "📶")}</div>
          <div class="device-info">
            <div class="device-name-line">
              <span class="device-name">${esc(d.name)}</span>
              ${newPill}
              ${typePill}
              ${privPill}
            </div>
            <div class="device-meta">
              ${d.ip ? `<code>${esc(d.ip)}</code>` : ""}
              <code>${esc(d.mac)}</code>
              <span>${esc(d.vendor)}</span>
              ${sigPart}
              ${connPart}
            </div>
          </div>
        </div>
        ${bwHtml}
        <div class="device-actions">
          ${d.is_new ? `<button type="button" class="btn btn-primary btn-sm" data-dev-trust="${esc(d.mac)}">✓ Trust</button>` : ""}
          <button type="button" class="btn btn-ghost btn-sm"
            data-dev-edit="${esc(d.mac)}"
            data-dev-name="${esc(d.custom_name || "")}"
            data-dev-auto="${esc(d.name)}"
            data-dev-cat="${esc(d.custom_name ? d.category : "")}"
            data-dev-ip="${esc(d.ip || "")}">Rename</button>
          ${canWatch ? `<button type="button" class="btn btn-ghost btn-sm" data-dev-watch-ip="${esc(d.ip)}" data-dev-watch-name="${esc(d.name)}">+ Watch</button>` : ""}
          ${canWol ? `<button type="button" class="btn btn-ghost btn-sm" data-dev-wol-mac="${esc(d.mac)}" data-dev-wol-ip="${esc(d.ip || "")}" data-dev-wol-name="${esc(d.name)}">+ WoL</button>` : ""}
        </div>
      </div>`;
  }

  function renderNetworkView() {
    if (!lastNetworkData) return;
    const data = lastNetworkData;
    const sum = data.summary || {};
    $("#net-stat-total").textContent = String(sum.total_devices ?? 0);
    $("#net-stat-split").textContent = `${sum.hotspot_devices ?? 0} on VPN Hotspot • ${sum.lan_devices ?? 0} on Home LAN`;

    const vpnSt = sum.vpn_stats;
    if (vpnSt && vpnSt.available) {
      $("#net-stat-vpn-rate").textContent = `↓ ${vpnSt.rx_rate_human}  ↑ ${vpnSt.tx_rate_human}`;
      $("#net-stat-vpn-total").textContent = `${vpnSt.interface}: ↓ ${vpnSt.rx_human} • ↑ ${vpnSt.tx_human}`;
    } else {
      $("#net-stat-vpn-rate").textContent = "Disconnected";
      $("#net-stat-vpn-total").textContent = "VPN tunnel inactive";
    }

    const wanSt = sum.wan_stats;
    if (wanSt && wanSt.available) {
      $("#net-stat-wan-rate").textContent = `↓ ${wanSt.rx_rate_human}  ↑ ${wanSt.tx_rate_human}`;
      $("#net-stat-wan-total").textContent = `${wanSt.interface}: ↓ ${wanSt.rx_human} • ↑ ${wanSt.tx_human}`;
    } else {
      $("#net-stat-wan-rate").textContent = "—";
      $("#net-stat-wan-total").textContent = "Interface stats unavailable";
    }

    const q = ($("#net-search").value || "").trim().toLowerCase();
    const scope = $("#net-filter-scope").value || "all";

    const matches = (d) => {
      if (!q) return true;
      const hay = `${d.name} ${d.hostname} ${d.ip} ${d.mac} ${d.vendor} ${d.category_label} ${d.category}`.toLowerCase();
      return hay.includes(q);
    };

    const hotspotDevs = (data.devices || []).filter((d) => d.network === "hotspot" && matches(d));
    const lanDevs = (data.devices || []).filter((d) => d.network !== "hotspot" && matches(d));

    $("#net-hotspot-count").textContent = String(hotspotDevs.length);
    $("#net-lan-count").textContent = String(lanDevs.length);

    $("#net-hotspot-section").classList.toggle("hidden", scope === "lan");
    $("#net-lan-section").classList.toggle("hidden", scope === "hotspot");

    const hsList = $("#net-hotspot-list");
    hsList.innerHTML = hotspotDevs.length
      ? hotspotDevs.map(renderDeviceRow).join("")
      : `<p class="muted small">No devices currently connected to <strong>${esc(sum.hotspot_ssid || "UK-Frame-VPN")}</strong>.</p>`;

    const lanList = $("#net-lan-list");
    lanList.innerHTML = lanDevs.length
      ? lanDevs.map(renderDeviceRow).join("")
      : `<p class="muted small">No matching LAN devices found. Click <strong>Scan Whole Network</strong> to sweep your subnet.</p>`;

    $("#net-scan-meta").textContent = `Last updated: ${data.scanned_at}${sum.mdns_available ? " • mDNS/Bonjour active" : ""}`;
    $("#net-alert-new").checked = sum.alert_new_devices !== false;
    const unknownCount = sum.unknown_devices || 0;
    const trustAllBtn = $("#btn-net-trust-all");
    trustAllBtn.classList.toggle("hidden", unknownCount === 0);
    trustAllBtn.textContent = `✓ Trust All New (${unknownCount})`;

    // Bind row action buttons
    $("#tab-network").querySelectorAll("button[data-dev-trust]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try {
          lastNetworkData = await api("/api/network/trust", { method: "POST", json: { mac: btn.dataset.devTrust } });
          renderNetworkView();
          showToast("Device marked as trusted.");
        } catch (err) {
          showToast(err.message, true);
        } finally {
          btn.disabled = false;
        }
      });
    });

    $("#tab-network").querySelectorAll("button[data-dev-edit]").forEach((btn) => {
      btn.addEventListener("click", () => {
        $("#dev-modal-mac").value = btn.dataset.devEdit;
        $("#dev-modal-name").value = btn.dataset.devName || "";
        $("#dev-modal-category").value = btn.dataset.devCat || "";
        $("#dev-modal-sub").textContent = `${btn.dataset.devAuto} • ${btn.dataset.devIp || "No IP"} • ${btn.dataset.devEdit}`;
        $("#device-modal").classList.remove("hidden");
      });
    });

    $("#tab-network").querySelectorAll("button[data-dev-watch-ip]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try {
          await api("/api/monitor/targets/add", {
            method: "POST",
            json: { name: btn.dataset.devWatchName, type: "ping", host: btn.dataset.devWatchIp },
          });
          showToast(`Added ${btn.dataset.devWatchName} to Watchdog!`);
        } catch (err) {
          showToast(err.message, true);
        } finally {
          btn.disabled = false;
        }
      });
    });

    $("#tab-network").querySelectorAll("button[data-dev-wol-mac]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try {
          await api("/api/wol/add", {
            method: "POST",
            json: { name: btn.dataset.devWolName, mac: btn.dataset.devWolMac, ip: btn.dataset.devWolIp },
          });
          showToast(`Saved ${btn.dataset.devWolName} to Wake-on-LAN!`);
        } catch (err) {
          showToast(err.message, true);
        } finally {
          btn.disabled = false;
        }
      });
    });
  }

  async function loadAdGuardStatus() {
    try {
      const a = await api("/api/adguard/status");
      const valEl = $("#net-stat-agh");
      const subEl = $("#net-stat-agh-sub");
      if (!valEl || !subEl) return;
      const host = window.location.hostname || "raspberrypi.local";
      const url = `http://${host}:${a.port || 3000}`;
      if (a.running) {
        if (a.dns_queries != null) {
          valEl.textContent = `${a.blocked_pct ?? 0}% Blocked`;
          subEl.innerHTML = `${a.blocked_filtering} / ${a.dns_queries} queries • <a href="${esc(url)}" target="_blank" rel="noopener" style="color:var(--primary)">Open :${a.port} ↗</a>`;
        } else {
          valEl.textContent = a.dns_listening ? "Active (Port 53)" : "Setup Wizard";
          subEl.innerHTML = `<a href="${esc(url)}" target="_blank" rel="noopener" style="color:var(--primary)">Open AdGuard Home (:${a.port}) ↗</a>`;
        }
      } else {
        valEl.textContent = "Not Installed";
        subEl.textContent = "Run ./adguard/install-adguard.sh on Pi";
      }
    } catch (_err) {
      // Non-critical widget
    }
  }

  async function loadNetwork(sweep = false) {
    const btn = $("#btn-net-sweep");
    if (sweep && btn) {
      btn.disabled = true;
      btn.textContent = "Scanning Subnet…";
    }
    try {
      const [netData] = await Promise.all([
        api(sweep ? "/api/network/devices?sweep=1" : "/api/network/devices"),
        loadAdGuardStatus(),
      ]);
      lastNetworkData = netData;
      renderNetworkView();
      if (sweep) {
        showToast(`Found ${lastNetworkData.summary.total_devices} device(s) on your network!`);
      }
    } catch (err) {
      if (sweep) showToast(err.message, true);
    } finally {
      if (sweep && btn) {
        btn.disabled = false;
        btn.textContent = "🔍 Scan Whole Network";
      }
    }
  }

  $("#btn-net-refresh").addEventListener("click", () => loadNetwork(false));
  $("#btn-net-sweep").addEventListener("click", () => loadNetwork(true));
  $("#btn-net-trust-all").addEventListener("click", async () => {
    try {
      lastNetworkData = await api("/api/network/trust", { method: "POST", json: { all: true } });
      renderNetworkView();
      showToast("All current devices marked as trusted.");
    } catch (err) {
      showToast(err.message, true);
    }
  });
  $("#net-alert-new").addEventListener("change", async () => {
    try {
      lastNetworkData = await api("/api/network/settings", {
        method: "POST",
        json: { alert_new_devices: $("#net-alert-new").checked },
      });
      renderNetworkView();
      showToast(
        $("#net-alert-new").checked ? "New-device push alerts enabled." : "New-device push alerts paused."
      );
    } catch (err) {
      showToast(err.message, true);
    }
  });
  $("#net-search").addEventListener("input", renderNetworkView);
  $("#net-filter-scope").addEventListener("change", renderNetworkView);

  // -------------------- Pi System Health Bar --------------------
  function setHealthItem(sel, text, level = "") {
    const el = $(sel);
    if (!el) return;
    el.textContent = text;
    el.className = level ? `health-item ${level}` : "health-item";
  }

  async function loadSystemHealth() {
    const h = await api("/api/system/health");
    if (h.cpu_temp_c != null) {
      const lvl = h.cpu_temp_c >= 80 ? "crit" : h.cpu_temp_c >= 70 ? "warn" : "ok";
      setHealthItem("#health-temp", `🌡 CPU: ${h.cpu_temp_c.toFixed(1)}°C`, lvl);
    } else {
      setHealthItem("#health-temp", "🌡 CPU: N/A");
    }

    const mem = h.memory || {};
    if (mem.available && mem.pct != null) {
      const lvl = mem.pct >= 90 ? "crit" : mem.pct >= 80 ? "warn" : "ok";
      const totalStr = mem.total_mb >= 1024 ? `${(mem.total_mb / 1024).toFixed(1)}G` : `${mem.total_mb}M`;
      setHealthItem("#health-ram", `🧠 RAM: ${mem.used_mb}M/${totalStr} (${Math.round(mem.pct)}%)`, lvl);
    } else {
      setHealthItem("#health-ram", "🧠 RAM: N/A");
    }

    const disk = h.disk || {};
    if (disk.available && disk.pct != null) {
      const lvl = disk.pct >= 90 ? "crit" : disk.pct >= 80 ? "warn" : "ok";
      setHealthItem("#health-disk", `💾 SD: ${disk.used_gb}/${disk.total_gb} GB (${Math.round(disk.pct)}%)`, lvl);
    } else {
      setHealthItem("#health-disk", "💾 SD: N/A");
    }

    const thr = h.throttled || {};
    if (thr.available) {
      setHealthItem("#health-power", `⚡ Power: ${thr.summary}`, thr.status || "ok");
    } else {
      setHealthItem("#health-power", "⚡ Power: OK", "ok");
    }

    const loadPart = h.load_1m != null ? ` • Load ${h.load_1m.toFixed(2)}` : "";
    setHealthItem("#health-uptime", `⏱ Up: ${h.uptime_human || "—"}${loadPart}`);
  }

  $("#btn-close-dev-modal").addEventListener("click", () => $("#device-modal").classList.add("hidden"));
  $("#device-alias-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      lastNetworkData = await api("/api/network/alias", {
        method: "POST",
        json: {
          mac: $("#dev-modal-mac").value,
          name: $("#dev-modal-name").value.trim(),
          category: $("#dev-modal-category").value,
        },
      });
      $("#device-modal").classList.add("hidden");
      renderNetworkView();
      showToast("Saved device label!");
    } catch (err) {
      showToast(err.message, true);
    }
  });

  async function refreshAll() {
    try {
      await Promise.all([loadGallery(), loadVpnStatus(), loadMonitor(), loadSystemHealth()]);
    } catch (_err) {
      // Handled by api()
    }
  }

  refreshAll();
  setInterval(() => {
    loadVpnStatus().catch(() => {});
    loadMonitor().catch(() => {});
    loadSystemHealth().catch(() => {});
    if (activeTab === "network") {
      loadNetwork(false).catch(() => {});
    }
  }, 10000);
})();
