/* 냉장고 털이 AI — 프런트 로직 (목업) */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const PAGE = document.body.dataset.page;

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.remove("show"), 2600);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body && !(opts.body instanceof FormData)
      ? { "Content-Type": "application/json" } : {},
    ...opts,
    body: opts.body instanceof FormData ? opts.body : opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || `요청 실패 (${res.status})`);
  }
  return data;
}

function fmtD(iso) {
  const d = new Date(iso + "T00:00:00");
  const wd = ["일", "월", "화", "수", "목", "금", "토"][d.getDay()];
  return `${d.getMonth() + 1}/${d.getDate()}(${wd})`;
}

function dBadge(days) {
  if (days < 0) return `<span class="badge r">기한 지남</span>`;
  if (days <= 2) return `<span class="badge r">D-${days} 긴급</span>`;
  if (days <= 7) return `<span class="badge y">D-${days} 주의</span>`;
  return `<span class="badge g">D-${days}</span>`;
}

function pctBar(p) {
  const cls = p < 70 ? "low" : p <= 110 ? "mid" : "over";
  const w = Math.min(p, 150);
  return `<div class="bar-wrap"><div class="bar ${cls}" style="width:${w * 0.66}%"></div></div>`;
}

/* 로딩 스켈레톤 */
function skelRows(n = 4) {
  return `<div class="skel-wrap">${Array.from({ length: n },
    () => `<div class="skeleton skel-row"></div>`).join("")}</div>`;
}
function skelCards(n = 4) {
  return Array.from({ length: n }, () => `<div class="skeleton skel-card"></div>`).join("");
}
function skelRecipes(n = 6) {
  return `<div class="skel-wrap grid3">${Array.from({ length: n },
    () => `<div class="skeleton skel-recipe"></div>`).join("")}</div>`;
}
function skelBar() {
  return `<div class="skeleton skel-bar"></div>`;
}

function recipeCard(r) {
  const ings = r.ingredients.map(i => {
    const amt = i.amount ? ` ${esc(i.amount)}` : "";
    const tip = i.amount_raw && i.amount_raw !== i.amount ? ` title="원본(${r.servings}인분): ${esc(i.amount_raw)}"` : "";
    if (r.matched.includes(i.name)) {
      return `<span${tip} class="${r.urgent_matched.includes(i.name) ? "urgent" : "hit"}">${esc(i.name)}${amt}</span>`;
    }
    return `<span${tip} class="miss">${esc(i.name)}${amt}</span>`;
  }).join("");
  const boosts = r.boost.map(b => `<span class="badge b">${esc(b)} 보충</span>`).join(" ");
  const meta = [r.type, r.servings ? r.servings + "인분" : "", r.cook_time, r.difficulty,
                r.servings > 1 ? "재료 1인분 기준" : ""]
    .filter(Boolean).join(" · ");
  return `
  <div class="recipe">
    <div class="score">${r.score}점</div>
    <div>
      <div class="title">${esc(r.title)}</div>
      <div class="meta">${esc(meta)}</div>
    </div>
    <div class="ing-tags">${ings}</div>
    ${boosts ? `<div>${boosts}</div>` : ""}
    <div class="steps-mini">${r.steps.slice(0, 2).map(esc).join(" → ")}${r.steps.length > 2 ? "…" : ""}</div>
    <div class="card-head" style="margin:0">
      <a class="rlink" href="${esc(r.url)}" target="_blank" rel="noopener">만개의레시피에서 보기 ↗</a>
      <button class="btn cook-btn" data-idx="${r.index}" ${r.matched.length === 0 ? "disabled" : ""}
        title="냉장고에서 이 요리에 사용한 재료를 제거합니다">요리했어요! 🍳</button>
      <span class="badge n">${r.matched.length}/${r.ingredients.length} 재료 보유</span>
    </div>
  </div>`;
}

let charts = {};
function renderChart(id, conf) {
  if (charts[id]) charts[id].destroy();
  charts[id] = new Chart($("#" + id), conf);
}
const C = {
  accent: "#a78bfa", accentSoft: "rgba(167,139,250,.25)",
  warn: "#fbbf24", danger: "#fb7185", ink: "#eceafb", grid: "rgba(155,138,255,.16)",
};
/* 플랫 다크 테마: 차트 텍스트·격자 색 전역 적용 */
Chart.defaults.color = "#a29cc9";
Chart.defaults.borderColor = C.grid;
Chart.defaults.font.family = "Pretendard Variable, Pretendard, -apple-system, Malgun Gothic, sans-serif";

