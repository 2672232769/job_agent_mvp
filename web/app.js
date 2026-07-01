const state = {
  syllabi: [],
  jobs: [],
  selectedSyllabi: new Set(),
  activeJobId: null,
  latestResult: null,
  latestSearchPlan: null,
  kgStatus: null,
  latestKgTask: null,
  kgTaskPollTimer: null,
  kgGraph: null,
  activePanel: "syllabus-panel",
};

const $ = (selector) => document.querySelector(selector);
const API_BASE = (window.JOB_AGENT_API_BASE || "").replace(/\/$/, "");
const delay = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms));

function showToast(message, isError = false) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.style.background = isError ? "#8b2d2a" : "#142220";
  toast.classList.add("show");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("show"), 3200);
}

function setActivePanel(panelId) {
  state.activePanel = panelId;
  document.querySelectorAll(".panel").forEach((panel) => {
    panel.classList.toggle("active-panel", panel.id === panelId);
  });
  document.querySelectorAll(".nav-item").forEach((item) => {
    item.classList.toggle("active", item.dataset.target === panelId);
  });
  window.location.hash = panelId.replace("-panel", "");
}

async function api(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, options);
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const detail = payload && payload.detail ? payload.detail : payload;
    throw new Error(detail || `请求失败：${response.status}`);
  }
  return payload;
}

function listText(items, limit = 3) {
  if (!items || !items.length) return "";
  return items.slice(0, limit).join("、") + (items.length > limit ? ` 等 ${items.length} 项` : "");
}

async function loadStats() {
  const stats = await api("/api/stats");
  $("#statSyllabi").textContent = stats.syllabus_count || 0;
  $("#statJobs").textContent = stats.job_count || 0;
  $("#statMatches").textContent = stats.match_run_count || 0;
}

async function loadKnowledgeGraphStatus() {
  const status = await api("/api/knowledge-graph/status");
  state.kgStatus = status;
  renderKnowledgeGraphStatus(status);
}

function renderKnowledgeGraphStatus(status) {
  const sqlite = status.sqlite || {};
  const extracted = status.extracted || {};
  const embeddings = status.embeddings || {};
  const neo4j = status.neo4j || {};
  $("#kgSqliteStats").innerHTML = `
    ${renderKgMetric("课程大纲", sqlite.syllabi || 0)}
    ${renderKgMetric("岗位", sqlite.jobs || 0)}
    ${renderKgMetric("完整 JD", sqlite.detailed_jobs || 0)}
    ${renderKgMetric("待构建岗位", sqlite.pending_job_graph_jobs || 0)}
    ${renderKgMetric("课程节点", extracted.course_node_count || 0)}
    ${renderKgMetric("岗位要求节点", extracted.job_requirement_count || 0)}
    ${renderKgMetric("课程向量", embeddings.course_node_embeddings || 0)}
    ${renderKgMetric("岗位要求向量", embeddings.job_requirement_embeddings || 0)}
    ${renderKgMetric("向量模型", embeddings.model || "-")}
    ${renderKgMetric("匹配关系边", extracted.graph_edge_count || 0)}
  `;

  const neo4jCounts = neo4j.counts || {};
  const configText = neo4j.configured ? (neo4j.connected ? "已连接" : "已配置但未连接") : "未配置";
  $("#kgNeo4jStats").innerHTML = `
    ${renderKgMetric("状态", configText)}
    ${renderKgMetric("数据库", neo4j.database || "-")}
    ${renderKgMetric("课程", neo4jCounts.syllabi || 0)}
    ${renderKgMetric("课程节点", neo4jCounts.course_nodes || 0)}
    ${renderKgMetric("岗位", neo4jCounts.jobs || 0)}
    ${renderKgMetric("岗位要求节点", neo4jCounts.job_requirements || 0)}
    ${renderKgMetric("关系", neo4jCounts.relationships || 0)}
    ${neo4j.error ? `<div class="kg-error">${escapeHtml(neo4j.error)}</div>` : ""}
  `;
}

function renderKgMetric(label, value) {
  return `
    <div class="kg-metric">
      <span>${escapeHtml(value)}</span>
      <label>${escapeHtml(label)}</label>
    </div>
  `;
}

function setKnowledgeGraphTaskStatus(message, stateName = "idle") {
  const box = $("#kgTaskStatus");
  if (!box) return;
  box.classList.remove("is-running", "is-error", "is-success");
  if (stateName === "running") box.classList.add("is-running");
  if (stateName === "error") box.classList.add("is-error");
  if (stateName === "success") box.classList.add("is-success");
  box.innerHTML = `<strong>任务状态</strong><span>${escapeHtml(message)}</span>`;
}

function renderKnowledgeGraphTask(task) {
  const box = $("#kgTaskStatus");
  if (!box) return;
  state.latestKgTask = task;
  const status = task.status || "queued";
  const stateName = status === "failed" ? "error" : status === "running" || status === "queued" ? "running" : "success";
  box.classList.remove("is-running", "is-error", "is-success");
  if (stateName === "running") box.classList.add("is-running");
  if (stateName === "error") box.classList.add("is-error");
  if (stateName === "success") box.classList.add("is-success");

  const total = Number(task.total || 0);
  const processed = Number(task.processed || 0);
  const percent = total ? Math.round((processed / total) * 100) : 100;
  const errors = task.errors || [];
  const statusText = {
    queued: "排队中",
    running: "运行中",
    succeeded: "已完成",
    failed: "失败",
    completed_with_errors: "部分完成",
  }[status] || status;

  box.innerHTML = `
    <div class="kg-task-head">
      <strong>岗位图谱后台任务 #${escapeHtml(task.id || "")}</strong>
      <span>${escapeHtml(statusText)}</span>
    </div>
    <div class="kg-progress">
      <div class="kg-progress-bar" style="width:${Math.max(0, Math.min(percent, 100))}%"></div>
    </div>
    <div class="kg-task-line">
      进度 ${processed}/${total}，成功 ${task.succeeded || 0}，失败 ${task.failed || 0}
      ${task.current_job_title ? `，当前：${escapeHtml(task.current_job_title)}` : ""}
    </div>
    <div class="kg-task-message">${escapeHtml(task.message || "")}</div>
    ${
      errors.length
        ? `<div class="kg-task-errors">
            ${errors
              .slice(-5)
              .map((item) => `<div>岗位 #${escapeHtml(item.job_id || "")} ${escapeHtml(item.job || "")}：${escapeHtml(item.error || "")}</div>`)
              .join("")}
            <button class="secondary-button compact-button" type="button" data-retry-kg-task="${escapeHtml(task.id || "")}">重试失败岗位</button>
          </div>`
        : ""
    }
  `;
}

async function loadHealth() {
  try {
    const health = await api("/api/health");
    if (!health || health.ok !== true) throw new Error("Invalid health response");
    $("#healthText").textContent = "后端已连接";
  } catch {
    $("#healthText").textContent = "后端未连接";
  }
}

async function loadSyllabi() {
  const data = await api("/api/syllabi");
  state.syllabi = data.items || [];
  renderSyllabi();
  renderSelectedCourses();
  renderJobCrawlSelectedCourses();
}

