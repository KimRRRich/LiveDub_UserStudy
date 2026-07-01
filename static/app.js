const FIELDS = [
  ["visual_quality", "画面质量"],
  ["occlusion", "遮挡处理"],
  ["lip_sync", "唇形同步"],
  ["teeth_quality", "牙齿质量"],
  ["identity_consistency", "身份一致性"],
];
const STORAGE_ID = "userstudy_participant_id";
const STORAGE_NAME = "userstudy_username";
const STORAGE_PARTICIPANTS = "userstudy_participants";
const STORAGE_ADMIN_SESSION = "userstudy_admin_session";

const state = {
  groups: [],
  participant: null,
  ratings: {},
  index: 0,
  adminSession: localStorage.getItem(STORAGE_ADMIN_SESSION) || "",
};

const $ = (id) => document.getElementById(id);

function show(viewId) {
  for (const id of ["loginView", "studyView", "doneView", "adminView"]) {
    $(id).classList.toggle("hidden", id !== viewId);
  }
}

function loadParticipantMap() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_PARTICIPANTS) || "{}");
  } catch {
    return {};
  }
}

function savedParticipantId(username) {
  const participants = loadParticipantMap();
  if (participants[username]) return participants[username];
  if (localStorage.getItem(STORAGE_NAME) === username) {
    return localStorage.getItem(STORAGE_ID);
  }
  return null;
}

function rememberParticipant(username, participantId) {
  const participants = loadParticipantMap();
  participants[username] = participantId;
  localStorage.setItem(STORAGE_PARTICIPANTS, JSON.stringify(participants));
  localStorage.setItem(STORAGE_ID, participantId);
  localStorage.setItem(STORAGE_NAME, username);
}

function forgetCurrentParticipant() {
  if (!state.participant) return;
  const participants = loadParticipantMap();
  delete participants[state.participant.username];
  localStorage.setItem(STORAGE_PARTICIPANTS, JSON.stringify(participants));
  if (localStorage.getItem(STORAGE_NAME) === state.participant.username) {
    localStorage.removeItem(STORAGE_ID);
    localStorage.removeItem(STORAGE_NAME);
  }
  state.participant = null;
  state.groups = [];
  state.ratings = {};
  state.index = 0;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
    ...options,
  });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      message = body.detail || message;
    } catch {
      // Keep HTTP status text.
    }
    throw new Error(message);
  }
  return response.json();
}