/* ------------------------------------------------------------------ 대시보드 */
async function initDashboard() {
  $("#stat-cards").innerHTML = skelCards(4);
  $("#urgent-list").innerHTML = skelRows(3);
  $("#deficit-list").innerHTML = skelRows(3);
  $("#recipe-cards").innerHTML = skelRecipes(3);

  const d = await api("/api/dashboard");
  const cal = d.calorie;

  $("#stat-cards").innerHTML = [
    `<div class="stat ${d.fridge.urgent_count ? "danger" : "ok"}">
       <div class="k">냉장고 재료</div><div class="v">${d.fridge.total}개</div>
       <div class="s">유통기한 3일 내 ${d.fridge.urgent_count}개 · 7일 내 ${d.fridge.caution_count}개</div></div>`,
    `<div class="stat ${cal.today_pct >= 100 ? "ok" : ""}">
       <div class="k">오늘 섭취 칼로리</div><div class="v">${cal.today}</div>
       <div class="s">목표 ${cal.target} kcal 중 ${cal.today_pct}%</div></div>`,
    `<div class="stat"><div class="k">최근 기록일 평균</div><div class="v">${cal.avg7}</div>
       <div class="s">내일 예측 약 ${cal.forecast} kcal</div></div>`,
    `<div class="stat ${d.nutrition_score < 60 ? "danger" : "ok"}">
       <div class="k">영양 밸런스 지수</div><div class="v">${d.nutrition_score}점</div>
       <div class="s">${d.deficits.length ? `부족: ${d.deficits.map(x => x.label).join(" · ")}` : "결핍 없음"}</div></div>`,
  ].join("");

  $("#urgent-list").innerHTML = d.fridge.urgent.length
    ? d.fridge.urgent.map(i => `
      <div class="row">
        <div class="left"><span class="name">${esc(i.name)}</span><span class="sub">${esc(i.amount || "")}</span></div>
        ${dBadge(i.days_left)}
      </div>`).join("")
    : `<div class="empty-note">임박 재료가 없습니다. 훌륭해요!</div>`;

  $("#deficit-list").innerHTML = d.deficits.length
    ? d.deficits.map(x => `
      <div class="row">
        <div class="left"><span class="name">${esc(x.label)}</span>
          <span class="sub">${x.avg}${esc(x.unit)} / 권장 ${x.rda}${esc(x.unit)}</span></div>
        ${pctBar(x.pct)}<span class="badge r">${x.pct}%</span>
      </div>`).join("")
    : `<div class="empty-note">결핍 영양소가 없습니다.</div>`;

  $("#recipe-cards").innerHTML = d.recipes.map(recipeCard).join("");
}

/* ------------------------------------------------------------------ 냉장고 */
async function initFridge() {
  const load = async () => {
    $("#fridge-summary").innerHTML = skelBar();
    $("#fridge-list").innerHTML = skelRows(7);
    const d = await api("/api/fridge");
    $("#fridge-summary").innerHTML =
      `총 ${d.summary.total}개 · 긴급 ${d.summary.urgent} · 주의 ${d.summary.caution}`;
    $("#fridge-list").innerHTML = d.items.map(i => `
      <div class="row">
        <div class="left">
          <span class="name">${esc(i.name)}</span>
          <span class="sub">${esc(i.category)} · ${esc(i.amount || "-")} · ${fmtD(i.expiry)} 도래</span>
          <span class="badge n">${esc(i.source)}</span>
        </div>
        ${dBadge(i.days_left)}
        <button class="btn sm danger-ghost" data-del="${i.id}">삭제</button>
      </div>`).join("") || `<div class="empty-note">등록된 재료가 없습니다.</div>`;
  };

  $("#fridge-form").addEventListener("submit", async e => {
    e.preventDefault();
    const f = e.target;
    try {
      await api("/api/fridge", { method: "POST", body: {
        name: f.name.value, category: f.category.value,
        amount: f.amount.value, expiry: f.expiry.value,
      }});
      f.reset();
      toast("재료가 등록되었습니다.");
      await load();
    } catch (err) { toast(err.message); }
  });

  $("#fridge-list").addEventListener("click", async e => {
    const id = e.target.dataset.del;
    if (!id) return;
    await api(`/api/fridge/${id}`, { method: "DELETE" });
    toast("삭제되었습니다.");
    await load();
  });

  await load();
}

