/* Backlog view: the active sprint, planned sprints, finished sprints and the
   unscheduled backlog. Rows drag between them to (re)assign a sprint. */

import {
  el, typeMark, keyEl, avatar, pointsBadge, statusPill, statusLabel, svgIcon,
  draggable, drag, showDropLine, removeDropLine, formatDate,
  plural, sumPoints, button,
} from './ui.js';

export function render(host, state, actions) {
  const sprints = state.sprints || [];
  const active = sprints.filter((sprint) => sprint.status === 'active');
  const planned = sprints.filter((sprint) => sprint.status === 'planned');
  const completed = sprints.filter((sprint) => sprint.status === 'completed');
  const backlogIssues = state.issues.filter((issue) => !issue.sprint_id);

  const wrap = el('div', { class: 'backlog' });
  wrap.appendChild(el('div', { class: 'view-head' }, [
    el('h2', { text: 'Backlog' }),
    el('span', {
      class: 'mono',
      text: `${backlogIssues.length} in backlog · ${sprints.length} ${plural(sprints.length, 'sprint')}`,
    }),
  ]));

  active.forEach((sprint) => wrap.appendChild(sprintBlock(sprint, state, actions, { active: true })));
  planned.forEach((sprint) => wrap.appendChild(sprintBlock(sprint, state, actions, { planned: true })));

  wrap.appendChild(backlogBlock(backlogIssues, state, actions));

  if (completed.length) {
    wrap.appendChild(completedBlock(completed, state, actions));
  }

  host.appendChild(wrap);
}

function sprintIssues(state, sprint) {
  return state.issues.filter((issue) => issue.sprint_id === sprint.id);
}

function sprintBlock(sprint, state, actions, flags = {}) {
  const issues = sprintIssues(state, sprint);
  const done = issues.filter((issue) => issue.status === 'done');
  const points = sumPoints(issues);
  const donePoints = sumPoints(done);

  const actionsBar = el('div', { class: 'sprint-actions' });

  if (flags.planned) {
    actionsBar.appendChild(button('Start sprint', {
      variant: 'primary',
      icon: 'play',
      onClick: () => actions.sprintAction(sprint.id, 'start'),
      title: 'Make this the active sprint',
    }));
    actionsBar.appendChild(button('', {
      variant: 'quiet',
      icon: 'trash',
      onClick: () => {
        if (window.confirm(`Delete ${sprint.name}? Its issues go back to the backlog.`)) {
          actions.deleteSprint(sprint.id);
        }
      },
      title: 'Delete sprint',
    }));
  } else if (flags.active) {
    actionsBar.appendChild(button('Complete sprint', {
      variant: 'secondary',
      icon: 'check',
      onClick: () => {
        const unfinished = issues.length - done.length;
        const message = unfinished
          ? `Complete ${sprint.name}? ${unfinished} unfinished ${plural(unfinished, 'issue')} will move to the backlog.`
          : `Complete ${sprint.name}? Everything in it is done.`;
        if (window.confirm(message)) actions.sprintAction(sprint.id, 'complete', { move_to: 'backlog' });
      },
    }));
  }

  const meta = el('span', { class: 'sprint-meta' }, [
    `${issues.length} ${plural(issues.length, 'issue')} · ${donePoints}/${points} pt done`,
    sprint.starts_on || sprint.ends_on
      ? ` · ${formatDate(sprint.starts_on) || '—'} → ${formatDate(sprint.ends_on) || '—'}`
      : '',
  ]);

  const head = el('div', { class: 'sprint-head' }, [
    el('span', { class: 'sprint-name', text: sprint.name }),
    flags.active ? statusTag('Active') : null,
    meta,
    el('span', { class: 'sprint-head-spacer' }),
    actionsBar,
  ]);

  const progress = el('div', { class: 'sprint-bar' }, [
    el('div', {
      class: 'sprint-bar-fill',
      attrs: {},
    }),
  ]);
  const fill = progress.firstChild;
  fill.style.width = `${points ? Math.round((donePoints / points) * 100) : 0}%`;

  const rows = el('ul', { class: 'rows' });
  issues.forEach((issue) => rows.appendChild(row(issue, state, actions)));
  if (!issues.length) {
    rows.appendChild(el('li', { class: 'row row-empty' }, [
      el('span', { class: 'row-dim', text: 'Empty — drag issues here to plan them in.' }),
    ]));
  }

  const block = el('section', {
    class: 'sprint-block',
    attrs: { 'aria-label': `${sprint.name}, ${statusLabel(sprint.status)}` },
  }, [
    head,
    sprint.goal ? el('p', { class: 'sprint-goal', text: sprint.goal }) : null,
    progress,
    rows,
  ]);

  attachDrop(block, head, () => ({ sprint_id: sprint.id }), actions);
  return block;
}

