const MEDIAPIPE_BUNDLE = "/vendor/tasks-vision/vision_bundle.mjs";
const MEDIAPIPE_WASM = "/vendor/tasks-vision/wasm";
const HAND_MODEL = "/models/hand_landmarker.task";
const SUBJECT_COLORS = {
  calculus: "#30d3be",
  linear_probability: "#4ea0ff",
  physics: "#f7a34a",
};
const SUBJECT_LABELS = {
  calculus: "高数",
  linear_probability: "线代/概率",
  physics: "大物",
};
const MONSTER_ASSETS = {
  calculus: "/assets/kenney/slime.png",
  linear_probability: "/assets/kenney/demon.png",
  physics: "/assets/kenney/ghost.png",
  advanced: "/assets/kenney/spider.png",
  elite: "/assets/kenney/scorpion.png",
};

const state = {
  token: "",
  session: null,
  sessionId: "",
  enemy: null,
  currentFormulaId: "",
  activeSubject: "",
  heldConcept: null,
  busy: false,
  gameOver: false,
  won: false,
  metrics: {},
  conceptStats: {},
  cursor: { x: window.innerWidth * 0.5, y: window.innerHeight * 0.5 },
  lastGesture: "move",
  lastHandGesture: "move",
  cameraReady: false,
  handDetected: false,
  handLandmarker: null,
  videoReady: false,
  trace: [],
  lastResult: "",
  decisionText: "等待建立 AI 会话。",
};
let lastRenderedFormulaKey = "";

const dom = {
  progressText: document.querySelector("#progressText"),
  lifeDots: document.querySelector("#lifeDots"),
  levelText: document.querySelector("#levelText"),
  aiDot: document.querySelector("#aiDot"),
  aiOnlineText: document.querySelector("#aiOnlineText"),
  aiTopStatus: document.querySelector("#aiTopStatus"),
  battleTicks: document.querySelector("#battleTicks"),
  enemyIndex: document.querySelector("#enemyIndex"),
  enemyName: document.querySelector("#enemyName"),
  monsterCreature: document.querySelector("#monsterCreature"),
  monsterSprite: document.querySelector("#monsterSprite"),
  formulaText: document.querySelector("#formulaText"),
  formulaSkill: document.querySelector("#formulaSkill"),
  formulaPanel: document.querySelector("#formulaPanel"),
  formulaSlots: document.querySelector("#formulaSlots"),
  heldText: document.querySelector("#heldText"),
  bookRow: document.querySelector("#bookRow"),
  contentDrawer: document.querySelector("#contentDrawer"),
  accuracyValue: document.querySelector("#accuracyValue"),
  reactionValue: document.querySelector("#reactionValue"),
  wrongValue: document.querySelector("#wrongValue"),
  streakValue: document.querySelector("#streakValue"),
  weakConcepts: document.querySelector("#weakConcepts"),
  decisionText: document.querySelector("#decisionText"),
  hintBox: document.querySelector("#hintBox"),
  hintText: document.querySelector("#hintText"),
  lastResultText: document.querySelector("#lastResultText"),
  aiTrace: document.querySelector("#aiTrace"),
  modelBadge: document.querySelector("#modelBadge"),
  cursor: document.querySelector("#cursor"),
  cameraCanvas: document.querySelector("#cameraCanvas"),
  cameraVideo: document.querySelector("#cameraVideo"),
  cameraState: document.querySelector("#cameraState"),
  gestureState: document.querySelector("#gestureState"),
  startOverlay: document.querySelector("#startOverlay"),
  startStatus: document.querySelector("#startStatus"),
  startButton: document.querySelector("#startButton"),
  loadingOverlay: document.querySelector("#loadingOverlay"),
  loadingTitle: document.querySelector("#loadingTitle"),
  loadingText: document.querySelector("#loadingText"),
  resultOverlay: document.querySelector("#resultOverlay"),
  resultTitle: document.querySelector("#resultTitle"),
  resultScore: document.querySelector("#resultScore"),
  resultExplanation: document.querySelector("#resultExplanation"),
  resultFeedback: document.querySelector("#resultFeedback"),
  reportOverlay: document.querySelector("#reportOverlay"),
  reportTitle: document.querySelector("#reportTitle"),
  reportProgress: document.querySelector("#reportProgress"),
  reportSummary: document.querySelector("#reportSummary"),
  reportStrengths: document.querySelector("#reportStrengths"),
  reportWeaknesses: document.querySelector("#reportWeaknesses"),
  reportChapters: document.querySelector("#reportChapters"),
  reportNextSteps: document.querySelector("#reportNextSteps"),
  errorOverlay: document.querySelector("#errorOverlay"),
  errorText: document.querySelector("#errorText"),
  retryButton: document.querySelector("#retryButton"),
  reloadButton: document.querySelector("#reloadButton"),
  monsterZone: document.querySelector("#monsterZone"),
};