function renderSyllabi() {
  const list = $("#syllabusList");
  if (!state.syllabi.length) {
    list.innerHTML = '<div class="empty-state">还没有课程大纲。请先上传 PDF、DOCX、TXT 或 MD 文件。</div>';
    renderMatchCourseList();
    renderJobCourseList();
    return;
  }

  list.innerHTML = state.syllabi
    .map((item) => {
      const profile = item.profile;
      const abilityText = profile ? listText(profile.abilities, 2) : "尚未分析课程能力";
      const directions = profile ? (profile.job_directions || []).slice(0, 3) : [];
      const selected = state.selectedSyllabi.has(item.id);
      return `
        <article class="course-item ${selected ? "selected" : ""}">
          <div class="course-top">
            <div>
              <div class="course-title">${escapeHtml(item.title)}</div>
              <div class="course-meta">${escapeHtml(item.file_name)} · ${item.text_length || 0} 字</div>
            </div>
            <input type="checkbox" ${selected ? "checked" : ""} data-select-syllabus="${item.id}" aria-label="选择课程" />
          </div>
          <div class="course-meta">${escapeHtml(abilityText)}</div>
          <div class="tag-row">
            ${directions.map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("")}
          </div>
          <div class="course-actions">
            <button data-view-profile="${item.id}">查看画像</button>
            <button data-analyze-syllabus="${item.id}">重新分析</button>
          </div>
        </article>
      `;
    })
    .join("");
  renderMatchCourseList();
  renderJobCourseList();
}

async function loadJobs() {
  const q = encodeURIComponent($("#jobSearchInput").value.trim());
  const data = await api(`/api/jobs?limit=300&q=${q}`);
  state.jobs = data.items || [];
  renderJobs();
}

function renderJobs() {
  const tbody = $("#jobTableBody");
  if (!state.jobs.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="empty-state">岗位池为空，或没有符合搜索条件的岗位。</td></tr>';
    return;
  }

  tbody.innerHTML = state.jobs
    .map(
      (job) => `
      <tr data-job-id="${job.id}" class="${state.activeJobId === job.id ? "active" : ""}">
        <td>
          <div class="job-title">${escapeHtml(job.title)}</div>
          <div class="job-sub">#${job.id} · ${escapeHtml(job.source || "")} · ${escapeHtml(job.city || "")} · ${escapeHtml(job.salary || "")}</div>
        </td>
        <td>${escapeHtml(job.company || "")}</td>
        <td>${escapeHtml(job.city || "")}</td>
        <td>${escapeHtml(job.salary || "")}</td>
      </tr>
    `,
    )
    .join("");
}

async function showJobDetail(jobId) {
  state.activeJobId = jobId;
  renderJobs();
  const job = await api(`/api/jobs/${jobId}`);
  $("#jobDetail").innerHTML = `
    <h3>${escapeHtml(job.title)}</h3>
    <div class="detail-meta">
      <span class="meta-chip">${escapeHtml(job.company || "")}</span>
      <span class="meta-chip">${escapeHtml(job.city || "城市不限")}</span>
      <span class="meta-chip">${escapeHtml(job.salary || "薪资未标注")}</span>
      <span class="meta-chip">${escapeHtml(job.education || "学历不限")}</span>
      <span class="meta-chip">${escapeHtml(job.experience || "经验不限")}</span>
    </div>
    <div class="description">${escapeHtml(job.description || "暂无完整岗位描述")}</div>
  `;
}

function renderSelectedCourses() {
  const selected = state.syllabi.filter((item) => state.selectedSyllabi.has(item.id));
  const box = $("#selectedCourses");
  if (!selected.length) {
    box.textContent = "暂未选择课程。";
    return;
  }
  box.innerHTML = selected.map((item) => `<span class="selected-pill">${escapeHtml(item.title)}</span>`).join("");
}

function renderJobCrawlSelectedCourses() {
  const box = $("#jobCrawlSelectedCourses");
  if (!box) return;
  const selected = state.syllabi.filter((item) => state.selectedSyllabi.has(item.id));
  if (!selected.length) {
    box.textContent = "暂未选择课程。";
    return;
  }
  box.innerHTML = selected.map((item) => `<span class="selected-pill">${escapeHtml(item.title)}</span>`).join("");
}

function filterSyllabiForPicker(searchInputId) {
  const input = document.getElementById(searchInputId);
  const keyword = (input?.value || "").trim().toLowerCase();
  return state.syllabi.filter((item) => {
    if (!keyword) return true;
    const profile = item.profile || {};
    const haystack = [
      item.title,
      item.file_name,
      ...(profile.abilities || []),
      ...(profile.knowledge_points || []),
      ...(profile.job_directions || []),
      ...(profile.technologies_tools_methods || []),
    ]
      .join(" ")
      .toLowerCase();
    return haystack.includes(keyword);
  });
}

function renderCoursePickerList(listId, searchInputId, className, emptyText) {
  const list = document.getElementById(listId);
  if (!list) return;
  const items = filterSyllabiForPicker(searchInputId);

  if (!items.length) {
    list.innerHTML = `<div class="empty-state">${escapeHtml(emptyText)}</div>`;
    return;
  }

  list.innerHTML = items
    .map((item) => {
      const profile = item.profile;
      const directions = profile ? (profile.job_directions || []).slice(0, 3) : [];
      const selected = state.selectedSyllabi.has(item.id);
      return `
        <article class="${className} ${selected ? "selected" : ""}" data-toggle-syllabus="${item.id}">
          <div class="course-top">
            <div>
              <div class="course-title">${escapeHtml(item.title)}</div>
              <div class="course-meta">${escapeHtml(item.file_name)} · ${item.text_length || 0} 字</div>
            </div>
            <input type="checkbox" ${selected ? "checked" : ""} data-select-syllabus="${item.id}" aria-label="选择课程" />
          </div>
          <div class="course-meta">${escapeHtml(profile?.summary ? profile.summary.slice(0, 80) : "尚未分析课程能力")}</div>
          <div class="tag-row">
            ${directions.map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("")}
          </div>
        </article>
      `;
    })
    .join("");
}

function renderMatchCourseList() {
  renderCoursePickerList("matchCourseList", "courseSearchInput", "match-course-option", "没有找到可选课程。");
}

function renderJobCourseList() {
  renderCoursePickerList("jobCourseList", "jobCourseSearchInput", "job-course-option", "没有找到可用于抓取的课程。");
}

async function runMatch() {
  const syllabusIds = [...state.selectedSyllabi];
  if (!syllabusIds.length) {
    showToast("请先选择至少一门课程。", true);
    return;
  }

  setActivePanel("match-panel");
  const button = $("#runMatchBtn");
  button.disabled = true;
  button.textContent = "匹配中";
  $("#matchResults").innerHTML = '<div class="empty-state">正在读取已构建的课程图谱和岗位图谱，生成匹配解释。</div>';

  try {
    const result = await api("/api/match", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        syllabus_ids: syllabusIds,
        limit: Number($("#matchLimitInput").value || 5),
        candidate_limit: Number($("#candidateLimitInput").value || 60),
        batch_size: 12,
      }),
    });
    state.latestResult = result;
    renderMatchResults(result);
    await loadStats();
    showToast(`匹配完成，已保存记录 #${result.match_run_id}`);
  } catch (error) {
    $("#matchResults").innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
    showToast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "开始匹配";
  }
}

