// Site-wide behaviour without inline handlers (the CSP is script-src 'self').
// <form data-confirm="..."> asks before submitting, e.g. before a paid engine test call.
document.addEventListener('submit', (ev) => {
  const msg = ev.target && ev.target.getAttribute && ev.target.getAttribute('data-confirm');
  if (msg && !window.confirm(msg)) ev.preventDefault();
}, true);