/* ------------------------------------------------------------------ 식단 기록 */
async function initDiet() {
  $("input[name=date]").value = new Date().toISOString().slice(0, 10);

  const loadChart = async () => {
    $("#cal-summary").innerHTML = skelBar();
    const cal = await api("/api/calorie/summary");
    $("#cal-summary").innerHTML =
      `평균 <b>${cal.avg7}</b> kcal · 내일 예측 <b>${cal.forecast}</b> kcal · 목표 ${cal.target} kcal`;
    renderChart("cal-chart", {
      type: "bar",
      data: {
        labels: cal.daily.map(d => fmtD(d.date)),
        datasets: [{
          label: "섭취 kcal", data: cal.daily.map(d => d.kcal),
          backgroundColor: cal.daily.map(d => (d.kcal > cal.target ? C.danger : C.accent)),
          borderRadius: 8,
        }, {
          label: "내일 예측", data: [...cal.daily.slice(0, 6).map(() => null), cal.forecast],
          backgroundColor: C.accentSoft, borderColor: C.accent, borderWidth: 2,
          borderDash: [5, 4], borderRadius: 8,
        }],
      },
      options: {
        maintainAspectRatio: false, responsive: true,
        plugins: { legend: { position: "bottom" } },
        scales: {
          y: { beginAtZero: true, grid: { color: C.grid },
               title: { display: true, text: "kcal" } },
          x: { grid: { display: false } },
        },
      },
    });
  };

  const loadMeals = async () => {
    $("#meal-list").innerHTML = skelRows(6);
    const d = await api("/api/meals?days=7");
    $("#meal-list").innerHTML = d.meals.map(m => `
      <div class="row">
        <div class="left">
          <span class="name">${fmtD(m.date)} ${esc(m.type)}</span>
          <span class="sub">${m.items.map(i => `${esc(i.name)} ${i.amount}g`).join(", ")}${m.memo ? " · " + esc(m.memo) : ""}</span>
        </div>
        <span class="badge g">${m.kcal} kcal</span>
        <button class="btn sm danger-ghost" data-del="${m.id}">삭제</button>
      </div>`).join("") || `<div class="empty-note">기록이 없습니다.</div>`;
  };

  $("#add-item").addEventListener("click", () => {
    const div = document.createElement("div");
    div.className = "form-row item-row";
    div.innerHTML = `<label>재료 <input name="item-name" placeholder="예: 두부"></label>
      <label>양(g) <input type="number" name="item-amount" value="100" min="1"></label>`;
    $("#meal-items").appendChild(div);
  });

  $("#meal-form").addEventListener("submit", async e => {
    e.preventDefault();
    const f = e.target;
    const names = [...f.querySelectorAll("[name=item-name]")];
    const amounts = [...f.querySelectorAll("[name=item-amount]")];
    const items = names.map((n, i) => ({ name: n.value, amount: +amounts[i].value }))
      .filter(i => i.name && i.amount > 0);
    if (!items.length) { toast("재료를 입력하세요."); return; }
    try {
      const d = await api("/api/meals", { method: "POST", body: {
        date: f.date.value, type: f.type.value, items,
      }});
      toast(`기록 완료 · ${d.meal.kcal} kcal`);
      $("#meal-form-result").innerHTML =
        `✅ 방금 기록: <b>${d.meal.kcal} kcal</b> — 오늘 누적 <b>${d.calorie.today}</b> kcal (목표 ${d.calorie.target})`;
      f.querySelectorAll("[name=item-name]").forEach(n => n.value = "");
      await Promise.all([loadChart(), loadMeals()]);
    } catch (err) { toast(err.message); }
  });

  $("#meal-list").addEventListener("click", async e => {
    const id = e.target.dataset.del;
    if (!id) return;
    await api(`/api/meals/${id}`, { method: "DELETE" });
    toast("삭제되었습니다.");
    await Promise.all([loadChart(), loadMeals()]);
  });

  await Promise.all([loadChart(), loadMeals()]);
}

/* ------------------------------------------------------------------ 레시피 */
async function initRecipes() {
  const load = async () => {
    $("#usual-summary").innerHTML = skelRows(2);
    $("#recipe-cards").innerHTML = skelRecipes(9);
    const d = await api("/api/recipes/recommend?limit=9");
    $("#usual-summary").innerHTML = `
      <div class="row"><div class="left">
        <span class="name">평소 자주 쓰는 재료</span>
        <span class="sub">${d.usual.top_items.map(t => `${esc(t.name)} (${t.count}회)`).join(" · ")}</span>
      </div></div>
      <div class="row"><div class="left">
        <span class="name">평소 식단 유형</span>
        <span class="sub">${d.usual.usual_types.map(esc).join(" · ")}</span>
      </div>
      <span class="badge g">이 유형 +12점</span></div>`;
    $("#recipe-cards").innerHTML = d.recipes.map(recipeCard).join("");
    $("#allergy-note").textContent = d.allergy_excluded
      ? `알레르기 설정으로 ${d.allergy_excluded}개 레시피를 제외했습니다.`
      : "알레르기로 제외된 레시피가 없습니다.";
  };

  $("#allergy-chips").addEventListener("click", async e => {
    const chip = e.target.closest(".chip");
    if (!chip) return;
    chip.classList.toggle("on");
    const keys = [...document.querySelectorAll("#allergy-chips .chip.on")]
      .map(c => c.dataset.allergy);
    try {
      await api("/api/profile", { method: "PATCH", body: { allergies: keys } });
      toast(keys.length
        ? `알레르기 설정 저장 — ${keys.length}개 항목 제외 중`
        : "알레르기 설정을 초기화했습니다.");
      await load();
    } catch (err) {
      toast(err.message);
      chip.classList.toggle("on"); // 저장 실패 시 원래 상태로 되돌림
    }
  });

  $("#re-roll").addEventListener("click", () => load().catch(e => toast(e.message)));
  await load();
}

