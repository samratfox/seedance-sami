import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  absoluteUrl,
  cancelJob,
  connectWebSocket,
  errorMessage,
  estimate,
  fetchBalance,
  fetchConfig,
  fetchHistory,
  fetchJob,
  setApiKey,
  submitGeneration,
} from "./api";

const STAGE_LABEL = {
  queued: "В очереди",
  estimating: "Оценка",
  generating: "Генерация",
  saving: "Сохранение",
  done: "Готово",
  failed: "Ошибка",
  partial: "Частично",
};

export default function App() {
  const [config, setConfig] = useState(null);
  const [balance, setBalance] = useState(null);
  const [prompt, setPrompt] = useState("");
  const [aspect, setAspect] = useState("1:1");
  const [sizeTier, setSizeTier] = useState("standard");
  const [quality, setQuality] = useState("medium");
  const [outputFormat, setOutputFormat] = useState("png");
  const [n, setN] = useState(1);
  const [references, setReferences] = useState([]); // массив File; порядок = @Image1..
  const [estimateData, setEstimateData] = useState(null);
  const [estimateLoading, setEstimateLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [job, setJob] = useState(null);
  const [progress, setProgress] = useState(null);
  const [results, setResults] = useState([]);
  const [historyImages, setHistoryImages] = useState([]);
  const [historyJobs, setHistoryJobs] = useState([]);
  const [error, setError] = useState("");
  const [tab, setTab] = useState("generate"); // generate | history
  // Экран подключения API-ключа
  const [keyInput, setKeyInput] = useState("");
  const [keyBusy, setKeyBusy] = useState(false);
  const [keyError, setKeyError] = useState("");
  const [keyOk, setKeyOk] = useState("");
  // Просмотр картинки из истории
  const [viewer, setViewer] = useState(null); // {images, index}
  // Drag-and-drop референсов в окно приложения
  const [dragOver, setDragOver] = useState(false);
  const wsRef = useRef(null);
  const estimateTimer = useRef(null);
  const jobRef = useRef(null);   // текущая задача — для фильтрации WS-сообщений

  // init
  useEffect(() => {
    fetchConfig()
      .then((cfg) => {
        setConfig(cfg);
        setAspect(cfg.default_aspect || "1:1");
        setSizeTier(cfg.default_size_tier || "standard");
        setQuality(cfg.default_quality || "medium");
        setOutputFormat(cfg.default_format || "png");
      })
      .catch((e) => setError(errorMessage(e)));
    refreshBalance();
    refreshHistory();
  }, []);

  const refreshBalance = () => fetchBalance().then(setBalance).catch(() => {});
  const refreshHistory = () =>
    fetchHistory(60).then((h) => {
      setHistoryImages(h.images || []);
      setHistoryJobs(h.jobs || []);
    }).catch(() => {});

  // Держим jobRef в актуальном состоянии — WS-хендлер читает его, чтобы фильтровать
  // сообщения по job_id без пересоздания соединения.
  useEffect(() => { jobRef.current = job; }, [job]);

  // WebSocket: одно соединение на весь сеанс + авто-реконнект. Раньше пересоздавалось
  // при каждой смене job — из-за этого терялись сообщения (включая финальные превью),
  // и картинки «иногда не появлялись».
  useEffect(() => {
    let ws = null;
    let reconnectTimer = null;
    let stopped = false;

    const onMessage = (msg) => {
      const cur = jobRef.current;
      if (cur && msg.job_id !== cur.job_id) return;
      setProgress(msg);
      if (msg.previews && msg.previews.length) {
        setResults((prev) => {
          const set = new Set(prev);
          msg.previews.forEach((p) => set.add(p));
          return Array.from(set);
        });
      }
      if (["done", "failed", "partial", "cancelled"].includes(msg.stage)) {
        setBusy(false);
        setCancelling(false);
        refreshHistory();
        refreshBalance();   // показать изменившийся баланс
        // Подстраховка: достаём авторитетный список картинок из БД — на случай,
        // если какие-то превью по WebSocket не дошли.
        if (cur && msg.job_id === cur.job_id) {
          fetchJob(cur.job_id)
            .then((data) => {
              if (data?.images?.length) {
                setResults(data.images.map((img) => absoluteUrl(img.url)));
              }
            })
            .catch(() => {});
        }
      }
    };

    const connect = () => {
      if (stopped) return;
      ws = connectWebSocket(onMessage);
      wsRef.current = ws;
      ws.onclose = () => {
        if (!stopped) reconnectTimer = setTimeout(connect, 1500);
      };
    };
    connect();

    return () => {
      stopped = true;
      clearTimeout(reconnectTimer);
      if (ws) ws.close();
    };
  }, []);

  // Подстраховка WebSocket: пока идёт генерация, периодически опрашиваем статус
  // задачи и подтягиваем готовые картинки из БД. Покрывает потерянные WS-сообщения
  // и обрывы соединения.
  useEffect(() => {
    if (!job || !busy) return;
    const id = setInterval(() => {
      fetchJob(job.job_id)
        .then((data) => {
          if (data?.images?.length) {
            setResults((prev) => {
              const set = new Set(prev);
              data.images.forEach((img) => set.add(absoluteUrl(img.url)));
              return Array.from(set);
            });
          }
          const j = data?.job;
          if (j && ["done", "failed", "partial", "cancelled"].includes(j.status)) {
            setBusy(false);
            setCancelling(false);
            if (data?.images?.length) {
              setResults(data.images.map((img) => absoluteUrl(img.url)));
            }
            refreshHistory();
            refreshBalance();
          }
        })
        .catch(() => {});
    }, 2500);
    return () => clearInterval(id);
  }, [job, busy]);

  // live estimate
  useEffect(() => {
    if (!config) return;
    setEstimateLoading(true);
    clearTimeout(estimateTimer.current);
    estimateTimer.current = setTimeout(() => {
      estimate({ aspect, size_tier: sizeTier, quality, n })
        .then((d) => setEstimateData(d))
        .catch(() => setEstimateData(null))
        .finally(() => setEstimateLoading(false));
    }, 250);
    return () => clearTimeout(estimateTimer.current);
  }, [aspect, sizeTier, quality, n, config]);

  const maxN = useMemo(() => {
    if (!config) return 10;
    return config.max_n_per_call || 10;
  }, [config]);

  const handleGenerate = useCallback(async () => {
    setError("");
    if (!prompt.trim()) {
      setError("Введите промпт");
      return;
    }
    setBusy(true);
    setCancelling(false);
    setResults([]);
    setProgress(null);
    try {
      const res = await submitGeneration({ prompt, aspect, size_tier: sizeTier, quality, output_format: outputFormat, n, references });
      setJob(res);
    } catch (e) {
      setError(errorMessage(e));
      setBusy(false);
    }
  }, [prompt, aspect, sizeTier, quality, outputFormat, n, references]);

  const maxRefs = config?.max_references || 16;

  const handleAddReferences = useCallback((files) => {
    const arr = Array.from(files || []).filter((f) => f.type.startsWith("image/"));
    setReferences((prev) => [...prev, ...arr].slice(0, maxRefs));
  }, [maxRefs]);

  const handleRemoveReference = useCallback((index) => {
    setReferences((prev) => prev.filter((_, i) => i !== index));
  }, []);

  const handleCancel = useCallback(async () => {
    if (!job?.job_id) return;
    setCancelling(true);
    try {
      await cancelJob(job.job_id);
    } catch (e) {
      setError(errorMessage(e));
      setCancelling(false);
    }
  }, [job]);

  const handleSetKey = useCallback(async () => {
    setKeyError("");
    setKeyOk("");
    if (keyInput.trim().length < 16) {
      setKeyError("Ключ выглядит слишком коротким");
      return;
    }
    setKeyBusy(true);
    try {
      const res = await setApiKey(keyInput.trim());
      setKeyOk(res.balance || "Ключ подключён");
      setKeyInput("");
      refreshBalance();
    } catch (e) {
      setKeyError(errorMessage(e));
    } finally {
      setKeyBusy(false);
    }
  }, [keyInput]);

  const hasKey = Boolean(balance?.has_key);
  const progressPct = progress?.progress ?? 0;

  // навигация в просмотрщике
  const goViewer = useCallback((delta) => {
    setViewer((v) => {
      if (!v) return v;
      const len = v.images.length;
      return { ...v, index: (v.index + delta + len) % len };
    });
  }, []);

  //Esc/стрелки для просмотрщика
  useEffect(() => {
    if (!viewer) return;
    const onKey = (e) => {
      if (e.key === "Escape") setViewer(null);
      else if (e.key === "ArrowRight") goViewer(1);
      else if (e.key === "ArrowLeft") goViewer(-1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [viewer, goViewer]);

  return (
    <div
      className={"app-shell" + (dragOver ? " drag-over" : "")}
      onDragOver={(e) => {
        if (e.dataTransfer.types.includes("Files")) {
          e.preventDefault();
          setDragOver(true);
        }
      }}
      onDragLeave={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget)) setDragOver(false);
      }}
      onDrop={(e) => {
        e.preventDefault();
        setDragOver(false);
        if (hasKey) handleAddReferences(e.dataTransfer.files);
      }}
    >
      <TopBar balance={balance} config={config} />

      {dragOver && (
        <div className="drop-overlay">
          <div className="drop-overlay-inner">
            {hasKey ? "Отпусти — добавлю как референсы" : "Сначала подключи API-ключ"}
          </div>
        </div>
      )}

      <div className="tabs">
        <button className={tab === "generate" ? "tab active" : "tab"} onClick={() => setTab("generate")}>Генерация</button>
        <button className={tab === "history" ? "tab active" : "tab"} onClick={() => setTab("history")}>История</button>
      </div>

      {error && <div className="alert danger">{error}</div>}

      {tab === "generate" && hasKey && (
        <PriceCheatSheet config={config} estimateData={estimateData} n={n} />
      )}

      {tab === "generate" && !hasKey && (
        <KeyPanel
          keyInput={keyInput} setKeyInput={setKeyInput}
          keyBusy={keyBusy} keyError={keyError} keyOk={keyOk}
          onSubmit={handleSetKey}
        />
      )}

      {tab === "generate" && hasKey && (
        <div className="panel">
          <PromptField
            value={prompt}
            onChange={setPrompt}
            references={references}
            maxLen={config?.max_prompt_length || 16000}
          />

          <div className="field">
            <span className="field-label">Соотношение сторон</span>
            <div className="aspect-pills">
              {(config?.aspects || ["1:1", "9:16", "16:9", "4:3", "3:4", "4:5", "3:2", "2:3"]).map((a) => (
                <button
                  key={a}
                  type="button"
                  className={"aspect-pill" + (aspect === a ? " active" : "")}
                  onClick={() => setAspect(a)}
                >
                  <span className={"aspect-rect ar-" + a.replace(":", "-")} />
                  {a}
                </button>
              ))}
            </div>
          </div>

          <div className="field">
            <span className="field-label">Размер (детализация)</span>
            <div className="tier-pills">
              {(config?.size_tiers || ["standard", "2k", "max"]).map((t) => (
                <button
                  key={t}
                  type="button"
                  className={"tier-pill" + (sizeTier === t ? " active" : "")}
                  onClick={() => setSizeTier(t)}
                >
                  {t === "standard" ? "Стандарт" : t === "2k" ? "2K" : "Макс (3.8K)"}
                </button>
              ))}
            </div>
            <div className="hint">
              Стандарт — быстро и дёшево. 2K — больше деталей. Макс — максимум (до 3840px).
            </div>
          </div>

          <div className="field">
            <span className="field-label">
              Референсы (свои персонажи/фото) — {references.length}/{maxRefs}
            </span>
            <ReferenceUploader
              references={references}
              maxRefs={maxRefs}
              onAdd={handleAddReferences}
              onRemove={handleRemoveReference}
            />
            <div className="hint">
              Порядок = @Image1, @Image2… В промпте ссылайся: «@Image1 — главный герой, @Image2 — фон».
            </div>
          </div>

          <div className="grid-2">
            <Selector label="Качество (quality)" value={quality} options={config?.qualities || []} onChange={setQuality} />
            <Selector
              label="Формат"
              value={outputFormat}
              options={config?.formats || []}
              onChange={setOutputFormat}
            />
          </div>
          <div className="hint format-hint">
            {outputFormat === "jpeg"
              ? "JPEG — меньше вес, но сжимает с потерями (мылит тонкие детали)"
              : outputFormat === "webp"
              ? "WebP — баланс размера и качества"
              : "PNG — без потерь, максимальная чёткость (больше вес)"}
          </div>
          <div className="field">
            <span className="field-label">Количество: {n}</span>
            <input
              type="range"
              min={1}
              max={maxN}
              value={Math.min(n, maxN)}
              onChange={(e) => setN(parseInt(e.target.value, 10))}
            />
            <div className="hint">До {maxN} за запуск.</div>
          </div>

          <EstimateBar data={estimateData} loading={estimateLoading} />

          <div className="btn-row">
            <button className="btn primary" disabled={busy || !prompt.trim()} onClick={handleGenerate}>
              {busy ? "Генерация…" : "Сгенерировать"}
            </button>
            {busy && (
              <button
                className="btn ghost"
                disabled={cancelling}
                onClick={handleCancel}
              >
                {cancelling ? "Отменяю…" : "Отменить"}
              </button>
            )}
          </div>

          {progress && (
            <div className={"progress-block" + (busy ? " is-active" : "")}>
              <div className="progress-head">
                <span>{STAGE_LABEL[progress.stage] || progress.stage}</span>
                <span>{progress.done_count}/{progress.total_count}</span>
              </div>
              <div className="progress-bar">
                <div className="progress-fill" style={{ width: `${progressPct}%` }} />
              </div>
              <div className="hint">{progress.message}</div>
            </div>
          )}

          {busy && (
            <ResultsGrid total={n} ready={results} />
          )}
          {!busy && results.length > 0 && (
            <ResultsGrid total={results.length} ready={results} />
          )}
        </div>
      )}

      {tab === "history" && (
        <HistoryPanel images={historyImages} jobs={historyJobs} onOpen={setViewer} />
      )}

      {viewer && (
        <Viewer images={viewer.images} index={viewer.index} onClose={() => setViewer(null)} onNav={goViewer} />
      )}
    </div>
  );
}

function ReferenceUploader({ references, maxRefs, onAdd, onRemove }) {
  const inputRef = useRef(null);
  const [previews, setPreviews] = useState([]);

  // генерируем object URLs для превью
  useEffect(() => {
    const urls = references.map((f) => URL.createObjectURL(f));
    setPreviews(urls);
    return () => urls.forEach((u) => URL.revokeObjectURL(u));
  }, [references]);

  return (
    <div className="ref-uploader">
      <div className="ref-grid">
        {previews.map((url, i) => (
          <div key={i} className="ref-cell">
            <img src={url} alt={`ref ${i + 1}`} />
            <span className="ref-badge">@Image{i + 1}</span>
            <button
              type="button"
              className="ref-remove"
              onClick={() => onRemove(i)}
              title="Удалить"
            >
              ✕
            </button>
          </div>
        ))}
        <button
          type="button"
          className="ref-add"
          onClick={() => inputRef.current?.click()}
          disabled={references.length >= maxRefs}
        >
          +<br />
          <span className="hint">добавить</span>
        </button>
      </div>
      <input
        ref={inputRef}
        type="file"
        accept="image/*"
        multiple
        style={{ display: "none" }}
        onChange={(e) => {
          onAdd(e.target.files);
          e.target.value = "";
        }}
      />
    </div>
  );
}

function ResultsGrid({ total, ready }) {
  // total — сколько всего ждём; ready — массив готовых URL.
  // Готовые ячейки показывают фото, остальные — skeleton-плейсхолдер.
  const cells = [];
  for (let i = 0; i < total; i++) {
    const url = ready[i];
    if (url) {
      cells.push(
        <a key={i} className="result-cell" href={absoluteUrl(url)} target="_blank" rel="noreferrer">
          <img src={absoluteUrl(url)} alt={`result ${i + 1}`} />
        </a>
      );
    } else {
      cells.push(
        <div key={i} className="result-cell skeleton">
          <span className="skeleton-num">{i + 1}</span>
          <span className="skeleton-spinner" />
        </div>
      );
    }
  }
  return <div className="results-grid">{cells}</div>;
}

function PromptField({ value, onChange, references, maxLen }) {
  const ref = useRef(null);
  const [showMenu, setShowMenu] = useState(false);
  const [previews, setPreviews] = useState([]);

  useEffect(() => {
    const urls = references.map((f) => URL.createObjectURL(f));
    setPreviews(urls);
    return () => urls.forEach((u) => URL.revokeObjectURL(u));
  }, [references]);

  const detect = () => {
    const el = ref.current;
    if (!el) return;
    const pos = el.selectionStart;
    const before = value.slice(0, pos);
    // Триггер: последний символ @, перед ним пробел или начало строки.
    if (references.length > 0 && /(^|\s)@$/.test(before)) {
      setShowMenu(true);
    } else {
      setShowMenu(false);
    }
  };

  const insert = (n) => {
    const el = ref.current;
    if (!el) return;
    const pos = el.selectionStart;
    const before = value.slice(0, pos).replace(/@$/, `@Image${n} `);
    const after = value.slice(pos);
    const next = before + after;
    onChange(next);
    setShowMenu(false);
    setTimeout(() => {
      el.focus();
      const newPos = before.length;
      el.setSelectionRange(newPos, newPos);
    }, 0);
  };

  return (
    <label className="field">
      <span className="field-label">Промпт</span>
      <div className="prompt-wrap">
        <textarea
          ref={ref}
          className="input textarea"
          placeholder="Опиши картинку… можно подробно: стиль, освещение, композиция. Напиши @ чтобы сослаться на референс."
          value={value}
          onChange={(e) => { onChange(e.target.value); setTimeout(detect, 0); }}
          onKeyUp={detect}
          onClick={detect}
          onInput={detect}
          rows={5}
          maxLength={maxLen}
        />
        {showMenu && (
          <div className="at-menu">
            <div className="at-menu-title">Референсы — кликни чтобы вставить</div>
            <div className="at-menu-list">
              {references.map((_, i) => (
                <button
                  key={i}
                  type="button"
                  className="at-item"
                  onClick={() => insert(i + 1)}
                >
                  {previews[i] && <img src={previews[i]} alt="" />}
                  <span>@Image{i + 1}</span>
                </button>
              ))}
            </div>
          </div>
        )}
      </div>
      <div className="prompt-meta">
        <span className="hint">{value.length} / {maxLen}</span>
        <span className="hint">{references.length > 0 ? "Напиши @ чтобы вставить референс" : "длинный промпт — детальнее результат"}</span>
      </div>
    </label>
  );
}

function PriceCheatSheet({ config, estimateData, n }) {
  const pp = config?.price_per_image || { low: 0.0015, medium: 0.009, high: 0.012 };
  const rub = config?.usd_to_rub || 92;
  const toRub = (usd) => usd * rub;
  // Живой просчёт под текущий выбор (берём из estimateData, если есть)
  const liveRub = estimateData?.total_rub;
  const liveUsd = estimateData?.total;
  return (
    <div className="cheatsheet">
      <div className="cs-title">Цены за 1 картинку (стандартный размер)</div>
      <div className="cs-row">
        {["low", "medium", "high"].map((q) => (
          <div key={q} className="cs-cell">
            <span className="cs-q">{q}</span>
            <span className="cs-price">{toRub(pp[q]).toFixed(2)} ₽</span>
            <span className="cs-usd">${pp[q].toFixed(4)}</span>
          </div>
        ))}
      </div>
      <div className="cs-live">
        {liveRub != null ? (
          <>Сейчас ({n} шт): <b>≈ {liveRub.toFixed(2)} ₽</b> <span className="muted">(${(liveUsd || 0).toFixed(4)})</span></>
        ) : (
          <span className="muted">Выбери параметры — покажу цену</span>
        )}
        <span className="cs-note"> · точная цена зависит от сложности промпта и размера — финальный расход по балансу</span>
      </div>
    </div>
  );
}

function TopBar({ balance, config }) {
  const hasKey = Boolean(balance?.has_key);
  let balanceText = "нет ключа";
  if (hasKey) {
    const usd = balance?.balance_usd;
    const rub = balance?.balance_rub;
    if (usd != null && rub != null) {
      balanceText = `$${usd.toFixed(2)} · ${rub.toFixed(0)} ₽`;
    } else if (balance?.error) {
      balanceText = "ошибка ключа";
    } else {
      balanceText = "ключ ok";
    }
  }
  return (
    <div className="topbar">
      <div className="brand-mark avatar">🎨</div>
      <div className="brand-copy">
        <h1>sami studio</h1>
        <div className="eyebrow">gpt-image-2</div>
      </div>
      <div className={"key-chip" + (hasKey ? " ready" : "")}>
        {balanceText}
      </div>
    </div>
  );
}

function KeyPanel({ keyInput, setKeyInput, keyBusy, keyError, keyOk, onSubmit }) {
  return (
    <div className="panel">
      <div className="key-intro">
        <h2>Подключите API-ключ</h2>
        <p className="hint">
          Генерации списываются с баланса вашего ключа. Зарегистрируйтесь
          на <a href="https://aigate.shop" target="_blank" rel="noreferrer">aigate.shop</a>,
          пополните баланс и скопируйте API-ключ из кабинета.
        </p>
      </div>
      {keyError && <div className="alert danger">{keyError}</div>}
      {keyOk && <div className="alert success">{keyOk}</div>}
      <label className="field">
        <span className="field-label">API-ключ</span>
        <input
          className="input"
          type="password"
          placeholder="sk-..."
          value={keyInput}
          onChange={(e) => setKeyInput(e.target.value)}
          autoComplete="off"
        />
      </label>
      <button className="btn primary" disabled={keyBusy || keyInput.trim().length < 16} onClick={onSubmit}>
        {keyBusy ? "Проверяю…" : "Подключить"}
      </button>
    </div>
  );
}

function Selector({ label, value, options, onChange, optionLabel }) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      <select className="input" value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map((opt) => (
          <option key={opt} value={opt}>{optionLabel ? optionLabel(opt) : opt}</option>
        ))}
      </select>
    </label>
  );
}