const cameraContext = dom.cameraCanvas.getContext("2d");

function initAccessToken() {
  const params = new URLSearchParams(window.location.search);
  const queryToken = params.get("token");
  if (queryToken) {
    sessionStorage.setItem("kd_demo_token", queryToken);
    params.delete("token");
    const query = params.toString();
    history.replaceState({}, "", `${window.location.pathname}${query ? `?${query}` : ""}`);
  }
  state.token = sessionStorage.getItem("kd_demo_token") || "";
}

async function api(path, body = {}) {
  const headers = { "Content-Type": "application/json" };
  if (state.token) {
    headers["X-Demo-Token"] = state.token;
  }
  const response = await fetch(path, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error || `请求失败：${response.status}`);
  }
  return data;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function setTrace(message) {
  state.trace.push(message);
  state.trace = state.trace.slice(-8);
  dom.aiTrace.innerHTML = state.trace
    .slice(-5)
    .map((item) => `<div class="trace-item">${escapeHtml(item)}</div>`)
    .join("");
}

function setBusy(busy, title = "AI 正在工作", text = "正在理解当前学情。") {
  state.busy = busy;
  dom.aiDot.classList.toggle("busy", busy);
  dom.aiOnlineText.textContent = busy ? "AI 工作中" : "AI 在线";
  dom.aiTopStatus.textContent = busy ? title : "推理服务可用";
  dom.loadingTitle.textContent = title;
  dom.loadingText.textContent = text;
  dom.loadingOverlay.classList.toggle("hidden", !busy);
}

function showError(error) {
  setBusy(false);
  dom.errorText.textContent = error instanceof Error ? error.message : String(error);
  dom.errorOverlay.classList.remove("hidden");
}

function currentFormula() {
  if (!state.enemy || !state.currentFormulaId) {
    return null;
  }
  return state.enemy.monster.formulas.find((item) => item.id === state.currentFormulaId) || null;
}