async function previewGraphragEvidence() {
  const syllabusIds = [...state.selectedSyllabi];
  if (!syllabusIds.length) {
    showToast("请先选择至少一门课程。", true);
    return;
  }
  setActivePanel("match-panel");
  const button = $("#previewEvidenceBtn");
  button.disabled = true;
  button.textContent = "检索中";
  $("#matchResults").innerHTML = '<div class="empty-state">正在从 Neo4j 检索课程能力与岗位要求的图谱证据。</div>';
  try {
    const payload = await api("/api/graphrag/evidence", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        syllabus_ids: syllabusIds,
        candidate_limit: Number($("#candidateLimitInput").value || 24),
      }),
    });
    renderGraphragEvidence(payload.items || []);
    showToast(`GraphRAG 召回 ${payload.count || 0} 个候选岗位`);
  } catch (error) {
    $("#matchResults").innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
    showToast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "预览图谱证据";
  }
}

function renderGraphragEvidence(items) {
  if (!items.length) {
    $("#matchResults").innerHTML = '<div class="empty-state">没有从 Neo4j 检索到课程能力与岗位要求的证据路径。</div>';
    return;
  }
  $("#matchResults").innerHTML = items
    .map((item, index) => {
      const job = item.job || {};
      const paths = item.evidence_paths || [];
      return `
        <article class="match-item evidence-item">
          <h3>${index + 1}. ${escapeHtml(job.title || `岗位 #${item.job_id}`)}</h3>
          <div class="match-company">${escapeHtml(job.company || "")}${job.city ? ` · ${escapeHtml(job.city)}` : ""}${job.salary ? ` · ${escapeHtml(job.salary)}` : ""}</div>
          ${job.url ? `<a class="link-button" href="${escapeHtml(job.url)}" target="_blank" rel="noopener noreferrer">打开岗位链接</a>` : ""}
          <div class="match-block">
            <strong>GraphRAG 证据路径</strong>
            <div class="requirement-evidence-list">
              ${paths
                .slice(0, 8)
                .map(
                  (path) => `
                    <div class="requirement-evidence">
                      <div class="requirement-line">${escapeHtml(path.requirement_text || path.requirement_name || "")}</div>
                      <div class="edge-rationale">
                        召回来源：${escapeHtml((path.retrieval_sources || []).join(" + ") || "-")}
                        · 混合 ${escapeHtml(formatNumber(path.hybrid_score))}
                        · 全文 ${escapeHtml(formatNumber(path.fulltext_score))}
                        · 向量 ${escapeHtml(formatNumber(path.vector_score))}
                      </div>
                      <div class="course-evidence">
                        <span>${escapeHtml(path.syllabus_title || "课程")} · ${escapeHtml(path.course_node_name || "")}</span>
                        ${escapeHtml(path.course_evidence || path.course_description || "")}
                      </div>
                      <div class="edge-rationale">${escapeHtml((path.path || []).join(" -> "))}</div>
                    </div>
                  `,
                )
                .join("")}
            </div>
          </div>
        </article>
      `;
    })
    .join("");
}

async function buildCourseKnowledgeGraph(allCourses = false) {
  const syllabusIds = allCourses ? [] : [...state.selectedSyllabi];
  if (!allCourses && !syllabusIds.length) {
    showToast("请先选择至少一门课程，或点击构建全部课程图谱。", true);
    return;
  }
  const button = allCourses ? $("#buildAllCourseKgBtn") : $("#buildCourseKgBtn");
  button.disabled = true;
  button.textContent = "构建中";
  setKnowledgeGraphTaskStatus(
    allCourses
      ? "正在构建全部课程图谱，并同步到 Neo4j。"
      : `正在构建 ${syllabusIds.length} 门已选课程图谱，并同步到 Neo4j。`,
    "running",
  );
  try {
    const payload = await api("/api/knowledge-graph/build-courses", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        syllabus_ids: syllabusIds,
        force: $("#kgCourseForceInput").checked,
      }),
    });
    await Promise.allSettled([loadStats(), loadKnowledgeGraphStatus()]);
    setKnowledgeGraphTaskStatus(
      `课程图谱完成：处理 ${payload.syllabus_ids?.length || 0} 门课程，生成/同步 ${payload.course_node_count || 0} 个课程节点和 ${payload.embedding_count || 0} 个向量。`,
      "success",
    );
    showToast(`课程图谱完成：${payload.course_node_count || 0} 个课程节点`);
  } catch (error) {
    setKnowledgeGraphTaskStatus(`课程图谱构建失败：${error.message}`, "error");
    showToast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = allCourses ? "构建全部课程图谱" : "构建已选课程图谱";
  }
}

async function buildJobKnowledgeGraph() {
  const button = $("#buildJobKgBtn");
  button.disabled = true;
  button.textContent = "启动中";
  const limit = Number($("#kgJobLimitInput").value || 30);
  const force = $("#kgJobForceInput").checked;
  setKnowledgeGraphTaskStatus(
    force
      ? `正在启动强制重建任务：重新处理最近 ${limit} 个完整 JD 岗位。`
      : `正在启动岗位图谱任务：从未完成队列继续处理 ${limit} 个岗位。`,
    "running",
  );
  try {
    const task = await api("/api/knowledge-graph/build-jobs/task", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        limit,
        force,
      }),
    });
    renderKnowledgeGraphTask(task);
    button.textContent = "构建中";
    showToast(`岗位图谱任务已启动：#${task.id}`);
    const finalTask = await pollKnowledgeGraphTask(task.id);
    await Promise.allSettled([loadStats(), loadKnowledgeGraphStatus()]);
    const result = finalTask.result || {};
    if (finalTask.status === "failed") {
      showToast(`岗位图谱任务失败：${finalTask.message || "请查看任务错误"}`, true);
    } else if (finalTask.status === "completed_with_errors") {
      showToast(`岗位图谱部分完成：成功 ${finalTask.succeeded || 0}，失败 ${finalTask.failed || 0}`, true);
    } else {
      showToast(`岗位图谱完成：${result.requirement_count || 0} 个岗位要求节点`);
    }
  } catch (error) {
    setKnowledgeGraphTaskStatus(`岗位图谱构建失败：${error.message}`, "error");
    showToast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "启动岗位图谱任务";
  }
}

async function pollKnowledgeGraphTask(taskId) {
  let task = null;
  while (true) {
    task = await api(`/api/knowledge-graph/tasks/${encodeURIComponent(taskId)}`);
    renderKnowledgeGraphTask(task);
    if (["succeeded", "failed", "completed_with_errors"].includes(task.status)) {
      return task;
    }
    await delay(1500);
  }
}

async function retryFailedKnowledgeGraphTask(taskId) {
  const sourceTask =
    state.latestKgTask && state.latestKgTask.id === taskId
      ? state.latestKgTask
      : await api(`/api/knowledge-graph/tasks/${encodeURIComponent(taskId)}`);
  const failedJobIds = [
    ...new Set((sourceTask.errors || []).map((item) => Number(item.job_id)).filter((id) => Number.isInteger(id) && id > 0)),
  ];
  if (!failedJobIds.length) {
    showToast("这个任务没有可重试的失败岗位。", true);
    return;
  }
  setKnowledgeGraphTaskStatus(`正在重试 ${failedJobIds.length} 个失败岗位。`, "running");
  try {
    const retryTask = await api("/api/knowledge-graph/build-jobs/task", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        job_ids: failedJobIds,
        force: $("#kgJobForceInput").checked,
      }),
    });
    renderKnowledgeGraphTask(retryTask);
    const finalTask = await pollKnowledgeGraphTask(retryTask.id);
    await Promise.allSettled([loadStats(), loadKnowledgeGraphStatus()]);
    if (finalTask.status === "failed") {
      showToast(`重试任务失败：${finalTask.message || "请查看任务错误"}`, true);
    } else {
      showToast(`重试完成：成功 ${finalTask.succeeded || 0}，失败 ${finalTask.failed || 0}`);
    }
  } catch (error) {
    setKnowledgeGraphTaskStatus(`重试失败：${error.message}`, "error");
    showToast(error.message, true);
  }
}

