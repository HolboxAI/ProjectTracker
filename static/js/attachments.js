/* Files on a comment: reading a picked file for upload, showing one that is
   already attached, and the chips for files still waiting to be sent.

   The bytes go up as base64 inside JSON rather than as multipart form data.
   The server is standard library only, and hand-rolling a multipart parser is
   a worse trade than a third of extra bytes on a screenshot. */

import { el, svgIcon, button } from './ui.js';

/* Mirrors storage.MAX_BYTES on the server, which is the authority. This copy
   exists so an oversized file is refused before it is read and base64'd --
   cheaper than a 413 after the work. */
export const MAX_BYTES = 8 * 1024 * 1024;

export function describeSize(bytes) {
  if (!bytes) return '0 KB';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/* A file as the upload route wants it: name, declared type, base64 body. */
export function readForUpload(file) {
  return new Promise((resolve, reject) => {
    if (file.size > MAX_BYTES) {
      reject(new Error(`${file.name} is larger than ${describeSize(MAX_BYTES)}`));
      return;
    }
    if (!file.size) {
      reject(new Error(`${file.name} is empty`));
      return;
    }
    const reader = new FileReader();
    reader.onerror = () => reject(new Error(`Could not read ${file.name}`));
    reader.onload = () => {
      const text = String(reader.result || '');
      const comma = text.indexOf(',');
      resolve({
        filename: file.name,
        content_type: file.type || 'application/octet-stream',
        data: comma === -1 ? '' : text.slice(comma + 1),
      });
    };
    reader.readAsDataURL(file);
  });
}

/* A file picked but not yet sent, with the way to change your mind. */
export function pendingChip(file, onRemove) {
  return el('span', { class: 'attach-chip' }, [
    svgIcon('paperclip', 12),
    el('span', { class: 'attach-name', text: file.name }),
    el('span', { class: 'attach-size', text: describeSize(file.size) }),
    button('', {
      variant: 'quiet',
      icon: 'close',
      size: 'sm',
      title: `Remove ${file.name}`,
      onClick: onRemove,
    }),
  ]);
}

/* One already-attached file. A verified image is drawn; anything else is a
   download chip, because the server sends it as an opaque attachment and an
   <img> tag pointed at it would only ever show a broken icon. */
export function attachmentChip(attachment) {
  const link = el('a', {
    class: attachment.is_image ? 'attachment-image' : 'attachment-file',
    attrs: {
      href: attachment.url,
      target: '_blank',
      rel: 'noopener noreferrer',
      title: `${attachment.filename} — ${describeSize(attachment.byte_size)}`,
    },
  });

  if (attachment.is_image) {
    link.appendChild(el('img', {
      attrs: { src: attachment.url, alt: attachment.filename, loading: 'lazy' },
    }));
    link.appendChild(el('span', { class: 'attachment-caption', text: attachment.filename }));
    return link;
  }

  link.appendChild(svgIcon('download', 13));
  link.appendChild(el('span', { class: 'attach-name', text: attachment.filename }));
  link.appendChild(el('span', {
    class: 'attach-size',
    text: describeSize(attachment.byte_size),
  }));
  return link;
}

export function attachmentList(attachments) {
  if (!attachments || !attachments.length) return null;
  return el('div', { class: 'attachments' }, attachments.map(attachmentChip));
}
