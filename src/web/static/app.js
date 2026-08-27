// Live task updates over SSE: append log lines, refresh the task panel.
(function () {
  const log = document.getElementById("log");
  const MAX_LINES = 400;

  function appendLog(line) {
    if (!log) return;
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    log.textContent += (log.textContent ? "\n" : "") + line;

    const lines = log.textContent.split("\n");
    if (lines.length > MAX_LINES) {
      log.textContent = lines.slice(lines.length - MAX_LINES).join("\n");
    }
    if (atBottom) log.scrollTop = log.scrollHeight;
  }

  function refreshPanel() {
    if (window.htmx) {
      window.htmx.ajax("GET", "/partials/status", { target: "#task-panel", swap: "outerHTML" });
    }
  }

  function updateProgress(task) {
    const bar = document.getElementById("task-bar");
    const counter = document.getElementById("task-counter");
    const message = document.getElementById("task-message");
    if (bar) bar.style.width = task.percent + "%";
    if (counter) counter.textContent = task.done + " / " + task.total;
    if (message && task.message) message.textContent = task.message;
  }

  function connect() {
    const source = new EventSource("/actions/events");

    source.onmessage = function (event) {
      let data;
      try {
        data = JSON.parse(event.data);
      } catch (e) {
        return;
      }

      if (data.type === "snapshot") {
        // The server replays the running task's log right after connect —
        // drop what the page was rendered with so lines are not doubled.
        if (log) log.textContent = "";
        if (data.task) updateProgress(data.task);
      } else if (data.type === "log") {
        appendLog(data.line);
        if (data.task) updateProgress(data.task);
      } else if (data.type === "progress") {
        if (data.task) updateProgress(data.task);
        // The panel itself changes when a job starts waiting for confirmation.
        if (data.task && data.task.awaiting_confirmation) refreshPanel();
      } else if (data.type === "started" || data.type === "finished" || data.type === "stopping") {
        refreshPanel();
        if (data.type === "finished") {
          // Numbers on the current page are stale once a job finishes.
          setTimeout(function () { window.location.reload(); }, 1200);
        }
      }
    };

    source.onerror = function () {
      source.close();
      setTimeout(connect, 3000); // the server restarts during development
    };
  }

  connect();

  // The task panel is re-rendered on every event, so remember what the user
  // picked in the runner and restore it after each swap.
  const RUNNER_KEY = "job-bless.runner-kind";

  function restoreRunnerChoice() {
    const saved = localStorage.getItem(RUNNER_KEY);
    if (!saved) return;
    const option = document.querySelector('.runner input[name="kind"][value="' + saved + '"]');
    if (option) option.checked = true;
  }

  document.addEventListener("change", function (event) {
    if (event.target.name === "kind") {
      localStorage.setItem(RUNNER_KEY, event.target.value);
    }
  });

  document.body.addEventListener("htmx:afterSwap", restoreRunnerChoice);
  restoreRunnerChoice();

  // "select all" checkbox on the vacancies page
  const checkAll = document.getElementById("check-all");
  if (checkAll) {
    checkAll.addEventListener("change", function () {
      document.querySelectorAll(".row-check").forEach(function (box) {
        box.checked = checkAll.checked;
      });
    });
  }
})();
