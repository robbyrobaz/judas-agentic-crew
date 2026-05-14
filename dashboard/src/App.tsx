import type { ReactNode } from "react";
import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  BarChart3,
  Bot,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Clock3,
  FlaskConical,
  Newspaper,
  Play,
  SendHorizonal,
  ShieldAlert,
  Signal,
  TrendingUp,
  X,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/utils";

type Row = Record<string, unknown>;

type OpenPosition = {
  id: number;
  symbol: string;
  direction: string;
  qty: number;
  entry: number;
  stop: number;
  target: number;
  risk_dollars: number;
  reward_dollars: number;
  rr: number | null;
  current_price: number | null;
  price_ts: string | null;
  unrealized_pnl: number | null;
  opened_at: string;
  label: string | null;
};

type Overview = {
  now_utc: string;
  now_et: string;
  now_phx: string;
  session: Record<string, unknown>;
  services: Record<string, string>;
  research_runtime: Record<string, unknown>;
  counts: Record<string, number>;
  latest_signal: Row | null;
  latest_trade: Row | null;
  latest_experiment: Row | null;
  open_positions: OpenPosition[];
  trading_stats: {
    headline: Record<string, number>;
    by_symbol: Row[];
    by_strategy: Row[];
    generated_at_phx: string;
  };
  research_stats: {
    headline: Record<string, number>;
    active_strategies: Row[];
    recent_walk_forward: Row[];
    recent_experiments: Row[];
  };
};

type ChatEntry = { role: "user" | "assistant"; content: string };

type RecommendedAction = {
  type: string;
  strategy_id?: number;
  reason?: string;
  demotion_id?: number | null;
};

type BriefSummary = {
  fires?: number;
  fills?: number;
  pnl_total?: number;
  pnl_by_strategy?: Array<{ strategy_id: number; name: string; pnl: number; trades: number }>;
  regime?: { vol_regime?: string; trend?: string; leaders?: string[] };
  surprises?: Array<Record<string, unknown>>;
  recommended_actions?: RecommendedAction[];
};

type BriefListItem = {
  id: number;
  brief_date: string;
  created_at_utc: string;
  summary: BriefSummary;
  acknowledged_at_utc: string | null;
};

type BriefDecision = {
  action_index: number;
  decision: string;
  action: RecommendedAction;
  result: string | null;
  decided_at_utc: string;
};

type BriefDetail = BriefListItem & {
  content_md: string;
  decisions: BriefDecision[];
};

async function getJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "number") {
    if (Math.abs(value) >= 1000) return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
    return value.toLocaleString(undefined, { maximumFractionDigits: 3 });
  }
  return String(value);
}

function formatPhxCompact(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("en-US", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
    hour12: true,
    timeZone: "America/Phoenix",
  });
}