async function loadKnowledgeGraphPreview() {
  const button = $("#previewKgBtn");
  button.disabled = true;
  button.textContent = "加载中";
  setKnowledgeGraphTaskStatus("正在从 Neo4j 读取图谱预览。", "running");
  $("#kgPreviewBox").innerHTML = '<div class="empty-state">正在读取 Neo4j 图谱预览。</div>';
  try {
    const payload = await api("/api/knowledge-graph/preview?limit=160");
    renderInteractiveKnowledgeGraphPreview(payload);
    const counts = payload.counts || {};
    const totalNodes =
      Number(counts.syllabi || 0) +
      Number(counts.course_nodes || 0) +
      Number(counts.jobs || 0) +
      Number(counts.companies || 0) +
      Number(counts.job_requirements || 0);
    const displayed = payload.displayed || {};
    setKnowledgeGraphTaskStatus(
      `图谱预览加载完成：Neo4j 总计 ${totalNodes} 个节点、${counts.relationships || 0} 条关系；当前展示 ${displayed.nodes || (payload.nodes || []).length} 个节点、${displayed.edges || (payload.edges || []).length} 条关系。`,
      "success",
    );
  } catch (error) {
    $("#kgPreviewBox").innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
    setKnowledgeGraphTaskStatus(`图谱预览失败：${error.message}`, "error");
    showToast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "加载图谱预览";
  }
}

function renderKnowledgeGraphPreview(payload) {
  const nodes = payload.nodes || [];
  const edges = payload.edges || [];
  const counts = payload.counts || {};
  if (!nodes.length) {
    $("#kgPreviewBox").innerHTML = '<div class="empty-state">Neo4j 中还没有可预览的项目图谱节点。</div>';
    return;
  }
  const grouped = nodes.reduce((acc, node) => {
    const label = node.label || "Node";
    if (!acc[label]) acc[label] = [];
    acc[label].push(node);
    return acc;
  }, {});
  const labelTotalMap = {
    Syllabus: counts.syllabi,
    CourseNode: counts.course_nodes,
    Job: counts.jobs,
    Company: counts.companies,
    JobRequirement: counts.job_requirements,
  };
  const orderedLabels = ["Job", "JobRequirement", "Company", "Syllabus", "CourseNode"];
  const groups = orderedLabels
    .filter((label) => grouped[label])
    .map((label) => [label, grouped[label]])
    .concat(Object.entries(grouped).filter(([label]) => !orderedLabels.includes(label)));
  $("#kgPreviewBox").innerHTML = `
    <div class="kg-node-groups">
      ${groups
        .map(
          ([label, items]) => {
            const total = Number(labelTotalMap[label] || items.length || 0);
            const sampleNote = total > items.length ? `，展示 ${items.length}` : "";
            return `
            <div class="kg-node-group">
              <h4>${escapeHtml(label)} · ${total}<small>${escapeHtml(sampleNote)}</small></h4>
              <div class="kg-node-list">
                ${items
                  .slice(0, 18)
                  .map((node) => `<span class="kg-node-pill">${escapeHtml(node.title || node.id)}</span>`)
                  .join("")}
              </div>
            </div>
          `;
          },
        )
        .join("")}
    </div>
    <div class="kg-edge-table-wrap">
      <table class="job-table kg-edge-table">
        <thead><tr><th>起点</th><th>关系</th><th>终点</th></tr></thead>
        <tbody>
          ${edges
            .slice(0, 60)
            .map(
              (edge) => `
                <tr>
                  <td>${escapeHtml(edge.source)}</td>
                  <td>${escapeHtml(edge.type)}</td>
                  <td>${escapeHtml(edge.target)}</td>
                </tr>
              `,
            )
            .join("")}
        </tbody>
      </table>
    </div>
  `;
}

function renderInteractiveKnowledgeGraphPreview(payload) {
  const nodes = payload.nodes || [];
  const edges = payload.edges || [];
  if (!nodes.length) {
    $("#kgPreviewBox").innerHTML = '<div class="empty-state">Neo4j 中还没有可预览的项目图谱节点。</div>';
    return;
  }
  if (state.kgGraph) {
    state.kgGraph.destroy();
    state.kgGraph = null;
  }

  const labelCounts = buildKnowledgeGraphLabelCounts(nodes);
  const edgeCounts = edges.reduce((acc, edge) => {
    const type = edge.type || "RELATION";
    acc[type] = (acc[type] || 0) + 1;
    return acc;
  }, {});

  $("#kgPreviewBox").innerHTML = `
    <div class="kg-graph-toolbar">
      <div>
        <h4>交互式知识图谱</h4>
        <p>当前展示 ${nodes.length} 个节点、${edges.length} 条关系。滚轮缩放，拖拽节点调整布局，点击节点或关系查看详情。</p>
      </div>
      <div class="kg-graph-actions">
        <button class="secondary-button" type="button" data-kg-layout="cose">重新布局</button>
        <button class="secondary-button" type="button" data-kg-fit>适配视图</button>
      </div>
    </div>
    <div class="kg-legend">
      ${renderKnowledgeGraphLegend(labelCounts)}
    </div>
    <div class="kg-graph-content">
      <div id="kgGraphCanvas" class="kg-graph-canvas"></div>
      <aside id="kgGraphInspector" class="kg-graph-inspector">
        <h4>图谱详情</h4>
        <p>点击左侧图中的节点或关系，可以查看它的类型、名称、属性和连接关系。</p>
        <div class="kg-edge-summary">${renderEdgeSummary(edgeCounts)}</div>
      </aside>
    </div>
    <details class="kg-edge-raw">
      <summary>查看原始关系列表</summary>
      <div class="kg-edge-table-wrap">
        <table class="job-table kg-edge-table">
          <thead><tr><th>起点</th><th>关系</th><th>终点</th></tr></thead>
          <tbody>
            ${edges
              .slice(0, 80)
              .map(
                (edge) => `
                  <tr>
                    <td>${escapeHtml(edge.source)}</td>
                    <td>${escapeHtml(edge.type)}</td>
                    <td>${escapeHtml(edge.target)}</td>
                  </tr>
                `,
              )
              .join("")}
          </tbody>
        </table>
      </div>
    </details>
  `;

  if (typeof cytoscape !== "function") {
    $("#kgGraphCanvas").innerHTML =
      '<div class="empty-state">Cytoscape.js 没有加载成功，无法渲染交互式图谱。请检查网络或刷新页面。</div>';
    return;
  }

  const cy = cytoscape({
    container: $("#kgGraphCanvas"),
    elements: buildCytoscapeElements(nodes, edges),
    wheelSensitivity: 0.22,
    minZoom: 0.12,
    maxZoom: 2.5,
    style: buildKnowledgeGraphStyle(),
    layout: buildKnowledgeGraphLayout(),
  });
  state.kgGraph = cy;

  cy.on("tap", "node", (event) => renderKnowledgeGraphInspector(event.target.data(), "node"));
  cy.on("tap", "edge", (event) => renderKnowledgeGraphInspector(event.target.data(), "edge"));
  cy.on("tap", (event) => {
    if (event.target === cy) {
      $("#kgGraphInspector").innerHTML = `
        <h4>图谱详情</h4>
        <p>点击左侧图中的节点或关系，可以查看它的类型、名称、属性和连接关系。</p>
        <div class="kg-edge-summary">${renderEdgeSummary(edgeCounts)}</div>
      `;
    }
  });

  $("[data-kg-layout]")?.addEventListener("click", () => {
    cy.layout(buildKnowledgeGraphLayout()).run();
  });
  $("[data-kg-fit]")?.addEventListener("click", () => {
    cy.fit(undefined, 40);
  });
}