function render() {
  const sessionState = state.session?.state || {};
  const target = sessionState.targetEnemies || state.session?.targetEnemies || 30;
  const defeated = sessionState.enemiesDefeated || 0;
  const lives = sessionState.lives ?? sessionState.maxLives ?? 6;
  const maxLives = sessionState.maxLives || 6;
  const metrics = state.metrics || sessionState.metrics || {};

  dom.progressText.textContent = `${defeated} / ${target}`;
  dom.levelText.textContent = `L${sessionState.level || 1}`;
  dom.lifeDots.innerHTML = Array.from(
    { length: maxLives },
    (_, index) => `<span class="life-dot ${index < lives ? "active" : ""}"></span>`,
  ).join("");
  dom.battleTicks.innerHTML = Array.from(
    { length: maxLives + 1 },
    (_, index) => `<span class="battle-tick ${index === 0 ? "start" : ""}"></span>`,
  ).join("");

  dom.accuracyValue.textContent = `${Number(metrics.accuracy || 0).toFixed(1)}%`;
  dom.reactionValue.textContent = `${Number(metrics.average_response_seconds || 0).toFixed(2)}s`;
  dom.wrongValue.textContent = String(metrics.wrong || 0);
  dom.streakValue.textContent = String(metrics.streak || 0);
  dom.weakConcepts.innerHTML = renderWeakConcepts();
  dom.decisionText.textContent = state.decisionText;
  dom.lastResultText.textContent = state.lastResult || "尚无判题结果。";

  const hintEnabled = Boolean(state.enemy?.decision?.hintEnabled);
  dom.hintBox.classList.toggle("hidden", !hintEnabled);
  if (hintEnabled) {
    dom.hintText.textContent = state.enemy.decision.hintText || "先判断公式需要完成哪类运算。";
  }

  dom.heldText.textContent = state.heldConcept ? state.heldConcept.title : "尚未抓取";
  dom.cursor.classList.toggle("holding", Boolean(state.heldConcept));
  dom.cursor.classList.toggle("grab", state.lastGesture === "grab");
  dom.enemyIndex.textContent = `怪物 #${sessionState.enemyIndex || 1}`;
  dom.enemyName.textContent = state.enemy?.decision?.enemyName || "等待 AI 生成";

  const formula = currentFormula();
  if (formula) {
    renderFormula(formula);
    dom.formulaSkill.textContent = `${SUBJECT_LABELS[formula.subject] || formula.subject} · ${formula.skill_label}`;
    dom.formulaPanel.style.borderColor = SUBJECT_COLORS[formula.subject] || "#a882ff";
  } else if (state.enemy) {
    lastRenderedFormulaKey = "";
    dom.formulaText.textContent = "本怪公式已全部清除";
    dom.formulaSkill.textContent = "等待 AI 生成下一只怪物";
  } else {
    lastRenderedFormulaKey = "";
    dom.formulaText.textContent = state.gameOver ? "本局已结束" : "AI 正在生成怪物";
    dom.formulaSkill.textContent = state.decisionText;
  }

  const progress = maxLives > 0 ? (maxLives - lives) / maxLives : 0;
  dom.monsterCreature.style.setProperty("--monster-y", `${Math.round(progress * 170)}px`);
  dom.monsterCreature.style.setProperty("--monster-scale", `${(1 + progress * 0.36).toFixed(3)}`);
  dom.formulaPanel.style.setProperty("--formula-shift", `${Math.round(progress * 14)}px`);
  dom.monsterZone.classList.toggle("near", lives <= 2);
  dom.monsterSprite.src = formula
    ? monsterAssetFor(formula, sessionState.level || 2)
    : MONSTER_ASSETS.calculus;

  if (state.enemy) {
    const formulas = state.enemy.monster.formulas;
    const currentIndex = formulas.findIndex((item) => item.id === state.currentFormulaId);
    dom.formulaSlots.innerHTML = formulas
      .map((item, index) => {
        const cleared = currentIndex === -1 || index < currentIndex;
        return `<span class="formula-slot ${cleared ? "cleared" : ""}">${index + 1}</span>`;
      })
      .join("");
  } else {
    dom.formulaSlots.innerHTML = "";
  }

  renderBooks();
  renderKnowledge();
  renderTrace();
}

function renderWeakConcepts() {
  const stats = Object.values(state.conceptStats || {});
  const weak = stats
    .filter((item) => item.attempts > 0)
    .sort((left, right) => left.accuracy - right.accuracy)
    .slice(0, 3)
    .map((item) => `<li>${escapeHtml(item.title)} · ${Number(item.accuracy).toFixed(0)}%</li>`);
  return weak.length ? weak.join("") : "<li>等待答题数据</li>";
}

function renderFormula(formula) {
  const key = `${formula.id}:${formula.latex || formula.formula}`;
  if (key === lastRenderedFormulaKey) {
    return;
  }
  lastRenderedFormulaKey = key;
  const latex = formula.latex || formula.formula;
  if (window.MathJax?.typesetPromise) {
    dom.formulaText.textContent = `\\[${latex}\\]`;
    window.MathJax.typesetPromise([dom.formulaText]).catch(() => {
      dom.formulaText.textContent = formula.formula;
    });
  } else {
    dom.formulaText.textContent = formula.formula;
  }
}

function monsterAssetFor(formula, level) {
  if (level >= 3) {
    return state.enemy?.decision?.formulaCount > 1
      ? MONSTER_ASSETS.elite
      : MONSTER_ASSETS.advanced;
  }
  if (formula.skill_label?.includes("热") || formula.skill_label?.includes("熵")) {
    return MONSTER_ASSETS.advanced;
  }
  return MONSTER_ASSETS[formula.subject] || MONSTER_ASSETS.calculus;
}

