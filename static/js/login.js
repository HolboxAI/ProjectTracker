/* Sign-in form.

   This file, login.html and the two stylesheets are the only things served to
   someone without a session, so it imports nothing: a dependency here would
   have to be public too, and the smaller that list is the less there is to get
   wrong about what a stranger can read.

   Nothing on this page renders a value that came from a user or the server --
   the one dynamic string is the server's own error message, set with
   textContent. */

const form = document.getElementById('login-form');
const usernameInput = document.getElementById('username');
const passwordInput = document.getElementById('password');
const errorBox = document.getElementById('login-error');
const submit = form.querySelector('button[type="submit"]');

const SUBMIT_LABEL = 'Sign in';

function showError(message) {
  errorBox.textContent = message;
  errorBox.hidden = false;
}

/* Where to land after signing in: the page that was asked for, or the view the
   hash names. Only ever a path on this site -- an attacker-supplied `next`
   must not be able to bounce a fresh session off to another host. */
function nextUrl() {
  const requested = new URLSearchParams(window.location.search).get('next');
  if (requested && requested.startsWith('/') && !requested.startsWith('//')
      && !requested.startsWith('/login')) {
    return requested;
  }
  const { pathname, hash } = window.location;
  if (pathname === '/' || pathname === '') return '/' + hash;
  return '/';
}

async function signIn(username, password) {
  const response = await fetch('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch (error) {
    payload = null;
  }
  if (!response.ok) {
    throw new Error((payload && payload.error) || 'Could not sign in. Try again.');
  }
  return payload;
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  errorBox.hidden = true;

  const username = usernameInput.value.trim();
  const password = passwordInput.value;
  if (!username || !password) {
    showError('Enter your username and password.');
    (username ? passwordInput : usernameInput).focus();
    return;
  }

  submit.disabled = true;
  submit.textContent = 'Signing in\u2026';
  try {
    await signIn(username, password);
    window.location.replace(nextUrl());
    return;
  } catch (error) {
    showError(error.message);
    passwordInput.value = '';
    passwordInput.focus();
  }
  submit.disabled = false;
  submit.textContent = SUBMIT_LABEL;
});

usernameInput.focus();
