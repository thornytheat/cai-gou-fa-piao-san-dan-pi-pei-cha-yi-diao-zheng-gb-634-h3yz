/* 对照页与批次页交互：客户端筛选（只做展示）、重跑请求 */
const state = {
  filter: "",
  busy: false,
};

function setNote(id, text, isError) {
  const node = document.getElementById(id);
  if (!node) return;
  node.textContent = text;
  node.classList.toggle("bad", Boolean(isError));
}

function applyFilter(verdict) {
  state.filter = verdict;
  const rows = document.querySelectorAll(".crow");
  let shown = 0;
  rows.forEach((row) => {
    const keep = !verdict || row.dataset.verdict === verdict;
    row.style.display = keep ? "" : "none";
    if (keep) shown += 1;
  });
  setNote("match-status", `${shown} 行`);
}

function bindFilters() {
  const bar = document.getElementById("verdict-filter");
  if (!bar) return;
  const links = bar.querySelectorAll("a");
  links.forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      const target = new URL(link.href, window.location.origin);
      const verdict = target.searchParams.get("verdict") || "";
      links.forEach((other) => other.classList.remove("on"));
      link.classList.add("on");
      applyFilter(verdict);
      window.history.replaceState({}, "", target.toString());
    });
  });
}

async function rerunBatch(button) {
  if (state.busy) return;
  state.busy = true;
  const batchId = button.dataset.batch;
  button.disabled = true;
  setNote("rerun-status", "重跑中");
  try {
    const response = await fetch(button.dataset.url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    const data = await response.json();
    if (!response.ok) {
      setNote("rerun-status", data.message || "重跑被拒绝", true);
      return;
    }
    setNote("rerun-status", data.same_batch ? `复用 ${data.batch_id}` : `新批次 ${data.batch_id}`);
  } catch (error) {
    setNote("rerun-status", "网络异常，重跑未完成", true);
  } finally {
    state.busy = false;
    button.disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  bindFilters();
  const buttons = document.querySelectorAll("button.rerun");
  buttons.forEach((button) => {
    button.addEventListener("click", () => rerunBatch(button));
  });
});
