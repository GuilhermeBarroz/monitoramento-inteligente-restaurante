"""
Componente interativo HTML5 Canvas via Streamlit Components v2 (`st.components.v2`).
Permite desenhar polígonos de mesas, arrastar vértices/formas, renomear IDs e
editar descrições diretamente no navegador sem depender de janelas OpenCV locais.
"""

import streamlit as st

_EDITOR_HTML = """
<div id="ze-root" class="ze-shell">
  <div class="ze-layout">
    <div class="ze-canvas-col">
      <div class="ze-toolbar">
        <div class="ze-btn-group">
          <button type="button" id="ze-mode-edit" class="ze-btn ze-btn-active" title="Selecionar mesas, mover e arrastar cantos">✋ Editar / Arrastar</button>
          <button type="button" id="ze-mode-draw" class="ze-btn" title="Clicar vértices no vídeo para desenhar uma nova mesa livre">✏️ Desenhar Polígono</button>
          <button type="button" id="ze-add-rect" class="ze-btn ze-btn-accent" title="Adiciona um retângulo editável no centro do salão">⬜ + Mesa Retangular</button>
        </div>
        <div id="ze-draw-actions" class="ze-btn-group ze-hidden">
          <button type="button" id="ze-draw-finish" class="ze-btn ze-btn-success">✅ Fechar Polígono</button>
          <button type="button" id="ze-draw-undo" class="ze-btn">↩️ Desfazer Ponto</button>
          <button type="button" id="ze-draw-cancel" class="ze-btn ze-btn-danger">❌ Cancelar</button>
        </div>
      </div>

      <div class="ze-canvas-wrap">
        <canvas id="ze-canvas"></canvas>
      </div>

      <div class="ze-statusbar">
        <span id="ze-hint">Clique em uma mesa para selecionar, arraste os pontos brancos para ajustar os cantos ou clique no (+) da borda para criar novos cantos.</span>
        <span id="ze-coords" class="ze-coords">X: - | Y: - px</span>
      </div>
    </div>

    <div class="ze-side-col">
      <div class="ze-panel">
        <div class="ze-panel-header">
          <strong>🪑 Mesas Mapeadas (<span id="ze-count">0</span>)</strong>
          <span id="ze-sync-badge" class="ze-badge ze-badge-ok">✓ Sincronizado</span>
        </div>

        <div id="ze-mesa-list" class="ze-list"></div>

        <div id="ze-props" class="ze-props ze-hidden">
          <div class="ze-props-title">
            <span>Propriedades da Mesa</span>
            <div class="ze-order-btns">
              <button type="button" id="ze-move-up" class="ze-mini-btn" title="Mover para cima">▲</button>
              <button type="button" id="ze-move-down" class="ze-mini-btn" title="Mover para baixo">▼</button>
            </div>
          </div>

          <label class="ze-label" for="ze-input-id">Nome / ID da Mesa *</label>
          <input id="ze-input-id" class="ze-input" type="text" maxlength="40" placeholder="Ex: Mesa 1, Varanda 02, VIP" />
          <div class="ze-field-note">Dica: evite acentos no Nome/ID se quiser leitura perfeita na barra de título da câmera.</div>

          <label class="ze-label" for="ze-input-desc">Descrição / Setor</label>
          <input id="ze-input-desc" class="ze-input" type="text" maxlength="80" placeholder="Ex: Perto da janela / Salão Principal" />

          <div class="ze-props-meta">
            <span id="ze-verts-info">Vértices: 4</span>
            <span class="ze-hint-inline">Botão direito em um vértice remove (mín. 3)</span>
          </div>

          <button type="button" id="ze-delete-mesa" class="ze-btn ze-btn-danger ze-btn-full">🗑️ Excluir Esta Mesa</button>
        </div>

        <div id="ze-no-selection" class="ze-empty-box">
          Selecione uma mesa no vídeo ou na lista acima para editar nome, descrição ou excluir.
        </div>

        <div id="ze-validation-err" class="ze-error ze-hidden"></div>

        <div class="ze-save-actions">
          <button type="button" id="ze-save-btn" class="ze-btn ze-btn-primary ze-btn-full">💾 Salvar Mesas e Zonas</button>
          <button type="button" id="ze-reset-btn" class="ze-btn ze-btn-full">↩️ Descartar Alterações</button>
        </div>
      </div>
    </div>
  </div>
</div>
"""

