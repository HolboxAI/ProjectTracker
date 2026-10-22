/* Modal forms: create an issue, create a project.

   Errors from the API are shown inside the form rather than as a toast --
   the person is looking at the form, and a field problem belongs there. */

import { el, clear, button, select, titleCase, relativeTime, assignableNames } from './ui.js';

let active = null;

export function isOpen() {
  return Boolean(active);
}

export function close() {
  if (!active) return;
  const { container, onClose } = active;
  active = null;
  container.hidden = true;
  clear(container);
  if (onClose) onClose();
}

let backdropBound = false;

function mount(container, node, onClose) {
  close();
  clear(container);
  container.hidden = false;
  container.appendChild(node);
  active = { container, onClose };

  if (!backdropBound) {
    backdropBound = true;
    container.addEventListener('mousedown', (event) => {
      if (event.target === container) close();
    });
  }

  const first = node.querySelector('[data-autofocus]') || node.querySelector('input, select, textarea');
  if (first) {
    first.focus();
    if (first.select) first.select();
  }
}

function errorLine() {
  const node = el('p', { class: 'form-error', attrs: { role: 'alert' } });
  node.hidden = true;
  return node;
}

/* ---- create issue ---------------------------------------------------- */

export function openCreateIssue(container, state, actions, prefill = {}, onClose) {
  const project = state.project;
  const sprints = state.sprints || [];
  const meta = state.meta || {};

  const titleInput = el('input', {
    class: 'input',
    attrs: { placeholder: 'What needs to be done?', 'data-autofocus': true, 'aria-label': 'Summary' },
  });
  titleInput.value = prefill.title || '';

  const activeSprint = sprints.find((sprint) => sprint.status === 'active');
  const defaultSprint = prefill.sprint_id !== undefined
    ? String(prefill.sprint_id || '')
    : (activeSprint ? String(activeSprint.id) : '');

  const statusSelect = select(
    meta.statuses.map((value) => ({ value, label: titleCase(value) })),
    prefill.status || 'todo',
    () => {},
  );
  const typeSelect = select(
    meta.types.map((value) => ({ value, label: titleCase(value) })),
    prefill.type || 'task',
    () => {},
  );
  const prioritySelect = select(
    meta.priorities.map((value) => ({ value, label: titleCase(value) })),
    prefill.priority || 'medium',
    () => {},
  );
  const assigneeSelect = select(
    peopleOptions(state, project, { includeEmpty: 'Unassigned' }),
    state.me,
    () => {},
  );
  // The reporter has always been recorded -- it just used to be taken silently
  // from whoever was signed in. Making it a field lets an administrator file a
  // ticket on somebody else's behalf, which is the usual reason to want it.
  const reporterSelect = select(
    peopleOptions(state, project, { fallback: state.me }),
    state.me,
    () => {},
  );
  const sprintSelect = select(
    [{ value: '', label: 'Backlog' }].concat(sprints.map((sprint) => ({
      value: String(sprint.id),
      label: `${sprint.name}${sprint.status === 'active' ? ' (active)' : ''}`,
    }))),
    defaultSprint,
    () => {},
  );
  const pointsInput = el('input', { class: 'input', attrs: { type: 'number', min: '0', max: '100', 'aria-label': 'Story points' } });
  const labelsInput = el('input', { class: 'input', attrs: { placeholder: 'comma, separated', 'aria-label': 'Labels' } });
  const descriptionInput = el('textarea', { class: 'input', attrs: { rows: '4', 'aria-label': 'Description' } });

  const error = errorLine();

  const form = el('form', { class: 'modal', attrs: { 'aria-label': 'Create issue' } }, [
    el('div', { class: 'modal-head' }, [
      el('span', { text: `New issue in ${project.key}` }),
    ]),
    el('div', { class: 'modal-body' }, [
      titleInput,
      /* The two people fields sit together at the top: they are the ones
         people want to set first, and the rest are triage details. */
      el('div', { class: 'form-grid' }, [
        labelled('Created by', reporterSelect),
        labelled('Assigned to', assigneeSelect),
      ]),
      el('div', { class: 'form-grid' }, [
        labelled('Type', typeSelect),
        labelled('Priority', prioritySelect),
      ]),
      el('div', { class: 'form-grid' }, [
        labelled('Status', statusSelect),
        labelled('Sprint', sprintSelect),
      ]),
      el('div', { class: 'form-grid' }, [
        labelled('Points', pointsInput),
        labelled('Labels', labelsInput),
      ]),
      labelled('Description', descriptionInput),
      error,
    ]),
    el('div', { class: 'modal-foot' }, [
      button('Cancel', { variant: 'secondary', onClick: () => close() }),
      el('button', { class: 'btn btn-primary', attrs: { type: 'submit' }, text: 'Create' }),
    ]),
  ]);

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const title = titleInput.value.trim();
    if (!title) {
      error.textContent = 'An issue needs a summary.';
      error.hidden = false;
      titleInput.focus();
      return;
    }
    const payload = {
      project: project.key,
      title,
      type: typeSelect.value,
      priority: prioritySelect.value,
      status: statusSelect.value,
      reporter: reporterSelect.value,
      assignee: assigneeSelect.value,
      story_points: pointsInput.value === '' ? null : Number(pointsInput.value),
      labels: labelsInput.value,
      description: descriptionInput.value,
      sprint_id: sprintSelect.value === '' ? null : Number(sprintSelect.value),
    };
    try {
      await actions.createIssue(payload);
      close();
    } catch (cause) {
      error.textContent = cause.message;
      error.hidden = false;
    }
  });

  mount(container, form, onClose);
}