function backlogBlock(issues, state, actions) {
  const points = sumPoints(issues);

  const rows = el('ul', { class: 'rows' });
  issues.forEach((issue) => rows.appendChild(row(issue, state, actions)));
  if (!issues.length) {
    rows.appendChild(el('li', { class: 'row row-empty' }, [
      el('span', { class: 'row-dim', text: 'Backlog is empty.' }),
    ]));
  }

  const block = el('section', { class: 'backlog-block', attrs: { 'aria-label': 'Backlog' } }, [
    el('div', { class: 'sprint-head' }, [
      el('span', { class: 'sprint-name', text: 'Backlog' }),
      el('span', {
        class: 'sprint-meta',
        text: `${issues.length} ${plural(issues.length, 'issue')} · ${points} pt`,
      }),
      el('span', { class: 'sprint-head-spacer' }),
      button('Create issue', {
        variant: 'secondary',
        icon: 'plus',
        onClick: () => actions.openCreateIssue({}),
      }),
    ]),
    rows,
  ]);

  const head = block.firstChild;
  attachDrop(block, head, () => ({ sprint_id: null }), actions);
  return block;
}

function completedBlock(sprints, state, actions) {
  const rows = el('ul', { class: 'rows' });
  sprints.forEach((sprint) => {
    const issues = sprintIssues(state, sprint);
    rows.appendChild(el('li', { class: 'row' }, [
      svgIcon('check', 14),
      el('span', { class: 'ticket-key', text: formatDate(sprint.ends_on) || '—' }),
      el('span', { class: 'row-title', text: sprint.name }),
      el('span', { class: 'row-dim', text: `${issues.length} ${plural(issues.length, 'issue')}` }),
      el('span', { class: 'row-dim', text: `${sumPoints(issues)} pt` }),
      button('Reopen', {
        variant: 'quiet',
        size: 'sm',
        onClick: () => actions.sprintAction(sprint.id, 'reopen'),
      }),
    ]));
  });

  return el('section', { class: 'backlog-block' }, [
    el('div', { class: 'sprint-head' }, [
      el('span', { class: 'sprint-name', text: 'Completed sprints' }),
      el('span', { class: 'sprint-meta', text: String(sprints.length) }),
    ]),
    rows,
  ]);
}

function row(issue, state, actions) {
  const owner = el('span', { class: 'row-owner' }, [
    avatar(issue.assignee, { empty: !issue.assignee }),
    el('span', { class: 'row-owner-name', text: issue.assignee || 'Unassigned' }),
  ]);

  const node = el('li', {
    class: 'row',
    attrs: {
      tabindex: '0',
      role: 'button',
      'aria-label': `${issue.key}, ${issue.title}`,
    },
    dataset: { issueId: issue.id, sprintId: issue.sprint_id || '' },
    on: {
      click: () => actions.openIssue(issue.id),
      keydown: (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          actions.openIssue(issue.id);
        }
      },
    },
  }, [
    typeMark(issue.type),
    keyEl(issue),
    el('span', { class: 'row-title', text: issue.title }),
    statusPill(issue.status),
    owner,
    pointsBadge(issue.story_points),
  ]);

  return draggable(node, { kind: 'issue', id: issue.id, sprint: issue.sprint_id || null });
}

function attachDrop(block, head, target, actions) {
  const rows = block.querySelector('.rows');

  const onOver = (event) => {
    if (!drag.current || drag.current.kind !== 'issue') return;
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
    head.classList.add('drop-active');
    showDropLine(rows, rows.children.length, 'li.row');
  };

  block.addEventListener('dragover', onOver);
  block.addEventListener('dragenter', onOver);

  block.addEventListener('dragleave', (event) => {
    if (event.relatedTarget && block.contains(event.relatedTarget)) return;
    head.classList.remove('drop-active');
    removeDropLine(rows);
  });

  block.addEventListener('drop', (event) => {
    event.preventDefault();
    const payload = drag.current;
    head.classList.remove('drop-active');
    removeDropLine(rows);
    if (!payload || payload.kind !== 'issue') return;
    const patch = target();
    if (patch.sprint_id === payload.sprint) return;
    actions.updateIssue(payload.id, patch);
  });
}

function statusTag(text) {
  return el('span', { class: 'label-chip', text });
}