function renderBooks() {
  if (!state.session) {
    dom.bookRow.innerHTML = "";
    return;
  }
  dom.bookRow.innerHTML = state.session.subjects
    .map(
      (subject) => `
        <button
          class="book-button ${state.activeSubject === subject.id ? "active" : ""}"
          style="--book-color:${SUBJECT_COLORS[subject.id]}"
          data-book="${subject.id}"
          type="button"
        >
          <strong>${escapeHtml(subject.label)}</strong>
          <span>${state.activeSubject === subject.id ? "已展开" : "选择课本"}</span>
        </button>
      `,
    )
    .join("");
}

function knowledgeForActiveSubject() {
  if (!state.activeSubject || !state.session) {
    return [];
  }
  const formula = currentFormula();
  if (formula && formula.subject === state.activeSubject) {
    return formula.candidates;
  }
  return state.session.knowledgeBySubject[state.activeSubject] || [];
}

function renderKnowledge() {
  if (!state.activeSubject) {
    dom.contentDrawer.innerHTML = "";
    return;
  }
  const items = knowledgeForActiveSubject();
  dom.contentDrawer.innerHTML = `
    <section class="knowledge-panel">
      <header>
        <h3>${escapeHtml(SUBJECT_LABELS[state.activeSubject] || state.activeSubject)}知识点</h3>
      </header>
      <div class="knowledge-grid">
        ${items
          .map(
            (item) => `
              <button
                class="knowledge-card ${state.heldConcept?.id === item.id ? "selected" : ""}"
                data-concept="${item.id}"
                type="button"
              >
                <strong>${escapeHtml(item.title)}</strong>
                <span>${escapeHtml(item.description)}</span>
              </button>
            `,
          )
          .join("")}
      </div>
    </section>
  `;
}

function renderTrace() {
  dom.aiTrace.innerHTML = state.trace
    .slice(-5)
    .map((item) => `<div class="trace-item">${escapeHtml(item)}</div>`)
    .join("") || '<div class="trace-item">等待 AI 推理记录</div>';
}

function updateSessionState(nextState) {
  if (!nextState) {
    return;
  }
  state.session.state = nextState;
  state.gameOver = Boolean(nextState.gameOver);
  state.won = Boolean(nextState.won);
  state.metrics = nextState.metrics || {};
  state.conceptStats = nextState.conceptStats || {};
}

async function startGame() {
  dom.startButton.disabled = true;
  dom.startStatus.textContent = "正在连接 AI 服务并请求摄像头权限...";
  setBusy(true, "建立游戏会话", "正在加载题库、摄像头和 Qwen 服务。");
  try {
    try {
      await initCamera();
    } catch (cameraError) {
      console.warn("Camera unavailable, mouse mode remains active", cameraError);
      state.cameraReady = false;
      dom.cameraState.textContent = "鼠标模式";
      dom.gestureState.textContent = "摄像头未授权";
    }
    const session = await api("/api/session");
    state.session = session;
    state.sessionId = session.sessionId;
    state.metrics = session.state.metrics;
    state.conceptStats = session.state.conceptStats;
    dom.modelBadge.textContent = session.model;
    state.trace = ["AI 会话已建立", "浏览器手势识别已启用"];
    dom.startOverlay.classList.add("hidden");
    await requestEnemy();
  } catch (error) {
    dom.startButton.disabled = false;
    dom.startStatus.textContent = "启动失败，请检查网络、API 配置和摄像头权限。";
    showError(error);
  } finally {
    if (!state.busy) {
      dom.startButton.disabled = false;
    }
  }
}