/* ---- create project -------------------------------------------------- */

export function openCreateProject(container, state, actions, onClose) {
  const nameInput = el('input', { class: 'input', attrs: { placeholder: 'Phoenix Platform', 'data-autofocus': true, 'aria-label': 'Project name' } });
  const keyInput = el('input', { class: 'input', attrs: { placeholder: 'PHX', maxlength: '10', 'aria-label': 'Project key', style: 'text-transform: uppercase' } });
  const leadInput = el('input', { class: 'input', attrs: { placeholder: 'Who leads this?', 'aria-label': 'Project lead' } });
  const descriptionInput = el('textarea', { class: 'input', attrs: { rows: '3', 'aria-label': 'Description' } });
  const error = errorLine();

  // Suggest a key from the name until the person types their own.
  let keyEdited = false;
  keyInput.addEventListener('input', () => { keyEdited = true; });
  nameInput.addEventListener('input', () => {
    if (keyEdited) return;
    const suggestion = nameInput.value
      .split(/\s+/)
      .filter(Boolean)
      .map((word) => word[0])
      .join('')
      .toUpperCase()
      .slice(0, 5);
    keyInput.value = /^[A-Z][A-Z0-9]*$/.test(suggestion) ? suggestion : '';
  });

  const form = el('form', { class: 'modal', attrs: { 'aria-label': 'Create project' } }, [
    el('div', { class: 'modal-head' }, [el('span', { text: 'New project' })]),
    el('div', { class: 'modal-body' }, [
      labelled('Name', nameInput),
      el('div', { class: 'form-grid' }, [
        labelled('Key', keyInput),
        labelled('Lead', leadInput),
      ]),
      labelled('Description', descriptionInput),
      error,
    ]),
    el('div', { class: 'modal-foot' }, [
      button('Cancel', { variant: 'secondary', onClick: () => close() }),
      el('button', { class: 'btn btn-primary', attrs: { type: 'submit' }, text: 'Create project' }),
    ]),
  ]);

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      await actions.createProject({
        key: keyInput.value.trim().toUpperCase(),
        name: nameInput.value.trim(),
        lead: leadInput.value.trim(),
        description: descriptionInput.value.trim(),
      });
      close();
    } catch (cause) {
      error.textContent = cause.message;
      error.hidden = false;
    }
  });

  mount(container, form, onClose);
}

function labelled(labelText, control) {
  return el('label', { class: 'field' }, [
    el('span', { class: 'field-label', text: labelText }),
    control,
  ]);
}

/* Options for a person picker: the workspace's accounts, plus the project's
   members, plus whatever the current value is -- so a name that is no longer
   an account (or not yet a member) still shows instead of silently blanking. */
function peopleOptions(state, project, { includeEmpty = null, fallback = null } = {}) {
  const names = assignableNames(state, project);
  if (fallback && !names.includes(fallback)) names.unshift(fallback);
  const options = names.map((name) => ({ value: name, label: name }));
  return includeEmpty ? [{ value: '', label: includeEmpty }].concat(options) : options;
}

/* ---- manage accounts -------------------------------------------------

   Administrators only; the rail only offers the button when bootstrap says
   the signed-in account is one. Every action here is re-checked server-side,
   so what this form shows is a convenience, not the control. */