function EstimateBar({ data, loading }) {
  if (loading) return <div className="estimate-bar">Считаем цену…</div>;
  if (!data) return null;
  const rub = data.total_rub != null ? `${data.total_rub.toFixed(2)} ₽` : `$${(data.total || 0).toFixed(4)}`;
  const tokens = data.tokens_estimated != null ? ` · ~${data.tokens_estimated} токенов` : "";
  return (
    <div className="estimate-bar">
      <span>≈ {rub}</span>
      <span className="muted">оценка{tokens}</span>
    </div>
  );
}

function HistoryPanel({ images, jobs, onOpen }) {
  if (!images || images.length === 0) {
    return (
      <div className="panel">
        <div className="hint">Пока нет сгенерированных картинок. Перейди во вкладку «Генерация».</div>
      </div>
    );
  }
  return (
    <div className="panel">
      <div className="hist-head">
        <span className="field-label">Все картинки ({images.length})</span>
        <span className="hint">клик — открыть</span>
      </div>
      <div className="gallery-grid">
        {images.map((img) => (
          <button
            key={img.id}
            className="gallery-cell"
            onClick={() => onOpen({ images, index: images.indexOf(img) })}
          >
            <img src={absoluteUrl(img.url)} alt={img.prompt || ""} loading="lazy" />
          </button>
        ))}
      </div>
    </div>
  );
}

