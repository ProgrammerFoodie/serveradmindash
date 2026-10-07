const form = document.getElementById("f");
const msg = document.getElementById("msg");
const NOTES = {
  idle: "You were signed out because of inactivity. Sign in again to continue.",
  expired: "Your session has ended. Sign in again to continue.",
};
document.getElementById("note").textContent = NOTES[new URLSearchParams(location.search).get("why")] || "";
form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const button = form.querySelector("button");
  button.disabled = true;
  msg.textContent = "";
  try {
    const res = await fetch("/login", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: form.username.value, password: form.password.value }),
    });
    if (res.ok) { location.href = "/"; return; }
    const body = await res.json().catch(() => ({}));
    const wait = res.headers.get("Retry-After");
    msg.textContent = (body.error || "Sign-in failed") + (res.status === 429 && wait ? ` (${Math.ceil(wait / 60)} min)` : "");
    form.password.value = "";
  } catch {
    msg.textContent = "Cannot reach the server";
  } finally {
    button.disabled = false;
  }
});
