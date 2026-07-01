const FIELDS = [
  ["visual_quality", "画面质量"],
  ["occlusion", "遮挡情况"],
  ["lip_sync", "唇形同步"],
  ["teeth_quality", "牙齿质量"],
];
const STORAGE_ID = "userstudy_participant_id";
const STORAGE_NAME = "userstudy_username";

const state = {
  groups: [],
  participant: null,
  ratings: {},
  index: 0,
};

const $ = (id) => document.getElementById(id);

function show(viewId) {
  for (const id of ["loginView", "studyView", "doneView"]) {
    $(id).classList.toggle("hidden", id !== viewId);
  }
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

function setStatus(text, warn = false) {
  $("saveStatus").textContent = text;
  $("saveStatus").classList.toggle("warn", warn);
}

function totalVideoCount() {
  return state.groups.reduce((total, group) => total + group.videos.length, 0);
}

function completedVideoCount() {
  return Object.keys(state.ratings).length;
}

function groupRated(group) {
  return group.videos.every((video) => state.ratings[video.id]);
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
  $("videoGrid").innerHTML = group.videos.map(videoCard).join("");
  $("prevBtn").disabled = state.index === 0;
  $("nextBtn").textContent = state.index === state.groups.length - 1 ? "保存并完成" : "保存并下一个样本";
  setStatus(groupRated(group) ? "当前样本已保存" : "");
  updateProgress();
  wireVideoPlayback();
}

function readVideoRating(video) {
  const rating = {};
  for (const [field, label] of FIELDS) {
    const selected = document.querySelector(`input[name="${scoreInputName(video.id, field)}"]:checked`);
    if (!selected) {
      throw new Error(`请完成 ${video.label} 的「${label}」评分`);
    }
    rating[field] = Number(selected.value);
  }
  return rating;
}

async function saveCurrentGroup() {
  const group = currentGroup();
  const pending = group.videos.map((video) => ({
    video,
    rating: readVideoRating(video),
  }));

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
  setStatus("当前样本已保存");
  updateProgress();
}

async function completeIfReady() {
  if (completedVideoCount() < totalVideoCount()) return false;
  await api("/api/complete", {
    method: "POST",
    body: JSON.stringify({ participant_id: state.participant.id }),
  });
  show("doneView");
  return true;
}

async function startStudy(username) {
  const participantId = localStorage.getItem(STORAGE_ID);
  const body = await api("/api/participants", {
    method: "POST",
    body: JSON.stringify({ username, participant_id: participantId }),
  });

  state.participant = body.participant;
  state.groups = body.groups || [];
  state.ratings = body.ratings || {};
  state.index = firstIncompleteGroupIndex();

  localStorage.setItem(STORAGE_ID, state.participant.id);
  localStorage.setItem(STORAGE_NAME, state.participant.username);

  if (!state.groups.length) {
    alert("没有可评分的视频，请检查 videos.json");
    return;
  }
  show("studyView");
  renderCurrent();
}

function wireEvents() {
  $("loginForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const username = $("username").value.trim();
    if (!username) return;
    try {
      await startStudy(username);
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

  $("prevBtn").addEventListener("click", () => {
    if (state.index > 0) {
      state.index -= 1;
      renderCurrent();
    }
  });

  $("editDoneBtn").addEventListener("click", () => {
    show("studyView");
    state.index = firstIncompleteGroupIndex();
    renderCurrent();
  });
}

wireEvents();

const savedName = localStorage.getItem(STORAGE_NAME);
if (savedName) {
  $("username").value = savedName;
}