_EDITOR_CSS = """
.ze-shell {
  font-family: var(--st-font, system-ui, -apple-system, sans-serif);
  color: var(--st-text-color, #fafafa);
  box-sizing: border-box;
  width: 100%;
}
.ze-shell *, .ze-shell *::before, .ze-shell *::after {
  box-sizing: border-box;
}
.ze-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 310px;
  gap: 14px;
  align-items: start;
}
@media (max-width: 980px) {
  .ze-layout {
    grid-template-columns: 1fr;
  }
}
.ze-canvas-col {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.ze-toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: space-between;
  align-items: center;
  padding: 8px 10px;
  background: var(--st-secondary-background-color, #1e222b);
  border: 1px solid rgba(128, 128, 128, 0.28);
  border-radius: 8px;
}
.ze-btn-group {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.ze-btn {
  border: 1px solid rgba(128, 128, 128, 0.35);
  background: rgba(255, 255, 255, 0.06);
  color: inherit;
  border-radius: 6px;
  padding: 6px 10px;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: background 0.15s ease, border-color 0.15s ease;
}
.ze-btn:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.14);
}
.ze-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.ze-btn-active {
  background: var(--st-primary-color, #ff4b4b);
  border-color: var(--st-primary-color, #ff4b4b);
  color: #ffffff;
}
.ze-btn-accent {
  background: rgba(34, 197, 94, 0.18);
  border-color: rgba(34, 197, 94, 0.55);
}
.ze-btn-success {
  background: #16a34a;
  border-color: #16a34a;
  color: #ffffff;
}
.ze-btn-primary {
  background: var(--st-primary-color, #ff4b4b);
  border-color: var(--st-primary-color, #ff4b4b);
  color: #ffffff;
  font-weight: 600;
  padding: 9px 12px;
}
.ze-btn-danger {
  background: rgba(220, 38, 38, 0.16);
  border-color: rgba(220, 38, 38, 0.55);
  color: #fca5a5;
}
.ze-btn-full {
  width: 100%;
}
.ze-canvas-wrap {
  position: relative;
  width: 100%;
  background: #0d1117;
  border: 1px solid rgba(128, 128, 128, 0.32);
  border-radius: 8px;
  overflow: hidden;
}
#ze-canvas {
  width: 100%;
  height: auto;
  display: block;
  touch-action: none;
  user-select: none;
}
.ze-statusbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  font-size: 12px;
  opacity: 0.88;
  padding: 6px 10px;
  background: var(--st-secondary-background-color, #1e222b);
  border: 1px solid rgba(128, 128, 128, 0.24);
  border-radius: 6px;
}
.ze-coords {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  white-space: nowrap;
  font-weight: 600;
}
.ze-panel {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px;
  background: var(--st-secondary-background-color, #1e222b);
  border: 1px solid rgba(128, 128, 128, 0.28);
  border-radius: 8px;
}
.ze-panel-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  font-size: 14px;
}
.ze-badge {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 999px;
  font-weight: 600;
  white-space: nowrap;
}
.ze-badge-ok {
  background: rgba(34, 197, 94, 0.2);
  color: #86efac;
}
.ze-badge-dirty {
  background: rgba(245, 158, 11, 0.24);
  color: #fcd34d;
}
.ze-list {
  display: flex;
  flex-direction: column;
  gap: 5px;
  max-height: 210px;
  overflow-y: auto;
  padding-right: 2px;
}
.ze-list-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  width: 100%;
  text-align: left;
  padding: 7px 9px;
  border-radius: 6px;
  border: 1px solid rgba(128, 128, 128, 0.24);
  background: rgba(255, 255, 255, 0.03);
  color: inherit;
  cursor: pointer;
  font-size: 13px;
}
.ze-list-item:hover {
  background: rgba(255, 255, 255, 0.08);
}
.ze-list-item.ze-selected {
  border-color: var(--st-primary-color, #ff4b4b);
  background: rgba(255, 75, 75, 0.14);
}
.ze-list-left {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.ze-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  flex-shrink: 0;
}
.ze-list-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 600;
}
.ze-list-verts {
  font-size: 11px;
  opacity: 0.72;
  white-space: nowrap;
}
.ze-props {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding-top: 8px;
  border-top: 1px solid rgba(128, 128, 128, 0.25);
}
.ze-props-title {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 13px;
  font-weight: 600;
}
.ze-order-btns {
  display: flex;
  gap: 4px;
}
.ze-mini-btn {
  border: 1px solid rgba(128, 128, 128, 0.35);
  background: rgba(255, 255, 255, 0.06);
  color: inherit;
  border-radius: 4px;
  padding: 2px 7px;
  font-size: 11px;
  cursor: pointer;
}
.ze-mini-btn:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}
.ze-label {
  font-size: 12px;
  font-weight: 600;
  margin-top: 2px;
}
.ze-input {
  width: 100%;
  padding: 7px 9px;
  border-radius: 6px;
  border: 1px solid rgba(128, 128, 128, 0.38);
  background: rgba(0, 0, 0, 0.22);
  color: inherit;
  font-size: 13px;
}
.ze-input:focus {
  outline: none;
  border-color: var(--st-primary-color, #ff4b4b);
}
.ze-field-note, .ze-hint-inline {
  font-size: 11px;
  opacity: 0.72;
  line-height: 1.3;
}
.ze-props-meta {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  margin: 2px 0 4px 0;
}
.ze-empty-box {
  font-size: 12px;
  opacity: 0.78;
  padding: 10px;
  border-radius: 6px;
  border: 1px dashed rgba(128, 128, 128, 0.35);
  text-align: center;
}
.ze-error {
  font-size: 12px;
  padding: 8px 10px;
  border-radius: 6px;
  background: rgba(220, 38, 38, 0.2);
  border: 1px solid rgba(220, 38, 38, 0.5);
  color: #fecaca;
}
.ze-save-actions {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding-top: 6px;
  border-top: 1px solid rgba(128, 128, 128, 0.25);
}
.ze-hidden {
  display: none !important;
}
"""