async function requestEnemy() {
  setBusy(true, "AI 正在设计怪物", "正在读取正确率、反应速度和最近答题记录。");
  try {
    const data = await api("/api/decide", { sessionId: state.sessionId });
    state.enemy = data;
    state.currentFormulaId = data.monster.currentFormulaId;
    state.activeSubject = "";
    state.heldConcept = null;
    state.decisionText = `${data.decision.enemyName} · L${data.decision.level} · ${data.decision.formulaCount} 个公式槽${data.decision.hintEnabled ? " · 提供提示" : ""}`;
    state.lastResult = data.decision.reason;
    updateSessionState(data.state);
    setTrace(`AI 生成怪物：${data.decision.enemyName}`);
    if (data.decision.hintEnabled && data.decision.hintText) {
      setTrace(`AI 提示：${data.decision.hintText}`);
    }
    setBusy(false);
    render();
  } catch (error) {
    showError(error);
  }
}

async function submitMatch() {
  if (!state.heldConcept || !state.currentFormulaId || state.busy) {
    return;
  }
  const formulaId = state.currentFormulaId;
  const concept = state.heldConcept;
  state.activeSubject = "";
  state.heldConcept = null;
  setBusy(true, "AI 正在分析", `正在判断“${concept.title}”与当前公式的语义关联。`);
  setTrace(`提交语义判断：${concept.title}`);
  try {
    const data = await api("/api/match", {
      sessionId: state.sessionId,
      formulaId,
      conceptId: concept.id,
    });
    updateSessionState(data.state);
    state.lastResult = data.match.feedback;
    setTrace(`AI 判题：${data.match.accepted ? "匹配成功" : "关联不足"} / ${data.match.score}%`);
    if (!data.match.accepted) {
      setTrace(`AI 提示：${data.match.feedback}`);
    }

    const monsterDefeated = data.resolved.monsterDefeated;
    const oldIndex = state.enemy.monster.formulas.findIndex((item) => item.id === formulaId);
    if (monsterDefeated) {
      state.enemy = null;
      state.currentFormulaId = "";
    } else {
      const next = state.enemy.monster.formulas[oldIndex + 1];
      state.currentFormulaId = next?.id || "";
    }

    dom.monsterCreature.classList.add(data.match.accepted ? "correct" : "wrong");
    setBusy(false);
    showResult(data.match);
    render();

    window.setTimeout(async () => {
      dom.monsterCreature.classList.remove("correct", "wrong");
      dom.resultOverlay.classList.add("hidden");
      if (state.gameOver) {
        await requestReport();
      } else if (monsterDefeated) {
        await requestEnemy();
      } else {
        render();
      }
    }, 2400);
  } catch (error) {
    state.heldConcept = concept;
    showError(error);
  }
}

function showResult(match) {
  dom.resultTitle.textContent = match.accepted ? "AI 判定：匹配成功" : "AI 判定：关联不足";
  dom.resultTitle.style.color = match.accepted ? "var(--green)" : "var(--red)";
  dom.resultScore.textContent = `${match.score}%`;
  dom.resultExplanation.textContent = match.explanation;
  dom.resultFeedback.textContent = match.feedback;
  dom.resultOverlay.classList.remove("hidden");
}

async function requestReport() {
  setBusy(true, "AI 正在生成学习报告", "正在汇总知识点掌握度、反应速度和错误记录。");
  try {
    const data = await api("/api/report", { sessionId: state.sessionId });
    updateSessionState(data.state);
    const report = data.report;
    dom.reportTitle.textContent = state.won ? "挑战成功 · AI 学习报告" : "挑战结束 · AI 学习报告";
    dom.reportProgress.textContent = `${state.session.state.enemiesDefeated} / ${state.session.state.targetEnemies}`;
    dom.reportSummary.textContent = report.summary;
    fillList(dom.reportStrengths, report.strengths);
    fillList(dom.reportWeaknesses, report.weaknesses);
    fillList(dom.reportChapters, report.reviewChapters);
    fillList(dom.reportNextSteps, report.nextSteps);
    dom.reportOverlay.classList.remove("hidden");
    setBusy(false);
  } catch (error) {
    showError(error);
  }
}

function fillList(element, items) {
  element.innerHTML = (items || [])
    .map((item) => `<li>${escapeHtml(item)}</li>`)
    .join("");
}

function selectBook(subject) {
  if (state.busy || state.gameOver) {
    return;
  }
  state.activeSubject = state.activeSubject === subject ? "" : subject;
  render();
}

