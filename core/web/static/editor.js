// The editor's script. Without it the page still works: the buttons are real
// form buttons. This adds the formatting buttons, rows and columns to add and
// remove, and the warning before leaving a page whose text is not downloaded.
const form = document.getElementById("editor");

if (form) {
  let dirty = form.hasAttribute("data-dirty");
  let submitting = false;
  form.addEventListener("input", () => { dirty = true; });
  form.addEventListener("submit", () => {
    // Previewing or downloading is not leaving; the page is armed again after.
    submitting = true;
    setTimeout(() => { submitting = false; }, 1500);
  });
  window.addEventListener("beforeunload", (event) => {
    if (dirty && !submitting) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  // ── Text: markers inserted where the cursor is ──
  const toolbar = form.querySelector("[data-toolbar]");
  const box = form.querySelector("textarea");
  if (toolbar && box) {
    toolbar.hidden = false;
    toolbar.addEventListener("click", (event) => {
      const button = event.target instanceof Element ? event.target.closest("button") : null;
      if (!button) return;
      const { selectionStart: start, selectionEnd: end, value } = box;
      if (button.dataset.mark) {
        const mark = button.dataset.mark;
        box.setRangeText(mark + value.slice(start, end) + mark, start, end, "end");
      } else if (button.dataset.line) {
        const lineStart = value.lastIndexOf("\n", start - 1) + 1;
        box.setRangeText(button.dataset.line, lineStart, lineStart, "preserve");
        box.setSelectionRange(start + button.dataset.line.length, end + button.dataset.line.length);
      }
      box.focus();
      dirty = true;
    });
  }

  // ── Grid: rows and columns, named c{row}_{column} for the server ──
  const grid = form.querySelector("[data-grid]");
  const gridBar = form.querySelector("[data-grid-bar]");
  if (grid && gridBar) {
    gridBar.hidden = false;
    const maxRows = Number(grid.dataset.maxRows);
    const maxCols = Number(grid.dataset.maxCols);
    const maxCell = Number(grid.dataset.maxCell);
    const rowsField = form.querySelector("[name=linhas]");
    const colsField = form.querySelector("[name=colunas]");
    const body = grid.tBodies[0];

    const makeCell = (r, c) => {
      const cell = document.createElement("td");
      const input = document.createElement("input");
      input.name = `c${r}_${c}`;
      input.maxLength = maxCell;
      input.lang = "pt-BR";
      input.spellcheck = true;
      input.setAttribute("autocorrect", "on");
      input.setAttribute("autocapitalize", "sentences");
      input.setAttribute("aria-label", `Linha ${r + 1}, coluna ${String.fromCharCode(65 + c)}`);
      cell.append(input);
      return cell;
    };
    const counts = () => [body.rows.length, body.rows[0] ? body.rows[0].cells.length : 0];
    const publish = () => {
      const [rows, cols] = counts();
      rowsField.value = String(rows);
      colsField.value = String(cols);
      dirty = true;
    };

    gridBar.addEventListener("click", (event) => {
      const button = event.target instanceof Element ? event.target.closest("button") : null;
      if (!button) return;
      const [rows, cols] = counts();
      if (button.hasAttribute("data-add-row") && rows < maxRows) {
        const row = body.insertRow();
        for (let c = 0; c < cols; c += 1) row.append(makeCell(rows, c));
      } else if (button.hasAttribute("data-remove-row") && rows > 1) {
        body.deleteRow(rows - 1);
      } else if (button.hasAttribute("data-add-column") && cols < maxCols) {
        Array.from(body.rows).forEach((row, r) => row.append(makeCell(r, cols)));
      } else if (button.hasAttribute("data-remove-column") && cols > 1) {
        Array.from(body.rows).forEach((row) => row.deleteCell(cols - 1));
      } else {
        return;
      }
      publish();
    });
  }
}
