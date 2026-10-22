/* Sign-in form with Firebase Google SSO and local credentials fallback. */

import { initializeApp } from 'https://www.gstatic.com/firebasejs/10.12.0/firebase-app.js';
import { getAuth, GoogleAuthProvider, signInWithPopup } from 'https://www.gstatic.com/firebasejs/10.12.0/firebase-auth.js';

const form = document.getElementById('login-form');
const usernameInput = document.getElementById('username');
const passwordInput = document.getElementById('password');
const errorBox = document.getElementById('login-error');
const submit = form.querySelector('button[type="submit"]');
const googleBtn = document.getElementById('google-signin-btn');
const googleBtnLabel = document.getElementById('google-btn-label');

const SUBMIT_LABEL = 'Sign in';
const GOOGLE_BTN_LABEL = 'Continue with Google';

function showError(message) {
  errorBox.textContent = message;
  errorBox.hidden = false;
}

function clearError() {
  errorBox.textContent = '';
  errorBox.hidden = true;
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

async function signInGoogle(idToken) {
  const response = await fetch('/api/auth/google', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id_token: idToken }),
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch (error) {
    payload = null;
  }
  if (!response.ok) {
    throw new Error((payload && payload.error) || 'Google sign-in failed. Try again.');
  }
  return payload;
}

// Fallback Firebase configuration
const FALLBACK_FIREBASE_CONFIG = {
  apiKey: 'AIzaSyBlhlDv10wzSoOPC5f2PCRs-C8SzQuqWDk',
  authDomain: 'projecttracker-17e6d.firebaseapp.com',
  projectId: 'projecttracker-17e6d',
  storageBucket: 'projecttracker-17e6d.firebasestorage.app',
  messagingSenderId: '615565232205',
  appId: '1:615565232205:web:0ae8e33669a5761dfe7404',
};

let firebaseAuth = null;
let googleProvider = null;

async function initFirebase() {
  let cfg = FALLBACK_FIREBASE_CONFIG;
  try {
    const res = await fetch('/api/auth/config');
    if (res.ok) {
      const data = await res.json();
      if (data && data.firebase && data.firebase.apiKey) {
        cfg = data.firebase;
      }
    }
  } catch (err) {
    // Keep fallback config
  }

  try {
    const app = initializeApp(cfg);
    firebaseAuth = getAuth(app);
    googleProvider = new GoogleAuthProvider();
    googleProvider.setCustomParameters({
      prompt: 'select_account',
    });
  } catch (err) {
    console.error('Failed to initialize Firebase Auth:', err);
    if (googleBtn) {
      googleBtn.disabled = true;
      googleBtn.title = 'Firebase authentication could not be loaded.';
    }
  }
}

initFirebase();

if (googleBtn) {
  googleBtn.addEventListener('click', async () => {
    clearError();
    if (!firebaseAuth || !googleProvider) {
      showError('Authentication service is initializing. Please try again.');
      return;
    }

    googleBtn.disabled = true;
    if (googleBtnLabel) googleBtnLabel.textContent = 'Connecting to Google\u2026';

    try {
      const result = await signInWithPopup(firebaseAuth, googleProvider);
      const user = result.user;
      if (!user) {
        throw new Error('No user returned from Google.');
      }
      if (googleBtnLabel) googleBtnLabel.textContent = 'Verifying with Tracker\u2026';
      const idToken = await user.getIdToken();
      await signInGoogle(idToken);
      window.location.replace(nextUrl());
      return;
    } catch (error) {
      if (error && (error.code === 'auth/popup-closed-by-user' || error.code === 'auth/cancelled-popup-request')) {
        // User closed the popup, do not show an aggressive error
      } else {
        showError(error.message || 'Google sign-in failed.');
      }
    } finally {
      googleBtn.disabled = false;
      if (googleBtnLabel) googleBtnLabel.textContent = GOOGLE_BTN_LABEL;
    }
  });
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  clearError();

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