function selectConcept(conceptId) {
  if (state.busy || state.gameOver) {
    return;
  }
  const item = knowledgeForActiveSubject().find((concept) => concept.id === conceptId);
  if (!item) {
    return;
  }
  state.heldConcept = item;
  state.activeSubject = "";
  setTrace(`抓取知识点：${item.title}`);
  render();
}

function handleGrabAt(point) {
  if (state.busy || state.gameOver) {
    return;
  }
  const element = document.elementFromPoint(point.x, point.y);
  if (!element) {
    return;
  }
  const book = element.closest("[data-book]");
  if (book) {
    selectBook(book.dataset.book);
    return;
  }
  const card = element.closest("[data-concept]");
  if (card) {
    selectConcept(card.dataset.concept);
    return;
  }
  if (state.heldConcept && dom.monsterZone.contains(element)) {
    submitMatch();
  }
}

function handleThrowAt(point) {
  if (!state.heldConcept || state.busy) {
    return;
  }
  const rect = dom.monsterZone.getBoundingClientRect();
  if (point.x >= rect.left && point.x <= rect.right && point.y >= rect.top && point.y <= rect.bottom) {
    submitMatch();
  }
}

function updateCursor(point) {
  state.cursor.x = Math.max(0, Math.min(window.innerWidth, point.x));
  state.cursor.y = Math.max(0, Math.min(window.innerHeight, point.y));
  dom.cursor.style.left = `${state.cursor.x}px`;
  dom.cursor.style.top = `${state.cursor.y}px`;
}

async function initCamera() {
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new Error("当前浏览器不支持摄像头访问，请使用最新版 Chrome 或 Edge。");
  }
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: false,
    video: {
      facingMode: "user",
      width: { ideal: 1280 },
      height: { ideal: 720 },
    },
  });
  dom.cameraVideo.srcObject = stream;
  await dom.cameraVideo.play();
  state.cameraReady = true;
  dom.cameraState.textContent = "摄像头在线";
  dom.gestureState.textContent = "手势加载中";
  initializeHandTracking();
}

async function initializeHandTracking() {
  try {
    const vision = await import(MEDIAPIPE_BUNDLE);
    const fileset = await vision.FilesetResolver.forVisionTasks(MEDIAPIPE_WASM);
    state.handLandmarker = await vision.HandLandmarker.createFromOptions(fileset, {
      baseOptions: {
        modelAssetPath: HAND_MODEL,
        delegate: "GPU",
      },
      runningMode: "VIDEO",
      numHands: 1,
      minHandDetectionConfidence: 0.6,
      minHandPresenceConfidence: 0.55,
      minTrackingConfidence: 0.55,
    });
    dom.gestureState.textContent = "手势识别在线";
  } catch (error) {
    console.warn("MediaPipe failed to initialize", error);
    dom.gestureState.textContent = "鼠标模式";
  }
}

function classifyGesture(landmarks) {
  if (!landmarks || landmarks.length < 21) {
    return "move";
  }
  const fingertips = [4, 8, 12, 16, 20];
  const pips = [3, 6, 10, 14, 18];
  const extended = fingertips.map((tip, index) => {
    const pip = pips[index];
    if (tip === 4) {
      return Math.abs(landmarks[tip].x - landmarks[0].x) > Math.abs(landmarks[pip].x - landmarks[0].x) * 1.12;
    }
    return landmarks[tip].y < landmarks[pip].y - 0.015;
  });
  const count = extended.filter(Boolean).length;
  if (count <= 1) {
    return "grab";
  }
  if (count >= 4) {
    return "throw";
  }
  return "move";
}

