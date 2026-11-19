// Brand page trend chart. Data comes from the JSON block in brand.html (no inline script: the CSP is script-src 'self').
// Farview palette: the portco in orange, competitors in navy tints, so the eye goes to the one line that matters.
window.addEventListener('load', () => {
  const el = document.getElementById('trend-data');
  if (!el || !window.Chart) return;
  const {labels, series, me} = JSON.parse(el.textContent);
  Chart.defaults.font.family = '"Helvetica Neue", Arial, sans-serif';
  Chart.defaults.font.size = 12;
  Chart.defaults.color = '#6E6D6C';
  const comp = ['#242E41', '#515150', '#7C8698', '#A3ABB9', '#C9CED8'];
  const ds = []; let i = 0;
  for (const [name, pts] of Object.entries(series)) {
    if (name === me) {
      ds.push({label: me + ' high', data: pts.map(p => p.hi), borderWidth: 0, pointRadius: 0, fill: '+1', backgroundColor: 'rgba(216,84,39,.12)', order: 3});
      ds.push({label: me + ' low', data: pts.map(p => p.lo), borderWidth: 0, pointRadius: 0, fill: false, order: 3});
      ds.push({label: name, data: pts.map(p => p.rate), borderColor: '#D85427', backgroundColor: '#D85427', borderWidth: 3,
               pointRadius: 3.5, pointHoverRadius: 6, tension: .3, fill: false, order: 1});
    } else {
      const c = comp[i++ % comp.length];
      ds.push({label: name, data: pts.map(p => p.rate), borderColor: c, backgroundColor: c, borderWidth: 1.6,
               pointRadius: 0, pointHoverRadius: 4, tension: .3, fill: false, order: 2});
    }
  }
  new Chart(document.getElementById('trend'), {
    type: 'line', data: {labels, datasets: ds},
    options: {
      maintainAspectRatio: false, interaction: {mode: 'index', intersect: false},
      plugins: {
        legend: {position: 'bottom', labels: {usePointStyle: true, pointStyle: 'circle', boxWidth: 8, boxHeight: 8, padding: 16,
                 filter: it => !/ (high|low)$/.test(it.text)}},
        tooltip: {backgroundColor: '#242E41', padding: 10, cornerRadius: 8, boxPadding: 4,
                  filter: it => !/ (high|low)$/.test(it.dataset.label),
                  callbacks: {label: c => ` ${c.dataset.label}: ${Math.round(c.parsed.y)}%`}}
      },
      scales: {
        y: {min: 0, max: 100, ticks: {callback: v => v + '%', stepSize: 25}, grid: {color: '#EFEBE9'}, border: {display: false}},
        x: {grid: {display: false}, border: {color: '#E6E1DF'}}
      }
    }
  });
});