async function adminApi(path, options = {}) {
  return api(path, {
    ...options,
    headers: {
      "X-Admin-Session": state.adminSession,
      ...(options.headers || {}),
    },
  });
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function formatValue(value) {
  return value === null || value === undefined || value === "" ? "-" : escapeHtml(value);
}

function setStatus(text, warn = false) {
  $("saveStatus").textContent = text;
  $("saveStatus").classList.toggle("warn", warn);
}

function totalVideoCount() {
  return state.groups.reduce((total, group) => total + group.videos.length, 0);
}

function completedVideoCount() {
  return Object.values(state.ratings).filter(videoRatingComplete).length;
}

function videoRatingComplete(rating) {
  return rating && FIELDS.every(([field]) => Number.isInteger(rating[field]));
}

function groupRated(group) {
  return group.videos.every((video) => videoRatingComplete(state.ratings[video.id]));
}

function groupStarted(group) {
  return group.videos.some((video) => state.ratings[video.id]);
}

function groupStatus(group) {
  if (groupRated(group)) return "done";
  if (groupStarted(group)) return "partial";
  return "empty";
}

function canEarlySubmit() {
  return state.groups.length > 0 && state.groups.some(groupRated) && !state.groups.some((group) => {
    const status = groupStatus(group);
    return status === "partial";
  });
}

function firstIncompleteGroupIndex() {
  const idx = state.groups.findIndex((group) => !groupRated(group));
  return idx === -1 ? Math.max(0, state.groups.length - 1) : idx;
}

function currentGroup() {
  return state.groups[state.index];
}

function updateProgress() {
  const totalSamples = state.groups.length;
  const ratedSamples = state.groups.filter(groupRated).length;
  const ratedVideos = completedVideoCount();
  const totalVideos = totalVideoCount();
  $("progressText").textContent = `${ratedSamples} / ${totalSamples} 样本，${ratedVideos} / ${totalVideos} 视频`;
  $("progressBar").style.width = totalSamples ? `${(ratedSamples / totalSamples) * 100}%` : "0%";
}

function renderSampleNav() {
  $("sampleNav").innerHTML = state.groups
    .map((group, index) => {
      const status = groupStatus(group);
      const active = index === state.index;
      const labels = {
        done: "已完成",
        partial: "部分完成",
        empty: "未开始",
      };
      return `
        <button
          type="button"
          class="sample-jump ${status} ${active ? "active" : ""}"
          data-sample-index="${index}"
          title="${labels[status]}：${escapeHtml(group.audio_id)}"
        >
          ${index + 1}
        </button>
      `;
    })
    .join("");
}

function scoreInputName(videoId, field) {
  return `${videoId}__${field}`;
}

function scoreControls(video, field) {
  const name = scoreInputName(video.id, field);
  const saved = state.ratings[video.id]?.[field];
  return `
    <div class="score-options">
      ${[1, 2, 3, 4, 5]
        .map(
          (score) => `
            <label>
              <input
                type="radio"
                name="${name}"
                value="${score}"
                ${saved === score ? "checked" : ""}
                required
              />
              ${score}
            </label>
          `,
        )
        .join("")}
    </div>
  `;
}

function ratingBlock(video) {
  return FIELDS.map(
    ([field, label]) => `
      <fieldset class="score-row">
        <legend>${label}</legend>
        ${scoreControls(video, field)}
      </fieldset>
    `,
  ).join("");
}

function videoCard(video) {
  const saved = state.ratings[video.id] ? "已保存" : "待评分";
  return `
    <article class="video-card" data-video-id="${video.id}">
      <header class="video-card-head">
        <h2>${video.label}</h2>
        <span class="card-state">${saved}</span>
      </header>
      <video controls playsinline preload="metadata" src="${video.url}"></video>
      <div class="rating-grid">
        ${ratingBlock(video)}
      </div>
    </article>
  `;
}

function pauseOtherVideos(activeVideo) {
  document.querySelectorAll("video").forEach((video) => {
    if (video !== activeVideo && !video.paused) {
      video.pause();
    }
  });
}

function wireVideoPlayback() {
  document.querySelectorAll(".video-card video").forEach((video) => {
    video.addEventListener("play", () => pauseOtherVideos(video));
  });
}

function renderCurrent() {
  const group = currentGroup();
  if (!group) {
    setStatus("视频清单为空", true);
    return;
  }

  $("participantLabel").textContent = state.participant.username;
  $("progressTitle").textContent = `样本 ${state.index + 1} / ${state.groups.length}`;
  $("sampleIndex").textContent = `当前样本：${group.audio_id}`;
  $("sampleReference").innerHTML = sampleReferenceBlock(group);
  $("videoGrid").innerHTML = group.videos.map(videoCard).join("");
  $("prevBtn").disabled = state.index === 0;
  $("nextBtn").textContent = state.index === state.groups.length - 1 ? "保存并完成" : "保存并下一个样本";
  setStatus(groupRated(group) ? "当前样本已保存" : "");
  updateProgress();
  renderSampleNav();
  wireVideoPlayback();
}

function sampleReferenceBlock(group) {
  if (!group.reference_image_url) {
    return "";
  }
  return `
    <section class="reference-panel" aria-label="原始人物参考图">
      <div>
        <p class="reference-label">原始人物参考图</p>
        <p class="reference-note">用于观察配音后身份一致性保持情况</p>
      </div>
      <img src="${group.reference_image_url}" alt="样本 ${escapeHtml(group.audio_id)} 的原始人物参考图" />
    </section>
  `;
}

function readVideoRating(video) {
  const rating = {};
  let selectedCount = 0;
  for (const [field, label] of FIELDS) {
    const selected = document.querySelector(`input[name="${scoreInputName(video.id, field)}"]:checked`);
    if (!selected) {
      throw new Error(`请完成 ${video.label} 的「${label}」评分`);
    }
    selectedCount += 1;
    rating[field] = Number(selected.value);
  }
  return selectedCount === 0 ? null : rating;
}

function readOptionalVideoRating(video) {
  const rating = {};
  let selectedCount = 0;
  for (const [field, label] of FIELDS) {
    const selected = document.querySelector(`input[name="${scoreInputName(video.id, field)}"]:checked`);
    if (selected) {
      selectedCount += 1;
      rating[field] = Number(selected.value);
    }
  }
  if (selectedCount === 0) return null;
  if (selectedCount !== FIELDS.length) {
    const missing = FIELDS.find(([field]) => !rating[field]);
    throw new Error(`请补全 ${video.label} 的「${missing[1]}」评分，或清空该视频评分后再跳过`);
  }
  return rating;
}

async function saveCurrentGroup({ requireAll = false } = {}) {
  const group = currentGroup();
  const pending = [];
  for (const video of group.videos) {
    const rating = requireAll ? readVideoRating(video) : readOptionalVideoRating(video);
    if (rating) {
      pending.push({ video, rating });
    }
  }
  if (requireAll && pending.length !== group.videos.length) {
    throw new Error("请完成当前样本下所有视频评分");
  }
  if (!pending.length) {
    setStatus("当前样本未评分，已跳过", true);
    return;
  }

  setStatus("保存中...");
  for (const item of pending) {
    await api("/api/ratings", {
      method: "POST",
      body: JSON.stringify({
        participant_id: state.participant.id,
        video_id: item.video.id,
        ...item.rating,
      }),
    });
    state.ratings[item.video.id] = item.rating;
  }
  setStatus(groupRated(group) ? "当前样本已保存" : "当前样本已部分保存");
  updateProgress();
  renderSampleNav();
}

async function completeIfReady({ allowPartial = false } = {}) {
  if (!allowPartial && completedVideoCount() < totalVideoCount()) return false;
  if (allowPartial && !canEarlySubmit()) {
    throw new Error("还有未完整评分的样本。请补全已开始样本下的所有视频，未开始样本可跳过。");
  }
  await api("/api/complete", {
    method: "POST",
    body: JSON.stringify({ participant_id: state.participant.id, allow_partial: allowPartial }),
  });
  show("doneView");
  return true;
}

async function startAdmin(username, password) {
  const body = await api("/api/admin/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
  state.adminSession = body.session;
  localStorage.setItem(STORAGE_ADMIN_SESSION, state.adminSession);
  await loadAdminStats();
  show("adminView");
}

async function startStudy(username, password) {
  const participantId = savedParticipantId(username);
  const body = await api("/api/participants", {
    method: "POST",
    body: JSON.stringify({ username, password, participant_id: participantId }),
  });

  state.participant = body.participant;
  state.groups = body.groups || [];
  state.ratings = body.ratings || {};
  state.index = firstIncompleteGroupIndex();

  rememberParticipant(state.participant.username, state.participant.id);

  if (!state.groups.length) {
    alert("没有可评分的视频，请检查 videos.json");
    return;
  }
  show("studyView");
  renderCurrent();
}

function statCard(label, value) {
  return `
    <article class="stat-card">
      <p class="stat-label">${escapeHtml(label)}</p>
      <p class="stat-value">${formatValue(value)}</p>
    </article>
  `;
}

function renderAdminStats(data) {
  const summary = data.summary;
  $("adminSummary").innerHTML = [
    statCard("注册人数", summary.participants),
    statCard("已完成", summary.completed),
    statCard("进行中", summary.in_progress),
    statCard("样本数", summary.samples),
    statCard("视频数", summary.videos),
    statCard("评分条数", summary.ratings),
    statCard("评分进度", `${summary.rating_progress}%`),
  ].join("");

  $("adminParticipants").innerHTML = data.participants.length
    ? data.participants
        .map(
          (row) => `
            <tr>
              <td>${formatValue(row.username)}</td>
              <td>${row.rated_count} / ${summary.videos} (${row.progress}%)</td>
              <td>${row.completed_at ? "是" : "否"}</td>
              <td>${formatValue(row.created_at)}</td>
              <td>${formatValue(row.updated_at)}</td>
              <td>
                <div class="row-actions">
                  <button type="button" class="secondary compact" data-admin-action="clear-ratings" data-username="${escapeHtml(row.username)}">清空评分</button>
                  <button type="button" class="danger compact" data-admin-action="delete-user" data-username="${escapeHtml(row.username)}">删除用户</button>
                </div>
              </td>
            </tr>
          `,
        )
        .join("")
    : `<tr><td class="empty" colspan="6">暂无注册用户</td></tr>`;

  $("adminMethods").innerHTML = data.method_stats.length
    ? data.method_stats
        .map(
          (row) => `
            <tr>
              <td>${formatValue(row.method)}</td>
              <td>${row.rating_count}</td>
              <td>${formatValue(row.visual_quality)}</td>
              <td>${formatValue(row.occlusion)}</td>
              <td>${formatValue(row.lip_sync)}</td>
              <td>${formatValue(row.teeth_quality)}</td>
              <td>${formatValue(row.identity_consistency)}</td>
            </tr>
          `,
        )
        .join("")
    : `<tr><td class="empty" colspan="7">暂无评分数据</td></tr>`;

  $("adminVideos").innerHTML = data.video_stats.length
    ? data.video_stats
        .map(
          (row) => `
            <tr>
              <td>${formatValue(row.audio_id)}</td>
              <td>${formatValue(row.method)}</td>
              <td>${row.rating_count}</td>
              <td>${formatValue(row.visual_quality)}</td>
              <td>${formatValue(row.occlusion)}</td>
              <td>${formatValue(row.lip_sync)}</td>
              <td>${formatValue(row.teeth_quality)}</td>
              <td>${formatValue(row.identity_consistency)}</td>
            </tr>
          `,
        )
        .join("")
    : `<tr><td class="empty" colspan="8">暂无评分数据</td></tr>`;
}

async function loadAdminStats() {
  const data = await adminApi("/api/admin/stats");
  renderAdminStats(data);
}

async function exportAdminCsv() {
  const response = await fetch("/api/admin/export.csv", {
    headers: { "X-Admin-Session": state.adminSession },
  });
  if (!response.ok) {
    throw new Error("导出失败，请重新登录管理员账户");
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "userstudy_ratings.csv";
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

async function adminPost(path, body = {}) {
  return adminApi(path, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

async function clearUserRatings(username) {
  if (!confirm(`确定清空用户「${username}」的所有评分记录吗？该用户仍可继续登录评分。`)) return;
  await adminPost("/api/admin/users/clear-ratings", { username });
  await loadAdminStats();
}

async function deleteUser(username) {
  if (!confirm(`确定删除用户「${username}」及其所有评分记录吗？此操作不可撤销。`)) return;
  await adminPost("/api/admin/users/delete", { username });
  await loadAdminStats();
}

async function clearAllUsers() {
  const value = prompt("将删除所有注册用户和所有评分记录。请输入 DELETE 确认。");
  if (value !== "DELETE") return;
  await adminPost("/api/admin/users/clear-all");
  localStorage.removeItem(STORAGE_PARTICIPANTS);
  localStorage.removeItem(STORAGE_ID);
  localStorage.removeItem(STORAGE_NAME);
  await loadAdminStats();
}

function wireEvents() {
  $("loginForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const username = $("username").value.trim();
    const password = $("password").value;
    if (!username || !password.trim()) return;
    try {
      try {
        await startAdmin(username, password);
      } catch {
        await startStudy(username, password);
      }
    } catch (error) {
      alert(error.message);
    }
  });

  $("ratingForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await saveCurrentGroup();
      if (state.index === state.groups.length - 1) {
        if (await completeIfReady()) return;
        state.index = firstIncompleteGroupIndex();
      } else {
        state.index += 1;
      }
      renderCurrent();
    } catch (error) {
      setStatus(error.message, true);
    }
  });

  $("earlySubmitBtn").addEventListener("click", async () => {
    try {
      await saveCurrentGroup();
      await completeIfReady({ allowPartial: true });
    } catch (error) {
      setStatus(error.message, true);
    }
  });

  $("prevBtn").addEventListener("click", () => {
    if (state.index > 0) {
      state.index -= 1;
      renderCurrent();
    }
  });

  $("sampleNav").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-sample-index]");
    if (!button) return;
    state.index = Number(button.dataset.sampleIndex);
    renderCurrent();
  });

  $("editDoneBtn").addEventListener("click", () => {
    show("studyView");
    state.index = firstIncompleteGroupIndex();
    renderCurrent();
  });

  $("logoutBtn").addEventListener("click", () => {
    forgetCurrentParticipant();
    $("password").value = "";
    show("loginView");
  });

  $("doneLogoutBtn").addEventListener("click", () => {
    forgetCurrentParticipant();
    $("password").value = "";
    show("loginView");
  });

  $("adminRefreshBtn").addEventListener("click", async () => {
    try {
      await loadAdminStats();
    } catch (error) {
      alert(error.message);
    }
  });

  $("adminExportBtn").addEventListener("click", async () => {
    try {
      await exportAdminCsv();
    } catch (error) {
      alert(error.message);
    }
  });

  $("adminClearAllBtn").addEventListener("click", async () => {
    try {
      await clearAllUsers();
    } catch (error) {
      alert(error.message);
    }
  });

  $("adminParticipants").addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-admin-action]");
    if (!button) return;
    const username = button.dataset.username;
    try {
      if (button.dataset.adminAction === "clear-ratings") {
        await clearUserRatings(username);
      } else if (button.dataset.adminAction === "delete-user") {
        await deleteUser(username);
      }
    } catch (error) {
      alert(error.message);
    }
  });

  $("adminLogoutBtn").addEventListener("click", () => {
    state.adminSession = "";
    localStorage.removeItem(STORAGE_ADMIN_SESSION);
    show("loginView");
  });
}

wireEvents();

const savedName = localStorage.getItem(STORAGE_NAME);
if (savedName) {
  $("username").value = savedName;
}
