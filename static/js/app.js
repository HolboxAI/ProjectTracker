/* App shell: state, routing, the rail and toolbar, and the actions every
   view calls. The URL hash is the single source of truth for which view is
   showing, which project is selected, the active filters and whether the
   issue drawer is open -- so every state is linkable and the back button
   works. */

import { api } from './api.js';
import * as board from './board.js';
import * as backlogView from './backlog.js';
import * as issuesView from './issues.js';
import * as reportsView from './reports.js';
import * as drawer from './drawer.js';
import { openCreateIssue, openCreateProject, openUsers, close as closeModal, isOpen as modalOpen } from './modal.js';
import { el, clear, svgIcon, button, avatar, assignableNames } from './ui.js';
import { readForUpload } from './attachments.js';

const VIEWS = ['board', 'backlog', 'issues', 'reports'];
const NAV = [
  { view: 'board', label: 'Board' },
  { view: 'backlog', label: 'Backlog' },
  { view: 'issues', label: 'All issues' },
  { view: 'reports', label: 'Reports' },
];

const dom = {
  app: document.getElementById('app'),
  rail: document.getElementById('rail'),
  topbar: document.getElementById('topbar'),
  view: document.getElementById('view'),
  scrim: document.getElementById('scrim'),
  drawer: document.getElementById('drawer'),
  modal: document.getElementById('modal-layer'),
  toasts: document.getElementById('toasts'),
};

const state = {
  ready: false,
  me: 'You',
  // The account behind `me`: null until bootstrap answers, and the only thing
  // that decides whether the rail offers account management.
  user: null,
  // Every active account, for the assignee pickers. Project members alone are
  // not enough -- see assignableNames() in ui.js.
  people: [],
  meta: { statuses: [], priorities: [], types: [] },
  projects: [],
  projectKey: null,
  project: null,
  sprints: [],
  issues: [],
  stats: null,
  filters: { q: '', assignee: 'all', priority: 'all', type: 'all' },
  route: { view: 'board', query: {} },
  openIssue: null,
};

/* ---- routing --------------------------------------------------------- */

function parseHash() {
  const raw = window.location.hash.replace(/^#\/?/, '');
  const [pathPart, queryPart] = raw.split('?');
  const segments = pathPart.split('/').filter(Boolean);
  const query = Object.fromEntries(new URLSearchParams(queryPart || ''));
  const view = VIEWS.includes(segments[0]) ? segments[0] : 'board';
  return { view, query };
}

function hashFor(route) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(route.query || {})) {
    if (value === undefined || value === null || value === '' || value === 'all') continue;
    params.set(key, value);
  }
  const text = params.toString();
  return `#/${route.view}${text ? `?${text}` : ''}`;
}

function navigate(patch = {}, { replace = false } = {}) {
  const next = {
    view: patch.view || state.route.view,
    query: { ...state.route.query, ...(patch.query || {}) },
  };
  const target = hashFor(next);
  const unchanged = window.location.hash === target;
  state.route = next;
  if (replace) {
    history.replaceState(null, '', target);
  } else if (!unchanged) {
    window.location.hash = target;
    return;
  }
  handleRoute();
}

/* ---- data ------------------------------------------------------------ */

async function ensureBootstrap() {
  if (state.ready) return;
  const boot = await api.bootstrap();
  state.meta = boot.meta;
  state.me = boot.me;
  state.projects = boot.projects;
  state.user = boot.user || null;
  state.people = boot.people || [];
  // `me` is the display name the views use as a default assignee and comment
  // author; `user` is the account it belongs to.
  state.me = boot.me || (state.user ? state.user.name : 'You');
  state.ready = true;
  buildRail();
  buildTopbar();
}

