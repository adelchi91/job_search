// Admin "Claude account" panel: drives `claude auth login` on the server through
// /admin/claude/*. Local mode opens the CLI's own localhost-callback URL in a new tab;
// server mode shows the manual sign-in link and a field to paste the code back.
(function () {
  const root = document.getElementById("claude");
  if (!root) return;
  const local = root.dataset.local === "1";
  const $ = (k) => root.querySelector(`[data-k="${k}"]`);
  const btn = (a) => root.querySelector(`[data-act="${a}"]`);
  let timer = null;
  let tab = null;
  let opened = false;

  async function call(path, body) {
    const res = await fetch(path, body ? { method: "POST", body } : {});
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || res.statusText);
    return data;
  }

  function renderStatus(s) {
    $("label").textContent = s.label;
    const line = root.querySelector(".claude-status");
    line.classList.toggle("ok", s.logged_in);
    line.classList.toggle("warn", s.expired);
    const bits = [s.org, s.version && `Claude Code ${s.version}`].filter(Boolean);
    $("meta").textContent = bits.join(" · ");
    btn("login").textContent = s.logged_in || s.expired ? "Re-authenticate" : "Sign in";
    btn("login").disabled = !s.installed;
    btn("logout").hidden = !s.logged_in;
  }

  function openTab(url) {
    if (opened || !url) return;
    opened = true;
    if (tab && !tab.closed) tab.location = url;
    else window.open(url, "_blank", "noopener");
  }

  function renderLogin(l) {
    const running = !!(l && l.running);
    btn("cancel").hidden = !running;
    btn("login").hidden = running;
    $("log").textContent = l && l.log.length ? l.log.join("\n") : "(no sign-in yet)";
    const showManual = running && !!l.manual_url && (!local || !l.local_url);
    $("manual").hidden = !showManual;
    if (showManual) $("manual-link").href = l.manual_url;
    $("hint").hidden = !(running && local && l.local_url);
    if (running && local && l.local_url) openTab(l.local_url);
    if (!running && tab && !opened) tab.close();
  }

  function render(data) {
    renderStatus(data.status);
    renderLogin(data.login);
    const running = !!(data.login && data.login.running);
    clearTimeout(timer);
    timer = running ? setTimeout(poll, 2000) : null;
    return running;
  }

  async function poll(refresh) {
    try {
      const wasRunning = !!timer;
      const data = await call("/admin/claude/status" + (refresh ? "?refresh=1" : ""));
      if (!render(data) && wasRunning && !refresh) poll(true);
    } catch (e) {
      $("label").textContent = "Could not read the Claude status: " + e.message;
    }
  }

  function act(name, body) {
    return call(`/admin/claude/${name}`, body || new FormData()).then(render, (e) => {
      $("log").textContent = e.message;
      root.querySelector("details").open = true;
    });
  }

  btn("login").addEventListener("click", () => {
    opened = false;
    // Open the tab inside the click so pop-up blockers allow it; we point it at the URL later.
    tab = local ? window.open("", "_blank") : null;
    act("login");
  });
  btn("cancel").addEventListener("click", () => act("cancel"));
  btn("logout").addEventListener("click", () => {
    if (confirm("Sign this server out of your Claude account?")) act("logout");
  });
  btn("code").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const form = new FormData(ev.target);
    ev.target.reset();
    act("code", form);
  });
  poll(false);
})();
