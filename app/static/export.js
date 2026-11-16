/* 调整单与导出页交互：下载回执、摘要复制 */
const state = {
  lastKind: "",
};

function setNote(text, isError) {
  const node = document.getElementById("export-status");
  if (!node) return;
  node.textContent = text;
  node.classList.toggle("bad", Boolean(isError));
}

function bindDownloads() {
  const bar = document.getElementById("export-links");
  if (!bar) return;
  const links = bar.querySelectorAll("a");
  links.forEach((link) => {
    link.addEventListener("click", () => {
      state.lastKind = link.textContent.trim();
      setNote(`下载 ${state.lastKind}`, false);
    });
  });
}

function bindDigests() {
  const cells = document.querySelectorAll("td[data-sha]");
  cells.forEach((cell) => {
    cell.title = cell.dataset.sha;
    cell.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(cell.dataset.sha);
        setNote("sha256 已复制", false);
      } catch (error) {
        setNote("剪贴板不可用", true);
      }
    });
  });
}

document.addEventListener("DOMContentLoaded", () => {
  bindDownloads();
  bindDigests();
});