async function refresh({ withStats } = {}) {
  await ensureBootstrap();

  const key = state.route.query.project
    || state.projectKey
    || (state.projects[0] && state.projects[0].key);

  if (!key) {
    state.project = null;
    state.issues = [];
    state.sprints = [];
    render();
    return;
  }

  const params = {
    project: key,
    q: state.filters.q,
    assignee: state.filters.assignee,
    priority: state.filters.priority,
    type: state.filters.type,
  };

  const [project, issueList, sprintList] = await Promise.all([
    api.project(key),
    api.issues(params),
    api.sprints(key),
  ]);

  state.projectKey = project.key;
  state.project = project;
  state.issues = issueList.issues;
  state.sprints = sprintList.sprints;

  const wantStats = withStats !== undefined ? withStats : state.route.view === 'reports';
  if (wantStats) state.stats = await api.stats(key);

  const openId = state.route.query.issue;
  if (openId) {
    try {
      state.openIssue = await api.issue(openId);
    } catch {
      state.openIssue = null;
      delete state.route.query.issue;
      history.replaceState(null, '', hashFor(state.route));
    }
  } else {
    state.openIssue = null;
  }

  syncRail();
  syncTopbar();
  render();
  syncFormFromRoute();
}

/* The new-issue form is addressable as ?new=1, the same way the drawer is
   addressable as ?issue=<id> -- so a link can open the form directly, and a
   reload does not silently drop it. */
function syncFormFromRoute() {
  const wanted = state.route.query.new === '1';
  if (wanted && state.project && !modalOpen()) openIssueForm({});
}

let routeToken = 0;

async function handleRoute() {
  state.route = parseHash();
  state.filters = {
    q: state.route.query.q || '',
    assignee: state.route.query.assignee || 'all',
    priority: state.route.query.priority || 'all',
    type: state.route.query.type || 'all',
  };
  const token = (routeToken += 1);
  try {
    await refresh();
  } catch (error) {
    toast(error.message, 'error');
  }
  if (token !== routeToken) return;
}

/* ---- actions --------------------------------------------------------- */

const actions = {
  rerender() {
    render();
  },

  openIssue(id) {
    navigate({ query: { issue: String(id) } });
  },

  closeIssue() {
    navigate({ query: { issue: undefined } });
  },

  async updateIssue(id, patch, options = {}) {
    try {
      await api.updateIssue(id, patch);
    } catch (error) {
      toast(error.message, 'error');
    }
    await refresh({ withStats: state.route.view === 'reports' });
    if (options.refocus) {
      const node = document.getElementById(options.refocus);
      if (node) {
        node.focus();
        if (node.tagName === 'INPUT' && node.select) node.select();
      }
    }
  },

  moveIssue(id, patch) {
    return actions.updateIssue(id, patch);
  },

  async addComment(id, body, files = []) {
    try {
      // Files first, then the comment that claims them: a comment and its
      // screenshots then arrive together, and the one write that matters -- the
      // comment -- either carries them all or does not happen at all.
      const attachmentIds = [];
      for (const file of files) {
        const payload = await readForUpload(file);
        const saved = await api.uploadAttachment(id, payload);
        attachmentIds.push(saved.attachment.id);
      }
      await api.addComment(id, { body, attachment_ids: attachmentIds });
    } catch (error) {
      toast(error.message, 'error');
    }
    await refresh();
    const box = document.getElementById('comment-box');
    if (box) box.focus();
  },

  async sprintAction(id, action, payload) {
    try {
      const result = await api.sprintAction(id, action, payload);
      if (action === 'complete') {
        toast(result.moved_to_backlog
          ? `Sprint completed — ${result.moved_to_backlog} unfinished issues moved to the backlog`
          : 'Sprint completed');
      } else if (action === 'start') {
        toast(`${result.sprint.name} is now active`);
      } else {
        toast(`${result.sprint.name} reopened`);
      }
    } catch (error) {
      toast(error.message, 'error');
    }
    await refresh();
  },

  async deleteSprint(id) {
    try {
      await api.deleteSprint(id);
      toast('Sprint deleted');
    } catch (error) {
      toast(error.message, 'error');
    }
    await refresh();
  },

  async createIssue(payload) {
    const created = await api.createIssue(payload);
    await refresh({ withStats: false });
    toast(`${created.issue.key} created`);
    actions.openIssue(created.issue.id);
    return created;
  },

  async createProject(payload) {
    const project = await api.createProject(payload);
    state.ready = false;
    await ensureBootstrap();
    navigate({ view: 'board', query: { project: project.key, issue: undefined } }, { replace: true });
    toast(`${project.key} created`);
    return project;
  },

  setFilters(patch) {
    navigate({ query: { ...patch, issue: undefined } }, { replace: true });
  },

  openCreateIssue(prefill) {
    openIssueForm(prefill || {});
  },

  /* ---- accounts ------------------------------------------------------ */

  async signOut() {
    try {
      await api.logout();
    } catch (error) {
      /* The server clears the cookie; if the call could not land, the cookie
         may outlive it, but sending them to the sign-in screen is still the
         right move -- and a stale cookie only fails the next call. */
    }
    window.location.replace('/login');
  },

  openUsers() {
    openUsers(dom.modal, state, actions);
  },

  /* These four do not catch their errors: the accounts modal shows the API's
     message next to the form, the way the create-issue form does. */
  listUsers() {
    return api.users();
  },

  createUser(payload) {
    return api.createUser(payload);
  },

  resetUserPassword(id) {
    return api.resetUserPassword(id);
  },

  async setUserActive(id, active) {
    const result = await api.updateUser(id, { is_active: active });
    toast(active ? 'Account activated' : 'Account deactivated');
    return result;
  },

  async deleteUser(id) {
    const result = await api.deleteUser(id);
    toast('Account deleted');
    return result;
  },

  toast,
};

