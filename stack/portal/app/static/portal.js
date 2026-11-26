const root = document.documentElement;

function setTheme(theme) {
  if (theme === "system") delete root.dataset.theme;
  else root.dataset.theme = theme;
  try {
    localStorage.setItem("devhub-theme", theme);
  } catch {
    // The selection still applies to this page.
  }
  for (const select of document.querySelectorAll("[data-theme-select]")) select.value = theme;
}

for (const select of document.querySelectorAll("[data-theme-select]")) {
  select.value = root.dataset.theme || "system";
  select.closest("[data-theme-picker]").hidden = false;
  select.addEventListener("change", () => setTheme(select.value));
}

for (const menu of document.querySelectorAll("[data-menu]")) {
  menu.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !menu.open) return;
    menu.open = false;
    menu.querySelector("summary").focus();
    event.stopPropagation();
  });
}

for (const input of document.querySelectorAll("[data-filter]")) {
  const table = document.getElementById(input.dataset.filter);
  const markedRows = [...table.querySelectorAll("[data-filter-row]")];
  const rows = markedRows.length ? markedRows : [...table.rows].filter((row) =>
    row.cells[0]?.tagName === "TD" && row.cells.length > 1);
  if (!rows.length) continue;
  let empty = table.querySelector("[data-filter-empty]");
  if (!empty) {
    empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "No results match this filter.";
    empty.hidden = true;
    table.after(empty);
  }
  input.closest("[data-filter-controls]").hidden = false;
  input.addEventListener("input", () => {
    const query = input.value.trim().toLowerCase();
    let matches = 0;
    for (const row of rows) {
      row.hidden = !(row.dataset.filterRow || row.textContent).toLowerCase().includes(query);
      if (!row.hidden) matches++;
    }
    empty.hidden = matches > 0 || !query;
  });
}

document.addEventListener("submit", (event) => {
  const message = event.submitter?.dataset.confirm || event.target.dataset.confirm;
  if (message && !window.confirm(message)) event.preventDefault();
});