async function downloadImage(url, filename) {
  const abs = absoluteUrl(url);
  // Пробуем скачать блобом — обходит ограничение download-атрибута для
  // кросс-доменных картинок (веб-апп и медиа могут быть на разных доменах).
  try {
    const resp = await fetch(abs);
    if (resp.ok) {
      const blob = await resp.blob();
      const blobUrl = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = blobUrl;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(blobUrl), 2000);
      return;
    }
  } catch {
    /* CORS или обрыв — ниже fallback */
  }
  // Fallback: открыть во внешнем браузере (в Telegram — через openLink).
  const tg = window.Telegram?.WebApp;
  if (tg?.openLink) tg.openLink(abs);
  else window.open(abs, "_blank");
}

function Viewer({ images, index, onClose, onNav }) {
  const img = images[index];
  if (!img) return null;
  return (
    <div className="viewer" onClick={onClose}>
      <div className="viewer-bar" onClick={(e) => e.stopPropagation()}>
        <span className="viewer-info">{index + 1} / {images.length}</span>
        <button
          className="btn small"
          onClick={(e) => {
            e.stopPropagation();
            const ext = (absoluteUrl(img.url).split("?")[0].split(".").pop() || "png").toLowerCase();
            downloadImage(img.url, `sami-${Date.now()}.${ext}`);
          }}
        >
          ↓ Скачать
        </button>
        <button className="btn small" onClick={onClose}>✕</button>
      </div>
      <div className="viewer-stage" onClick={(e) => e.stopPropagation()}>
        <button className="nav-btn" onClick={() => onNav(-1)}>‹</button>
        <img src={absoluteUrl(img.url)} alt={img.prompt || ""} />
        <button className="nav-btn" onClick={() => onNav(1)}>›</button>
      </div>
      {img.prompt && (
        <div className="viewer-caption" onClick={(e) => e.stopPropagation()}>
          <span className="muted">{img.size} · {img.quality}{img.cost_real != null ? ` · $${img.cost_real.toFixed(4)}` : ""}</span>
          <div className="viewer-prompt">{img.prompt}</div>
        </div>
      )}
    </div>
  );
}
