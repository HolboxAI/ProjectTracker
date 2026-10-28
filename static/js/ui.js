/* Small DOM + formatting helpers shared by every view.
   No framework: these build real nodes, so nothing here interpolates
   user text into HTML. */

export function el(tag, options = {}, children = []) {
  const node = document.createElement(tag);
  if (options.class) node.className = options.class;
  if (options.text !== undefined && options.text !== null) node.textContent = String(options.text);
  if (options.html !== undefined) node.innerHTML = options.html;
  if (options.attrs) {
    for (const [key, value] of Object.entries(options.attrs)) {
      if (value === null || value === undefined || value === false) continue;
      node.setAttribute(key, value === true ? '' : String(value));
    }
  }
  if (options.dataset) {
    for (const [key, value] of Object.entries(options.dataset)) {
      if (value !== null && value !== undefined) node.dataset[key] = String(value);
    }
  }
  if (options.on) {
    for (const [event, handler] of Object.entries(options.on)) node.addEventListener(event, handler);
  }
  append(node, children);
  return node;
}

export function append(node, children) {
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

/* Icon markup is the one place innerHTML is used, and only ever with the
   literals below. */
export function icon(markup, size = 16, className = '') {
  const span = el('span', { class: className });
  span.style.display = 'grid';
  span.style.placeItems = 'center';
  span.innerHTML = `<svg width="${size}" height="${size}" viewBox="0 0 16 16" fill="none" `
    + `stroke="currentColor" stroke-width="1.5" stroke-linecap="round" `
    + `stroke-linejoin="round" aria-hidden="true">${markup}</svg>`;
  return span;
}

export const ICONS = {
  search: '<circle cx="7" cy="7" r="4.2"/><path d="M10.2 10.2 13.5 13.5"/>',
  close: '<path d="M4 4l8 8M12 4l-8 8"/>',
  chevronDown: '<path d="M4 6.5 8 10.5l4-4"/>',
  menu: '<path d="M2.5 4.5h11M2.5 8h11M2.5 11.5h11"/>',
  plus: '<path d="M8 3.5v9M3.5 8h9"/>',
  trash: '<path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.6 8.5h5.8l.6-8.5"/>',
  flag: '<path d="M4 14V2.5h7.5L10 5.5l1.5 3H4"/>',
  bookmark: '<path d="M4 2.5h8v11L8 10.4 4 13.5z"/>',
  circleDot: '<circle cx="8" cy="8" r="5.2"/><circle cx="8" cy="8" r="1.6" fill="currentColor"/>',
  checkSquare: '<rect x="2.6" y="2.6" width="10.8" height="10.8" rx="2"/><path d="M5.4 8.2 7.3 10l3.4-3.6"/>',
  bug: '<ellipse cx="8" cy="8.6" rx="3.6" ry="4.2"/><path d="M8 4.4V2.4M4.4 6.6 2.6 5.4M11.6 6.6l1.8-1.2M4.2 9.6l-2 .6M11.8 9.6l2 .6M4.6 12.4l-1.6 1.4M11.4 12.4l1.6 1.4"/>',
  prioHighest: '<path d="M4 11.5 8 7.5l4 4"/><path d="M4 6.5 8 2.5l4 4"/>',
  prioHigh: '<path d="M4 10.5 8 6.5l4 4"/>',
  prioMedium: '<path d="M4 6.5h8M4 9.5h8"/>',
  prioLow: '<path d="M4 6 8 10l4-4"/>',
  user: '<circle cx="8" cy="6" r="2.6"/><path d="M3.2 13.4c.7-2.3 2.6-3.6 4.8-3.6s4.1 1.3 4.8 3.6"/>',
  sprinter: '<rect x="2.5" y="3.5" width="11" height="9" rx="1.6"/><path d="M2.5 6.6h11M6 2v3M10 2v3"/>',
  play: '<path d="M5.5 3.6 12 8l-6.5 4.4z"/>',
  check: '<path d="M3.5 8.4 6.4 11.3l6.1-6.4"/>',
  paperclip: '<path d="M10.6 6.1 6.3 10.4a1.9 1.9 0 0 0 2.7 2.7l4.6-4.6a3.3 3.3 0 0 0-4.7-4.7l-5 5a4.6 4.6 0 0 0 6.5 6.5l4.3-4.3"/>',
  download: '<path d="M8 2.5v7.6M5.2 7.4 8 10.2l2.8-2.8M3 13.5h10"/>',
};

export function svgIcon(name, size = 16, className = '') {
  return icon(ICONS[name] || '', size, className);
}

/* ---- vocabulary ------------------------------------------------------ */

export const STATUS_META = {
  todo: { label: 'To do', color: 'var(--status-todo)' },
  in_progress: { label: 'In progress', color: 'var(--status-in-progress)' },
  in_review: { label: 'In review', color: 'var(--status-in-review)' },
  done: { label: 'Done', color: 'var(--status-done)' },
};

export const TYPE_META = {
  story: { label: 'Story', color: 'var(--type-story)', icon: 'bookmark' },
  bug: { label: 'Bug', color: 'var(--type-bug)', icon: 'bug' },
  task: { label: 'Task', color: 'var(--type-task)', icon: 'checkSquare' },
  epic: { label: 'Epic', color: 'var(--type-epic)', icon: 'flag' },
};

export const PRIORITY_META = {
  highest: { label: 'Highest', icon: 'prioHighest', className: 'prio-highest' },
  high: { label: 'High', icon: 'prioHigh', className: 'prio-high' },
  medium: { label: 'Medium', icon: 'prioMedium', className: 'prio-medium' },
  low: { label: 'Low', icon: 'prioLow', className: 'prio-low' },
};

export const SPRINT_STATUS_LABEL = {
  planned: 'Planned',
  active: 'Active',
  completed: 'Completed',
};

export function statusLabel(status) {
  return (STATUS_META[status] || { label: status }).label;
}

export function statusColor(status) {
  return (STATUS_META[status] || { color: 'var(--text-muted)' }).color;
}

export function titleCase(text) {
  if (!text) return 'None';
  return String(text).replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

/* ---- display components --------------------------------------------- */

export function typeMark(type) {
  const meta = TYPE_META[type] || TYPE_META.task;
  const mark = el('span', {
    class: 'type-mark',
    attrs: { title: meta.label, 'aria-label': meta.label },
  });
  mark.style.color = meta.color;
  mark.appendChild(svgIcon(meta.icon, 15));
  return mark;
}

export function priorityMark(priority) {
  const meta = PRIORITY_META[priority] || PRIORITY_META.medium;
  const mark = el('span', {
    class: `prio ${meta.className}`,
    attrs: { title: `${meta.label} priority`, 'aria-label': `${meta.label} priority` },
  });
  mark.appendChild(svgIcon(meta.icon, 14));
  return mark;
}

export function statusPill(status) {
  const meta = STATUS_META[status] || { label: status, color: 'var(--text-muted)' };
  const dot = el('span', { class: 'status-dot' });
  dot.style.background = meta.color;
  return el('span', { class: 'status-pill' }, [dot, el('span', { text: meta.label })]);
}

export function statusText(status) {
  const meta = STATUS_META[status] || { label: status, color: 'var(--text-muted)' };
  const span = el('span', { class: 'status-pill' }, [el('span', { text: meta.label })]);
  span.style.color = meta.color;
  return span;
}

export function avatar(name, { empty = false } = {}) {
  const label = initials(name);
  return el('span', {
    class: `avatar${empty || !label ? ' avatar-empty' : ''}`,
    text: label || '?',
    attrs: { title: name || 'Unassigned' },
  });
}

export function initials(name) {
  if (!name) return '';
  const parts = String(name).trim().split(/\s+/).slice(0, 2);
  return parts.map((part) => part[0]).join('').toUpperCase();
}

export function pointsBadge(points) {
  if (points === null || points === undefined) return null;
  return el('span', { class: 'points', text: points, attrs: { title: `${points} story points` } });
}

export function labelChips(labels, limit = 3) {
  const wrap = el('span', { class: 'ticket-labels' });
  (labels || []).slice(0, limit).forEach((label) => {
    wrap.appendChild(el('span', { class: 'label-chip', text: label }));
  });
  if ((labels || []).length > limit) {
    wrap.appendChild(el('span', { class: 'label-chip', text: `+${labels.length - limit}` }));
  }
  return wrap;
}

export function keyEl(issue) {
  return el('span', { class: 'ticket-key', text: issue.key });
}

export function dueEl(due) {
  if (!due) return null;
  const overdue = due < todayISO();
  return el('span', {
    class: `due${overdue ? ' due-overdue' : ''}`,
    text: `due ${formatDate(due)}`,
    attrs: { title: `Due ${due}` },
  });
}

/* ---- dates ----------------------------------------------------------- */

export function todayISO() {
  return new Date().toISOString().slice(0, 10);
}

export function formatDate(value) {
  if (!value) return '';
  const date = new Date(`${String(value).slice(0, 10)}T00:00:00`);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

export function relativeTime(value) {
  if (!value) return '';
  const then = new Date(value);
  if (Number.isNaN(then.getTime())) return '';
  const seconds = Math.round((Date.now() - then.getTime()) / 1000);
  if (seconds < 45) return 'just now';
  if (seconds < 90) return '1 min ago';
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days}d ago`;
  return formatDate(value);
}

/* ---- drag and drop --------------------------------------------------- */

let current = null;

export const drag = {
  start(payload) {
    current = payload;
  },
  end() {
    current = null;
  },
  get current() {
    return current;
  },
};

export function draggable(node, payload) {
  node.draggable = true;
  node.addEventListener('dragstart', (event) => {
    drag.start(payload);
    node.dataset.dragging = 'true';
    if (event.dataTransfer) {
      event.dataTransfer.effectAllowed = 'move';
      // Firefox will not start a drag without some payload set.
      try {
        event.dataTransfer.setData('text/plain', String(payload.id));
      } catch {
        /* ignore */
      }
    }
  });
  node.addEventListener('dragend', () => {
    drag.end();
    delete node.dataset.dragging;
  });
  return node;
}

export function insertionIndex(container, clientY, selector) {
  const items = [...container.querySelectorAll(selector)]
    .filter((node) => node.dataset.dragging !== 'true'
      && !node.classList.contains('drop-line'));
  let index = items.length;
  for (let i = 0; i < items.length; i += 1) {
    const box = items[i].getBoundingClientRect();
    if (clientY < box.top + box.height / 2) {
      index = i;
      break;
    }
  }
  return index;
}

export function showDropLine(container, index, selector = 'li.ticket') {
  removeDropLine(container);
  const line = el('li', { class: 'drop-line' });
  line.dataset.index = String(index);
  // Only match real draggable items, so decorative rows do not shift the
  // insertion point.
  const items = [...container.children].filter((node) => node.matches && node.matches(selector));
  container.insertBefore(line, items[index] || null);
  return line;
}

export function removeDropLine(container) {
  container.querySelectorAll('.drop-line').forEach((line) => line.remove());
}

export function dropLineIndex(container, fallback) {
  const line = container.querySelector('.drop-line');
  if (!line) return fallback;
  return Number(line.dataset.index);
}

/* ---- misc ------------------------------------------------------------ */

export function plural(count, singular, pluralForm) {
  return count === 1 ? singular : (pluralForm || `${singular}s`);
}

export function sumPoints(issues) {
  return issues.reduce((total, issue) => total + (issue.story_points || 0), 0);
}

/* Everyone a ticket can be assigned to.

   The accounts on this instance, plus any name the project already carries
   that is not an account -- a member from before accounts existed, or a name
   somebody typed. Offering only project members was the problem: a new project
   starts with its lead as its only member, so the assignee picker looked empty
   even though the workspace had people in it. */
export function assignableNames(state, project) {
  const names = new Set();
  ((state && state.people) || []).forEach((person) => {
    if (person && person.name) names.add(person.name);
  });
  ((project && project.members) || []).forEach((member) => {
    if (member && member.name) names.add(member.name);
  });
  return [...names].sort((a, b) => a.localeCompare(b));
}

export function button(label, { variant = 'quiet', onClick, icon: iconName, size = '', title } = {}) {
  const classes = ['btn', `btn-${variant}`];
  if (size) classes.push(`btn-${size}`);
  const node = el('button', {
    class: classes.join(' '),
    attrs: { type: 'button', title: title || null },
    on: onClick ? { click: onClick } : {},
  }, [iconName ? svgIcon(iconName, 14) : null, label ? el('span', { text: label }) : null]);
  return node;
}

export function select(options, value, onChange, extra = {}) {
  const node = el('select', {
    class: 'input',
    attrs: { ...extra },
    on: { change: (event) => onChange(event.target.value) },
  });
  options.forEach((option) => {
    const item = el('option', { text: option.label, attrs: { value: option.value } });
    if (String(option.value) === String(value)) item.selected = true;
    node.appendChild(item);
  });
  return node;
}

export function field(labelText, control) {
  return el('div', { class: 'field' }, [
    el('span', { class: 'field-label', text: labelText }),
    control,
  ]);
}
