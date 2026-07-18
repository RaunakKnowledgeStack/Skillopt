document.addEventListener("DOMContentLoaded", () => {
  const mobileNav = document.getElementById("mobileNav");
  const drawer = document.getElementById("accountDrawer");
  const backdrop = document.getElementById("drawerBackdrop");
  const flashes = document.querySelectorAll(".flash");

  window.toggleMenu = function toggleMenu() {
    if (!mobileNav) return;
    mobileNav.classList.toggle("open");
  };

  window.toggleDrawer = function toggleDrawer(open) {
    if (!drawer || !backdrop) return;
    drawer.classList.toggle("open", open);
    backdrop.classList.toggle("open", open);
    document.body.classList.toggle("drawer-open", open);
  };

  if (flashes.length) {
    setTimeout(() => {
      flashes.forEach((el) => {
        el.style.opacity = "0";
        el.style.transform = "translateY(-4px)";
      });
    }, 3500);
  }

  const assistantChat = document.getElementById("assistantChat");
  const assistantForm = document.getElementById("assistantForm");
  const assistantInput = document.getElementById("assistantInput");
  const presetButtons = document.querySelectorAll(".preset-btn");

  if (!assistantChat || !assistantForm || !assistantInput) return;

  const assistantUrl = assistantForm.dataset.assistantUrl;
  const userAvatar = assistantChat.dataset.userAvatar || "U";
  const history = [];

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function addBubble(role, text) {
    const wrapper = document.createElement("div");
    wrapper.className = `assistant-msg ${role}`;
    wrapper.innerHTML = `
      <div class="avatar avatar-sm">${role === "bot" ? "A" : userAvatar}</div>
      <div class="msg-bubble">${escapeHtml(text)}</div>
    `;
    assistantChat.appendChild(wrapper);
    assistantChat.scrollTop = assistantChat.scrollHeight;
  }

  presetButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      assistantInput.value = btn.textContent.trim();
      assistantInput.focus();
    });
  });

  assistantForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = assistantInput.value.trim();
    if (!message) return;

    addBubble("mine", message);
    history.push({ role: "user", text: message });
    assistantInput.value = "";

    try {
      const response = await fetch(assistantUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message, history }),
      });
      const data = await response.json();
      const reply = data.reply || "I could not generate a response.";
      history.push({ role: "model", text: reply });
      addBubble("bot", reply);
    } catch (error) {
      addBubble("bot", "Something went wrong. Try again.");
    }
  });
});
