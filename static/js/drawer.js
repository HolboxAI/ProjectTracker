/* Issue detail drawer: the full record for one issue -- editable fields,
   description, comments and the activity trail. */

import {
  el, append, clear, typeMark, statusPill, avatar, keyEl,
  relativeTime, formatDate, titleCase, button, select, field, statusLabel,
  assignableNames,
} from './ui.js';
import { attachmentList, pendingChip } from './attachments.js';

let descriptionEditing = false;
let activityOpen = false;
let lastIssueId = null;
/* Files chosen for the comment box but not yet sent. They survive a rerender,
   which matters because a PATCH anywhere in the drawer rebuilds this whole
   panel -- a screenshot picked a second ago should still be waiting. */
let pendingFiles = [];
/* Set between clicking Comment and the rerender that follows, so a second
   click during an upload cannot post the same remark twice. */
let submitting = false;

export function render(node, state, actions) {
  const data = state.openIssue;
  if (!data) {
    clear(node);
    lastIssueId = null;
    return;
  }

  const issue = data.issue;
  if (issue.id !== lastIssueId) {
    lastIssueId = issue.id;
    descriptionEditing = false;
    activityOpen = false;
    // A file picked for one ticket must not follow you to the next one.
    pendingFiles = [];
  }
  submitting = false;

  clear(node);
  node.setAttribute('aria-label', `${issue.key}: ${issue.title}`);
  append(node, [header(issue, actions), body(data, state, actions)]);
}

function header(issue, actions) {
  return el('div', { class: 'drawer-head' }, [
    typeMark(issue.type),
    keyEl(issue),
    el('span', { class: 'ticket-key', text: `· ${issue.project_name}` }),
    el('span', { class: 'spacer' }),
    button('', {
      variant: 'quiet',
      icon: 'close',
      onClick: () => actions.closeIssue(),
      title: 'Close (Esc)',
    }),
  ]);
}

function body(data, state, actions) {
  return el('div', { class: 'drawer-body' }, [
    el('div', { class: 'drawer-main' }, [
      titleField(data.issue, actions),
      descriptionSection(data.issue, actions),
      commentsSection(data, actions),
      activitySection(data, actions),
    ]),
    el('div', { class: 'drawer-side' }, sideFields(data.issue, state, actions)),
  ]);
}

function titleField(issue, actions) {
  const input = el('input', {
    class: 'title-input',
    attrs: { id: 'drawer-title', value: issue.title, 'aria-label': 'Issue title' },
    on: {
      change: (event) => {
        const next = event.target.value.trim();
        if (!next) {
          event.target.value = issue.title;
          return;
        }
        if (next !== issue.title) actions.updateIssue(issue.id, { title: next }, { refocus: 'drawer-title' });
      },
      keydown: (event) => {
        if (event.key === 'Enter') event.target.blur();
        if (event.key === 'Escape') {
          event.target.value = issue.title;
          event.target.blur();
        }
      },
    },
  });

  return el('div', {}, [
    input,
    el('div', { class: 'drawer-meta' }, [
      statusPill(issue.status),
      el('span', { class: 'comment-time', text: `updated ${relativeTime(issue.updated_at)}` }),
    ]),
  ]);
}

function descriptionSection(issue, actions) {
  const section = el('div', {}, [el('div', { class: 'section-label', text: 'Description' })]);

  if (!descriptionEditing) {
    const hasText = Boolean(issue.description);
    const viewer = el('div', {
      class: 'description-body',
      text: hasText ? issue.description : 'No description yet.',
      on: { dblclick: () => { descriptionEditing = true; actions.rerender(); } },
    });
    if (!hasText) viewer.style.color = 'var(--text-muted)';
    section.appendChild(viewer);
    section.appendChild(button(hasText ? 'Edit' : 'Add description', {
      variant: 'quiet',
      size: 'sm',
      onClick: () => {
        descriptionEditing = true;
        actions.rerender();
      },
    }));
    return section;
  }

  const textarea = el('textarea', {
    class: 'input',
    attrs: { id: 'description-input', rows: '6', 'aria-label': 'Description' },
  });
  textarea.value = issue.description || '';

  const save = async () => {
    descriptionEditing = false;
    await actions.updateIssue(issue.id, { description: textarea.value });
  };

  section.appendChild(textarea);
  section.appendChild(el('div', { class: 'composer' }, [
    el('div', { class: 'sprint-actions' }, [
      button('Save', { variant: 'primary', onClick: save }),
      button('Cancel', {
        variant: 'secondary',
        onClick: () => {
          descriptionEditing = false;
          actions.rerender();
        },
      }),
    ]),
  ]));
  return section;
}

