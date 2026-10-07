// Masonry for a CSS grid: each card spans as many thin rows as it is tall, so a short card moves up into the space
// beside a tall one instead of leaving a gap. DOM order, and with it drag-and-drop ordering, is untouched.
// Row spans are set through the CSSOM (element.style), which the page's CSP allows.

const ROW = 4;   // px, must match grid-auto-rows of .board.masonry in app.css
const GAP = 12;  // px, must match the board's gap

export function masonry(board) {
  if (typeof ResizeObserver === "undefined" || typeof MutationObserver === "undefined") return;          // without it the board stays a plain grid
  board.classList.add("masonry");
  const span = (card) => {
    const h = card.getBoundingClientRect().height;
    if (h > 0) card.style.gridRowEnd = `span ${Math.ceil((h + GAP) / ROW)}`;
  };
  const ro = new ResizeObserver((entries) => entries.forEach((e) => span(e.target)));
  const watch = () => [...board.children].forEach((c) => { if (!c.__masonry) { c.__masonry = true; ro.observe(c); } });
  watch();
  new MutationObserver(watch).observe(board, { childList: true });
}