function buildKnowledgeGraphLabelCounts(nodes) {
  return nodes.reduce((acc, node) => {
    const label = node.label || "Node";
    acc[label] = (acc[label] || 0) + 1;
    return acc;
  }, {});
}

function renderKnowledgeGraphLegend(labelCounts) {
  const labels = ["Syllabus", "CourseNode", "Job", "Company", "JobRequirement"];
  return labels
    .filter((label) => labelCounts[label])
    .map(
      (label) => `
        <span class="kg-legend-item">
          <span class="kg-legend-dot" style="background:${knowledgeGraphColor(label)}"></span>
          ${escapeHtml(knowledgeGraphLabelName(label))} · ${labelCounts[label]}
        </span>
      `,
    )
    .join("");
}

function renderEdgeSummary(edgeCounts) {
  const entries = Object.entries(edgeCounts);
  if (!entries.length) return "<p>当前预览没有关系边。</p>";
  return entries
    .map(([type, count]) => `<span class="kg-edge-chip">${escapeHtml(type)} · ${count}</span>`)
    .join("");
}

function buildCytoscapeElements(nodes, edges) {
  const validNodeIds = new Set(nodes.map((node) => String(node.id)));
  const nodeElements = nodes.map((node) => ({
    data: {
      id: String(node.id),
      label: node.label || "Node",
      title: node.title || node.id,
      displayLabel: trimGraphLabel(node.title || node.id, 26),
      properties: node.properties || {},
    },
    classes: node.label || "Node",
  }));
  const edgeElements = edges
    .filter((edge) => validNodeIds.has(String(edge.source)) && validNodeIds.has(String(edge.target)))
    .map((edge, index) => ({
      data: {
        id: `edge-${index}`,
        source: String(edge.source),
        target: String(edge.target),
        type: edge.type || "RELATION",
        displayLabel: edge.type || "RELATION",
      },
    }));
  return [...nodeElements, ...edgeElements];
}

function buildKnowledgeGraphStyle() {
  return [
    {
      selector: "node",
      style: {
        "background-color": "#8fb9b6",
        "border-width": 1,
        "border-color": "#5f7c78",
        color: "#142220",
        label: "data(displayLabel)",
        "font-size": 9,
        "font-family": "Inter, Arial, sans-serif",
        "text-wrap": "wrap",
        "text-max-width": 88,
        "text-valign": "center",
        "text-halign": "center",
        width: 48,
        height: 48,
        "overlay-opacity": 0,
      },
    },
    {
      selector: ".Syllabus",
      style: { "background-color": knowledgeGraphColor("Syllabus"), shape: "round-rectangle", width: 76, height: 42 },
    },
    {
      selector: ".CourseNode",
      style: { "background-color": knowledgeGraphColor("CourseNode"), width: 58, height: 58 },
    },
    {
      selector: ".Job",
      style: { "background-color": knowledgeGraphColor("Job"), shape: "round-rectangle", width: 82, height: 46 },
    },
    {
      selector: ".Company",
      style: { "background-color": knowledgeGraphColor("Company"), shape: "hexagon", width: 58, height: 58 },
    },
    {
      selector: ".JobRequirement",
      style: { "background-color": knowledgeGraphColor("JobRequirement"), width: 64, height: 64 },
    },
    {
      selector: "edge",
      style: {
        width: 1.2,
        "line-color": "#9aaca8",
        "target-arrow-color": "#9aaca8",
        "target-arrow-shape": "triangle",
        "curve-style": "bezier",
        label: "data(displayLabel)",
        "font-size": 7,
        color: "#566461",
        "text-background-color": "#fbfdfc",
        "text-background-opacity": 0.82,
        "text-background-padding": 2,
        "overlay-opacity": 0,
      },
    },
    {
      selector: "node:selected",
      style: {
        "border-width": 4,
        "border-color": "#1f7a82",
      },
    },
    {
      selector: "edge:selected",
      style: {
        width: 2.8,
        "line-color": "#1f7a82",
        "target-arrow-color": "#1f7a82",
      },
    },
  ];
}

function buildKnowledgeGraphLayout() {
  return {
    name: "cose",
    animate: false,
    fit: true,
    padding: 48,
    nodeRepulsion: 8500,
    idealEdgeLength: 120,
    edgeElasticity: 120,
    gravity: 0.22,
    numIter: 1200,
  };
}

function renderKnowledgeGraphInspector(data, kind) {
  const box = $("#kgGraphInspector");
  if (!box) return;
  if (kind === "edge") {
    box.innerHTML = `
      <h4>关系</h4>
      <dl class="kg-property-list">
        <div><dt>类型</dt><dd>${escapeHtml(data.type || "")}</dd></div>
        <div><dt>起点</dt><dd>${escapeHtml(data.source || "")}</dd></div>
        <div><dt>终点</dt><dd>${escapeHtml(data.target || "")}</dd></div>
      </dl>
    `;
    return;
  }
  const props = data.properties || {};
  const visibleProps = Object.entries(props)
    .filter(([key, value]) => !["embedding", "project"].includes(key) && value !== null && value !== undefined && value !== "")
    .slice(0, 16);
  box.innerHTML = `
    <h4>${escapeHtml(data.title || data.id)}</h4>
    <div class="kg-inspector-type">${escapeHtml(knowledgeGraphLabelName(data.label || "Node"))}</div>
    <dl class="kg-property-list">
      ${visibleProps
        .map(
          ([key, value]) => `
            <div>
              <dt>${escapeHtml(key)}</dt>
              <dd>${escapeHtml(formatGraphProperty(value))}</dd>
            </div>
          `,
        )
        .join("")}
    </dl>
  `;
}

