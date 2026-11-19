// Brand page trend chart. Data comes from the JSON block in brand.html (no inline script: the CSP is script-src 'self').
window.addEventListener('load', () => {
  const el = document.getElementById('trend-data');
  if (!el || !window.Chart) return;
  const {labels, series, me} = JSON.parse(el.textContent);
  const colors = ['#D85427', '#242E41', '#6B7A99', '#9AA6BD', '#C3CAD8'];
  const ds = []; let i = 0;
  for (const [name, pts] of Object.entries(series)) {
    const c = colors[i++ % colors.length];
    if (name === me) {
      ds.push({label: me + ' high', data: pts.map(p => p.hi), borderWidth: 0, pointRadius: 0, fill: '+1', backgroundColor: 'rgba(216,84,39,.15)'});
      ds.push({label: me + ' low', data: pts.map(p => p.lo), borderWidth: 0, pointRadius: 0, fill: false});
    }
    ds.push({label: name, data: pts.map(p => p.rate), borderColor: c, backgroundColor: c, borderWidth: name === me ? 3 : 1.5, tension: .25, fill: false});
  }
  new Chart(document.getElementById('trend'), {type: 'line', data: {labels, datasets: ds},
    options: {plugins: {legend: {labels: {filter: i => !/ (high|low)$/.test(i.text)}}}, scales: {y: {min: 0, max: 100, ticks: {callback: v => v + '%'}}}}});
});