function commentsSection(data, actions) {
  const { issue, comments } = data;
  const section = el('div', {}, [
    el('div', { class: 'section-label', text: `Comments (${comments.length})` }),
  ]);

  if (!comments.length) {
    section.appendChild(el('p', { class: 'row-dim', text: 'No comments yet.' }));
  }

  const list = el('ul', { class: 'comments' });
  comments.forEach((comment) => {
    list.appendChild(el('li', { class: 'comment' }, [
      avatar(comment.author),
      el('div', { class: 'comment-body' }, [
        el('div', { class: 'comment-meta' }, [
          el('span', { class: 'comment-author', text: comment.author }),
          el('span', { class: 'comment-time', text: relativeTime(comment.created_at) }),
        ]),
        // A comment may be a screenshot with nothing typed above it.
        comment.body ? el('div', { class: 'comment-text', text: comment.body }) : null,
        attachmentList(comment.attachments),
      ]),
    ]));
  });
  section.appendChild(list);

  const textarea = el('textarea', {
    class: 'input',
    attrs: { id: 'comment-box', rows: '3', placeholder: 'Add a comment…', 'aria-label': 'Add a comment' },
    on: {
      keydown: (event) => {
        if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') submit();
      },
    },
  });

  const tray = el('div', { class: 'attach-tray' });

  /* Hidden from sight but present in the document: a hidden input that is not
     in the DOM cannot be opened by a click, and the button below is what
     carries its accessible name. */
  const picker = el('input', {
    class: 'visually-hidden',
    attrs: {
      type: 'file',
      multiple: true,
      id: 'comment-files',
      tabindex: '-1',
      'aria-label': 'Attach a screenshot or document to this comment',
    },
  });
  picker.addEventListener('change', () => {
    for (const file of [...picker.files]) pendingFiles.push(file);
    picker.value = ''; // clearing it means picking the same file twice still fires
    renderTray();
  });

  function renderTray() {
    clear(tray);
    pendingFiles.forEach((file, index) => {
      tray.appendChild(pendingChip(file, () => {
        pendingFiles.splice(index, 1);
        renderTray();
      }));
    });
  }
  renderTray();

  function submit() {
    const body = textarea.value.trim();
    if (submitting || (!body && !pendingFiles.length)) return;
    submitting = true;
    // The files are handed over and forgotten here: app.js uploads them, then
    // posts the comment that carries them, then rerenders either way.
    const files = pendingFiles.slice();
    pendingFiles = [];
    renderTray();
    actions.addComment(issue.id, body, files);
  }

  section.appendChild(el('div', { class: 'composer' }, [
    textarea,
    tray,
    picker,
    el('div', { class: 'sprint-actions' }, [
      button('Comment', { variant: 'primary', onClick: submit }),
      button('Attach file', {
        variant: 'secondary',
        icon: 'paperclip',
        title: 'Screenshot, image or document',
        onClick: () => picker.click(),
      }),
      el('span', { class: 'comment-time', text: '⌘↵ to post' }),
    ]),
  ]));

  return section;
}

function activitySection(data, actions) {
  const activity = data.activity || [];
  const section = el('div', {}, [
    el('div', { class: 'section-label' }, [
      button(`Activity (${activity.length})`, {
        variant: 'quiet',
        size: 'sm',
        title: activityOpen ? 'Hide activity' : 'Show activity',
        onClick: () => {
          activityOpen = !activityOpen;
          actions.rerender();
        },
      }),
    ]),
  ]);

  if (!activityOpen) return section;

  const list = el('ul', { class: 'activity-list' });
  activity.forEach((entry) => {
    list.appendChild(el('li', { class: 'activity-item' }, [
      el('span', { class: 'activity-strong', text: entry.actor }),
      el('span', { text: describe(entry) }),
      el('span', { class: 'activity-time', text: relativeTime(entry.created_at) }),
    ]));
  });
  section.appendChild(list);
  return section;
}