function knowledgeGraphLabelName(label) {
  const names = {
    Syllabus: "课程大纲",
    CourseNode: "课程能力节点",
    Job: "岗位",
    Company: "公司",
    JobRequirement: "岗位要求",
  };
  return names[label] || label;
}

function knowledgeGraphColor(label) {
  const colors = {
    Syllabus: "#8fd3a7",
    CourseNode: "#b8dfc9",
    Job: "#7fb7c1",
    Company: "#f0c36a",
    JobRequirement: "#d7c0f2",
  };
  return colors[label] || "#b8c7c4";
}

function trimGraphLabel(value, maxLength) {
  const text = String(value || "");
  return text.length > maxLength ? `${text.slice(0, maxLength - 1)}…` : text;
}

function formatGraphProperty(value) {
  if (Array.isArray(value)) return value.join("、");
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value);
}

function formatNumber(value) {
  const number = Number(value || 0);
  return Number.isFinite(number) ? number.toFixed(3) : "0.000";
}

async function runManualCrawl(event) {
  event.preventDefault();
  const request = $("#manualCrawlInput").value.trim();
  if (!request) {
    showToast("请输入岗位关键词或抓取要求。", true);
    return;
  }
  const button = $("#manualCrawlBtn");
  button.disabled = true;
  button.textContent = "抓取中";
  try {
    const payload = await api("/api/jobs/crawl", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        request,
        source: $("#manualCrawlSource").value,
        pages: Number($("#manualCrawlPages").value || 1),
        headless: $("#manualCrawlHeadless").checked,
      }),
    });
    await Promise.allSettled([loadJobs(), loadStats()]);
    showToast(formatCrawlToast("抓取完成", payload));
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "开始抓取";
  }
}

function courseCrawlPayload() {
  const syllabusIds = [...state.selectedSyllabi];
  if (!syllabusIds.length) {
    throw new Error("请先选择至少一门课程。");
  }
  return {
    syllabus_ids: syllabusIds,
    city: $("#courseCrawlCity").value.trim(),
    max_keywords: Number($("#courseCrawlKeywordLimit").value || 6),
    source: $("#courseCrawlSource").value,
    pages: Number($("#courseCrawlPages").value || 1),
    headless: $("#courseCrawlHeadless").checked,
  };
}

async function buildSearchPlan() {
  const button = $("#buildSearchPlanBtn");
  button.disabled = true;
  button.textContent = "生成中";
  $("#searchPlanBox").innerHTML = '<div class="empty-state">智能体正在分析课程能力和岗位方向。</div>';
  try {
    const payload = courseCrawlPayload();
    const plan = await api("/api/jobs/search-plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        syllabus_ids: payload.syllabus_ids,
        city: payload.city,
        max_keywords: payload.max_keywords,
      }),
    });
    state.latestSearchPlan = plan;
    renderSearchPlan(plan);
    showToast("搜索计划已生成");
  } catch (error) {
    $("#searchPlanBox").innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
    showToast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "生成搜索计划";
  }
}

