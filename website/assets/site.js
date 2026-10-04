import { detectInstallPlatform, installCommand } from "./install-platform.js";

document.documentElement.classList.remove("no-js");
document.documentElement.classList.add("js");

const platformSelect = document.querySelector("#install-platform");
const installCommandNode = document.querySelector("#hero-install-command");
if (platformSelect && installCommandNode) {
  const detectedPlatform = detectInstallPlatform({
    userAgentDataPlatform: navigator.userAgentData?.platform,
    platform: navigator.platform,
    userAgent: navigator.userAgent,
  });
  platformSelect.value = detectedPlatform;
  installCommandNode.textContent = installCommand(detectedPlatform);
  platformSelect.addEventListener("change", function () {
    installCommandNode.textContent = installCommand(platformSelect.value);
  });
}

const menuButton = document.querySelector(".menu-toggle");
const nav = document.querySelector("#primary-nav");
if (menuButton && nav) {
  menuButton.addEventListener("click", function () {
    const open = menuButton.getAttribute("aria-expanded") !== "true";
    menuButton.setAttribute("aria-expanded", String(open));
    nav.classList.toggle("is-open", open);
  });
  nav.addEventListener("click", function (event) {
    if (event.target.closest("a")) {
      menuButton.setAttribute("aria-expanded", "false");
      nav.classList.remove("is-open");
    }
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      menuButton.setAttribute("aria-expanded", "false");
      nav.classList.remove("is-open");
      menuButton.focus();
    }
  });
}

document.querySelectorAll(".copy-button").forEach(function (button) {
  button.addEventListener("click", async function () {
    const target = document.getElementById(button.dataset.copy);
    if (!target) return;
    const status = button.dataset.copyStatus
      ? document.getElementById(button.dataset.copyStatus)
      : document.querySelector(".copy-status");
    try {
      await navigator.clipboard.writeText(target.textContent);
      const codeLabel = button.closest(".code-card")?.querySelector(".code-label span")?.textContent;
      if (status) status.textContent = button.dataset.copySuccess || (codeLabel ? codeLabel + " command copied." : "Command copied.");
      const oldText = button.textContent;
      button.textContent = button.dataset.copyDone || (oldText === "复制" ? "已复制" : "Copied");
      window.setTimeout(function () { button.textContent = oldText; }, 1500);
    } catch (_) {
      if (status) status.textContent = button.dataset.copyFallback || "Select and copy the command from the code block.";
    }
  });
});

if ("IntersectionObserver" in window && !window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
  const sections = document.querySelectorAll(".section");
  const observer = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (entry.isIntersecting) {
        entry.target.classList.add("in-view");
        observer.unobserve(entry.target);
      }
    });
  }, { threshold: 0.08 });
  sections.forEach(function (section) { observer.observe(section); });
}

if (window.location.hostname === "www.kyrozen.chat") {
  window.location.replace("https://kyrozen.chat" + window.location.pathname + window.location.search + window.location.hash);
}