function describe(entry) {
  switch (entry.field) {
    case 'created':
      return 'created this issue';
    case 'comment':
      return 'commented';
    case 'status':
      return `moved this from ${entry.from} to ${entry.to}`;
    case 'sprint':
      return `moved this to ${entry.to}`;
    case 'assignee':
      return entry.to ? `assigned this to ${entry.to}` : 'unassigned this';
    case 'story_points':
      return `set the estimate to ${entry.to === null || entry.to === undefined ? 'none' : `${entry.to} points`}`;
    case 'due_date':
      return entry.to ? `set the due date to ${formatDate(entry.to)}` : 'cleared the due date';
    case 'labels':
      return `changed labels from ${entry.from} to ${entry.to}`;
    default:
      return `changed ${entry.field} from ${entry.from} to ${entry.to}`;
  }
}

function sideFields(issue, state, actions) {
  const project = state.project || { members: [] };
  const sprints = state.sprints || [];
  const statuses = (state.meta && state.meta.statuses) || [];
  const priorities = (state.meta && state.meta.priorities) || [];
  const types = (state.meta && state.meta.types) || [];

  // Everyone in the workspace, not just this project's members -- plus the
  // current assignee, which may be a name that is no longer anyone's account.
  const memberNames = assignableNames(state, project);
  if (issue.assignee && !memberNames.includes(issue.assignee)) memberNames.push(issue.assignee);

  const assigneeOptions = [{ value: '', label: 'Unassigned' }]
    .concat(memberNames.map((name) => ({ value: name, label: name })));

  const sprintOptions = [{ value: '', label: 'Backlog' }]
    .concat(sprints.map((sprint) => ({
      value: String(sprint.id),
      label: `${sprint.name}${sprint.status === 'active' ? ' (active)' : ''}`,
    })));

  const patch = (field_, value) => actions.updateIssue(issue.id, { [field_]: value });

  const pointsInput = el('input', {
    class: 'input',
    attrs: { type: 'number', min: '0', max: '100', id: 'points-input', 'aria-label': 'Story points' },
    on: {
      change: (event) => {
        const raw = event.target.value;
        patch('story_points', raw === '' ? null : Number(raw));
      },
    },
  });
  if (issue.story_points !== null && issue.story_points !== undefined) {
    pointsInput.value = String(issue.story_points);
  }

  const dueInput = el('input', {
    class: 'input',
    attrs: { type: 'date', id: 'due-input', 'aria-label': 'Due date' },
    on: { change: (event) => patch('due_date', event.target.value || null) },
  });
  if (issue.due_date) dueInput.value = issue.due_date;

  const labelsInput = el('input', {
    class: 'input',
    attrs: { id: 'labels-input', placeholder: 'comma, separated', 'aria-label': 'Labels' },
    on: {
      change: (event) => {
        const next = event.target.value.split(',').map((part) => part.trim()).filter(Boolean);
        patch('labels', next);
      },
    },
  });
  labelsInput.value = (issue.labels || []).join(', ');

  const nodes = [
    field('Status', select(
      statuses.map((value) => ({ value, label: statusLabel(value) })),
      issue.status,
      (value) => patch('status', value),
    )),
    field('Assignee', select(assigneeOptions, issue.assignee, (value) => patch('assignee', value))),
    el('div', { class: 'field-inline' }, [
      field('Priority', select(
        priorities.map((value) => ({ value, label: titleCase(value) })),
        issue.priority,
        (value) => patch('priority', value),
      )),
      field('Type', select(
        types.map((value) => ({ value, label: titleCase(value) })),
        issue.type,
        (value) => patch('type', value),
      )),
    ]),
    field('Sprint', select(sprintOptions, issue.sprint_id ? String(issue.sprint_id) : '', (value) => {
      patch('sprint_id', value === '' ? null : Number(value));
    })),
    el('div', { class: 'field-inline' }, [
      field('Points', pointsInput),
      field('Due', dueInput),
    ]),
    field('Labels', labelsInput),
    el('div', { class: 'field-meta' }, [
      metaRow('Key', issue.key),
      metaRow('Reporter', issue.reporter || '—'),
      metaRow('Created', formatDate(issue.created_at)),
      metaRow('Resolved', issue.resolved_at ? formatDate(issue.resolved_at) : '—'),
    ]),
    // Deliberately no delete. A ticket is the record of something that was
    // reported; the activity trail, the comments and the reporter's name are
    // the value, and all of it goes when the row does. The route is gone too,
    // so this is not merely a hidden button.
  ];

  return nodes;
}

function metaRow(label, value) {
  return el('div', { class: 'field-meta-row' }, [
    el('span', { class: 'field-label', text: label }),
    el('span', { class: 'ticket-key', text: value }),
  ]);
}