function drawHandLandmarks(landmarks) {
  const width = dom.cameraCanvas.width;
  const height = dom.cameraCanvas.height;
  if (!landmarks) {
    return;
  }
  const connections = [
    [0, 1], [1, 2], [2, 3], [3, 4],
    [0, 5], [5, 6], [6, 7], [7, 8],
    [5, 9], [9, 10], [10, 11], [11, 12],
    [9, 13], [13, 14], [14, 15], [15, 16],
    [13, 17], [17, 18], [18, 19], [19, 20], [0, 17],
  ];
  cameraContext.strokeStyle = "#50e3c2";
  cameraContext.lineWidth = 2;
  for (const [start, end] of connections) {
    cameraContext.beginPath();
    cameraContext.moveTo(landmarks[start].x * width, landmarks[start].y * height);
    cameraContext.lineTo(landmarks[end].x * width, landmarks[end].y * height);
    cameraContext.stroke();
  }
  cameraContext.fillStyle = "#ffe064";
  for (const point of landmarks) {
    cameraContext.beginPath();
    cameraContext.arc(point.x * width, point.y * height, 2.5, 0, Math.PI * 2);
    cameraContext.fill();
  }
}

let lastVideoTime = -1;
function processCameraFrame() {
  if (!state.cameraReady || dom.cameraVideo.readyState < 2) {
    return;
  }
  const width = dom.cameraCanvas.width;
  const height = dom.cameraCanvas.height;
  cameraContext.clearRect(0, 0, width, height);
  cameraContext.drawImage(dom.cameraVideo, 0, 0, width, height);
  if (state.handLandmarker && dom.cameraVideo.currentTime !== lastVideoTime) {
    lastVideoTime = dom.cameraVideo.currentTime;
    try {
      const result = state.handLandmarker.detectForVideo(dom.cameraVideo, performance.now());
      const landmarks = result.landmarks?.[0] || null;
      state.handDetected = Boolean(landmarks);
      if (landmarks) {
        drawHandLandmarks(landmarks);
        const palm = landmarks[9];
        updateCursor({
          x: (1 - palm.x) * window.innerWidth,
          y: palm.y * window.innerHeight,
        });
        const gesture = classifyGesture(landmarks);
        state.lastGesture = gesture;
        dom.gestureState.textContent = gesture === "grab" ? "握拳" : gesture === "throw" ? "张开" : "移动";
        if (gesture !== state.lastHandGesture) {
          if (gesture === "grab") {
            handleGrabAt(state.cursor);
          }
          if (gesture === "throw") {
            handleThrowAt(state.cursor);
          }
          state.lastHandGesture = gesture;
        }
      } else {
        state.handDetected = false;
        state.lastGesture = "move";
        state.lastHandGesture = "move";
        dom.gestureState.textContent = "等待手部";
      }
    } catch (error) {
      console.warn("Hand detection frame failed", error);
    }
  }
}

let animationFrame = 0;
function animationLoop() {
  animationFrame = requestAnimationFrame(animationLoop);
  processCameraFrame();
  dom.cursor.classList.toggle("grab", state.lastGesture === "grab");
}

document.addEventListener("pointermove", (event) => {
  if (!state.cameraReady || !state.handDetected) {
    updateCursor({ x: event.clientX, y: event.clientY });
  }
});

document.addEventListener("click", (event) => {
  const book = event.target.closest("[data-book]");
  if (book) {
    selectBook(book.dataset.book);
    return;
  }
  const card = event.target.closest("[data-concept]");
  if (card) {
    selectConcept(card.dataset.concept);
    return;
  }
  if (state.heldConcept && dom.monsterZone.contains(event.target)) {
    submitMatch();
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key.toLowerCase() === "f") {
    handleGrabAt(state.cursor);
  }
  if (event.code === "Space") {
    event.preventDefault();
    handleThrowAt(state.cursor);
  }
});

dom.startButton.addEventListener("click", startGame);
dom.retryButton.addEventListener("click", () => {
  dom.errorOverlay.classList.add("hidden");
  if (!state.sessionId) {
    startGame();
  } else if (state.gameOver) {
    requestReport();
  } else if (!state.enemy) {
    requestEnemy();
  } else {
    setBusy(false);
    render();
  }
});
dom.reloadButton.addEventListener("click", () => window.location.reload());

initAccessToken();
updateCursor(state.cursor);
render();
animationLoop();
window.addEventListener("load", () => {
  window.MathJax?.startup?.promise?.then(() => {
    lastRenderedFormulaKey = "";
    if (state.enemy) {
      render();
    }
  });
});