export function openUsers(container, state, actions, onClose) {
  const list = el('div', { class: 'user-list' }, [
    el('p', { class: 'user-loading', text: 'Loading accounts\u2026' }),
  ]);
  const error = errorLine();
  const handoff = el('p', { class: 'form-note' });
  handoff.hidden = true;

  const nameInput = el('input', {
    class: 'input',
    attrs: { placeholder: 'Jane Doe', 'aria-label': 'Full name' },
  });
  const usernameInput = el('input', {
    class: 'input',
    attrs: {
      placeholder: 'jane', 'aria-label': 'Username',
      autocapitalize: 'none', autocorrect: 'off', spellcheck: 'false',
    },
  });
  const passwordInput = el('input', {
    class: 'input',
    attrs: { placeholder: 'leave empty to generate one', 'aria-label': 'First password' },
  });
  const adminInput = el('input', { attrs: { type: 'checkbox', 'aria-label': 'Administrator' } });

  const form = el('form', { class: 'modal modal-wide', attrs: { 'aria-label': 'Accounts' } }, [
    el('div', { class: 'modal-head' }, [el('span', { text: 'Accounts' })]),
    el('div', { class: 'modal-body' }, [
      list,
      handoff,
      el('div', { class: 'form-grid' }, [
        labelled('Full name', nameInput),
        labelled('Username', usernameInput),
      ]),
      el('div', { class: 'form-grid' }, [
        labelled('First password', passwordInput),
        labelled('Administrator', adminInput),
      ]),
      error,
    ]),
    el('div', { class: 'modal-foot' }, [
      button('Close', { variant: 'secondary', onClick: () => close() }),
      el('button', { class: 'btn btn-primary', attrs: { type: 'submit' }, text: 'Create account' }),
    ]),
  ]);

  function fail(cause) {
    error.textContent = cause.message;
    error.hidden = false;
  }

  /* A generated password is shown exactly once, in the response that created
     it. This is the only moment it can be read, so say so. */
  function announce(account, password) {
    handoff.textContent = `Password for ${account.username}: ${password} \u2014 `
      + 'copy it now and pass it on; it cannot be read back later.';
    handoff.hidden = false;
  }

  function rowActions(account) {
    const act = (work) => async () => {
      error.hidden = true;
      try {
        await work();
        await refresh();
      } catch (cause) {
        fail(cause);
      }
    };

    return el('div', { class: 'user-actions' }, [
      button('Reset password', {
        variant: 'quiet',
        size: 'sm',
        onClick: act(async () => {
          const result = await actions.resetUserPassword(account.id);
          announce(result.user, result.password);
        }),
      }),
      button(account.is_active ? 'Deactivate' : 'Activate', {
        variant: 'quiet',
        size: 'sm',
        onClick: act(() => actions.setUserActive(account.id, !account.is_active)),
      }),
      button('Delete', {
        variant: 'danger',
        size: 'sm',
        onClick: act(async () => {
          const sure = window.confirm(
            `Delete the account for ${account.name}? Their issues and comments stay, `
            + 'still attributed by name.',
          );
          if (!sure) return;
          await actions.deleteUser(account.id);
        }),
      }),
    ]);
  }

  function renderUsers(users) {
    clear(list);
    const rows = users.map((account) => el('tr', {
      class: account.is_active ? '' : 'is-off',
    }, [
      el('td', { text: account.name }),
      el('td', { class: 'mono', text: account.username }),
      el('td', { text: account.is_admin ? 'Administrator' : 'Member' }),
      el('td', {
        class: 'mono',
        text: account.last_login_at ? relativeTime(account.last_login_at) : 'never',
      }),
      el('td', {}, [rowActions(account)]),
    ]));

    list.appendChild(el('table', { class: 'user-table' }, [
      el('thead', {}, [el('tr', {}, [
        el('th', { text: 'Name' }),
        el('th', { text: 'Username' }),
        el('th', { text: 'Role' }),
        el('th', { text: 'Last sign-in' }),
        el('th', {}),
      ])]),
      el('tbody', {}, rows),
    ]));
  }

  async function refresh() {
    try {
      const payload = await actions.listUsers();
      renderUsers(payload.users);
    } catch (cause) {
      fail(cause);
    }
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    error.hidden = true;
    try {
      const result = await actions.createUser({
        name: nameInput.value.trim(),
        username: usernameInput.value.trim(),
        password: passwordInput.value,
        is_admin: adminInput.checked,
      });
      announce(result.user, result.password);
      nameInput.value = '';
      usernameInput.value = '';
      passwordInput.value = '';
      adminInput.checked = false;
      nameInput.focus();
      await refresh();
    } catch (cause) {
      fail(cause);
    }
  });

  mount(container, form, onClose);
  refresh();
}
