const form = document.getElementById('login');
const err = document.getElementById('login-error');
form.onsubmit = async (e) => {
  e.preventDefault();
  err.hidden = true;
  const r = await fetch('/api/login', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({passcode: document.getElementById('passcode').value})});
  if (r.ok) { location.href = '/'; return; }
  const body = await r.json().catch(() => ({}));
  err.textContent = body.error || 'Could not log in';
  err.hidden = false;
};