/* ------------------------------------------------------------------ 영양 분석 */
async function initNutrition() {
  $("#score-big").textContent = "…";
  $("#nutri-rows").innerHTML = skelRows(8);
  $("#advice-list").innerHTML = skelRows(3);
  $("#deficit-top").innerHTML = `<span class="badge n">분석 중…</span>`;

  const d = await api("/api/nutrition/summary?days=7");
  $("#score-big").textContent = d.score + "점";
  $("#score-label").textContent =
    d.score >= 80 ? "균형 좋음" : d.score >= 60 ? "보통 — 개선 여지 있음" : "결핍 주의";

  $("#deficit-top").innerHTML = d.deficits.map(x =>
    `<span class="badge r">${esc(x.label)} ${x.pct}%</span>`).join("")
    || `<span class="badge g">결핍 없음</span>`;

  renderChart("nutri-radar", {
    type: "radar",
    data: {
      labels: d.rows.map(r => r.label),
      datasets: [{
        label: "권장 섭취 대비 %", data: d.rows.map(r => r.pct),
        backgroundColor: "rgba(167,139,250,.2)", borderColor: C.accent,
        pointBackgroundColor: d.rows.map(r => r.status === "부족" ? C.danger : C.accent),
      }, {
        label: "권장선(100%)", data: d.rows.map(() => 100),
        borderColor: "rgba(240,171,252,.75)", borderDash: [5, 4], pointRadius: 0,
        backgroundColor: "transparent",
      }],
    },
    options: {
      maintainAspectRatio: false, responsive: true,
      scales: { r: { min: 0, max: 150, ticks: { stepSize: 50, backdropColor: "transparent" },
                      grid: { color: C.grid } } },
      plugins: { legend: { position: "bottom" } },
    },
  });

  $("#nutri-rows").innerHTML = d.rows.map(r => `
    <div class="row">
      <div class="left" style="min-width:170px"><span class="name">${esc(r.label)}</span></div>
      <span class="sub" style="flex:0 0 150px">평균 ${r.avg}${esc(r.unit)} / ${r.rda}${esc(r.unit)}</span>
      ${pctBar(r.pct)}
      <span class="badge ${r.status === "부족" ? "r" : r.status === "과다" ? "b" : "g"}">${r.status} ${r.pct}%</span>
    </div>`).join("");

  $("#advice-list").innerHTML = d.deficits.map(x => `
    <div class="row">
      <div class="left"><span class="name">${esc(x.label)} 부족</span>
        <span class="sub">권장의 ${x.pct}% — ${x.foods.map(esc).join(", ")} 등으로 보충 권장</span></div>
      <a class="link" href="/recipes">보충 레시피 →</a>
    </div>`).join("")
    || `<div class="empty-note">특별한 결핍이 없습니다. 현재 식단을 유지하세요.</div>`;

  const uk = Object.entries(d.unknown_items || {});
  $("#unknown-note").textContent = uk.length
    ? `영양 DB에 없어 미집계: ${uk.map(([n, c]) => `${n}(${c})`).join(", ")}` : "";
}

/* ------------------------------------------------------------- 요리했어요 */
async function cookRecipe(idx, btn) {
  btn.disabled = true;
  try {
    const d = await api("/api/fridge/consume", {
      method: "POST",
      body: { recipe_index: idx }, // api()가 JSON 직렬화·Content-Type 처리
    });
    const removed = d.removed || [];
    const kcal = d.meal ? ` · 식단에 ${d.meal.kcal} kcal 기록` : "";
    toast(`🍽️ 요리했어요! 재료 ${removed.length}개 소진${kcal}`);
    if (PAGE === "recipes") initRecipes();
    if (PAGE === "dashboard") initDashboard();
  } catch (e) {
    toast(e.message || "소진 처리 실패");
    btn.disabled = false;
  }
}

/* ------------------------------------------------------------------ 부팅 */
document.addEventListener("click", e => {
  const btn = e.target.closest(".cook-btn");
  if (btn && !btn.disabled) cookRecipe(btn.dataset.idx, btn);
});

document.addEventListener("DOMContentLoaded", () => {
  const inits = {
    dashboard: initDashboard, fridge: initFridge, diet: initDiet,
    recipes: initRecipes, nutrition: initNutrition,
  };
  const init = inits[PAGE];
  if (init) init().catch(e => toast("로딩 실패: " + e.message));
});
