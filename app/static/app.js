/* 导入页交互：选择样例 → 触发导入 → 结果回显 */
const state = {
  busy: false,
  message: "",
};

function setStatus(text, isError) {
  const node = document.getElementById("import-status");
  if (!node) return;
  node.textContent = text;
  node.classList.toggle("bad", Boolean(isError));
  state.message = text;
}

function selectedSamples() {
  const boxes = document.querySelectorAll('input[name="sample"]');
  const names = [];
  boxes.forEach((box) => {
    if (box.checked) names.push(box.value);
  });
  return names;
}

async function runImport() {
  if (state.busy) return;
  const files = selectedSamples();
  if (files.length === 0) {
    setStatus("先勾选至少一个文件", true);
    return;
  }
  const button = document.getElementById("run-import");
  state.busy = true;
  if (button) button.disabled = true;
  setStatus("导入中");
  try {
    const response = await fetch("/api/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ files }),
    });
    const data = await response.json();
    if (!response.ok) {
      setStatus(data.message || "导入失败", true);
      return;
    }
    setStatus(`批次 ${data.batch_id} · 采纳 ${data.counters.accepted} 行`);
    window.setTimeout(() => window.location.reload(), 400);
  } catch (error) {
    setStatus("网络异常，未完成导入", true);
  } finally {
    state.busy = false;
    if (button) button.disabled = false;
  }
}

async function reloadSamples() {
  const button = document.getElementById("reload-samples");
  if (button) button.disabled = true;
  try {
    const response = await fetch("/api/samples");
    const data = await response.json();
    setStatus(`${data.files.length} 个样例文件`);
  } catch (error) {
    setStatus("样例清单读取失败", true);
  } finally {
    if (button) button.disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const run = document.getElementById("run-import");
  if (run) run.addEventListener("click", runImport);
  const reload = document.getElementById("reload-samples");
  if (reload) reload.addEventListener("click", reloadSamples);
});