_EDITOR_JS = """
export default function(component) {
  const { data, setTriggerValue, parentElement } = component;
  const root = parentElement.getElementById("ze-root");
  if (!root) return;

  const PALETTE = [
    "#22c55e", "#06b6d4", "#3b82f6", "#f97316",
    "#a855f7", "#ec4899", "#eab308", "#14b8a6"
  ];

  const canvas = parentElement.getElementById("ze-canvas");
  const ctx = canvas.getContext("2d");

  const elModeEdit = parentElement.getElementById("ze-mode-edit");
  const elModeDraw = parentElement.getElementById("ze-mode-draw");
  const elAddRect = parentElement.getElementById("ze-add-rect");
  const elDrawActions = parentElement.getElementById("ze-draw-actions");
  const elDrawFinish = parentElement.getElementById("ze-draw-finish");
  const elDrawUndo = parentElement.getElementById("ze-draw-undo");
  const elDrawCancel = parentElement.getElementById("ze-draw-cancel");

  const elHint = parentElement.getElementById("ze-hint");
  const elCoords = parentElement.getElementById("ze-coords");
  const elCount = parentElement.getElementById("ze-count");
  const elSyncBadge = parentElement.getElementById("ze-sync-badge");
  const elList = parentElement.getElementById("ze-mesa-list");

  const elProps = parentElement.getElementById("ze-props");
  const elNoSel = parentElement.getElementById("ze-no-selection");
  const elMoveUp = parentElement.getElementById("ze-move-up");
  const elMoveDown = parentElement.getElementById("ze-move-down");
  const elInputId = parentElement.getElementById("ze-input-id");
  const elInputDesc = parentElement.getElementById("ze-input-desc");
  const elVertsInfo = parentElement.getElementById("ze-verts-info");
  const elDeleteMesa = parentElement.getElementById("ze-delete-mesa");
  const elValidationErr = parentElement.getElementById("ze-validation-err");
  const elSaveBtn = parentElement.getElementById("ze-save-btn");
  const elResetBtn = parentElement.getElementById("ze-reset-btn");

  function cloneMesas(src) {
    return (src || []).map((m) => ({
      id: String(m.id || ""),
      descricao: String(m.descricao || ""),
      pontos: (m.pontos || []).map((p) => [Math.round(p[0]), Math.round(p[1])]),
    }));
  }

  if (!root._zeState) {
    root._zeState = {
      mesas: cloneMesas(data?.mesas),
      selectedIndex: (data?.mesas && data.mesas.length > 0) ? 0 : -1,
      mode: "edit",
      drawingPoints: [],
      dragInfo: null,
      hoverInfo: null,
      mousePos: null,
      dirty: false,
      saving: false,
      loadedRevision: data?.revision ?? 0,
      loadedAck: data?.ack ?? 0,
      imgW: Number(data?.largura || 1280),
      imgH: Number(data?.altura || 720),
      bgSrc: null,
      bgImg: null,
    };
  }

  const state = root._zeState;
  const incomingRev = data?.revision ?? 0;
  const incomingAck = data?.ack ?? 0;
  if (incomingRev !== state.loadedRevision) {
    state.mesas = cloneMesas(data?.mesas);
    state.selectedIndex = state.mesas.length > 0 ? Math.min(Math.max(state.selectedIndex, 0), state.mesas.length - 1) : -1;
    state.drawingPoints = [];
    state.mode = "edit";
    state.dirty = false;
    state.saving = false;
    state.loadedRevision = incomingRev;
    state.loadedAck = incomingAck;
  } else if (incomingAck !== state.loadedAck) {
    state.saving = false;
    state.loadedAck = incomingAck;
  }

  state.imgW = Number(data?.largura || state.imgW || 1280);
  state.imgH = Number(data?.altura || state.imgH || 720);
  if (canvas.width !== state.imgW) canvas.width = state.imgW;
  if (canvas.height !== state.imgH) canvas.height = state.imgH;

  if (data?.img_b64 && data.img_b64 !== state.bgSrc) {
    state.bgSrc = data.img_b64;
    const img = new Image();
    img.onload = () => {
      state.bgImg = img;
      draw();
    };
    img.src = data.img_b64;
  }

  function colorFor(idx) {
    return PALETTE[idx % PALETTE.length];
  }

  function hexToRgba(hex, alpha) {
    const clean = hex.replace("#", "");
    const r = parseInt(clean.substring(0, 2), 16);
    const g = parseInt(clean.substring(2, 4), 16);
    const b = parseInt(clean.substring(4, 6), 16);
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
  }

  function clampPt(x, y) {
    return [
      Math.max(0, Math.min(state.imgW, Math.round(x))),
      Math.max(0, Math.min(state.imgH, Math.round(y))),
    ];
  }

  function getScaleFactors() {
    const rect = canvas.getBoundingClientRect();
    const sx = rect.width > 0 ? canvas.width / rect.width : 1;
    const sy = rect.height > 0 ? canvas.height / rect.height : 1;
    return { rect, sx, sy, s: Math.max(sx, sy, 1) };
  }

  function toCanvasCoords(clientX, clientY) {
    const { rect, sx, sy } = getScaleFactors();
    return clampPt((clientX - rect.left) * sx, (clientY - rect.top) * sy);
  }

  function dist(ax, ay, bx, by) {
    return Math.hypot(ax - bx, ay - by);
  }

  function pointInPoly(x, y, pts) {
    let inside = false;
    for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
      const xi = pts[i][0], yi = pts[i][1];
      const xj = pts[j][0], yj = pts[j][1];
      const intersect = ((yi > y) !== (yj > y)) &&
        (x < (xj - xi) * (y - yi) / ((yj - yi) || 1e-9) + xi);
      if (intersect) inside = !inside;
    }
    return inside;
  }

  function validateMesas() {
    const seen = new Set();
    for (let i = 0; i < state.mesas.length; i++) {
      const m = state.mesas[i];
      const cleanId = (m.id || "").trim();
      if (!cleanId) {
        return `A mesa #${i + 1} está sem nome/ID.`;
      }
      const norm = cleanId.toLowerCase();
      if (seen.has(norm)) {
        return `Nome duplicado: "${cleanId}". Cada mesa precisa ter um nome único.`;
      }
      seen.add(norm);
      if (!m.pontos || m.pontos.length < 3) {
        return `"${cleanId}" precisa ter pelo menos 3 vértices.`;
      }
    }
    return null;
  }

  function nextTableName() {
    const used = new Set(state.mesas.map((m) => (m.id || "").trim().toLowerCase()));
    let num = state.mesas.length + 1;
    while (used.has(`mesa ${num}`)) {
      num += 1;
    }
    return `Mesa ${num}`;
  }

  function hitTestEdit(x, y) {
    const { s } = getScaleFactors();
    const vertR = 13 * s;
    const midR = 10 * s;

    if (state.selectedIndex >= 0 && state.selectedIndex < state.mesas.length) {
      const selMesa = state.mesas[state.selectedIndex];
      const pts = selMesa.pontos;
      for (let p = 0; p < pts.length; p++) {
        if (dist(x, y, pts[p][0], pts[p][1]) <= vertR) {
          return { type: "vertex", mesaIdx: state.selectedIndex, ptIdx: p };
        }
      }
      for (let p = 0; p < pts.length; p++) {
        const next = pts[(p + 1) % pts.length];
        const mx = (pts[p][0] + next[0]) / 2;
        const my = (pts[p][1] + next[1]) / 2;
        if (dist(x, y, mx, my) <= midR) {
          return { type: "mid", mesaIdx: state.selectedIndex, ptIdx: p, mx: Math.round(mx), my: Math.round(my) };
        }
      }
    }

    for (let i = 0; i < state.mesas.length; i++) {
      if (i === state.selectedIndex) continue;
      const pts = state.mesas[i].pontos;
      for (let p = 0; p < pts.length; p++) {
        if (dist(x, y, pts[p][0], pts[p][1]) <= vertR) {
          return { type: "vertex", mesaIdx: i, ptIdx: p };
        }
      }
    }

    if (state.selectedIndex >= 0 && state.selectedIndex < state.mesas.length) {
      if (pointInPoly(x, y, state.mesas[state.selectedIndex].pontos)) {
        return { type: "poly", mesaIdx: state.selectedIndex };
      }
    }

    for (let i = state.mesas.length - 1; i >= 0; i--) {
      if (i === state.selectedIndex) continue;
      if (pointInPoly(x, y, state.mesas[i].pontos)) {
        return { type: "poly", mesaIdx: i };
      }
    }

    return null;
  }

  function draw() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    if (state.bgImg) {
      ctx.drawImage(state.bgImg, 0, 0, canvas.width, canvas.height);
    } else {
      ctx.fillStyle = "#111827";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = "#9ca3af";
      ctx.font = "18px sans-serif";
      ctx.fillText("Carregando imagem da câmera...", 24, 48);
    }

    const { s } = getScaleFactors();
    const fontPx = Math.max(13, Math.round(14 * s));

    // Draw saved/working polygons
    state.mesas.forEach((mesa, idx) => {
      const pts = mesa.pontos;
      if (!pts || pts.length < 3) return;

      const cor = colorFor(idx);
      const isSelected = idx === state.selectedIndex;

      ctx.beginPath();
      ctx.moveTo(pts[0][0], pts[0][1]);
      for (let i = 1; i < pts.length; i++) {
        ctx.lineTo(pts[i][0], pts[i][1]);
      }
      ctx.closePath();

      ctx.fillStyle = hexToRgba(cor, isSelected ? 0.34 : 0.18);
      ctx.fill();

      ctx.lineWidth = (isSelected ? 3.2 : 2.0) * s;
      ctx.strokeStyle = isSelected ? "#ffffff" : cor;
      ctx.stroke();

      if (isSelected) {
        ctx.lineWidth = 2.0 * s;
        ctx.strokeStyle = cor;
        ctx.stroke();
      }

      // Label pill at first vertex (clamped inside canvas)
      const label = (mesa.id || `Mesa ${idx + 1}`).trim() || `Mesa ${idx + 1}`;
      ctx.font = `600 ${fontPx}px sans-serif`;
      const textW = ctx.measureText(label).width;
      const padX = 8 * s;
      const boxH = fontPx + 10 * s;
      const boxW = textW + padX * 2;
      const lx = Math.max(2, Math.min(canvas.width - boxW - 2, pts[0][0]));
      const ly = Math.max(2, pts[0][1] - boxH);

      ctx.fillStyle = isSelected ? "rgba(15, 23, 42, 0.92)" : "rgba(15, 23, 42, 0.78)";
      ctx.fillRect(lx, ly, boxW, boxH);
      ctx.strokeStyle = cor;
      ctx.lineWidth = 1.5 * s;
      ctx.strokeRect(lx, ly, boxW, boxH);

      ctx.fillStyle = "#ffffff";
      ctx.fillText(label, lx + padX, ly + boxH - 6 * s);

      // Selected table handles (vertices + edge midpoints)
      if (isSelected && state.mode === "edit") {
        // Midpoint (+) handles to add vertices cleanly
        for (let p = 0; p < pts.length; p++) {
          const next = pts[(p + 1) % pts.length];
          const mx = (pts[p][0] + next[0]) / 2;
          const my = (pts[p][1] + next[1]) / 2;
          const isMidHover = state.hoverInfo && state.hoverInfo.type === "mid" && state.hoverInfo.ptIdx === p;
          const rMid = (isMidHover ? 6.5 : 4.5) * s;

          ctx.beginPath();
          ctx.arc(mx, my, rMid, 0, Math.PI * 2);
          ctx.fillStyle = isMidHover ? "#fde047" : "rgba(255,255,255,0.65)";
          ctx.fill();
          ctx.strokeStyle = "#0f172a";
          ctx.lineWidth = 1.2 * s;
          ctx.stroke();
        }

        // Corner vertex handles
        for (let p = 0; p < pts.length; p++) {
          const isVertHover = state.hoverInfo && state.hoverInfo.type === "vertex" &&
            state.hoverInfo.mesaIdx === idx && state.hoverInfo.ptIdx === p;
          const r = (isVertHover ? 8.5 : 6.5) * s;
          ctx.beginPath();
          ctx.arc(pts[p][0], pts[p][1], r, 0, Math.PI * 2);
          ctx.fillStyle = "#ffffff";
          ctx.fill();
          ctx.lineWidth = 2.5 * s;
          ctx.strokeStyle = cor;
          ctx.stroke();
        }
      }
    });

    // Draw polygon currently being traced
    if (state.mode === "draw" && state.drawingPoints.length > 0) {
      const dpts = state.drawingPoints;
      ctx.beginPath();
      ctx.moveTo(dpts[0][0], dpts[0][1]);
      for (let i = 1; i < dpts.length; i++) {
        ctx.lineTo(dpts[i][0], dpts[i][1]);
      }
      if (state.mousePos) {
        ctx.lineTo(state.mousePos.x, state.mousePos.y);
      }
      ctx.strokeStyle = "#fde047";
      ctx.lineWidth = 2.5 * s;
      ctx.stroke();

      dpts.forEach((pt, idx) => {
        ctx.beginPath();
        ctx.arc(pt[0], pt[1], (idx === 0 ? 8 : 5.5) * s, 0, Math.PI * 2);
        ctx.fillStyle = idx === 0 ? "#22c55e" : "#fde047";
        ctx.fill();
        ctx.strokeStyle = "#0f172a";
        ctx.lineWidth = 1.8 * s;
        ctx.stroke();
      });
    }
  }

  function updateUI() {
    elCount.textContent = String(state.mesas.length);

    if (state.saving) {
      elSyncBadge.textContent = "⏳ Salvando...";
      elSyncBadge.className = "ze-badge ze-badge-dirty";
    } else if (state.dirty) {
      elSyncBadge.textContent = "● Alterações pendentes";
      elSyncBadge.className = "ze-badge ze-badge-dirty";
    } else {
      elSyncBadge.textContent = "✓ Sincronizado";
      elSyncBadge.className = "ze-badge ze-badge-ok";
    }

    // Mode toolbar UI
    const isDraw = state.mode === "draw";
    elModeEdit.classList.toggle("ze-btn-active", !isDraw);
    elModeDraw.classList.toggle("ze-btn-active", isDraw);
    elDrawActions.classList.toggle("ze-hidden", !isDraw);
    elDrawFinish.disabled = state.drawingPoints.length < 3;
    elDrawUndo.disabled = state.drawingPoints.length === 0;

    if (isDraw) {
      elHint.textContent = "Clique no vídeo para marcar os cantos (mín. 3). Clique na bolinha verde inicial ou em 'Fechar Polígono' para concluir.";
      canvas.style.cursor = "crosshair";
    } else {
      elHint.textContent = "Arraste os círculos brancos para ajustar cantos, o centro para mover a mesa, ou os pontinhos da borda para criar novos cantos.";
    }

    // Render list cleanly using DOM nodes (XSS-safe)
    elList.textContent = "";
    state.mesas.forEach((m, idx) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "ze-list-item" + (idx === state.selectedIndex ? " ze-selected" : "");

      const left = document.createElement("span");
      left.className = "ze-list-left";

      const dot = document.createElement("span");
      dot.className = "ze-dot";
      dot.style.backgroundColor = colorFor(idx);

      const nameSpan = document.createElement("span");
      nameSpan.className = "ze-list-name";
      nameSpan.textContent = (m.id || "").trim() || `(Sem nome #${idx + 1})`;

      left.appendChild(dot);
      left.appendChild(nameSpan);

      const vertsSpan = document.createElement("span");
      vertsSpan.className = "ze-list-verts";
      vertsSpan.textContent = `${m.pontos.length}v`;

      btn.appendChild(left);
      btn.appendChild(vertsSpan);

      btn.addEventListener("click", () => {
        state.selectedIndex = idx;
        state.mode = "edit";
        updateUI();
        draw();
      });

      elList.appendChild(btn);
    });

    // Properties panel
    const hasSel = state.selectedIndex >= 0 && state.selectedIndex < state.mesas.length;
    elProps.classList.toggle("ze-hidden", !hasSel);
    elNoSel.classList.toggle("ze-hidden", hasSel);

    if (hasSel) {
      const sel = state.mesas[state.selectedIndex];
      if (elInputId.value !== sel.id) elInputId.value = sel.id;
      if (elInputDesc.value !== sel.descricao) elInputDesc.value = sel.descricao;
      elVertsInfo.textContent = `Vértices: ${sel.pontos.length}`;
      elMoveUp.disabled = state.selectedIndex === 0;
      elMoveDown.disabled = state.selectedIndex === state.mesas.length - 1;
    }

    const errMsg = validateMesas();
    if (errMsg) {
      elValidationErr.textContent = `⚠️ ${errMsg}`;
      elValidationErr.classList.remove("ze-hidden");
      elSaveBtn.disabled = true;
    } else {
      elValidationErr.classList.add("ze-hidden");
      elSaveBtn.disabled = state.saving;
    }
    elResetBtn.disabled = !state.dirty || state.saving;
  }

  function finishDrawing() {
    if (state.drawingPoints.length < 3) return;
    const nome = nextTableName();
    state.mesas.push({
      id: nome,
      descricao: "",
      pontos: state.drawingPoints.map((p) => [p[0], p[1]]),
    });
    state.selectedIndex = state.mesas.length - 1;
    state.drawingPoints = [];
    state.mode = "edit";
    state.dirty = true;
    updateUI();
    draw();
  }

  // Event handlers
  function onModeEdit() {
    state.mode = "edit";
    state.drawingPoints = [];
    updateUI();
    draw();
  }

  function onModeDraw() {
    state.mode = "draw";
    state.drawingPoints = [];
    state.dragInfo = null;
    updateUI();
    draw();
  }

  function onAddRect() {
    const cx = Math.round(state.imgW * 0.5);
    const cy = Math.round(state.imgH * 0.5);
    const offset = (state.mesas.length % 5 - 2) * 28;
    const hw = Math.round(state.imgW * 0.11);
    const hh = Math.round(state.imgH * 0.12);

    const x1 = Math.max(10, Math.min(state.imgW - hw * 2 - 10, cx - hw + offset));
    const y1 = Math.max(10, Math.min(state.imgH - hh * 2 - 10, cy - hh + offset));
    const x2 = x1 + hw * 2;
    const y2 = y1 + hh * 2;

    state.mesas.push({
      id: nextTableName(),
      descricao: "",
      pontos: [[x1, y1], [x2, y1], [x2, y2], [x1, y2]],
    });
    state.selectedIndex = state.mesas.length - 1;
    state.mode = "edit";
    state.drawingPoints = [];
    state.dirty = true;
    updateUI();
    draw();
  }

  function onDrawUndo() {
    state.drawingPoints.pop();
    updateUI();
    draw();
  }

  function onDrawCancel() {
    state.drawingPoints = [];
    state.mode = "edit";
    updateUI();
    draw();
  }

  function onInputId() {
    if (state.selectedIndex < 0) return;
    state.mesas[state.selectedIndex].id = elInputId.value;
    state.dirty = true;
    updateUI();
    draw();
  }

  function onInputDesc() {
    if (state.selectedIndex < 0) return;
    state.mesas[state.selectedIndex].descricao = elInputDesc.value;
    state.dirty = true;
    updateUI();
  }

  function onMoveUp() {
    const i = state.selectedIndex;
    if (i <= 0) return;
    [state.mesas[i - 1], state.mesas[i]] = [state.mesas[i], state.mesas[i - 1]];
    state.selectedIndex = i - 1;
    state.dirty = true;
    updateUI();
    draw();
  }

  function onMoveDown() {
    const i = state.selectedIndex;
    if (i < 0 || i >= state.mesas.length - 1) return;
    [state.mesas[i + 1], state.mesas[i]] = [state.mesas[i], state.mesas[i + 1]];
    state.selectedIndex = i + 1;
    state.dirty = true;
    updateUI();
    draw();
  }

  function onDeleteMesa() {
    if (state.selectedIndex < 0) return;
    state.mesas.splice(state.selectedIndex, 1);
    if (state.mesas.length === 0) {
      state.selectedIndex = -1;
    } else if (state.selectedIndex >= state.mesas.length) {
      state.selectedIndex = state.mesas.length - 1;
    }
    state.dirty = true;
    updateUI();
    draw();
  }

  function onSave() {
    const errMsg = validateMesas();
    if (errMsg) return;
    state.saving = true;
    updateUI();
    const payload = {
      mesas: state.mesas.map((m) => ({
        id: m.id.trim(),
        descricao: (m.descricao || "").trim(),
        pontos: m.pontos.map((p) => [Math.round(p[0]), Math.round(p[1])]),
      })),
      largura: state.imgW,
      altura: state.imgH,
      nonce: Date.now(),
    };
    setTriggerValue("save_payload", payload);
  }

  function onReset() {
    state.mesas = cloneMesas(data?.mesas);
    state.selectedIndex = state.mesas.length > 0 ? 0 : -1;
    state.drawingPoints = [];
    state.mode = "edit";
    state.dirty = false;
    state.saving = false;
    updateUI();
    draw();
  }

  function onPointerDown(e) {
    if (e.button === 2) return; // Contextmenu handled separately
    const [x, y] = toCanvasCoords(e.clientX, e.clientY);

    if (state.mode === "draw") {
      const { s } = getScaleFactors();
      if (state.drawingPoints.length >= 3 && dist(x, y, state.drawingPoints[0][0], state.drawingPoints[0][1]) <= 16 * s) {
        finishDrawing();
        return;
      }
      state.drawingPoints.push([x, y]);
      updateUI();
      draw();
      return;
    }

    const hit = hitTestEdit(x, y);
    if (!hit) {
      state.selectedIndex = -1;
      state.dragInfo = null;
      updateUI();
      draw();
      return;
    }

    state.selectedIndex = hit.mesaIdx;

    if (hit.type === "mid") {
      state.mesas[hit.mesaIdx].pontos.splice(hit.ptIdx + 1, 0, [hit.mx, hit.my]);
      state.dragInfo = { type: "vertex", mesaIdx: hit.mesaIdx, ptIdx: hit.ptIdx + 1 };
      state.dirty = true;
    } else if (hit.type === "vertex") {
      state.dragInfo = { type: "vertex", mesaIdx: hit.mesaIdx, ptIdx: hit.ptIdx };
    } else if (hit.type === "poly") {
      state.dragInfo = {
        type: "poly",
        mesaIdx: hit.mesaIdx,
        startX: x,
        startY: y,
        origPts: state.mesas[hit.mesaIdx].pontos.map((p) => [p[0], p[1]]),
      };
    }

    try {
      canvas.setPointerCapture(e.pointerId);
    } catch (_) {}
    updateUI();
    draw();
  }

  function onPointerMove(e) {
    const [x, y] = toCanvasCoords(e.clientX, e.clientY);
    state.mousePos = { x, y };
    elCoords.textContent = `X: ${x} | Y: ${y} px`;

    if (state.mode === "draw") {
      draw();
      return;
    }

    if (state.dragInfo) {
      const drag = state.dragInfo;
      const mesa = state.mesas[drag.mesaIdx];
      if (!mesa) return;

      if (drag.type === "vertex") {
        mesa.pontos[drag.ptIdx] = clampPt(x, y);
        state.dirty = true;
        draw();
        return;
      }

      if (drag.type === "poly") {
        let dx = x - drag.startX;
        let dy = y - drag.startY;

        let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
        for (const pt of drag.origPts) {
          if (pt[0] < minX) minX = pt[0];
          if (pt[0] > maxX) maxX = pt[0];
          if (pt[1] < minY) minY = pt[1];
          if (pt[1] > maxY) maxY = pt[1];
        }
        dx = Math.max(-minX, Math.min(state.imgW - maxX, dx));
        dy = Math.max(-minY, Math.min(state.imgH - maxY, dy));

        mesa.pontos = drag.origPts.map((pt) => [Math.round(pt[0] + dx), Math.round(pt[1] + dy)]);
        state.dirty = true;
        draw();
        return;
      }
    }

    const hit = hitTestEdit(x, y);
    state.hoverInfo = hit;
    if (!hit) {
      canvas.style.cursor = "default";
    } else if (hit.type === "vertex" || hit.type === "mid") {
      canvas.style.cursor = "grab";
    } else {
      canvas.style.cursor = "move";
    }
    draw();
  }

  function onPointerUp(e) {
    if (state.dragInfo) {
      state.dragInfo = null;
      try {
        canvas.releasePointerCapture(e.pointerId);
      } catch (_) {}
      updateUI();
      draw();
    }
  }

  function onPointerLeave() {
    state.mousePos = null;
    state.hoverInfo = null;
    elCoords.textContent = "X: - | Y: - px";
    draw();
  }

  function onContextMenu(e) {
    if (state.mode !== "edit") return;
    const [x, y] = toCanvasCoords(e.clientX, e.clientY);
    const hit = hitTestEdit(x, y);
    if (hit && hit.type === "vertex") {
      e.preventDefault();
      const mesa = state.mesas[hit.mesaIdx];
      if (mesa && mesa.pontos.length > 3) {
        mesa.pontos.splice(hit.ptIdx, 1);
        state.selectedIndex = hit.mesaIdx;
        state.dirty = true;
        updateUI();
        draw();
      }
    }
  }

  function onDblClick() {
    if (state.mode === "draw" && state.drawingPoints.length >= 3) {
      // Remove ponto duplicado gerado pelo segundo clique do dblclick
      const n = state.drawingPoints.length;
      if (n >= 4 && dist(state.drawingPoints[n - 1][0], state.drawingPoints[n - 1][1], state.drawingPoints[n - 2][0], state.drawingPoints[n - 2][1]) <= 6) {
        state.drawingPoints.pop();
      }
      finishDrawing();
    }
  }

  elModeEdit.addEventListener("click", onModeEdit);
  elModeDraw.addEventListener("click", onModeDraw);
  elAddRect.addEventListener("click", onAddRect);
  elDrawFinish.addEventListener("click", finishDrawing);
  elDrawUndo.addEventListener("click", onDrawUndo);
  elDrawCancel.addEventListener("click", onDrawCancel);

  elInputId.addEventListener("input", onInputId);
  elInputDesc.addEventListener("input", onInputDesc);
  elMoveUp.addEventListener("click", onMoveUp);
  elMoveDown.addEventListener("click", onMoveDown);
  elDeleteMesa.addEventListener("click", onDeleteMesa);
  elSaveBtn.addEventListener("click", onSave);
  elResetBtn.addEventListener("click", onReset);

  canvas.addEventListener("pointerdown", onPointerDown);
  canvas.addEventListener("pointermove", onPointerMove);
  canvas.addEventListener("pointerup", onPointerUp);
  canvas.addEventListener("pointerleave", onPointerLeave);
  canvas.addEventListener("contextmenu", onContextMenu);
  canvas.addEventListener("dblclick", onDblClick);

  updateUI();
  draw();

  return () => {
    elModeEdit.removeEventListener("click", onModeEdit);
    elModeDraw.removeEventListener("click", onModeDraw);
    elAddRect.removeEventListener("click", onAddRect);
    elDrawFinish.removeEventListener("click", finishDrawing);
    elDrawUndo.removeEventListener("click", onDrawUndo);
    elDrawCancel.removeEventListener("click", onDrawCancel);

    elInputId.removeEventListener("input", onInputId);
    elInputDesc.removeEventListener("input", onInputDesc);
    elMoveUp.removeEventListener("click", onMoveUp);
    elMoveDown.removeEventListener("click", onMoveDown);
    elDeleteMesa.removeEventListener("click", onDeleteMesa);
    elSaveBtn.removeEventListener("click", onSave);
    elResetBtn.removeEventListener("click", onReset);

    canvas.removeEventListener("pointerdown", onPointerDown);
    canvas.removeEventListener("pointermove", onPointerMove);
    canvas.removeEventListener("pointerup", onPointerUp);
    canvas.removeEventListener("pointerleave", onPointerLeave);
    canvas.removeEventListener("contextmenu", onContextMenu);
    canvas.removeEventListener("dblclick", onDblClick);
  };
}
"""

_ZONE_EDITOR_COMPONENT = st.components.v2.component(
    "mesa_zone_editor_v1",
    html=_EDITOR_HTML,
    css=_EDITOR_CSS,
    js=_EDITOR_JS,
    isolate_styles=True,
)


def renderizar_editor_zonas(
    mesas: list,
    largura: int,
    altura: int,
    img_b64: str,
    revision: int = 0,
    ack: int = 0,
    key: str = "editor_zonas_browser",
):
    """Monta o editor interativo de polígonos e nomes no Streamlit."""
    return _ZONE_EDITOR_COMPONENT(
        key=key,
        data={
            "mesas": mesas,
            "largura": int(largura),
            "altura": int(altura),
            "img_b64": img_b64,
            "revision": int(revision),
            "ack": int(ack),
        },
        on_save_payload_change=lambda: None,
        width="stretch",
        height="content",
    )
