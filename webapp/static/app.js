(() => {
  const search = document.getElementById('tool-search');
  const category = document.getElementById('category-filter');
  const chips = document.getElementById('category-chips');
  const cards = [...document.querySelectorAll('.tool-card')];
  const count = document.getElementById('visible-count');
  const empty = document.getElementById('empty-state');
  if (!search || !category) return;

  function filter() {
    const q = search.value.trim().toLowerCase();
    const c = category.value;
    let shown = 0;
    cards.forEach(card => {
      const matches = (!q || card.dataset.search.includes(q)) && (!c || card.dataset.category === c);
      card.hidden = !matches;
      if (matches) shown++;
    });
    count.textContent = shown;
    empty.hidden = shown !== 0;
    if (chips) chips.querySelectorAll('.chip').forEach(chip => chip.classList.toggle('selected', chip.dataset.category === c));
  }
  search.addEventListener('input', filter);
  category.addEventListener('change', filter);
  if (chips) chips.addEventListener('click', event => {
    const chip = event.target.closest('.chip');
    if (!chip) return;
    category.value = chip.dataset.category;
    filter();
  });
})();