async function runCourseCrawl() {
  const button = $("#courseCrawlBtn");
  button.disabled = true;
  button.textContent = "抓取中";
  $("#searchPlanBox").innerHTML = '<div class="empty-state">正在生成搜索计划并抓取岗位，首次课程图谱分析可能较慢。</div>';
  try {
    const payload = courseCrawlPayload();
    const result = await api("/api/jobs/crawl-from-courses", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    state.latestSearchPlan = result.plan;
    renderSearchPlan(result.plan, result);
    await Promise.allSettled([loadJobs(), loadStats()]);
    showToast(formatCrawlToast("课程抓取完成", result));
  } catch (error) {
    $("#searchPlanBox").innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
    showToast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "生成并抓取";
  }
}

function formatCrawlToast(prefix, payload) {
  const warnings = payload.warnings || [];
  const warningText = warnings.length ? `；提示：${warnings[0]}` : "";
  return `${prefix}：新增 ${payload.inserted} 个，重复 ${payload.skipped_duplicates} 个${warningText}`;
}

function renderSearchPlan(plan, crawlResult = null) {
  const keywords = plan.keywords || [];
  const signals = plan.course_signals || [];
  const directions = plan.job_directions || [];
  $("#searchPlanBox").innerHTML = `
    <div class="plan-section">
      <h4>建议搜索词</h4>
      <div class="plan-keywords">
        ${keywords.length ? keywords.map((item) => `<span class="tag">${escapeHtml(item)}</span>`).join("") : '<span class="empty-state">暂无关键词</span>'}
      </div>
    </div>
    <div class="plan-section">
      <h4>课程技术依据</h4>
      ${signals.length ? `<ul>${signals.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : '<div class="empty-state">暂无课程技术依据。</div>'}
    </div>
    <div class="plan-section">
      <h4>可能对口岗位方向</h4>
      ${directions.length ? `<ul>${directions.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : '<div class="empty-state">暂无岗位方向。</div>'}
    </div>
    ${plan.rationale ? `<div class="plan-section"><h4>分析说明</h4><div class="explanation">${escapeHtml(plan.rationale)}</div></div>` : ""}
    ${
      crawlResult
        ? `<div class="plan-section"><h4>抓取结果</h4><div class="empty-state">抓取 ${crawlResult.fetched} 个，新增 ${crawlResult.inserted} 个，重复 ${crawlResult.skipped_duplicates} 个。${renderWarningsText(crawlResult.warnings)}</div></div>`
        : ""
    }
  `;
}

function renderWarningsText(warnings) {
  if (!warnings || !warnings.length) return "";
  return ` 提示：${escapeHtml(warnings[0])}`;
}

function renderMatchResults(result) {
  const matches = result.matches || [];
  state.latestResult = result;
  if (!matches.length) {
    $("#matchResults").innerHTML = '<div class="empty-state">没有找到明确匹配的岗位。</div>';
    return;
  }

  $("#matchResults").innerHTML = matches
    .map(
      (item, index) => `
      <article class="match-item">
        <h3>${index + 1}. ${escapeHtml(item.job_title || "未命名岗位")}</h3>
        <div class="match-company">${escapeHtml(item.company || "")} · 岗位ID ${escapeHtml(String(item.job_id || ""))}${item.city ? ` · ${escapeHtml(item.city)}` : ""}${item.salary ? ` · ${escapeHtml(item.salary)}` : ""}</div>
        <div class="match-actions">
          ${item.job_url ? `<a class="link-button" href="${escapeHtml(item.job_url)}" target="_blank" rel="noopener noreferrer">打开岗位链接</a>` : ""}
          <button class="link-button" data-match-detail="${index}">查看详情对比</button>
        </div>
        <div class="match-block">
          <strong>岗位要求与课程依据</strong>
          ${renderRequirementEvidence(item, true)}
          ${item.explanation ? `<div class="explanation compact-explanation">${escapeHtml(item.explanation)}</div>` : ""}
        </div>
      </article>
    `,
    )
    .join("");
}

function getResultSyllabi() {
  const resultSyllabi = state.latestResult?.syllabi;
  if (Array.isArray(resultSyllabi) && resultSyllabi.length) return resultSyllabi;
  const ids = state.latestResult?.syllabus_ids || [];
  return state.syllabi.filter((item) => ids.includes(item.id) || state.selectedSyllabi.has(item.id));
}

function renderSyllabusLinks(syllabi) {
  if (!syllabi || !syllabi.length) return "";
  return `
    <div class="syllabus-link-box">
      <strong>参与匹配的大纲</strong>
      <div class="syllabus-link-list">
        ${syllabi
          .map(
            (item) => `
              <a class="syllabus-link" href="${escapeHtml(item.file_url || item.text_url || "#")}" target="_blank" rel="noopener noreferrer">
                ${escapeHtml(item.title || item.file_name || `大纲 #${item.id}`)}
              </a>
            `,
          )
          .join("")}
      </div>
      <div class="syllabus-link-hint">优先打开原始大纲文件；如果原文件不可用，系统会显示已提取的大纲文本。</div>
    </div>
  `;
}

function renderRequirementEvidence(item, compact = false) {
  const pairs = item.evidence_pairs || [];
  if (pairs.length) {
    const visiblePairs = compact ? pairs.slice(0, 4) : pairs;
    return `
      <div class="requirement-evidence-list">
        ${visiblePairs
          .map(
            (pair) => `
              <div class="requirement-evidence">
                <div class="requirement-line">${escapeHtml(pair.job_requirement || pair.job_evidence || "")}</div>
                <div class="course-evidence">
                  <span>${escapeHtml(pair.syllabus_title || "对应课程依据")}${pair.course_node ? ` · ${escapeHtml(pair.course_node)}` : ""}</span>
                  ${escapeHtml(pair.course_evidence || "")}
                </div>
                ${
                  pair.rationale
                    ? `<div class="edge-rationale">${escapeHtml(pair.rationale)}</div>`
                    : ""
                }
              </div>
            `,
          )
          .join("")}
        ${compact && pairs.length > visiblePairs.length ? `<div class="more-evidence">还有 ${pairs.length - visiblePairs.length} 条岗位需求，点击“查看详情对比”查看完整内容。</div>` : ""}
      </div>
    `;
  }

  const requirements = item.matched_job_requirements || [];
  const courseContent = item.matched_course_content || [];
  if (!requirements.length && !courseContent.length) {
    return '<div class="empty-state">暂无可展示的匹配依据。</div>';
  }
  if (!requirements.length) {
    return `<ul>${courseContent.map((text) => `<li>${escapeHtml(text)}</li>`).join("")}</ul>`;
  }

  const visibleRequirements = compact ? requirements.slice(0, 4) : requirements;
  const extraCourseContent = courseContent.slice(requirements.length);
  return `
    <div class="requirement-evidence-list">
      ${visibleRequirements
        .map((requirement, index) => {
          const course = courseContent[index] || "";
          return `
            <div class="requirement-evidence">
              <div class="requirement-line">${escapeHtml(requirement)}</div>
              ${
                course
                  ? `<div class="course-evidence"><span>对应课程依据</span>${escapeHtml(course)}</div>`
                  : '<div class="course-evidence muted-evidence"><span>对应课程依据</span>模型未单独返回这条要求的课程依据，可结合下方整体解释判断。</div>'
              }
            </div>
          `;
        })
        .join("")}
      ${compact && requirements.length > visibleRequirements.length ? `<div class="more-evidence">还有 ${requirements.length - visibleRequirements.length} 条岗位要求，点击“查看详情对比”查看完整内容。</div>` : ""}
      ${
        !compact && extraCourseContent.length
          ? `<div class="requirement-evidence supplemental-evidence">
              <div class="requirement-line">补充课程依据</div>
              <ul>${extraCourseContent.map((text) => `<li>${escapeHtml(text)}</li>`).join("")}</ul>
            </div>`
          : ""
      }
    </div>
  `;
}

function openMatchDetail(index) {
  const item = state.latestResult?.matches?.[index];
  if (!item) return;
  const job = item.job_detail || {};
  const syllabi = getResultSyllabi();
  $("#matchDetailTitle").textContent = item.job_title || "岗位匹配详情";
  $("#matchDetailSubtitle").textContent = `${item.company || job.company || ""} · 岗位ID ${item.job_id || ""}`;
  $("#matchDetailBody").innerHTML = `
    <div class="detail-meta">
      ${job.city || item.city ? `<span class="meta-chip">${escapeHtml(job.city || item.city)}</span>` : ""}
      ${job.salary || item.salary ? `<span class="meta-chip">${escapeHtml(job.salary || item.salary)}</span>` : ""}
      ${job.education || item.education ? `<span class="meta-chip">${escapeHtml(job.education || item.education)}</span>` : ""}
      ${job.experience || item.experience ? `<span class="meta-chip">${escapeHtml(job.experience || item.experience)}</span>` : ""}
      ${item.job_url ? `<a class="link-button" href="${escapeHtml(item.job_url)}" target="_blank" rel="noopener noreferrer">打开招聘页面</a>` : ""}
    </div>
    ${renderSyllabusLinks(syllabi)}
    <div class="jd-box">
      <h3>岗位要求与课程依据</h3>
      ${renderRequirementEvidence(item)}
    </div>
    <div class="jd-box">
      <h3>整体解释</h3>
      <div class="explanation">${escapeHtml(item.explanation || "")}</div>
    </div>
    <div class="jd-box">
      <h3>完整岗位 JD</h3>
      <div class="description">${escapeHtml(job.description || "暂无完整岗位描述")}</div>
    </div>
  `;
  $("#matchDetailModal").hidden = false;
}

function closeMatchDetail() {
  $("#matchDetailModal").hidden = true;
}

async function uploadSyllabus(event) {
  event.preventDefault();
  const form = $("#uploadForm");
  const formData = new FormData();
  const file = $("#fileInput").files[0];
  if (!file) return;
  formData.append("file", file);
  formData.append("title", $("#titleInput").value.trim());
  formData.append("analyze", $("#analyzeInput").checked ? "true" : "false");

  const button = form.querySelector("button[type='submit']");
  button.disabled = true;
  button.textContent = "处理中";
  try {
    const item = await api("/api/syllabi", { method: "POST", body: formData });
    form.reset();
    $("#analyzeInput").checked = true;
    $("#fileNameText").textContent = "尚未选择文件";
    await loadSyllabi();
    await loadStats();
    showToast(`已入库：${item.title}`);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "上传并入库";
  }
}

async function analyzeSyllabus(id) {
  showToast("正在重新分析课程画像");
  try {
    await api(`/api/syllabi/${id}/analyze`, { method: "POST" });
    await loadSyllabi();
    showToast("课程画像已更新");
  } catch (error) {
    showToast(error.message, true);
  }
}

async function viewProfile(id) {
  try {
    setActivePanel("match-panel");
    const item = await api(`/api/syllabi/${id}`);
    const profile = item.profile;
    if (!profile) {
      showToast("该课程还没有画像，请先分析。", true);
      return;
    }
    $("#matchResults").innerHTML = `
      <article class="match-item">
        <h3>${escapeHtml(item.title)} · 课程画像</h3>
        <div class="match-block"><strong>课程说明</strong><div class="explanation">${escapeHtml(profile.summary || "")}</div></div>
        <div class="match-block"><strong>知识点</strong><ul>${profile.knowledge_points.map((text) => `<li>${escapeHtml(text)}</li>`).join("")}</ul></div>
        <div class="match-block"><strong>能力</strong><ul>${profile.abilities.map((text) => `<li>${escapeHtml(text)}</li>`).join("")}</ul></div>
        <div class="match-block"><strong>技术、工具、方法</strong><ul>${profile.technologies_tools_methods.map((text) => `<li>${escapeHtml(text)}</li>`).join("")}</ul></div>
        <div class="match-block"><strong>岗位方向</strong><ul>${profile.job_directions.map((text) => `<li>${escapeHtml(text)}</li>`).join("")}</ul></div>
      </article>
    `;
  } catch (error) {
    showToast(error.message, true);
  }
}

async function showMatchRuns() {
  try {
    setActivePanel("match-panel");
    const data = await api("/api/match-runs?limit=8");
    const runs = data.items || [];
    if (!runs.length) {
      $("#matchResults").innerHTML = '<div class="empty-state">还没有历史匹配记录。</div>';
      return;
    }
    $("#matchResults").innerHTML = runs
      .map(
        (run) => `
        <article class="match-item">
          <h3>匹配记录 #${run.id}</h3>
          <div class="match-company">${escapeHtml(run.created_at)} · ${run.match_count} 个岗位</div>
          <button class="secondary-button" data-load-run="${run.id}">载入结果</button>
        </article>
      `,
      )
      .join("");
  } catch (error) {
    showToast(error.message, true);
  }
}

async function loadMatchRun(id) {
  try {
    const run = await api(`/api/match-runs/${id}`);
    renderMatchResults(run.result || {});
  } catch (error) {
    showToast(error.message, true);
  }
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function bindEvents() {
  $("#uploadForm").addEventListener("submit", uploadSyllabus);
  $("#manualCrawlForm").addEventListener("submit", runManualCrawl);
  $("#buildSearchPlanBtn").addEventListener("click", buildSearchPlan);
  $("#courseCrawlBtn").addEventListener("click", runCourseCrawl);
  $("#refreshSyllabiBtn").addEventListener("click", () => loadSyllabi().then(() => showToast("大纲列表已刷新")));
  $("#refreshJobsBtn").addEventListener("click", () => loadJobs().then(() => showToast("岗位池已刷新")));
  $("#refreshKgBtn").addEventListener("click", () => loadKnowledgeGraphStatus().then(() => showToast("知识图谱状态已刷新")));
  $("#buildCourseKgBtn").addEventListener("click", () => buildCourseKnowledgeGraph(false));
  $("#buildAllCourseKgBtn").addEventListener("click", () => buildCourseKnowledgeGraph(true));
  $("#buildJobKgBtn").addEventListener("click", buildJobKnowledgeGraph);
  $("#previewKgBtn").addEventListener("click", loadKnowledgeGraphPreview);
  $("#jobSearchBtn").addEventListener("click", loadJobs);
  $("#fileInput").addEventListener("change", () => {
    const file = $("#fileInput").files[0];
    $("#fileNameText").textContent = file ? file.name : "尚未选择文件";
  });
  $("#jobSearchInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter") loadJobs();
  });
  $("#runMatchBtn").addEventListener("click", runMatch);
  $("#previewEvidenceBtn").addEventListener("click", previewGraphragEvidence);
  $("#refreshRunsBtn").addEventListener("click", showMatchRuns);

  document.addEventListener("change", (event) => {
    const target = event.target;
    if (target.matches("[data-select-syllabus]")) {
      const id = Number(target.dataset.selectSyllabus);
      if (target.checked) state.selectedSyllabi.add(id);
      else state.selectedSyllabi.delete(id);
      renderSyllabi();
      renderSelectedCourses();
      renderJobCrawlSelectedCourses();
      renderMatchCourseList();
      renderJobCourseList();
    }
  });

  document.addEventListener("click", (event) => {
    const target = event.target.closest("button, tr");
    if (!target) return;
    if (target.dataset.jobId) showJobDetail(Number(target.dataset.jobId));
    if (target.dataset.analyzeSyllabus) analyzeSyllabus(Number(target.dataset.analyzeSyllabus));
    if (target.dataset.viewProfile) viewProfile(Number(target.dataset.viewProfile));
    if (target.dataset.loadRun) loadMatchRun(Number(target.dataset.loadRun));
    if (target.dataset.matchDetail) openMatchDetail(Number(target.dataset.matchDetail));
    if (target.dataset.retryKgTask) retryFailedKnowledgeGraphTask(target.dataset.retryKgTask);
  });

  document.addEventListener("click", (event) => {
    const option = event.target.closest("[data-toggle-syllabus]");
    if (!option || event.target.matches("input")) return;
    const id = Number(option.dataset.toggleSyllabus);
    if (state.selectedSyllabi.has(id)) state.selectedSyllabi.delete(id);
    else state.selectedSyllabi.add(id);
    renderSyllabi();
    renderSelectedCourses();
    renderJobCrawlSelectedCourses();
    renderMatchCourseList();
    renderJobCourseList();
  });

  $("#courseSearchInput").addEventListener("input", renderMatchCourseList);
  $("#jobCourseSearchInput").addEventListener("input", renderJobCourseList);
  $("#clearSelectedBtn").addEventListener("click", () => {
    state.selectedSyllabi.clear();
    renderSyllabi();
    renderSelectedCourses();
    renderJobCrawlSelectedCourses();
    renderMatchCourseList();
    renderJobCourseList();
  });
  $("#clearJobCourseSelectedBtn").addEventListener("click", () => {
    state.selectedSyllabi.clear();
    renderSyllabi();
    renderSelectedCourses();
    renderJobCrawlSelectedCourses();
    renderMatchCourseList();
    renderJobCourseList();
  });
  $("#closeMatchDetailBtn").addEventListener("click", closeMatchDetail);
  $("#matchDetailModal").addEventListener("click", (event) => {
    if (event.target.id === "matchDetailModal") closeMatchDetail();
  });

  document.querySelectorAll(".nav-item").forEach((button) => {
    button.addEventListener("click", () => {
      setActivePanel(button.dataset.target);
    });
  });
}

async function init() {
  bindEvents();
  const hashPanel = `${window.location.hash.replace("#", "")}-panel`;
  if (document.getElementById(hashPanel)) {
    setActivePanel(hashPanel);
  } else {
    setActivePanel(state.activePanel);
  }
  await loadHealth();
  await Promise.allSettled([loadStats(), loadSyllabi(), loadJobs(), loadKnowledgeGraphStatus()]);
}

init().catch((error) => showToast(error.message, true));