/* ---- theme ----------------------------------------------------------- */

const THEME_STORAGE = 'covex-theme';
const THEME_OPTIONS = [
  { value: 'light', label: 'Light' },
  { value: 'dark', label: 'Dark' },
  { value: 'system', label: 'System' },
];
const darkQuery = window.matchMedia('(prefers-color-scheme: dark)');

function themeChoice() {
  try {
    const stored = localStorage.getItem(THEME_STORAGE);
    if (THEME_OPTIONS.some((option) => option.value === stored)) return stored;
  } catch {
    /* storage blocked (private mode) -- fall through to system */
  }
  return 'system';
}

function applyTheme(choice) {
  const dark = choice === 'dark' || (choice === 'system' && darkQuery.matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  document.documentElement.dataset.themeChoice = choice;
}

function syncThemeSwitch() {
  if (!dom.themeSwitch) return;
  const choice = document.documentElement.dataset.themeChoice || 'system';
  dom.themeSwitch.querySelectorAll('button').forEach((node) => {
    node.setAttribute('aria-pressed', String(node.dataset.theme === choice));
  });
}

function setTheme(choice) {
  try {
    localStorage.setItem(THEME_STORAGE, choice);
  } catch {
    /* not fatal: the theme still applies for this session */
  }
  applyTheme(choice);
  syncThemeSwitch();
}

function buildThemeSwitch() {
  const group = el('div', {
    class: 'theme-switch',
    attrs: { role: 'group', 'aria-label': 'Colour theme' },
  });
  THEME_OPTIONS.forEach((option) => {
    const node = el('button', {
      class: 'theme-btn',
      text: option.label,
      attrs: { type: 'button', title: `${option.label} theme` },
      on: { click: () => setTheme(option.value) },
    });
    node.dataset.theme = option.value;
    group.appendChild(node);
  });
  dom.themeSwitch = group;
  return group;
}

/* "System" means reacting while the page is open, not only at load. */
darkQuery.addEventListener('change', () => {
  if (themeChoice() === 'system') {
    applyTheme('system');
    syncThemeSwitch();
  }
});

/* ---- chrome ---------------------------------------------------------- */

let activeFilterKey = null;

function buildRail() {
  clear(dom.rail);

  const mark = el('span', { class: 'brand-mark' });
  mark.innerHTML = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" '
    + 'stroke="currentColor" stroke-width="1.6" aria-hidden="true">'
    + '<path d="M2 4.5h12v2.2a1.3 1.3 0 0 0 0 2.6v2.2H2V9.3a1.3 1.3 0 0 0 0-2.6z"/></svg>';

  dom.rail.appendChild(el('div', { class: 'brand' }, [
    mark,
    el('span', { class: 'brand-text', text: 'Covex-Board' }),
  ]));

  const projectList = el('ul', { class: 'project-list' });
  if (!state.projects.length) {
    projectList.appendChild(el('li', {}, [el('span', { class: 'row-dim', text: 'No projects yet.' })]));
  }
  state.projects.forEach((project) => {
    const item = el('button', {
      class: 'project-btn',
      attrs: { type: 'button', title: `${project.name} — ${project.open_issues} open` },
      on: {
        click: () => {
          navigate({ view: state.route.view, query: { project: project.key, issue: undefined } });
          dom.app.classList.remove('rail-open');
        },
      },
    }, [
      el('span', { class: 'project-key', text: project.key }),
      el('span', { class: 'project-name', text: project.name }),
      el('span', { class: 'nav-btn-count', text: project.open_issues }),
    ]);
    item.dataset.project = project.key;
    projectList.appendChild(el('li', {}, [item]));
  });

  dom.rail.appendChild(el('div', { class: 'rail-section' }, [
    el('div', { class: 'rail-label', text: 'Projects' }),
    projectList,
    button('New project', {
      variant: 'quiet',
      size: 'sm',
      icon: 'plus',
      onClick: () => openCreateProject(dom.modal, state, actions),
    }),
  ]));

  const navList = el('ul', { class: 'nav-list' });
  NAV.forEach((item) => {
    const node = el('button', {
      class: 'nav-btn',
      attrs: { type: 'button' },
      on: {
        click: () => {
          navigate({ view: item.view, query: { issue: undefined } });
          dom.app.classList.remove('rail-open');
        },
      },
    }, [
      svgIcon(item.view === 'board' ? 'circleDot'
        : item.view === 'backlog' ? 'sprinter'
          : item.view === 'issues' ? 'checkSquare' : 'prioMedium', 14),
      el('span', { class: 'project-name', text: item.label }),
    ]);
    node.dataset.view = item.view;
    navList.appendChild(el('li', {}, [node]));
  });

  dom.rail.appendChild(el('div', { class: 'rail-section' }, [
    el('div', { class: 'rail-label', text: 'Views' }),
    navList,
  ]));

  /* The foot of the rail is the bottom-left panel: what this project is, the
     colour-theme switch, then the keyboard hints. The switch lives down here
     rather than in the topbar because theme applies to the whole window, not
     to the view -- the toolbar stays about search, filters and new issue. */
  const footer = el('div', { class: 'rail-footer' });

  dom.foot = el('div', { class: 'rail-foot' });
  footer.appendChild(dom.foot);

  /* Who you are signed in as, and the two things that act on that: managing
     accounts (only an administrator gets this) and signing out. Both live at
     the foot of the rail with the theme switch, because neither is a view. */
  footer.appendChild(buildAccountBlock());

  footer.appendChild(el('div', { class: 'rail-section' }, [
    el('div', { class: 'rail-label', text: 'Theme' }),
    buildThemeSwitch(),
  ]));

  footer.appendChild(el('div', { class: 'rail-foot' }, [
    el('div', {}, [el('kbd', { text: 'c' }), ' new issue · ', el('kbd', { text: '/' }), ' search']),
  ]));

  dom.rail.appendChild(footer);
  syncThemeSwitch();
}

function buildAccountBlock() {
  const block = el('div', { class: 'rail-section account-block' }, [
    el('div', { class: 'rail-label', text: 'Signed in' }),
  ]);

  const user = state.user;
  block.appendChild(el('div', { class: 'account-row' }, [
    avatar(state.me),
    el('div', { class: 'account-id' }, [
      el('div', { class: 'account-name', text: state.me, attrs: { title: state.me } }),
      el('div', {
        class: 'account-meta',
        text: user ? (user.is_admin ? 'Administrator' : 'Member') : '',
      }),
    ]),
  ]));

  if (user && user.is_admin) {
    block.appendChild(button('Manage accounts', {
      variant: 'quiet',
      icon: 'user',
      onClick: () => actions.openUsers(),
    }));
  }

  block.appendChild(button('Sign out', {
    variant: 'quiet',
    icon: 'close',
    onClick: () => actions.signOut(),
  }));

  return block;
}

function syncRail() {
  dom.rail.querySelectorAll('.project-btn').forEach((node) => {
    if (node.dataset.project === state.projectKey) node.setAttribute('aria-current', 'true');
    else node.removeAttribute('aria-current');
  });
  dom.rail.querySelectorAll('.nav-btn').forEach((node) => {
    if (node.dataset.view === state.route.view) node.setAttribute('aria-current', 'page');
    else node.removeAttribute('aria-current');
  });
  if (dom.foot && state.project) {
    dom.foot.textContent = `${state.project.key} · ${state.project.total_issues} issues · `
      + `${state.project.members.length} members`;
  }
}

function buildTopbar() {
  clear(dom.topbar);

  const toggle = button('', {
    variant: 'quiet',
    icon: 'menu',
    onClick: () => dom.app.classList.toggle('rail-open'),
    title: 'Toggle navigation',
  });
  toggle.classList.add('rail-toggle');

  dom.title = el('h1', { class: 'topbar-title', text: 'Covex-Board' });
  dom.sub = el('div', { class: 'topbar-sub' });

  dom.searchInput = el('input', {
    class: 'input',
    attrs: {
      type: 'search',
      id: 'search-input',
      placeholder: 'Search issues',
      'aria-label': 'Search issues',
    },
    on: { input: debounce((event) => actions.setFilters({ q: event.target.value }), 240) },
  });

  const search = el('div', { class: 'search' }, [
    svgIcon('search', 14, 'search-icon'),
    dom.searchInput,
  ]);

  dom.filters = el('div', { class: 'filter-group' });

  const createButton = el('button', {
    class: 'btn btn-primary',
    attrs: { type: 'button' },
    on: { click: () => actions.openCreateIssue({}) },
  }, [svgIcon('plus', 14), el('span', { text: 'New issue' })]);

  dom.topbar.appendChild(toggle);
  dom.topbar.appendChild(el('div', { class: 'topbar-heading' }, [dom.title, dom.sub]));
  dom.topbar.appendChild(el('span', { class: 'topbar-spacer' }));
  dom.topbar.appendChild(search);
  dom.topbar.appendChild(dom.filters);
  dom.topbar.appendChild(createButton);
}

function buildFilters() {
  clear(dom.filters);
  activeFilterKey = state.projectKey;

  const people = assignableNames(state, state.project);
  const assigneeOptions = [{ value: 'all', label: 'Anyone' }, { value: 'unassigned', label: 'Unassigned' }]
    .concat(people.map((name) => ({ value: name, label: name })));

  const make = (name, options, current) => {
    const node = el('select', {
      class: 'input',
      attrs: { 'aria-label': name },
      on: { change: (event) => actions.setFilters({ [name]: event.target.value }) },
    });
    options.forEach((option) => {
      const item = el('option', { text: option.label, attrs: { value: option.value } });
      if (String(option.value) === String(current)) item.selected = true;
      node.appendChild(item);
    });
    node.dataset.filter = name;
    return node;
  };

  dom.filters.appendChild(make('assignee', assigneeOptions, state.filters.assignee));
  dom.filters.appendChild(make('priority', [{ value: 'all', label: 'Any priority' }]
    .concat((state.meta.priorities || []).map((value) => ({ value, label: titleCase(value) }))),
  state.filters.priority));
  dom.filters.appendChild(make('type', [{ value: 'all', label: 'Any type' }]
    .concat((state.meta.types || []).map((value) => ({ value, label: titleCase(value) }))),
  state.filters.type));
}

function syncTopbar() {
  if (!dom.title) return;
  const project = state.project;
  dom.title.textContent = project ? project.name : 'Covex-Board';

  const active = (state.sprints || []).find((sprint) => sprint.status === 'active');
  dom.sub.textContent = project
    ? `${project.key}${active ? ` · ${active.name}` : ''} · ${project.open_points} open points`
    : '';

  if (dom.searchInput && document.activeElement !== dom.searchInput) {
    dom.searchInput.value = state.filters.q || '';
  }

  if (activeFilterKey !== state.projectKey) buildFilters();
  dom.filters.querySelectorAll('select').forEach((node) => {
    const value = state.filters[node.dataset.filter];
    if (value !== undefined) node.value = value;
  });

  dom.filters.style.display = state.route.view === 'reports' ? 'none' : 'flex';
}

function titleCase(text) {
  return String(text).replace(/_/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function debounce(fn, wait) {
  let timer = null;
  return (...args) => {
    window.clearTimeout(timer);
    timer = window.setTimeout(() => fn(...args), wait);
  };
}

function openIssueForm(prefill) {
  if (!state.project) return;
  openCreateIssue(dom.modal, state, actions, prefill, () => {
    if (state.route.query.new) {
      delete state.route.query.new;
      history.replaceState(null, '', hashFor(state.route));
    }
  });
}

function dismissModal() {
  closeModal();
}

/* ---- render ---------------------------------------------------------- */

let drawerHideTimer = null;

function render() {
  clear(dom.view);

  switch (state.route.view) {
    case 'backlog':
      backlogView.render(dom.view, state, actions);
      break;
    case 'issues':
      issuesView.render(dom.view, state, actions);
      break;
    case 'reports':
      reportsView.render(dom.view, state, actions);
      break;
    default:
      board.render(dom.view, state, actions);
  }

  drawer.render(dom.drawer, state, actions);
  syncDrawer();
}

function syncDrawer() {
  const open = Boolean(state.openIssue);
  window.clearTimeout(drawerHideTimer);

  if (open) {
    dom.drawer.hidden = false;
    dom.scrim.hidden = false;
    window.requestAnimationFrame(() => {
      dom.drawer.classList.add('open');
      dom.scrim.classList.add('open');
    });
  } else {
    dom.drawer.classList.remove('open');
    dom.scrim.classList.remove('open');
    drawerHideTimer = window.setTimeout(() => {
      dom.drawer.hidden = true;
      dom.scrim.hidden = true;
    }, 180);
  }
}

function toast(message, kind = 'info') {
  const node = el('div', {
    class: `toast${kind === 'error' ? ' toast-error' : ''}`,
    text: message,
    attrs: { role: 'status' },
  });
  dom.toasts.appendChild(node);
  window.setTimeout(() => node.remove(), kind === 'error' ? 6500 : 3600);
}

/* ---- keyboard -------------------------------------------------------- */

document.addEventListener('keydown', (event) => {
  const target = event.target;
  const typing = target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA'
    || target.tagName === 'SELECT' || target.isContentEditable);

  if (event.key === 'Escape') {
    if (modalOpen()) {
      dismissModal();
      return;
    }
    if (state.openIssue) {
      actions.closeIssue();
      return;
    }
    dom.app.classList.remove('rail-open');
    return;
  }

  if (typing || event.metaKey || event.ctrlKey || event.altKey) return;

  if (event.key === 'c') {
    event.preventDefault();
    if (!modalOpen()) actions.openCreateIssue({});
  } else if (event.key === '/') {
    event.preventDefault();
    if (dom.searchInput) {
      dom.searchInput.focus();
      dom.searchInput.select();
    }
  } else if (event.key === 'b') {
    navigate({ view: 'board', query: { issue: undefined } });
  } else if (event.key === 'k') {
    navigate({ view: 'backlog', query: { issue: undefined } });
  }
});

dom.scrim.addEventListener('click', () => actions.closeIssue());

window.addEventListener('hashchange', handleRoute);

(async function start() {
  try {
    await handleRoute();
  } catch (error) {
    clear(dom.view);
    dom.view.appendChild(el('div', { class: 'empty' }, [
      el('div', { class: 'empty-title', text: 'Cannot reach the Covex-Board API' }),
      el('p', { text: error.message }),
      el('p', { text: 'Check that server.py is running, then reload this page.' }),
    ]));
  }
}());

export { state, actions, navigate, refresh };
