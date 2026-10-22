/* Board view: one column per workflow status, cards draggable between them. */

import {
  el, svgIcon, typeMark, priorityMark, avatar, pointsBadge, labelChips, keyEl, dueEl,
  statusColor, statusLabel, draggable, drag, insertionIndex, showDropLine,
  removeDropLine, dropLineIndex, plural, sumPoints, button,
} from './ui.js';

export function render(host, state, actions) {
  const statuses = state.meta.statuses || [];
  const hasFilters = Boolean(state.filters.q
    || (state.filters.assignee && state.filters.assignee !== 'all')
    || (state.filters.priority && state.filters.priority !== 'all')
    || (state.filters.type && state.filters.type !== 'all'));

  const head = el('div', { class: 'view-head' }, [
    el('h2', { text: 'Board' }),
    el('span', {
      class: 'mono',
      text: `${state.issues.length} ${plural(state.issues.length, 'issue')} · `
        + `${sumPoints(state.issues)} points · drag a card to change status`,
    }),
  ]);

  if (!state.issues.length) {
    host.appendChild(head);
    host.appendChild(hasFilters ? filteredEmpty(actions) : emptyProject(state, actions));
    return;
  }

  const board = el('div', { class: 'board' });
  statuses.forEach((status) => board.appendChild(column(status, state, actions)));
  host.appendChild(head);
  host.appendChild(board);
}

function column(status, state, actions) {
  const issues = state.issues
    .filter((issue) => issue.status === status)
    .sort((a, b) => a.rank - b.rank);

  const dot = el('span', { class: 'status-dot' });
  dot.style.background = statusColor(status);

  const addButton = button('', {
    variant: 'quiet',
    icon: 'plus',
    onClick: () => actions.openCreateIssue({ status }),
    title: `New issue in ${statusLabel(status)}`,
  });

  const head = el('div', { class: 'column-head' }, [
    dot,
    el('span', { class: 'column-title', text: statusLabel(status) }),
    el('span', { class: 'column-count', text: `${issues.length} · ${sumPoints(issues)}pt` }),
    addButton,
  ]);

  const cards = el('ul', { class: 'cards' });
  issues.forEach((issue) => cards.appendChild(ticket(issue, actions)));
  if (!issues.length) {
    cards.appendChild(el('li', { class: 'column-empty', text: 'Nothing here yet.' }));
  }

  const node = el('section', {
    class: 'column',
    attrs: { 'aria-label': `${statusLabel(status)} column`, 'data-status': status },
  }, [head, cards]);

  attachDrop(node, cards, status, actions);
  return node;
}

function ticket(issue, actions) {
  const top = el('div', { class: 'ticket-top' }, [
    typeMark(issue.type),
    keyEl(issue),
    el('span', { class: 'spacer' }),
    dueEl(issue.due_date),
    priorityMark(issue.priority),
  ]);

  const foot = el('div', { class: 'ticket-foot' }, [
    avatar(issue.assignee, { empty: !issue.assignee }),
    labelChips(issue.labels, 2),
    el('span', { class: 'spacer' }),
    issue.sprint_name ? el('span', { class: 'ticket-key', text: issue.sprint_name }) : null,
    pointsBadge(issue.story_points),
  ]);

  const node = el('li', {
    class: 'ticket',
    attrs: {
      tabindex: '0',
      role: 'button',
      'aria-label': `${issue.key}, ${issue.title}. ${statusLabel(issue.status)}, `
        + `${issue.priority} priority.`,
    },
    dataset: { issueId: issue.id, status: issue.status },
    on: {
      click: () => actions.openIssue(issue.id),
      keydown: (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          actions.openIssue(issue.id);
        }
      },
    },
  }, [top, el('p', { class: 'ticket-title', text: issue.title }), foot]);

  return draggable(node, { kind: 'issue', id: issue.id, status: issue.status });
}

/* Dropping anywhere in the column targets the column's card list. */
function attachDrop(column, cards, status, actions) {
  const onOver = (event) => {
    if (!drag.current || drag.current.kind !== 'issue') return;
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
    cards.classList.add('drop-active');
    showDropLine(cards, insertionIndex(cards, event.clientY, 'li.ticket'), 'li.ticket');
  };

  column.addEventListener('dragover', onOver);
  column.addEventListener('dragenter', onOver);

  column.addEventListener('dragleave', (event) => {
    if (event.relatedTarget && column.contains(event.relatedTarget)) return;
    cards.classList.remove('drop-active');
    removeDropLine(cards);
  });

  column.addEventListener('drop', (event) => {
    event.preventDefault();
    const payload = drag.current;
    const index = dropLineIndex(cards, insertionIndex(cards, event.clientY, 'li.ticket'));
    cards.classList.remove('drop-active');
    removeDropLine(cards);
    if (!payload || payload.kind !== 'issue') return;
    if (payload.status === status && payload.index === index) return;
    actions.moveIssue(payload.id, { status, index });
  });
}

function emptyProject(state, actions) {
  return el('div', { class: 'empty' }, [
    el('div', { class: 'empty-title', text: `${state.project ? state.project.name : 'This project'} has no issues yet` }),
    el('p', { text: 'Create the first one and it will appear on this board. Press c anywhere to open the form.' }),
    button('Create issue', {
      variant: 'primary',
      icon: 'plus',
      onClick: () => actions.openCreateIssue({}),
    }),
  ]);
}

function filteredEmpty(actions) {
  return el('div', { class: 'empty' }, [
    el('div', { class: 'empty-title', text: 'No issues match these filters' }),
    el('p', { text: 'Widen the search or reset the filters to see the whole project.' }),
    button('Clear filters', { variant: 'secondary', onClick: () => actions.setFilters({ q: '', assignee: 'all', priority: 'all', type: 'all' }) }),
  ]);
}