function App() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [signals, setSignals] = useState<Row[]>([]);
  const [trades, setTrades] = useState<Row[]>([]);
  const [experiments, setExperiments] = useState<Row[]>([]);
  const [chat, setChat] = useState<ChatEntry[]>([]);
  const [message, setMessage] = useState("");
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [youtubeQuery, setYoutubeQuery] = useState("");
  const [chatBusy, setChatBusy] = useState(false);
  const [latestBrief, setLatestBrief] = useState<BriefDetail | null>(null);
  const [briefExpanded, setBriefExpanded] = useState(false);
  const [briefBusy, setBriefBusy] = useState<string | null>(null);

  const refreshBrief = async () => {
    try {
      const list = await getJson<{ briefs: BriefListItem[] }>("/api/briefs");
      const top = list.briefs[0];
      if (!top) {
        setLatestBrief(null);
        return;
      }
      const detail = await getJson<BriefDetail>(`/api/briefs/${top.brief_date}`);
      setLatestBrief(detail);
    } catch (err) {
      console.error(err);
    }
  };

  const ackLatestBrief = async () => {
    if (!latestBrief) return;
    setBriefBusy("ack");
    try {
      await getJson<{ ok: boolean }>(`/api/briefs/${latestBrief.brief_date}/ack`, { method: "POST" });
      await refreshBrief();
    } finally {
      setBriefBusy(null);
    }
  };

  const decideBriefAction = async (index: number, decision: "apply" | "reject") => {
    if (!latestBrief) return;
    const key = `${decision}:${index}`;
    setBriefBusy(key);
    try {
      await getJson(`/api/briefs/${latestBrief.brief_date}/recommended/${index}/${decision}`, {
        method: "POST",
      });
      await refreshBrief();
    } finally {
      setBriefBusy(null);
    }
  };

  const refresh = async () => {
    const [overviewData, signalsData, tradesData, experimentsData] = await Promise.all([
      getJson<Overview>("/api/overview"),
      getJson<{ signals: Row[] }>("/api/signals"),
      getJson<{ trades: Row[] }>("/api/trades"),
      getJson<{ experiments: Row[] }>("/api/experiments"),
    ]);
    setOverview(overviewData);
    setSignals(signalsData.signals);
    setTrades(tradesData.trades);
    setExperiments(experimentsData.experiments);
  };

  useEffect(() => {
    refresh().catch(console.error);
    refreshBrief().catch(console.error);
    const interval = window.setInterval(() => {
      refresh().catch(console.error);
    }, 30000);
    const briefInterval = window.setInterval(() => {
      refreshBrief().catch(console.error);
    }, 60000);
    return () => {
      window.clearInterval(interval);
      window.clearInterval(briefInterval);
    };
  }, []);

  const sendMessage = async () => {
    const trimmed = message.trim();
    if (!trimmed || chatBusy) return;
    setChat((current) => [...current, { role: "user", content: trimmed }]);
    setMessage("");
    setChatBusy(true);
    try {
      const data = await getJson<{ response: string; history: ChatEntry[] }>("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: trimmed }),
      });
      setChat(data.history);
      await refresh();
    } finally {
      setChatBusy(false);
    }
  };

  const runAction = async (path: string, body?: object) => {
    setBusyAction(path);
    try {
      const data = await getJson<{ ok: boolean; output: string }>(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: body ? JSON.stringify(body) : undefined,
      });
      setChat((current) => [
        ...current,
        {
          role: "assistant",
          content: `${path === "/api/run/doctor" ? "Doctor" : "Research"} run ${data.ok ? "completed" : "failed"}.\n\n${data.output}`,
        },
      ]);
      await refresh();
    } finally {
      setBusyAction(null);
    }
  };

  const headlineStats = useMemo(() => {
    const t = overview?.trading_stats?.headline ?? {};
    const r = overview?.research_stats?.headline ?? {};
    return [
      { icon: TrendingUp, label: "Realized P&L", value: formatValue(t.realized_pnl), detail: `${formatValue(t.pnl_today)} today` },
      { icon: Activity, label: "Trades", value: formatValue(t.total_trades), detail: `${formatValue(t.open_trades)} open / ${formatValue(t.closed_trades)} closed` },
      { icon: Signal, label: "Signal Quality", value: formatValue(t.avg_signal_quality), detail: `${formatValue(t.approved_signals)} approved / ${formatValue(t.skipped_signals)} skipped` },
      { icon: FlaskConical, label: "Active Strategies", value: formatValue(r.active_strategy_count), detail: `${formatValue(r.walk_forward_count)} walk-forward runs` },
    ];
  }, [overview]);

  return (
    <div className="min-h-screen bg-background text-foreground bg-haze font-body">
      <div className="mx-auto max-w-[1760px] px-4 pb-5 pt-4 lg:px-5">
        <div className="grid gap-4 xl:grid-cols-[1.95fr_0.72fr]">
          <main className="min-w-0">
            <section className="rounded-[26px] border border-line/70 bg-panel/90 p-5 shadow-panel backdrop-blur-md">
              <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
                <div>
                  <h1 className="font-display text-4xl leading-none">Judas Futures Agentic Trading</h1>
                </div>
                <div className="grid gap-2 xl:w-[220px]">
                  <TimeChip label="Session" value={String(overview?.session?.active_session ?? "closed")} detail={String(overview?.session?.reason ?? "")} />
                </div>
              </div>
              <div className="mt-3 flex flex-wrap gap-2">
                <InlineActionButton
                  label={busyAction === "/api/run/doctor" ? "Doctor..." : "Doctor"}
                  icon={ShieldAlert}
                  loading={busyAction === "/api/run/doctor"}
                  onClick={() => runAction("/api/run/doctor")}
                />
                <InlineActionButton
                  label={busyAction === "/api/run/research" ? "Research..." : "Research"}
                  icon={Play}
                  loading={busyAction === "/api/run/research"}
                  onClick={() => runAction("/api/run/research")}
                />
                <InlineActionButton
                  label={busyAction === "/api/run/researcher" ? "Researcher..." : "Run Researcher Now"}
                  disabled={!!busyAction}
                  loading={busyAction === "/api/run/researcher"}
                  onClick={() => runAction("/api/run/researcher")}
                />
                <div style={{ display: "flex", gap: "4px", alignItems: "center" }}>
                  <input
                    type="text"
                    placeholder="YouTube concept..."
                    value={youtubeQuery}
                    onChange={(e) => setYoutubeQuery(e.target.value)}
                    style={{ fontSize: "12px", padding: "2px 6px", borderRadius: "4px", border: "1px solid #555", background: "#1a1a2e", color: "#e0e0e0", width: "160px" }}
                  />
                  <InlineActionButton
                    label={busyAction === "/api/run/youtube" ? "Searching..." : "YouTube"}
                    disabled={!!busyAction || !youtubeQuery.trim()}
                    loading={busyAction === "/api/run/youtube"}
                    onClick={() => runAction("/api/run/youtube", { query: youtubeQuery })}
                  />
                </div>
                <InlineActionButton
                  label={busyAction === "/api/run/sweep" ? "Sweeping..." : "Sweep All Symbols"}
                  disabled={!!busyAction}
                  loading={busyAction === "/api/run/sweep"}
                  onClick={() => runAction("/api/run/sweep")}
                />
                <InlineStatChip
                  label="Research"
                  value={String(overview?.research_runtime?.state ?? "unknown")}
                  detail={
                    overview?.research_runtime?.started_at_utc
                      ? `Started ${formatPhxCompact(String(overview.research_runtime.started_at_utc))}`
                      : "No runtime record"
                  }
                />
              </div>
              <div className="mt-5 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
                {headlineStats.map((item) => (
                  <MetricCard key={item.label} icon={item.icon} label={item.label} value={item.value} detail={item.detail} />
                ))}
              </div>
            </section>

            {latestBrief ? (
              <div className="mt-4">
                <BriefPanel
                  brief={latestBrief}
                  expanded={briefExpanded}
                  onToggle={() => setBriefExpanded((v) => !v)}
                  busy={briefBusy}
                  onAck={ackLatestBrief}
                  onDecide={decideBriefAction}
                />
              </div>
            ) : null}

            {(overview?.open_positions ?? []).length > 0 && (
              <div className="mt-4 rounded-[20px] border border-line/70 bg-panel/90 p-4 shadow-panel backdrop-blur-md">
                <div className="mb-2 flex items-center gap-2">
                  <TrendingUp className="h-4 w-4 text-accent" />
                  <span className="text-sm font-semibold uppercase tracking-wider text-muted">Open Positions</span>
                  <span className="ml-1 text-xs text-muted/60">price from last bar cache · updates each scan</span>
                </div>
                <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
                  {(overview?.open_positions ?? []).map((pos) => {
                    const pnlColor = pos.unrealized_pnl == null ? "text-muted" : pos.unrealized_pnl >= 0 ? "text-green-400" : "text-red-400";
                    const dirColor = pos.direction === "long" ? "text-green-400" : "text-red-400";
                    return (
                      <div key={pos.id} className="rounded-xl border border-line/50 bg-background/60 p-3">
                        <div className="flex items-baseline justify-between">
                          <span className="font-display text-lg font-bold">{pos.symbol}</span>
                          <span className={`text-xs font-semibold uppercase ${dirColor}`}>{pos.direction} ×{pos.qty}</span>
                        </div>
                        {pos.label && <div className="text-[10px] text-muted/60">{pos.label}</div>}
                        <div className="mt-2 flex items-baseline gap-1">
                          <span className="text-xs text-muted">Unrealized</span>
                          <span className={`text-base font-bold ${pnlColor}`}>
                            {pos.unrealized_pnl == null ? "—" : `${pos.unrealized_pnl >= 0 ? "+" : ""}$${pos.unrealized_pnl.toFixed(2)}`}
                          </span>
                        </div>
                        {pos.current_price != null && (
                          <div className="text-xs text-muted">
                            now {pos.current_price.toFixed(2)} · entry {pos.entry.toFixed(2)}
                            {pos.price_ts && <span className="ml-1 text-muted/50">({pos.price_ts.slice(0, 16)} UTC)</span>}
                          </div>
                        )}
                        {pos.current_price == null && (
                          <div className="text-xs text-muted/50">no price yet — waiting for next scan</div>
                        )}
                        <div className="mt-1 grid grid-cols-3 gap-1 text-[11px] text-muted">
                          <div>stop<br/><span className="text-foreground">{pos.stop.toFixed(2)}</span></div>
                          <div>target<br/><span className="text-foreground">{pos.target.toFixed(2)}</span></div>
                          <div>R:R<br/><span className="text-foreground">{pos.rr != null ? `${pos.rr}R` : "—"}</span></div>
                        </div>
                        <div className="mt-1 flex gap-3 text-[11px]">
                          <span className="text-red-400/80">risk ${Math.abs(pos.risk_dollars)}</span>
                          <span className="text-green-400/80">reward ${pos.reward_dollars}</span>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            <div className="mt-4 grid gap-4 2xl:grid-cols-2">
              <Panel title="Trading Headline Stats" icon={BarChart3}>
                <StatsGrid
                  items={[
                    ["Trades Today", overview?.trading_stats?.headline?.trades_today],
                    ["P&L Today", overview?.trading_stats?.headline?.pnl_today],
                    ["Total Trades", overview?.trading_stats?.headline?.total_trades],
                    ["Win Rate", overview?.trading_stats?.headline?.win_rate],
                    ["Signal Count", overview?.trading_stats?.headline?.signal_count],
                    ["Avg Signal Quality", overview?.trading_stats?.headline?.avg_signal_quality],
                    ["Approved Signals", overview?.trading_stats?.headline?.approved_signals],
                    ["Skipped Signals", overview?.trading_stats?.headline?.skipped_signals],
                  ]}
                />
              </Panel>

              <Panel title="Research Headline Stats" icon={FlaskConical}>
                <StatsGrid
                  items={[
                    ["Active Strategies", overview?.research_stats?.headline?.active_strategy_count],
                    ["Research Experiments", overview?.research_stats?.headline?.research_experiment_count],
                    ["Walk-Forward Runs", overview?.research_stats?.headline?.walk_forward_count],
                    ["Sweep Runs", overview?.research_stats?.headline?.sweep_count],
                    ["Research Runtime", overview?.research_runtime?.state],
                    ["Session Status", overview?.session?.reason],
                    ["Last Signal Direction", overview?.latest_signal?.direction],
                    ["Last Trade Status", overview?.latest_trade?.status],
                  ]}
                />
              </Panel>

              <Panel title="P&L By Symbol" icon={TrendingUp}>
                <DataTable
                  rows={overview?.trading_stats?.by_symbol ?? []}
                  fields={["symbol", "trade_count", "realized_pnl", "open_trades"]}
                />
              </Panel>

              <Panel title="P&L By Strategy Version" icon={Signal}>
                <DataTable
                  rows={overview?.trading_stats?.by_strategy ?? []}
                  fields={["strategy_family", "strategy_version", "trade_count", "realized_pnl", "open_trades"]}
                />
              </Panel>

              <Panel title="Active Portfolio Baseline" icon={FlaskConical}>
                <DataTable
                  rows={overview?.research_stats?.active_strategies ?? []}
                  fields={["symbol", "strategy_name", "engine", "profit_factor", "trades", "winrate", "total_pnl_dollars", "max_drawdown_dollars"]}
                />
              </Panel>

              <Panel title="Recent Walk-Forward Results" icon={Activity}>
                <DataTable
                  rows={overview?.research_stats?.recent_walk_forward ?? []}
                  fields={["symbol", "name", "window_count", "avg_test_profit_factor", "avg_test_expectancy_r", "total_test_trades", "avg_test_max_drawdown_dollars", "robustness_score"]}
                />
              </Panel>

              <Panel title="Recent Signals" icon={Signal}>
                <DataTable
                  rows={signals}
                  fields={["created_at", "symbol", "direction", "quality_score", "risk_decision", "entry", "stop", "target"]}
                />
              </Panel>

              <Panel title="Recent Trades" icon={Activity}>
                <DataTable
                  rows={trades}
                  fields={["opened_at", "symbol", "direction", "qty", "entry_fill", "stop_price", "target_price", "status", "pnl_dollars"]}
                />
              </Panel>

              <Panel title="Recent Research Experiments" icon={FlaskConical}>
                <DataTable
                  rows={experiments}
                  fields={["ts_utc", "experiment_type", "name", "status", "summary"]}
                />
              </Panel>

              <Panel title="Service Status" icon={Bot}>
                <div className="grid gap-3 md:grid-cols-2">
                  {Object.entries(overview?.services ?? {}).map(([unit, status]) => (
                    <div key={unit} className="rounded-2xl border border-line/70 bg-white/60 p-3">
                      <p className="text-xs uppercase tracking-[0.24em] text-foreground/45">{unit}</p>
                      <p className="mt-1.5 text-xs leading-5 text-foreground/74">{status}</p>
                    </div>
                  ))}
                </div>
              </Panel>
            </div>
          </main>

          <aside className="min-w-0 xl:sticky xl:top-3 xl:h-[calc(100vh-1.5rem)]">
            <div className="flex h-full flex-col rounded-[22px] border border-line/70 bg-[#14352f] p-2.5 text-white shadow-panel">
              <div className="mb-2 flex items-center justify-between gap-2 rounded-2xl border border-white/10 bg-white/5 px-3 py-2">
                <p className="text-xs font-semibold tracking-[0.18em] text-white/72">CHAT</p>
                <p className="text-xs text-white/72">{overview ? formatPhxCompact(overview.now_phx) : "loading..."}</p>
              </div>

              <div className="flex-1 min-w-0 overflow-hidden rounded-[18px] border border-white/10 bg-white/6 p-2.5">
                <div className="flex h-full min-w-0 flex-col">
                  <div className="flex-1 min-w-0 space-y-1.5 overflow-y-auto pr-1">
                    {chat.length === 0 ? (
                      <div className="rounded-xl border border-dashed border-white/14 bg-white/6 p-2.5 text-xs leading-5 text-white/72">
                        Ask about stats, trades, research, or run doctor / research.
                      </div>
                    ) : (
                      chat.map((entry, index) => (
                        <div
                          key={`${entry.role}-${index}`}
                          className={cn(
                            "min-w-0 max-w-full overflow-hidden break-words rounded-xl px-2.5 py-2 text-sm leading-5",
                            entry.role === "assistant" ? "bg-white/10 text-white" : "bg-[#d58c3f] text-[#1d140a]",
                          )}
                        >
                          <p className="mb-0.5 text-[10px] uppercase tracking-[0.2em] opacity-70">{entry.role}</p>
                          <div className="markdown-body markdown-dark min-w-0 max-w-full overflow-x-auto break-words [&_pre]:max-w-full [&_pre]:overflow-x-auto [&_code]:break-words [&_table]:max-w-full [&_table]:overflow-x-auto">
                            <ReactMarkdown remarkPlugins={[remarkGfm]}>{entry.content}</ReactMarkdown>
                          </div>
                        </div>
                      ))
                    )}
                    {chatBusy ? <ThinkingBubble /> : null}
                  </div>
                  <div className="mt-2 border-t border-white/10 pt-2">
                    <div className="flex items-end gap-2">
                      <textarea
                        value={message}
                        onChange={(event) => setMessage(event.target.value)}
                        onKeyDown={(event) => {
                          if (event.key === "Enter" && !event.shiftKey) {
                            event.preventDefault();
                            sendMessage().catch(console.error);
                          }
                        }}
                        placeholder="Ask about stats, backtests, trades..."
                        className="min-h-[72px] flex-1 resize-none rounded-xl border border-white/12 bg-[rgba(255,255,255,0.08)] px-2.5 py-2 text-sm leading-5 text-white caret-white outline-none placeholder:text-white/42"
                        style={{ color: "#ffffff", WebkitTextFillColor: "#ffffff" }}
                      />
                      <button
                        onClick={() => sendMessage().catch(console.error)}
                        disabled={chatBusy || !message.trim()}
                        className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-[#d58c3f] text-[#23180b] transition hover:bg-[#f1a24d] disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        <SendHorizonal size={16} />
                      </button>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </aside>
        </div>
      </div>
    </div>
  );
}

function TimeChip({ label, value, detail }: { label: string; value: string; detail?: string }) {
  return (
    <div className="rounded-2xl border border-line/70 bg-white/72 px-3 py-2.5">
      <p className="text-[11px] uppercase tracking-[0.24em] text-foreground/45">{label}</p>
      <p className="mt-1.5 text-sm font-semibold capitalize">{value}</p>
      {detail ? <p className="mt-1 text-[11px] leading-4 text-foreground/58">{detail}</p> : null}
    </div>
  );
}

function MetricCard({
  icon: Icon,
  label,
  value,
  detail,
}: {
  icon: typeof Clock3;
  label: string;
  value: string;
  detail: string;
}) {
  return (
    <div className="rounded-2xl border border-line/70 bg-white/72 p-3">
      <div className="flex items-center gap-2.5">
        <div className="rounded-xl bg-accent p-1.5 text-white">
          <Icon size={16} />
        </div>
        <div>
          <p className="text-xs uppercase tracking-[0.24em] text-foreground/45">{label}</p>
          <p className="text-lg font-semibold">{value}</p>
        </div>
      </div>
      <p className="mt-2 text-[11px] leading-4 text-foreground/60">{detail}</p>
    </div>
  );
}

function StatsGrid({ items }: { items: [string, unknown][] }) {
  return (
    <div className="grid gap-2.5 sm:grid-cols-2">
      {items.map(([label, value]) => (
        <div key={label} className="rounded-2xl border border-line/70 bg-white/60 p-3">
          <p className="text-xs uppercase tracking-[0.24em] text-foreground/45">{label}</p>
          <p className="mt-1.5 text-base font-semibold">{formatValue(value)}</p>
        </div>
      ))}
    </div>
  );
}

function Panel({
  title,
  icon: Icon,
  children,
}: {
  title: string;
  icon: typeof Bot;
  children: ReactNode;
}) {
  return (
    <section className="rounded-[24px] border border-line/70 bg-panel/92 p-4 shadow-panel backdrop-blur-sm">
      <div className="mb-3 flex items-center gap-2.5">
        <div className="rounded-xl bg-accent p-1.5 text-white">
          <Icon size={16} />
        </div>
        <h3 className="text-base font-semibold">{title}</h3>
      </div>
      {children}
    </section>
  );
}

function ActionButton({
  title,
  subtitle,
  icon: Icon,
  loading,
  onClick,
}: {
  title: string;
  subtitle: string;
  icon: typeof Play;
  loading: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      disabled={loading}
      className="flex w-full items-center gap-3 rounded-2xl border border-white/10 bg-white/7 px-3 py-3 text-left transition hover:bg-white/12 disabled:cursor-not-allowed disabled:opacity-60"
    >
      <div className="rounded-xl bg-[#d58c3f] p-2 text-[#24180a]">
        <Icon size={16} />
      </div>
      <div>
        <p className="text-sm font-semibold">{loading ? `${title}...` : title}</p>
        <p className="text-[11px] leading-4 text-white/68">{subtitle}</p>
      </div>
    </button>
  );
}

function InlineActionButton({
  label,
  icon: Icon,
  loading,
  disabled,
  onClick,
}: {
  label: string;
  icon?: typeof Play;
  loading: boolean;
  disabled?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled ?? loading}
      className="inline-flex items-center gap-2 rounded-xl border border-line/70 bg-white/68 px-3 py-2 text-xs font-semibold text-foreground transition hover:bg-white/88 disabled:cursor-not-allowed disabled:opacity-60"
    >
      {Icon ? <Icon size={14} /> : null}
      <span>{label}</span>
    </button>
  );
}

function InlineStatChip({
  label,
  value,
  detail,
}: {
  label: string;
  value: string;
  detail: string;
}) {
  return (
    <div className="rounded-xl border border-line/70 bg-white/68 px-3 py-2">
      <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.18em] text-foreground/50">
        <span>{label}</span>
        <span className="text-foreground/70">{value}</span>
      </div>
      <p className="mt-0.5 text-[11px] leading-4 text-foreground/62">{detail}</p>
    </div>
  );
}

function DataTable({ rows, fields }: { rows: Row[]; fields: string[] }) {
  return (
    <div className="overflow-hidden rounded-2xl border border-line/70 bg-white/58">
      <div className="overflow-x-auto">
        <table className="min-w-full border-collapse">
          <thead>
            <tr className="bg-black/4">
              {fields.map((field) => (
                <th key={field} className="border-b border-line/70 px-3 py-2 text-left text-[10px] uppercase tracking-[0.2em] text-foreground/50">
                  {field.replaceAll("_", " ")}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td colSpan={fields.length} className="px-3 py-4 text-xs text-foreground/56">
                  No data yet.
                </td>
              </tr>
            ) : (
              rows.map((row, index) => (
                <tr key={index} className="odd:bg-white/45">
                  {fields.map((field) => (
                    <td key={field} className="border-b border-line/50 px-3 py-2 text-xs align-top text-foreground/76">
                      {formatValue(row[field])}
                    </td>
                  ))}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function BriefPanel({
  brief,
  expanded,
  onToggle,
  busy,
  onAck,
  onDecide,
}: {
  brief: BriefDetail;
  expanded: boolean;
  onToggle: () => void;
  busy: string | null;
  onAck: () => void;
  onDecide: (index: number, decision: "apply" | "reject") => void;
}) {
  const summary = brief.summary || {};
  const pnl = typeof summary.pnl_total === "number" ? summary.pnl_total : 0;
  const pnlClass = pnl > 0 ? "text-emerald-700" : pnl < 0 ? "text-rose-700" : "text-foreground/70";
  const regime = summary.regime || {};
  const actions = summary.recommended_actions || [];
  const decisionsByIndex = new Map(brief.decisions.map((d) => [d.action_index, d]));
  const acked = !!brief.acknowledged_at_utc;

  return (
    <section className="rounded-[24px] border border-line/70 bg-panel/92 p-4 shadow-panel backdrop-blur-sm">
      <div className="mb-3 flex flex-wrap items-center gap-2.5">
        <div className="rounded-xl bg-accent p-1.5 text-white">
          <Newspaper size={16} />
        </div>
        <h3 className="text-base font-semibold">Today's Brief — {brief.brief_date}</h3>
        <span className={cn("ml-2 text-base font-bold", pnlClass)}>{formatValue(pnl)}</span>
        <Pill label={`fires ${formatValue(summary.fires)}`} />
        <Pill label={`fills ${formatValue(summary.fills)}`} />
        {regime.vol_regime ? <Pill label={`vol ${regime.vol_regime}`} /> : null}
        {regime.trend ? <Pill label={`trend ${regime.trend}`} /> : null}
        {(regime.leaders || []).map((l) => (
          <Pill key={l} label={l} />
        ))}
        <div className="ml-auto flex items-center gap-2">
          {acked ? (
            <span className="inline-flex items-center gap-1 rounded-full bg-emerald-100 px-2.5 py-1 text-[11px] font-semibold text-emerald-800">
              <CheckCircle2 size={12} /> Acked
            </span>
          ) : (
            <button
              onClick={onAck}
              disabled={busy === "ack"}
              className="inline-flex items-center gap-1.5 rounded-xl border border-line/70 bg-white/68 px-3 py-1.5 text-xs font-semibold text-foreground transition hover:bg-white/88 disabled:opacity-60"
            >
              <Check size={14} /> {busy === "ack" ? "Acking..." : "Mark Reviewed"}
            </button>
          )}
          <button
            onClick={onToggle}
            className="inline-flex items-center gap-1.5 rounded-xl border border-line/70 bg-white/68 px-3 py-1.5 text-xs font-semibold text-foreground transition hover:bg-white/88"
          >
            {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
            {expanded ? "Hide" : "Show"} Markdown
          </button>
        </div>
      </div>

      {expanded ? (
        <>
          {actions.length > 0 ? (
            <div className="mb-3 space-y-1.5">
              <p className="text-xs uppercase tracking-[0.24em] text-foreground/45">Recommended Actions</p>
              {actions.map((action, index) => {
                const prior = decisionsByIndex.get(index);
                return (
                  <div
                    key={index}
                    className="flex flex-wrap items-center gap-2 rounded-xl border border-line/70 bg-white/60 px-3 py-2"
                  >
                    <span className="rounded-full bg-foreground/10 px-2 py-0.5 text-[10px] uppercase tracking-[0.18em] text-foreground/70">
                      {action.type}
                    </span>
                    <span className="text-xs text-foreground/76">
                      {action.strategy_id !== undefined ? `strategy ${action.strategy_id}` : ""}
                      {action.reason ? ` — ${action.reason}` : ""}
                    </span>
                    <span className="ml-auto text-[11px] text-foreground/60">
                      {prior ? <>{prior.decision}{prior.result ? ` · ${prior.result}` : ""}</> : <>pending</>}
                    </span>
                  </div>
                );
              })}
            </div>
          ) : null}
          <div className="rounded-2xl border border-line/70 bg-white/58 p-3">
            <div className="markdown-body">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{brief.content_md || "*(empty)*"}</ReactMarkdown>
            </div>
          </div>
        </>
      ) : null}
    </section>
  );
}

function Pill({ label }: { label: string }) {
  return (
    <span className="inline-flex items-center rounded-full border border-line/70 bg-white/68 px-2.5 py-1 text-[11px] font-medium text-foreground/74">
      {label}
    </span>
  );
}

function ThinkingBubble() {
  return (
    <div className="rounded-xl bg-white/10 px-2.5 py-2 text-white">
      <p className="mb-0.5 text-[10px] uppercase tracking-[0.2em] opacity-70">assistant</p>
      <div className="flex items-center gap-2.5">
        <div className="thinking-dots" aria-label="Thinking">
          <span />
          <span />
          <span />
        </div>
        <span className="text-xs text-white/72">Thinking...</span>
      </div>
    </div>
  );
}

export default App;
